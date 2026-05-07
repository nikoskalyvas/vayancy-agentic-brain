"""
ARQ Worker — updated with TravelOS hold expiry cron.

Added:
  expire_stale_holds — runs every 5 minutes, marks expired holds as 'expired'
                       so inventory is freed back to the PMS for new guests.
"""
from __future__ import annotations
import json
import uuid
from datetime import date
from typing import Any

import asyncpg
import arq
from arq import cron
import structlog

from app.config import settings
from app.core.supervisor import agentic_brain, _notify_channel
from app.core.memory import AgentMemory
from app.db.session import get_pool, init_schema, close_pool

log = structlog.get_logger()


async def _notify(
    conn:        asyncpg.Connection,
    workflow_id: str,
    property_id: str,
    status:      str,
    detail:      str,
) -> None:
    await conn.execute(
        "SELECT pg_notify($1, $2)",
        _notify_channel(property_id),
        json.dumps({
            "workflow_id": workflow_id,
            "property_id": property_id,
            "agent":       "worker",
            "action":      "workflow_status",
            "status":      status,
            "details":     {"detail": detail},
        }),
    )


def _initial_state(
    workflow_id: str,
    event_id:    str,
    property_id: str,
    payload:     dict[str, Any],
    agents:      list[str] | None = None,
) -> dict:
    return {
        "workflow_id":    workflow_id,
        "event_id":       event_id,
        "property_id":    property_id,
        "payload":        payload,
        "memory_context": "",
        "guest_phone":    payload.get("guest_phone"),
        "agents_to_run":  agents or [],
        "results":        [],
        "retry_agents":   [],
    }


async def _store_outputs(
    results:        list[dict],
    memory:         AgentMemory,
    property_id:    str,
    guest_phone:    str | None,
    reservation_id: str | None,
) -> None:
    if not guest_phone:
        return
    for r in results:
        if r.get("output"):
            await memory.store_interaction(
                guest_phone=guest_phone,
                property_id=property_id,
                reservation_id=reservation_id,
                direction="outbound",
                content=r["output"],
            )


# ── Workflow jobs ─────────────────────────────────────────────────────────────

async def run_booking_workflow(
    ctx:         dict,
    workflow_id: str,
    event_id:    str,
    property_id: str,
    payload:     dict[str, Any],
) -> dict:
    pool:   asyncpg.Pool = ctx["db_pool"]
    memory: AgentMemory  = ctx["memory"]

    log.info("workflow_start", workflow_id=workflow_id, event_id=event_id,
             event=payload.get("event"), property_id=property_id)
    try:
        result = await agentic_brain.ainvoke(
            _initial_state(workflow_id, event_id, property_id, payload),
            config={"configurable": {"db_pool": pool, "memory": memory}},
        )
        await _store_outputs(
            result.get("results", []), memory, property_id,
            payload.get("guest_phone"), payload.get("reservation_id"),
        )
        async with pool.acquire() as conn:
            await _notify(conn, workflow_id, property_id, "completed",
                          str([r["agent"] for r in result.get("results", [])]))
        log.info("workflow_complete", workflow_id=workflow_id)
        return {"workflow_id": workflow_id, "status": "completed"}

    except Exception as e:
        log.error("workflow_failed", workflow_id=workflow_id, error=str(e))
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO workflow_logs
                    (id, workflow_id, event_id, property_id,
                     agent, action, status, details)
                VALUES (gen_random_uuid()::text,$1,$2,$3,'worker','error','error',$4)
                """,
                workflow_id, event_id, property_id,
                json.dumps({"error": str(e)}),
            )
            await _notify(conn, workflow_id, property_id, "error", str(e))
        raise


async def run_whatsapp_reply_workflow(
    ctx:         dict,
    workflow_id: str,
    event_id:    str,
    property_id: str,
    payload:     dict[str, Any],
) -> dict:
    pool:   asyncpg.Pool = ctx["db_pool"]
    memory: AgentMemory  = ctx["memory"]

    log.info("whatsapp_reply_start", workflow_id=workflow_id,
             from_phone=payload.get("guest_phone"), property_id=property_id)
    try:
        result = await agentic_brain.ainvoke(
            _initial_state(workflow_id, event_id, property_id, payload,
                           agents=["guest"]),
            config={"configurable": {"db_pool": pool, "memory": memory}},
        )
        await _store_outputs(
            result.get("results", []), memory, property_id,
            payload.get("guest_phone"), None,
        )
        log.info("whatsapp_reply_complete", workflow_id=workflow_id)
        return {"workflow_id": workflow_id, "status": "completed"}

    except Exception as e:
        log.error("whatsapp_reply_failed", workflow_id=workflow_id, error=str(e))
        raise


# ── Scheduled jobs ────────────────────────────────────────────────────────────

async def scheduled_pricing_review(ctx: dict) -> None:
    pool, memory = ctx["db_pool"], ctx["memory"]
    wid = str(uuid.uuid4())
    log.info("scheduled_pricing_review", workflow_id=wid)
    await agentic_brain.ainvoke(
        _initial_state(wid, f"cron-pricing-{wid}", settings.property_id,
                       {"event": "scheduled.pricing_review",
                        "triggered_by": "cron"}, agents=["revenue"]),
        config={"configurable": {"db_pool": pool, "memory": memory}},
    )


async def scheduled_checkout_dispatch(ctx: dict) -> None:
    pool, memory = ctx["db_pool"], ctx["memory"]
    wid = str(uuid.uuid4())
    log.info("scheduled_checkout_dispatch", workflow_id=wid)
    await agentic_brain.ainvoke(
        _initial_state(wid, f"cron-checkout-{wid}", settings.property_id,
                       {"event": "guest.checkout",
                        "triggered_by": "cron"}, agents=["operations"]),
        config={"configurable": {"db_pool": pool, "memory": memory}},
    )


async def pre_arrival_reminders(ctx: dict) -> None:
    """
    Daily 09:00 — send pre-arrival reminder to guests checking in 3 days from now.
    Message includes: property name, charge date, amount, policies reminder.
    Stored as evidence with WA message ID.
    """
    pool = await get_pool()
    rows = await pool.fetch(
        """
        SELECT b.id, b.guest_name, b.guest_email, b.guest_phone,
               b.check_in, b.check_out, b.guests, b.tenant_id,
               p.name AS property_name
        FROM   travelos_bookings b
        JOIN   travelos_properties p ON p.pms_unit_id = b.unit_id
                                    AND p.tenant_id   = b.tenant_id
        WHERE  b.check_in = (CURRENT_DATE + INTERVAL '3 days')::text
          AND  b.status   = 'confirmed'
        """
    )
    log.info("pre_arrival_reminders", count=len(rows))
    for r in rows:
        try:
            # Send pre-arrival email
            from app.core.email import send_pre_arrival_email
            await send_pre_arrival_email(
                to=r["guest_email"],
                guest_name=r["guest_name"],
                property_name=r["property_name"],
                check_in=r["check_in"],
                charge_date=r["check_in"],
            )
        except Exception as e:
            log.warning("pre_arrival_email_failed", booking_id=str(r["id"]), error=str(e))

        try:
            import httpx as _h
            await _h.AsyncClient(timeout=5).post(
                f"http://localhost:8000/dispute/send-messages",
                headers={"Authorization": f"Bearer {settings.secret_key}"},
                json={
                    "booking_ref":   str(r["id"]),
                    "property_id":   str(r["tenant_id"]),
                    "property_name": r["property_name"],
                    "guest_name":    r["guest_name"],
                    "guest_email":   r["guest_email"],
                    "guest_phone":   r["guest_phone"] or "",
                    "check_in":      r["check_in"],
                    "check_out":     r["check_out"],
                    "guests":        r["guests"],
                    "charge_date":   r["check_in"],
                    "message_type":  "pre_arrival",
                },
            )
        except Exception as e:
            log.warning("pre_arrival_reminder_failed",
                        booking_id=str(r["id"]), error=str(e))


async def charge_day_notices(ctx: dict) -> None:
    """
    Daily 07:00 — send charge notice to guests checking in today.
    "Your card will be charged today." — eliminates 'I didn't know I'd be charged' disputes.
    Also triggers pre-auth capture for Stripe-payment bookings.
    """
    pool = await get_pool()
    rows = await pool.fetch(
        """
        SELECT b.id, b.guest_name, b.guest_email, b.guest_phone,
               b.check_in, b.channel, b.tenant_id,
               p.name AS property_name
        FROM   travelos_bookings b
        JOIN   travelos_properties p ON p.pms_unit_id = b.unit_id
                                    AND p.tenant_id   = b.tenant_id
        WHERE  b.check_in = CURRENT_DATE::text
          AND  b.status   = 'confirmed'
        """
    )
    log.info("charge_day_notices", count=len(rows))
    for r in rows:
        try:
            import httpx as _h
            client = _h.AsyncClient(timeout=5)

            # Send charge notice message
            if r["guest_phone"]:
                await client.post(
                    "http://localhost:8000/dispute/send-messages",
                    headers={"Authorization": f"Bearer {settings.secret_key}"},
                    json={
                        "booking_ref":   str(r["id"]),
                        "property_id":   str(r["tenant_id"]),
                        "property_name": r["property_name"],
                        "guest_name":    r["guest_name"],
                        "guest_email":   r["guest_email"],
                        "guest_phone":   r["guest_phone"],
                        "check_in":      r["check_in"],
                        "check_out":     r["check_in"],
                        "guests":        1,
                        "charge_date":   r["check_in"],
                        "message_type":  "charge_notice",
                    },
                )

            # Capture pre-auth for Stripe-payment bookings
            if r["channel"] == "travelos_stripe":
                await client.post(
                    f"http://localhost:8000/payments/capture/{r['id']}",
                    headers={"Authorization": f"Bearer {settings.secret_key}"},
                )
                log.info("auto_capture_triggered", booking_id=str(r["id"]))

        except Exception as e:
            log.warning("charge_day_notice_failed",
                        booking_id=str(r["id"]), error=str(e))


async def scheduled_houfy_price_sync(ctx: dict) -> None:
    """
    Daily 05:00 — push PriceLabs nightly rates to Houfy pricing calendar.

    Runs after the 04:00 Revenue Agent pricing review so that any rate
    adjustments made by the Revenue Agent are reflected in Houfy within
    the hour. Playwright runs headless inside the worker container.

    Requires in .env:
        HOUFY_EMAIL, HOUFY_PASSWORD
        HOUFY_LISTING_ID_* (one per property)
        HOSTHUB_API_KEY, HOSTHUB_RATE_PLAN_NIDRI, HOSTHUB_RATE_PLAN_BOAT,
        HOSTHUB_RATE_PLAN_ADAMAN_NICOLETA, HOSTHUB_RATE_PLAN_ADAMAN_MARIA
    """
    try:
        import sys
        import os
        # The scripts/ directory is mounted at /scripts inside the worker container
        # (see docker-compose volume). Add it to sys.path so we can import the module.
        scripts_dir = os.path.join(os.path.dirname(__file__), "..", "..", "..", "scripts")
        scripts_dir = os.path.abspath(scripts_dir)
        if scripts_dir not in sys.path:
            sys.path.insert(0, scripts_dir)

        from sync_houfy_prices import run_sync  # type: ignore[import]
        await run_sync(days=60, dry_run=False, visible=False, inspect=False)
        log.info("houfy_price_sync_complete")
    except Exception as e:
        log.error("houfy_price_sync_failed", error=str(e))


async def expire_stale_holds(ctx: dict) -> None:
    """
    TravelOS: mark holds whose TTL has passed as 'expired'.
    Runs every 5 minutes. Frees inventory back to the PMS for new guests.
    Any hold that expired without a booking is logged as abandoned.
    """
    pool: asyncpg.Pool = ctx["db_pool"]

    result = await pool.execute(
        """
        UPDATE travelos_holds
        SET    status = 'expired'
        WHERE  status    = 'active'
          AND  expires_at < NOW()
        """
    )

    # Extract count from "UPDATE N"
    count = int(result.split()[-1]) if result else 0
    if count > 0:
        log.info("holds_expired", count=count)


# ── HostHub availability cache refresh ────────────────────────────────────────

def _parse_ical_blocked(ical_text: str, date_from: date, date_to: date) -> set[date]:
    """
    Parse a raw iCal string and return the set of dates in [date_from, date_to)
    that are blocked by any VEVENT.

    Handles DTSTART/DTEND in DATE format (YYYYMMDD) and DATETIME format
    (YYYYMMDDTHHMMSSz / YYYYMMDDTHHMMSS). VEVENT end dates are exclusive
    per the RFC (checkout day is NOT blocked), which aligns with how HostHub,
    Booking.com, and mphb all export iCal.
    """
    import re
    from datetime import date as _date, timedelta as _td

    blocked: set[_date] = set()

    for vevent in re.split(r"BEGIN:VEVENT", ical_text)[1:]:
        # DTSTART — may have VALUE=DATE or VALUE=DATE-TIME param
        start_m = re.search(r"DTSTART(?:;[^:]+)?:(\d{8})", vevent)
        end_m   = re.search(r"DTEND(?:;[^:]+)?:(\d{8})", vevent)
        if not start_m or not end_m:
            continue
        try:
            ev_from = _date(
                int(start_m.group(1)[:4]),
                int(start_m.group(1)[4:6]),
                int(start_m.group(1)[6:8]),
            )
            ev_to = _date(
                int(end_m.group(1)[:4]),
                int(end_m.group(1)[4:6]),
                int(end_m.group(1)[6:8]),
            )
        except ValueError:
            continue

        current = max(ev_from, date_from)
        while current < min(ev_to, date_to):
            blocked.add(current)
            current += _td(days=1)

    return blocked


async def _fetch_ical(url: str) -> str:
    """Download a raw iCal feed. Raises on HTTP errors or timeout."""
    import httpx
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(connect=10.0, read=15.0, write=10.0, pool=10.0),
        follow_redirects=True,
    ) as client:
        r = await client.get(url)
        r.raise_for_status()
        return r.text


async def refresh_hosthub_cache(ctx: dict) -> None:
    """
    Refresh the villa availability cache from iCal feeds for all managed villas.

    Downloads each property's iCal URL, parses blocked dates, then upserts
    90-day availability rows into hosthub_calendar_cache.
    The /villas/search endpoint reads from this cache instead of calling
    any PMS API live on user requests.

    Uses iCal feeds rather than the HostHub REST API to avoid Railway →
    HostHub networking issues (REST API hangs; iCal is served statically
    and is not subject to the same proxy/firewall rules).

    Runs every 30 minutes (see WorkerSettings.cron_jobs).
    Also called once on worker startup so data is available immediately.

    Rate data is not available from iCal — price_per_night will be NULL
    in the cache (prices shown as None in the public API).

    Requires env vars:
        ICAL_BOAT, ICAL_NIDRI, ICAL_ADAMAN_NICOLETA, ICAL_ADAMAN_MARIA
        ICAL_KTIMA_3BED, ICAL_KTIMA_2BED
        ICAL_GARDEN_HOUSE (has a default value in config)
    """
    from datetime import date as _date, timedelta as _td
    from app.db.hosthub_cache import upsert_calendar, CachedDay

    today     = _date.today()
    date_from = today
    date_to   = today + _td(days=365)

    # Map: cache_key → list of iCal URLs.
    # A villa is available on a date only if ALL its units are unblocked.
    # For multi-unit villas (Adaman, Ktima), each unit gets its own cache row
    # and _check_villa takes the first available unit.
    feeds: list[tuple[str, str]] = []  # (cache_key, ical_url)

    for cache_key, url in [
        ("boat-villa",        settings.ical_boat),
        ("nidri-hills-villa", settings.ical_nidri),
        ("adaman-nicoleta",   settings.ical_adaman_nicoleta),
        ("adaman-maria",      settings.ical_adaman_maria),
        ("ktima-3bed",        settings.ical_ktima_3bed),
        ("ktima-2bed",        settings.ical_ktima_2bed),
        ("garden-house",      settings.ical_garden_house),
    ]:
        if url:
            feeds.append((cache_key, url))

    if not feeds:
        log.warning("ical_cache_skip", reason="no iCal URLs configured")
        return

    for cache_key, url in feeds:
        try:
            ical_text = await _fetch_ical(url)
            blocked   = _parse_ical_blocked(ical_text, date_from, date_to)

            days: list[CachedDay] = [
                CachedDay(
                    stay_date=today + _td(days=i),
                    available=(today + _td(days=i)) not in blocked,
                    price_per_night=None,  # iCal feeds carry no pricing data
                )
                for i in range(365)
            ]

            await upsert_calendar(cache_key, days)
            log.info(
                "ical_cache_refreshed",
                cache_key=cache_key,
                blocked_dates=len(blocked),
            )

        except Exception as e:
            log.error("ical_cache_error", cache_key=cache_key, url=url, error=str(e))


# ── Lifecycle ─────────────────────────────────────────────────────────────────────────────────

async def startup(ctx: dict) -> None:
    await init_schema()
    pool = await get_pool()
    ctx["db_pool"] = pool
    ctx["memory"]  = AgentMemory(pool)
    log.info("arq_worker_started", property_id=settings.property_id)
    # Pre-populate availability cache so /villas/search works immediately after deploy
    await refresh_hosthub_cache(ctx)


async def shutdown(ctx: dict) -> None:
    await close_pool()
    log.info("arq_worker_stopped")


class WorkerSettings:
    redis_settings = arq.connections.RedisSettings.from_dsn(settings.redis_url)
    functions       = [run_booking_workflow, run_whatsapp_reply_workflow]
    cron_jobs       = [
        cron(scheduled_pricing_review,    hour={0, 4, 8, 12, 16, 20}, minute=0),
        cron(scheduled_checkout_dispatch, hour=8,  minute=0),
        # Dispute prevention: pre-arrival reminders (3 days before check-in)
        cron(pre_arrival_reminders,       hour=9,  minute=0),
        # Dispute prevention: charge day notice + auto-capture (check-in morning)
        cron(charge_day_notices,          hour=7,  minute=0),
        # TravelOS: expire stale holds every 5 minutes
        cron(expire_stale_holds,          minute={0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55}),
        # Houfy: push PriceLabs rates to Houfy calendar daily after 04:00 pricing review
        cron(scheduled_houfy_price_sync,  hour=5, minute=0),
        # HostHub cache: refresh availability + rates every 30 min
        cron(refresh_hosthub_cache,       minute={0, 30}),
    ]
    on_startup  = startup
    on_shutdown = shutdown
    max_jobs    = 10
    job_timeout = settings.worker_job_timeout
    keep_result = 3600
