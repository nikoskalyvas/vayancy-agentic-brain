"""
HostHub availability + rate cache — backend/app/db/hosthub_cache.py

Stores per-rental, per-date availability and nightly pricing fetched in the
background by the `refresh_hosthub_cache` ARQ cron job.

The /villas/search endpoint reads from this table to return real availability
and pricing without making live HostHub API calls during user requests.

Table: hosthub_calendar_cache
  rental_id        — HostHub rental ID (e.g. "n19trsyt73")
  stay_date        — the calendar date (one row per night)
  available        — False when a Booking or Hold covers this date
  price_per_night  — nightly rate from the rental's default rate plan (EUR)
  fetched_at       — when this row was last refreshed

Staleness: rows older than _STALE_HOURS are treated as missing (fail open).
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import NamedTuple

import structlog

from app.db.session import get_pool

log = structlog.get_logger()

_STALE_HOURS = 4  # cache rows older than this → treated as missing


class CachedDay(NamedTuple):
    stay_date:       date
    available:       bool
    price_per_night: float | None


async def get_cached_availability(
    rental_ids: list[str],
    check_in:   str,
    check_out:  str,
) -> dict[str, list[CachedDay]]:
    """
    Return fresh cache rows for each rental_id for every night in [check_in, check_out).

    Only rows newer than _STALE_HOURS are returned — stale rows are excluded so
    the caller treats missing dates as unknown and fails open.

    Returns dict[rental_id -> list[CachedDay]], with an empty list when no fresh
    data exists for that rental.
    """
    pool = await get_pool()
    rows = await pool.fetch(
        """
        SELECT rental_id, stay_date, available, price_per_night
        FROM   hosthub_calendar_cache
        WHERE  rental_id = ANY($1)
          AND  stay_date >= $2
          AND  stay_date <  $3
          AND  fetched_at > NOW() - ($4 || ' hours')::INTERVAL
        ORDER  BY rental_id, stay_date
        """,
        rental_ids,
        date.fromisoformat(check_in),
        date.fromisoformat(check_out),
        str(_STALE_HOURS),
    )

    result: dict[str, list[CachedDay]] = {rid: [] for rid in rental_ids}
    for row in rows:
        result[row["rental_id"]].append(
            CachedDay(
                stay_date=row["stay_date"],
                available=row["available"],
                price_per_night=float(row["price_per_night"]) if row["price_per_night"] is not None else None,
            )
        )
    return result


async def upsert_calendar(rental_id: str, days: list[CachedDay]) -> None:
    """Upsert one row per day for the given rental."""
    if not days:
        return
    pool = await get_pool()
    await pool.executemany(
        """
        INSERT INTO hosthub_calendar_cache (rental_id, stay_date, available, price_per_night, fetched_at)
        VALUES ($1, $2, $3, $4, NOW())
        ON CONFLICT (rental_id, stay_date) DO UPDATE SET
            available       = EXCLUDED.available,
            price_per_night = EXCLUDED.price_per_night,
            fetched_at      = EXCLUDED.fetched_at
        """,
        [
            (rental_id, d.stay_date, d.available, d.price_per_night)
            for d in days
        ],
    )


def check_available_from_cache(
    days:      list[CachedDay],
    check_in:  str,
    check_out: str,
) -> bool | None:
    """
    Determine availability from cached rows.

    Returns:
      True   — all nights in range have available=True
      False  — at least one night is blocked
      None   — cache is incomplete for this range (treat as unknown / fail open)
    """
    ci = date.fromisoformat(check_in)
    co = date.fromisoformat(check_out)
    needed = {ci + timedelta(days=i) for i in range((co - ci).days)}
    if not needed:
        return True

    cached = {d.stay_date: d.available for d in days}
    if not needed.issubset(cached.keys()):
        return None  # incomplete — some nights have no cache row

    return all(cached[d] for d in needed)


def sum_price_from_cache(
    days:      list[CachedDay],
    check_in:  str,
    check_out: str,
) -> float | None:
    """
    Sum nightly prices from cached rows.

    Returns None if any night in the range is missing a price (so callers
    don't show a partial/wrong total).
    """
    ci = date.fromisoformat(check_in)
    co = date.fromisoformat(check_out)
    needed = {ci + timedelta(days=i) for i in range((co - ci).days)}
    if not needed:
        return None

    price_map = {
        d.stay_date: d.price_per_night
        for d in days
        if d.price_per_night is not None
    }
    if not needed.issubset(price_map.keys()):
        return None  # incomplete pricing

    return round(sum(price_map[d] for d in needed), 2)
