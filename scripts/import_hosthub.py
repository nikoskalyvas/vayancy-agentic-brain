import asyncio, uuid, httpx, asyncpg

HOSTHUB_API_KEY = "NDU1NDliYzYtMmQ1ZS00OTZhLWJiMmMtZmUyYTA2MGE3YTAx"
TENANT_ID       = "42aeac37-2344-4a97-9757-23f853963dbb"
DATABASE_URL    = "postgresql://postgres:tOlmvINfpHhFfbuwNtYTESCzIrFOwCoJ@shinkansen.proxy.rlwy.net:21271/railway"
API_BASE        = "https://app.hosthub.com/api/2019-03-01"

RENTALS = [
    ("n19trsyt73", "Nidry Hills"),
    ("quktckazxe", "Boat Villa"),
    ("bqke38mc1b", "Adaman_Nicoleta"),
    ("z3sc4f7avm", "Adaman_Maria"),
]

async def fetch(rental_id):
    events = []
    url = f"{API_BASE}/rentals/{rental_id}/calendar-events"
    params = {"is_visible": "true"}
    headers = {"Authorization": HOSTHUB_API_KEY, "Accept": "application/json"}
    async with httpx.AsyncClient(timeout=30) as c:
        while url:
            r = await c.get(url, params=params, headers=headers)
            r.raise_for_status()
            d = r.json()
            events.extend(d.get("data", []))
            nxt = d.get("navigation", {}).get("next")
            url = nxt if nxt and nxt.startswith("http") else (f"https://app.hosthub.com{nxt}" if nxt else None)
            params = {}
    return events

async def main():
    print("Importing HostHub bookings...")
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=3)
    ins = skp = err = 0
    for rid, name in RENTALS:
        print(f"\n→ {name}")
        try:
            events = await fetch(rid)
            print(f"  Fetched {len(events)} events")
            async with pool.acquire() as conn:
                for e in events:
                    if e.get("type") != "Booking" or e.get("cancelled_at"):
                        skp += 1
                        continue
                    key = f"hosthub-{e['id']}"
                    if await conn.fetchrow("SELECT id FROM travelos_bookings WHERE idempotency_key=$1", key):
                        skp += 1
                        continue
                    src = e.get("source") or {}
                    await conn.execute(
                        """INSERT INTO travelos_bookings
                            (id,tenant_id,idempotency_key,pms_booking_id,unit_id,
                             check_in,check_out,guests,guest_name,guest_email,
                             guest_phone,status,channel,commission_pct,notes,created_at)
                           VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,'confirmed',$12,5.0,$13,NOW())""",
                        str(uuid.uuid4()), uuid.UUID(TENANT_ID), key, e["id"], rid,
                        e["date_from"], e["date_to"],
                        int(e.get("guest_number") or e.get("guest_adults") or 1),
                        e.get("guest_name") or e.get("title") or "Guest",
                        e.get("guest_email") or f"{e['id']}@hosthub",
                        e.get("guest_phone") or "",
                        src.get("channel_type_code") or "hosthub",
                        e.get("notes") or ""
                    )
                    ins += 1
                    print(f"  ✓ {e['date_from']}→{e['date_to']} {e.get('guest_name','?')}")
        except Exception as ex:
            print(f"  ✗ {ex}")
            err += 1
    await pool.close()
    print(f"\nDone — Inserted:{ins} Skipped:{skp} Errors:{err}")

asyncio.run(main())
