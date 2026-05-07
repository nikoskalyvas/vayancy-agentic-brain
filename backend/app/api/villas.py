"""
Public villa search endpoint — backend/app/api/villas.py

GET /villas/search?check_in=YYYY-MM-DD&check_out=YYYY-MM-DD&guests=N

Returns all Vayancy villas, each with:
  - available: bool (True for unmanaged villas; real check for HostHub villas)
  - price_total: float | null (sum of nightly rates via HostHub)
  - price_per_night: float | null

Managed villas (checked against HostHub):
  - Adaman Villas
  - Boat Villa
  - Nidri Hills Villa

Always-available villas (not yet on HostHub/PriceLabs):
  - Petroto Villas
  - Ktima Bird Paradise
  - Thalassa Apartments
  - Garden House

Mounted in main.py:
  from app.api.villas import router as villas_router
  app.include_router(villas_router, prefix="/villas")
"""
from __future__ import annotations

import asyncio
from datetime import date

import structlog
from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

from app.config import settings
from app.db.hosthub_cache import (
    check_available_from_cache,
    get_cached_availability,
)

router = APIRouter(tags=["villas"])
log    = structlog.get_logger()


# ── Static villa catalogue ────────────────────────────────────────────────────

# Each entry:
#   key:        unique slug
#   name:       display name
#   guests:     max guest count
#   bedrooms:   bedroom count
#   location:   human-readable location
#   lat/lng:    map coordinates
#   img:        theme-relative image path (resolved on WP side)
#   link:       WP villa page path
#   hosthub_id: HostHub rental ID — None means always available

_VILLAS: list[dict] = [
    {
        "key":        "boat-villa",
        "name":       "Boat Villa",
        "guests":     14,
        "bedrooms":   "7",
        "location":   "Geni, Lefkada, Greece",
        "lat":        38.6763,
        "lng":        20.7082,
        "img":        "/images/boat.webp",
        "link":       "/our-villas/boat-villa/",
        "hosthub_id": "BOAT_RENTAL_ID",   # replaced by settings.hosthub_rental_boat
    },
    {
        "key":        "nidri-hills-villa",
        "name":       "Nidri Hills Villa",
        "guests":     10,
        "bedrooms":   "5",
        "location":   "Nydri, Lefkada, Greece",
        "lat":        38.7172,
        "lng":        20.7108,
        "img":        "/images/nidri.webp",
        "link":       "/our-villas/nidri-hills-villa/",
        "hosthub_id": "NIDRI_RENTAL_ID",  # replaced by settings.hosthub_rental_nidri
    },
    {
        "key":        "adaman-villas",
        "name":       "Adaman Villas",
        "guests":     8,
        "bedrooms":   "4",
        "location":   "Spartochori, Meganisi, Greece",
        "lat":        38.6577,
        "lng":        20.7665,
        "img":        "/images/adaman-overview (4).webp",
        "link":       "/our-villas/adaman-villas/",
        "hosthub_id": "ADAMAN_RENTAL_ID", # replaced by settings.hosthub_rental_adaman
    },
    {
        "key":        "petroto-villas",
        "name":       "Petroto Villas",
        "guests":     6,
        "bedrooms":   "3",
        "location":   "Tsoukalades, Lefkada, Greece",
        "lat":        38.8209,
        "lng":        20.6642,
        "img":        "/images/petroto_43.webp",
        "link":       "/our-villas/petroto-villas/",
        "hosthub_id": None,
    },
    {
        "key":        "ktima-bird-paradise",
        "name":       "Ktima Bird Paradise",
        "guests":     6,
        "bedrooms":   "3",
        "location":   "Geni, Lefkada, Greece",
        "lat":        38.6960,
        "lng":        20.7198,
        "img":        "/images/ktima2bed-13.webp",
        "link":       "/our-villas/ktima-bird-paradise/",
        "hosthub_id": None,
    },
    {
        "key":        "thalassa-apartments",
        "name":       "Thalassa Apartments",
        "guests":     4,
        "bedrooms":   "2",
        "location":   "Nydri, Lefkada, Greece",
        "lat":        38.7191,
        "lng":        20.7232,
        "img":        "/images/thalassa.webp",
        "link":       "/our-villas/thalassa-apartments/",
        "hosthub_id": None,
    },
    {
        "key":        "garden-house",
        "name":       "Garden House",
        "guests":     11,
        "bedrooms":   "4",
        "location":   "Chania, Crete, Greece",
        "lat":        35.4749,
        "lng":        24.0446,
        "img":        "/images/gardenhouse.webp",
        "link":       "/our-villas/garden-house/",
        "hosthub_id": None,
    },
]


def _get_hosthub_id(villa: dict) -> str | list[str] | None:
    """
    Resolve the HostHub rental ID(s) for a villa.
    Adaman Villas has two units — returns a list; others return a single string.
    Returns None if not configured.
    """
    key = villa["key"]
    if key == "adaman-villas":
        ids = [settings.hosthub_rental_adaman_nicoleta, settings.hosthub_rental_adaman_maria]
        ids = [i for i in ids if i]
        return ids if ids else None
    elif key == "boat-villa":
        return settings.hosthub_rental_boat or None
    elif key == "nidri-hills-villa":
        return settings.hosthub_rental_nidri or None
    return None


def _get_cache_keys(villa: dict) -> list[str] | None:
    """
    Return the hosthub_calendar_cache key(s) for a villa.

    Keys match what refresh_hosthub_cache writes (iCal-sourced rows).
    Multi-unit villas return a list; villa is available if any unit is free.
    Returns None for villas that have no iCal feed configured.
    """
    key = villa["key"]
    if key == "boat-villa":
        return ["boat-villa"] if settings.ical_boat else None
    if key == "nidri-hills-villa":
        return ["nidri-hills-villa"] if settings.ical_nidri else None
    if key == "adaman-villas":
        keys = []
        if settings.ical_adaman_nicoleta:
            keys.append("adaman-nicoleta")
        if settings.ical_adaman_maria:
            keys.append("adaman-maria")
        return keys if keys else None
    if key == "ktima-bird-paradise":
        keys = []
        if settings.ical_ktima_3bed:
            keys.append("ktima-3bed")
        if settings.ical_ktima_2bed:
            keys.append("ktima-2bed")
        return keys if keys else None
    if key == "garden-house":
        return ["garden-house"] if settings.ical_garden_house else None
    return None


async def _check_villa(
    villa: dict,
    check_in: str,
    check_out: str,
    guests: int,
) -> dict:
    """Return villa dict enriched with available/price fields.

    Reads from hosthub_calendar_cache (populated every 30 min by the
    refresh_hosthub_cache cron job via iCal feeds) instead of calling
    any PMS API live. Fails open on cache miss.
    """
    result = {**villa, "available": True, "price_total": None, "price_per_night": None}
    cache_keys = _get_cache_keys(villa)

    if not cache_keys:
        # No iCal feed configured — always shown as available, no pricing
        return result

    try:
        # Read from cache — one DB query for all units of this villa
        cache = await get_cached_availability(cache_keys, check_in, check_out)

        available_unit: str | None = None

        for ck in cache_keys:
            days = cache.get(ck, [])
            avail = check_available_from_cache(days, check_in, check_out)

            if avail is None:
                # Incomplete cache — fail open: show as available
                log.warning("ical_cache_miss", villa=villa["key"], cache_key=ck)
                available_unit = ck
                break

            if avail:
                available_unit = ck
                break

        result["available"] = available_unit is not None

        # iCal feeds carry no pricing data — price fields stay None

    except Exception as e:
        log.error("villa_check_error", villa=villa["key"], error=str(e))
        result["available"] = True  # fail open on DB error

    return result


@router.get("/search")
async def search_villas(
    check_in:  str | None = Query(None, description="YYYY-MM-DD"),
    check_out: str | None = Query(None, description="YYYY-MM-DD"),
    guests:    int        = Query(1, ge=1, le=20),
) -> JSONResponse:
    """
    Return all villas with availability and pricing.
    If no dates provided, returns all villas as available (no pricing).
    """
    # Validate dates if provided
    if check_in and check_out:
        try:
            ci = date.fromisoformat(check_in)
            co = date.fromisoformat(check_out)
            if co <= ci:
                return JSONResponse(
                    status_code=400,
                    content={"error": "check_out must be after check_in"},
                )
        except ValueError:
            return JSONResponse(
                status_code=400,
                content={"error": "Invalid date format — use YYYY-MM-DD"},
            )

    if check_in and check_out:
        tasks   = [_check_villa(v, check_in, check_out, guests) for v in _VILLAS]
        results = await asyncio.gather(*tasks)
    else:
        # No dates — all villas available, no pricing
        results = [{**v, "available": True, "price_total": None, "price_per_night": None} for v in _VILLAS]

    # Filter by guest count
    results = [r for r in results if r["guests"] >= guests]

    # Strip internal fields from public response
    _public_keys = {"key", "name", "guests", "bedrooms", "location", "lat", "lng",
                    "img", "link", "available", "price_total", "price_per_night"}
    results = [{k: v for k, v in r.items() if k in _public_keys} for r in results]

    return JSONResponse(content={
        "villas":    results,
        "check_in":  check_in,
        "check_out": check_out,
        "guests":    guests,
    })
