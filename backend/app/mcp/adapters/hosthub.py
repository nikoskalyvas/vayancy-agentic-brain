"""
HostHub PMS Adapter — backend/app/mcp/adapters/hosthub.py

Implements PMSAdapter against the HostHub API v2019-03-01.
https://app.hosthub.com/api/2019-03-01

Auth: API key in Authorization header (no Bearer prefix — raw key).
Key concepts:
  - Rentals     → properties (use rental_id as unit_id)
  - CalendarEvents → bookings and holds
  - RateMandates   → rate updates (push via rate-plans)
  - No direct availability endpoint — derive from calendar events

HostHub differences from WebHotelier:
  1. No availability endpoint — we check calendar events for blocked dates
  2. Bookings are created as CalendarEvents of type "Booking"
  3. Rates are updated via RateMandates on a rate plan
  4. Money is expressed in cents — always divide by 100
  5. IDs are encoded strings (e.g. "w54dWRE") not integers
"""
from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, timedelta
from typing import Any

import httpx
import structlog

from .base import (
    PMSAdapter,
    AvailabilityResult,
    RateDetails,
    BookingResult,
    BookingDetails,
)

log = structlog.get_logger()

_API_BASE     = "https://app.hosthub.com/api/2019-03-01"
_MAX_RETRIES  = 3
_RETRY_STATUS = {429, 500, 502, 503, 504}
_TIMEOUT      = httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=10.0)


def _cents_to_float(money: dict | None) -> float:
    """Convert HostHub money object {cents, currency} to float."""
    if not money:
        return 0.0
    return round((money.get("cents") or 0) / 100, 2)


def _float_to_cents(amount: float, currency: str = "EUR") -> dict:
    """Convert float to HostHub money object."""
    return {"cents": int(round(amount * 100)), "currency": currency}


class HostHubAdapter(PMSAdapter):
    """
    HostHub adapter.

    Args:
        api_key:   HostHub API key from Settings page — used directly as
                   Authorization header value (no Bearer prefix).
        rental_id: HostHub rental ID (encoded string e.g. "w54dWRE").
                   Used as the unit_id throughout.
    """

    def __init__(self, api_key: str, rental_id: str) -> None:
        self._api_key   = api_key
        self._rental_id = rental_id

    # ── HTTP ──────────────────────────────────────────────────────────────────

    def _headers(self) -> dict:
        return {
            "Authorization": self._api_key,
            "Content-Type":  "application/json",
            "Accept":        "application/json",
        }

    async def _request(
        self,
        method: str,
        path: str,
        **kwargs,
    ) -> dict | list:
        url = path if path.startswith("http") else f"{_API_BASE}{path}"
        for attempt in range(_MAX_RETRIES):
            try:
                async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                    r = await client.request(
                        method, url, headers=self._headers(), **kwargs
                    )
                    if r.status_code in _RETRY_STATUS and attempt < _MAX_RETRIES - 1:
                        wait = int(r.headers.get("Retry-After", 2 ** attempt))
                        log.warning("hosthub_retry",
                                    status=r.status_code, attempt=attempt + 1, wait=wait)
                        await asyncio.sleep(wait)
                        continue
                    if r.status_code == 204:
                        return {}
                    r.raise_for_status()
                    return r.json()
            except httpx.TimeoutException:
                if attempt < _MAX_RETRIES - 1:
                    await asyncio.sleep(2 ** attempt)
                    continue
                raise
        raise RuntimeError("HostHub: max retries exceeded")

    # ── Availability ──────────────────────────────────────────────────────────

    async def check_availability(
        self,
        check_in:  str,
        check_out: str,
        guests:    int,
        unit_id:   str | None = None,
    ) -> AvailabilityResult:
        """
        HostHub has no dedicated availability endpoint.
        We derive availability by fetching calendar events for the rental
        and checking whether any active booking/hold overlaps the requested dates.
        """
        rental_id = unit_id or self._rental_id
        try:
            # Fetch all visible calendar events — no date filter
            # We need ALL active bookings/holds, not just recently updated ones
            data = await self._request(
                "GET",
                f"/rentals/{rental_id}/calendar-events",
                params={"is_visible": "true"},
            )

            events = data.get("data", []) if isinstance(data, dict) else []

            # Paginate through all results
            nav = data.get("navigation", {}) if isinstance(data, dict) else {}
            while nav.get("next"):
                next_path = nav["next"]
                page = await self._request("GET", next_path)
                events.extend(page.get("data", []))
                nav = page.get("navigation", {})

            ci = date.fromisoformat(check_in)
            co = date.fromisoformat(check_out)

            for event in events:
                if event.get("type") not in ("Booking", "Hold"):
                    continue
                ev_from = date.fromisoformat(event["date_from"])
                ev_to   = date.fromisoformat(event["date_to"])
                # Overlap check: event blocks if [ev_from, ev_to) overlaps [ci, co)
                if ev_from < co and ev_to > ci:
                    log.info("hosthub_unavailable",
                             rental_id=rental_id,
                             blocking_event=event.get("id"))
                    return AvailabilityResult(
                        available=False, units=[], currency="EUR"
                    )

            # Get rental details for the response
            rental = await self._request("GET", f"/rentals/{rental_id}")
            unit = {
                "unit_id":    rental_id,
                "name":       rental.get("name", rental_id),
                "max_guests": guests,  # HostHub doesn't expose capacity via API
                "available":  True,
                "base_rate":  None,
                "amenities":  [],
                "photos":     [],
            }

            return AvailabilityResult(
                available=True, units=[unit], currency="EUR"
            )

        except Exception as e:
            log.error("hosthub_availability_error", error=str(e))
            return AvailabilityResult(available=False, units=[], error=str(e))

    # ── Rate details ──────────────────────────────────────────────────────────

    async def get_rate_details(
        self,
        unit_id:   str,
        check_in:  str,
        check_out: str,
        guests:    int,
    ) -> RateDetails:
        """
        Fetch daily rates from the default rate plan for the rental.
        Sum rates for each night in the requested period.
        """
        rental_id = unit_id or self._rental_id
        d1 = date.fromisoformat(check_in)
        d2 = date.fromisoformat(check_out)
        nights = (d2 - d1).days

        try:
            # Get rate plans for rental — use the default one
            plans_data = await self._request(
                "GET", f"/rentals/{rental_id}/rate-plans"
            )
            plans = plans_data.get("data", [])
            if not plans:
                raise ValueError("No rate plans found for rental")

            default_plan = next(
                (p for p in plans if p.get("default")),
                plans[0]
            )
            plan_id = default_plan["id"]

            # Get daily rates for the plan
            rates_data = await self._request(
                "GET", f"/rate-plans/{plan_id}/rates"
            )
            daily_rates = rates_data.get("data", [])

            # Build date → rate map
            rate_map: dict[str, float] = {}
            currency = "EUR"
            for r in daily_rates:
                if r.get("is_available") and r.get("amount"):
                    rate_map[r["date"]] = _cents_to_float(r["amount"])
                    currency = r["amount"].get("currency", "EUR")

            # Sum rates for requested nights
            total_rate = 0.0
            nights_priced = 0
            current = d1
            while current < d2:
                day_str = str(current)
                if day_str in rate_map:
                    total_rate += rate_map[day_str]
                    nights_priced += 1
                current += timedelta(days=1)

            # If we couldn't get rates for all nights, use average of what we have
            if nights_priced > 0 and nights_priced < nights:
                avg = total_rate / nights_priced
                total_rate += avg * (nights - nights_priced)
            elif nights_priced == 0:
                # No rate data available
                total_rate = 0.0

            base_rate = round(total_rate / nights, 2) if nights > 0 else 0.0
            tax_amount = round(total_rate * 0.13, 2)  # Greek VAT 13%
            gross_total = round(total_rate + tax_amount, 2)

            return RateDetails(
                unit_id=rental_id,
                check_in=check_in,
                check_out=check_out,
                nights=nights,
                base_rate=base_rate,
                total_price=total_rate,
                cleaning_fee=0.0,
                tax_amount=tax_amount,
                gross_total=gross_total,
                currency=currency,
                cancellation_policy="Contact property for cancellation policy",
                min_stay=1,
            )

        except Exception as e:
            log.error("hosthub_rate_error", rental_id=rental_id, error=str(e))
            d1 = date.fromisoformat(check_in)
            d2 = date.fromisoformat(check_out)
            return RateDetails(
                unit_id=rental_id,
                check_in=check_in,
                check_out=check_out,
                nights=(d2 - d1).days,
                base_rate=0, total_price=0, cleaning_fee=0,
                tax_amount=0, gross_total=0,
                currency="EUR",
                cancellation_policy="",
                min_stay=1,
                error=str(e),
            )

    # ── Create booking ────────────────────────────────────────────────────────

    async def create_booking(
        self,
        unit_id:         str,
        check_in:        str,
        check_out:       str,
        guests:          int,
        guest_name:      str,
        guest_email:     str,
        guest_phone:     str,
        idempotency_key: str,
        notes:           str = "",
        hold_ref:        str | None = None,
    ) -> BookingResult:
        """
        Create a booking as a CalendarEvent of type Booking.
        HostHub uses reservation_id as the external reference — we use
        idempotency_key so retries return the same result.
        """
        rental_id = unit_id or self._rental_id

        # Idempotency: check if a booking with this reservation_id already exists
        try:
            existing = await self._request(
                "GET",
                f"/rentals/{rental_id}/calendar-events",
                params={"is_visible": "true"},
            )
            for event in existing.get("data", []):
                if event.get("reservation_id") == idempotency_key:
                    log.info("hosthub_booking_idempotent",
                             idempotency_key=idempotency_key,
                             event_id=event["id"])
                    return BookingResult(
                        success=True,
                        pms_booking_id=event["id"],
                        status="confirmed",
                        raw=event,
                    )
        except Exception:
            pass  # If idempotency check fails, proceed with creation

        payload: dict[str, Any] = {
            "type":           "Booking",
            "date_from":      check_in,
            "date_to":        check_out,
            "guest_name":     guest_name,
            "guest_email":    guest_email,
            "guest_phone":    guest_phone[:20] if guest_phone else "",
            "guest_adults":   guests,
            "reservation_id": idempotency_key,
            "notes":          notes or "",
            "source_id":      "vayancy",
        }

        try:
            data = await self._request(
                "POST",
                f"/rentals/{rental_id}/calendar-events",
                json=payload,
            )
            event_id = data.get("id")
            log.info("hosthub_booking_created",
                     event_id=event_id, rental_id=rental_id)
            return BookingResult(
                success=True,
                pms_booking_id=event_id,
                status="confirmed",
                raw=data,
            )
        except Exception as e:
            log.error("hosthub_booking_failed",
                      rental_id=rental_id, error=str(e))
            return BookingResult(
                success=False,
                pms_booking_id=None,
                status="failed",
                error=str(e),
            )

    # ── Get booking details ───────────────────────────────────────────────────

    async def get_booking_details(self, pms_booking_id: str) -> BookingDetails:
        try:
            event = await self._request(
                "GET", f"/calendar-events/{pms_booking_id}"
            )
            return BookingDetails(
                pms_booking_id=pms_booking_id,
                status="confirmed" if event.get("is_visible") else "cancelled",
                unit_id=event.get("rental", {}).get("id", self._rental_id),
                unit_name=event.get("rental", {}).get("name", ""),
                check_in=event.get("date_from", ""),
                check_out=event.get("date_to", ""),
                guest_name=event.get("guest_name", ""),
                guest_email=event.get("guest_email"),
                guest_phone=event.get("guest_phone"),
                total_amount=_cents_to_float(event.get("total_payout")),
                currency=event.get("total_payout", {}).get("currency", "EUR")
                         if event.get("total_payout") else "EUR",
                notes=event.get("notes"),
            )
        except Exception as e:
            log.error("hosthub_get_booking_error",
                      event_id=pms_booking_id, error=str(e))
            return BookingDetails(
                pms_booking_id=pms_booking_id,
                status="error", unit_id="", unit_name="",
                check_in="", check_out="", guest_name="",
                guest_email=None, guest_phone=None,
                total_amount=0, currency="EUR",
                error=str(e),
            )

    # ── Cancel booking ────────────────────────────────────────────────────────

    async def cancel_booking(
        self, pms_booking_id: str, reason: str = ""
    ) -> dict:
        """
        Cancel a HostHub booking by deleting the calendar event.
        Deletion sets is_visible=False and unblocks the calendar.
        """
        try:
            await self._request(
                "DELETE", f"/calendar-events/{pms_booking_id}"
            )
            log.info("hosthub_booking_cancelled", event_id=pms_booking_id)
            return {
                "success":        True,
                "pms_booking_id": pms_booking_id,
                "status":         "cancelled",
            }
        except Exception as e:
            log.error("hosthub_cancel_error",
                      event_id=pms_booking_id, error=str(e))
            return {
                "success":        False,
                "pms_booking_id": pms_booking_id,
                "error":          str(e),
            }

    # ── HostHub-specific helpers (not in PMSAdapter ABC) ─────────────────────

    async def get_rental(self) -> dict:
        """Fetch rental details — name, location, coordinates."""
        return await self._request("GET", f"/rentals/{self._rental_id}")

    async def list_rentals(self) -> list[dict]:
        """List all rentals accessible with this API key."""
        data = await self._request("GET", "/rentals")
        return data.get("data", [])

    async def push_rate_overrides(
        self,
        date_rates: list[dict],
        currency:   str = "EUR",
        dry_run:    bool = True,
    ) -> dict:
        """
        Push nightly rate overrides via RateMandates.

        Args:
            date_rates: list of {"date": "YYYY-MM-DD", "rate": float}
            currency:   ISO 4217 currency code
            dry_run:    if True, returns preview without committing

        Rate floor/ceiling guards are applied here (€150–€2500).
        """
        FLOOR   = 150.0
        CEILING = 2500.0

        mandate_days = []
        for item in date_rates:
            rate = float(item.get("rate", 0))
            clamped = max(FLOOR, min(CEILING, rate))
            if clamped != rate:
                log.warning("hosthub_rate_clamped",
                            date=item["date"],
                            original=rate,
                            clamped=clamped)
            mandate_days.append({
                "date":  item["date"],
                "price": _float_to_cents(clamped, currency),
            })

        if dry_run:
            return {
                "dry_run":     True,
                "would_apply": mandate_days,
                "count":       len(mandate_days),
                "note":        "Call with dry_run=False to commit.",
            }

        # Get default rate plan
        plans_data = await self._request(
            "GET", f"/rentals/{self._rental_id}/rate-plans"
        )
        plans = plans_data.get("data", [])
        if not plans:
            raise ValueError("No rate plans found")
        plan_id = next(
            (p["id"] for p in plans if p.get("default")),
            plans[0]["id"]
        )

        # Create rate mandate
        result = await self._request(
            "POST",
            f"/rate-plans/{plan_id}/rate-mandates",
            json=mandate_days,
        )
        log.info("hosthub_rates_pushed",
                 plan_id=plan_id, count=len(mandate_days))
        return {
            "applied":     True,
            "count":       len(mandate_days),
            "mandate_id":  result.get("id"),
            "status":      result.get("status"),
            "mandate_url": result.get("url"),
        }

    async def get_calendar_events(
        self,
        rental_id:   str | None = None,
        updated_gte: int | None = None,
    ) -> list[dict]:
        """
        Fetch calendar events for a rental. Used by the operations agent
        to get upcoming checkouts.

        Args:
            rental_id:   defaults to self._rental_id
            updated_gte: Unix timestamp — only events updated after this
        """
        rid = rental_id or self._rental_id
        params: dict = {"is_visible": "true"}
        if updated_gte:
            params["updated_gte"] = updated_gte

        all_events: list[dict] = []
        data = await self._request(
            "GET", f"/rentals/{rid}/calendar-events", params=params
        )
        all_events.extend(data.get("data", []))

        nav = data.get("navigation", {})
        while nav.get("next"):
            page = await self._request("GET", nav["next"])
            all_events.extend(page.get("data", []))
            nav = page.get("navigation", {})

        return all_events

    async def get_greek_taxes(self, calendar_event_id: str) -> dict:
        """
        Fetch Greek tax breakdown for a booking.
        Includes VAT, accommodation tax, climate tax, AADE value.
        Useful for Epsilon Net folio creation.
        """
        return await self._request(
            "GET",
            f"/calendar-events/{calendar_event_id}/calendar-event-gr-taxes",
        )
