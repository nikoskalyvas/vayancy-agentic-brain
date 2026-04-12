"""
ARQ Worker — updated with TravelOS hold expiry cron.

Added:
  expire_stale_holds — runs every 5 minutes, marks expired holds as 'expired'
                       so inventory is freed back to the PMS for new guests.
"""
from __future__ import annotations
import json
import uuid
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


# ── Lifecycle ─────────────────────────────────────────────────────────────────

async def startup(ctx: dict) -> None:
    await init_schema()
    pool = await get_pool()
    ctx["db_pool"] = pool
    ctx["memory"]  = AgentMemory(pool)
    log.info("arq_worker_started", property_id=settings.property_id)


async def shutdown(ctx: dict) -> None:
    await close_pool()
    log.info("arq_worker_stopped")


class WorkerSettings:
    redis_settings = arq.connections.RedisSettings.from_dsn(settings.redis_url)
    functions       = [run_booking_workflow, run_whatsapp_reply_workflow]
    cron_jobs       = [
        cron(scheduled_pricing_review,    hour={0, 4, 8, 12, 16, 20}, minute=0),
        cron(scheduled_checkout_dispatch, hour=8, minute=0),
        # TravelOS: expire stale holds every 5 minutes
        cron(expire_stale_holds,          minute={0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55}),
    ]
    on_startup  = startup
    on_shutdown = shutdown
    max_jobs    = 10
    job_timeout = settings.worker_job_timeout
    keep_result = 3600
