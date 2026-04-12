"""
WebHotelier PMS Adapter.
Implements PMSAdapter against the WebHotelier v2 REST API.
Credentials are passed at construction (loaded from tenant config, not env vars)
so the same class can serve multiple tenants each with their own API key.
"""
from __future__ import annotations
import asyncio
import json
import math
from datetime import date, datetime

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

_MAX_RETRIES    = 3
_RETRY_STATUSES = {429, 500, 502, 503, 504}


class WebHotelierAdapter(PMSAdapter):
    """
    WebHotelier v2 adapter.

    Args:
        api_key:     WebHotelier Bearer token.
        property_id: WebHotelier property ID (X-Property-Id header).
        api_base:    Override API base URL (useful for staging).
    """

    def __init__(
        self,
        api_key: str,
        property_id: str,
        api_base: str = "https://api.webhotelier.net/v2",
    ) -> None:
        self._api_key    = api_key
        self._prop_id    = property_id
        self._api_base   = api_base.rstrip("/")

    # ── Internal HTTP ─────────────────────────────────────────────────────────

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type":  "application/json",
            "X-Property-Id": self._prop_id,
        }

    async def _request(
        self,
        method: str,
        path: str,
        idempotency_key: str | None = None,
        **kwargs,
    ) -> dict | list:
        url     = f"{self._api_base}{path}"
        headers = self._headers()
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key

        for attempt in range(_MAX_RETRIES):
            try:
                async with httpx.AsyncClient(timeout=15) as client:
                    r = await client.request(
                        method, url, headers=headers, **kwargs
                    )
                    if r.status_code in _RETRY_STATUSES and attempt < _MAX_RETRIES - 1:
                        wait = int(r.headers.get("Retry-After", 2 ** attempt))
                        log.warning(
                            "webhotelier_retry",
                            status=r.status_code,
                            attempt=attempt + 1,
                            wait=wait,
                        )
                        await asyncio.sleep(wait)
                        continue
                    r.raise_for_status()
                    return r.json()
            except httpx.TimeoutException:
                if attempt < _MAX_RETRIES - 1:
                    await asyncio.sleep(2 ** attempt)
                    continue
                raise
        raise RuntimeError("WebHotelier: max retries exceeded")

    # ── Adapter contract ──────────────────────────────────────────────────────

    async def check_availability(
        self,
        check_in: str,
        check_out: str,
        guests: int,
        unit_id: str | None = None,
    ) -> AvailabilityResult:
        try:
            params: dict = {
                "from":  check_in,
                "to":    check_out,
                "adults": guests,
                "property_id": self._prop_id,
            }
            if unit_id:
                params["room_type_id"] = unit_id

            data  = await self._request("GET", "/availability", params=params)
            units = data.get("room_types") or data.get("units") or []

            available_units = [
                {
                    "unit_id":    u.get("id") or u.get("room_type_id"),
                    "name":       u.get("name") or u.get("room_name"),
                    "max_guests": u.get("max_occupancy") or u.get("capacity"),
                    "available":  u.get("available", True),
                    "base_rate":  u.get("rate") or u.get("price_per_night"),
                    "amenities":  u.get("amenities", []),
                    "photos":     u.get("photos", []),
                }
                for u in units
                if u.get("available", True)
            ]

            return AvailabilityResult(
                available=len(available_units) > 0,
                units=available_units,
                currency=data.get("currency", "EUR"),
            )
        except Exception as e:
            log.error("webhotelier_availability_error", error=str(e))
            return AvailabilityResult(available=False, units=[], error=str(e))

    async def get_rate_details(
        self,
        unit_id: str,
        check_in: str,
        check_out: str,
        guests: int,
    ) -> RateDetails:
        try:
            data = await self._request(
                "GET",
                f"/availability/rates",
                params={
                    "room_type_id": unit_id,
                    "from":   check_in,
                    "to":     check_out,
                    "adults": guests,
                    "property_id": self._prop_id,
                },
            )

            d1     = date.fromisoformat(check_in)
            d2     = date.fromisoformat(check_out)
            nights = (d2 - d1).days

            base_rate     = float(data.get("rate_per_night") or data.get("base_rate", 0))
            cleaning_fee  = float(data.get("cleaning_fee", 0))
            subtotal      = base_rate * nights + cleaning_fee
            tax_rate      = float(data.get("tax_rate", 0.13))
            tax_amount    = round(subtotal * tax_rate, 2)
            gross_total   = round(subtotal + tax_amount, 2)

            return RateDetails(
                unit_id=unit_id,
                check_in=check_in,
                check_out=check_out,
                nights=nights,
                base_rate=base_rate,
                total_price=subtotal,
                cleaning_fee=cleaning_fee,
                tax_amount=tax_amount,
                gross_total=gross_total,
                currency=data.get("currency", "EUR"),
                cancellation_policy=data.get("cancellation_policy", "Contact property"),
                min_stay=int(data.get("min_stay", 1)),
            )
        except Exception as e:
            log.error("webhotelier_rate_error", unit_id=unit_id, error=str(e))
            d1     = date.fromisoformat(check_in)
            d2     = date.fromisoformat(check_out)
            return RateDetails(
                unit_id=unit_id,
                check_in=check_in,
                check_out=check_out,
                nights=(d2-d1).days,
                base_rate=0, total_price=0, cleaning_fee=0,
                tax_amount=0, gross_total=0,
                currency="EUR",
                cancellation_policy="",
                min_stay=1,
                error=str(e),
            )

    async def create_booking(
        self,
        unit_id: str,
        check_in: str,
        check_out: str,
        guests: int,
        guest_name: str,
        guest_email: str,
        guest_phone: str,
        idempotency_key: str,
        notes: str = "",
        hold_ref: str | None = None,
    ) -> BookingResult:
        # Split guest name into first/last for WebHotelier
        name_parts = guest_name.strip().split(" ", 1)
        first_name = name_parts[0]
        last_name  = name_parts[1] if len(name_parts) > 1 else ""

        payload: dict = {
            "property_id":   self._prop_id,
            "room_type_id":  unit_id,
            "checkin_date":  check_in,
            "checkout_date": check_out,
            "adults":        guests,
            "source":        "vayancy_travelos",
            "channel":       "direct",
            "guest": {
                "first_name": first_name,
                "last_name":  last_name,
                "email":      guest_email,
                "phone":      guest_phone,
            },
            "notes": notes,
        }
        if hold_ref:
            payload["hold_reference"] = hold_ref

        try:
            data = await self._request(
                "POST",
                "/reservations",
                idempotency_key=idempotency_key,
                json=payload,
            )
            pms_id = str(data.get("id") or data.get("reservation_id", ""))
            log.info(
                "webhotelier_booking_created",
                pms_id=pms_id,
                idempotency_key=idempotency_key,
            )
            return BookingResult(
                success=True,
                pms_booking_id=pms_id,
                status="confirmed",
                raw=data,
            )
        except Exception as e:
            log.error(
                "webhotelier_booking_failed",
                idempotency_key=idempotency_key,
                error=str(e),
            )
            return BookingResult(
                success=False,
                pms_booking_id=None,
                status="failed",
                error=str(e),
            )

    async def get_booking_details(self, pms_booking_id: str) -> BookingDetails:
        try:
            data  = await self._request("GET", f"/reservations/{pms_booking_id}")
            guest = data.get("guest") or {}
            room  = data.get("room") or data.get("room_type") or {}
            return BookingDetails(
                pms_booking_id=pms_booking_id,
                status=data.get("status", "unknown"),
                unit_id=str(room.get("id") or data.get("room_type_id", "")),
                unit_name=room.get("name") or data.get("room_name", ""),
                check_in=data.get("checkin_date") or data.get("check_in", ""),
                check_out=data.get("checkout_date") or data.get("check_out", ""),
                guest_name=f"{guest.get('first_name','')} {guest.get('last_name','')}".strip(),
                guest_email=guest.get("email"),
                guest_phone=guest.get("phone"),
                total_amount=float(data.get("total_amount") or data.get("total", 0)),
                currency=data.get("currency", "EUR"),
                notes=data.get("notes"),
            )
        except Exception as e:
            log.error("webhotelier_get_booking_error", pms_id=pms_booking_id, error=str(e))
            return BookingDetails(
                pms_booking_id=pms_booking_id,
                status="error", unit_id="", unit_name="",
                check_in="", check_out="", guest_name="",
                guest_email=None, guest_phone=None,
                total_amount=0, currency="EUR",
                error=str(e),
            )

    async def cancel_booking(
        self, pms_booking_id: str, reason: str = ""
    ) -> dict:
        try:
            await self._request(
                "PATCH",
                f"/reservations/{pms_booking_id}",
                json={"status": "cancelled", "cancellation_reason": reason},
            )
            log.info("webhotelier_booking_cancelled", pms_id=pms_booking_id)
            return {"success": True, "pms_booking_id": pms_booking_id, "status": "cancelled"}
        except Exception as e:
            log.error("webhotelier_cancel_error", pms_id=pms_booking_id, error=str(e))
            return {"success": False, "pms_booking_id": pms_booking_id, "error": str(e)}
