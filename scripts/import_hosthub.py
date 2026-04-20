"""
HostHub Booking Import Script
Pulls all active calendar events from HostHub and inserts them into
travelos_bookings so the Vayancy dashboard shows real data.

Usage:
  python3 scripts/import_hosthub.py

Required env vars (or edit the constants below):
  HOSTHUB_API_KEY   — HostHub API key
  TENANT_ID         — Vayancy tenant UUID
  DATABASE_URL      — PostgreSQL connection string
"""
from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime

import asyncpg
import httpx

# ── Config — edit these or set as env vars ────────────────────────────────────
HOSTHUB_API_KEY = os.getenv("HOSTHUB_API_KEY", "NDU1NDliYzYtMmQ1ZS00OTZhLWJiMmMtZmUyYTA2MGE3YTAx")
TENANT_ID       = os.getenv("TENANT_ID",       "42aeac37-2344-4a97-9757-23f853963dbb")
DATABASE_URL    = os.getenv("DATABASE_URL",     "postgresql://postgres:tOlmvINfpHhFfbuwNtYTESCzIrFOwCoJ@postgres.railway.internal:5432/railway")
API_BASE        = "https://app.hosthub.com/api/2019-03-01"

# Rental IDs to import — add/remove as needed
RENTALS = [
    ("n19trsyt73", "Nidry Hills",      "Lefkada"),
    ("quktckazxe", "Boat Villa",       "Lefkada"),
    ("bqke38mc1b", "Adaman_Nicoleta",  "Meganisi"),
    ("z3sc4f7avm", "Adaman_Maria",     "Meganisi"),
]

# ── Fetch from HostHub ────────────────────────────────────────────────────────

async def fetch_all_events(rental_id: str) -> list[dict]:
    """Fetch all visible calendar events for a rental, paginating."""
    events = []
    url = f"{API_BASE}/rentals/{rental_id}/calendar-events"
    params = {"is_visible": "true"}
    headers = {"Authorization": HOSTHUB_API_KEY, "Accept": "application/json"}

    async with httpx.AsyncClient(timeout=30) as client:
        while url:
            r = await client.get(url, params=params, headers=headers)
            r.raise_for_status()
            data = r.json()
            events.extend(data.get("data", []))

            nav = data.get("navigation", {})
            next_url = nav.get("next")
            if next_url:
                # next_url is a full URL on subsequent pages
                url = next_url if next_url.startswith("http") else f"https://app.hosthub.com{next_url}"
                params = {}  # params already embedded in next_url
            else:
                url = None

    return events

# ── Insert into DB ────────────────────────────────────────────────────────────

def cents_to_float(money: dict | None) -> float:
    if not money:
        return 0.0
    return round((money.get("cents") or 0) / 100, 2)

async def upsert_booking(
    conn: asyncpg.Connection,
    event: dict,
    rental_id: str,
    rental_name: str,
    tenant_id: str,
) -> str:
    """Insert or update a booking. Returns 'inserted', 'updated', or 'skipped'."""

    # Only import actual bookings, not holds
    if event.get("type") != "Booking":
        return "skipped"

    # Skip cancelled events
    if event.get("cancelled_at"):
        return "skipped"

    event_id    = event["id"]
    guest_name  = event.get("guest_name") or event.get("title") or "Unknown Guest"
    guest_email = event.get("guest_email") or f"{event_id}@hosthub.import"
    guest_phone = event.get("guest_phone") or ""
    check_in    = event["date_from"]
    check_out   = event["date_to"]
    guests      = int(event.get("guest_number") or event.get("guest_adults") or 1)
    payout      = cents_to_float(event.get("total_payout"))
    source      = event.get("source", {}) or {}
    channel     = source.get("channel_type_code") or "hosthub"
    notes       = event.get("notes") or ""
    reservation_id = event.get("reservation_id") or event_id

    # Use event_id as idempotency key — safe to re-run
    idempotency_key = f"hosthub-{event_id}"

    # Check if already exists
    existing = await conn.fetchrow(
        "SELECT id FROM travelos_bookings WHERE idempotency_key = $1",
        idempotency_key
    )

    if existing:
        return "skipped"

    booking_id = str(uuid.uuid4())
    await conn.execute(
        """
        INSERT INTO travelos_bookings
            (id, tenant_id, idempotency_key, pms_booking_id,
             unit_id, check_in, check_out, guests,
             guest_name, guest_email, guest_phone,
             status, channel, commission_pct, notes, created_at)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,
                'confirmed',$12,5.0,$13,NOW())
        """,
        booking_id,
        uuid.UUID(tenant_id),
        idempotency_key,
        event_id,          # pms_booking_id = HostHub event ID
        rental_id,         # unit_id
        check_in,
        check_out,
        guests,
        guest_name,
        guest_email,
        guest_phone,
        channel,
        notes,
    )
    return "inserted"

# ── Main ──────────────────────────────────────────────────────────────────────

async def main():
    print("Vayancy — HostHub Booking Import")
    print("=" * 50)

    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=3)

    total_inserted = 0
    total_skipped  = 0
    total_errors   = 0

    for rental_id, rental_name, location in RENTALS:
        print(f"\n→ {rental_name} ({rental_id})")
        try:
            events = await fetch_all_events(rental_id)
            bookings = [e for e in events if e.get("type") == "Booking"]
            print(f"  Found {len(bookings)} bookings, {len(events)-len(bookings)} holds")

            async with pool.acquire() as conn:
                for event in events:
                    try:
                        result = await upsert_booking(
                            conn, event, rental_id, rental_name, TENANT_ID
                        )
                        if result == "inserted":
                            total_inserted += 1
                            guest = event.get("guest_name") or event.get("title") or "?"
                            print(f"  ✓ {event['date_from']} → {event['date_to']}  {guest}")
                        elif result == "skipped":
                            total_skipped += 1
                    except Exception as e:
                        total_errors += 1
                        print(f"  ✗ Error on {event.get('id')}: {e}")

        except Exception as e:
            print(f"  ✗ Failed to fetch {rental_name}: {e}")
            total_errors += 1

    await pool.close()

    print(f"\n{'='*50}")
    print(f"Done.")
    print(f"  Inserted: {total_inserted}")
    print(f"  Skipped:  {total_skipped} (already imported or holds)")
    print(f"  Errors:   {total_errors}")
    print(f"\nRefresh your dashboard to see the bookings.")

if __name__ == "__main__":
    asyncio.run(main())
