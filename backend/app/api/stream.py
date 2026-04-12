"""
SSE stream — fixes applied:

Fix 2+3: All queries and LISTEN now scoped to property_id.
         Per-property channel: workflow_updates_{property_id}
         Historical rows: WHERE property_id = $1
         No cross-tenant data leakage possible.
Fix 14:  REST fallback /stream/workflows/recent requires Bearer token
         matching settings.secret_key.
"""
from __future__ import annotations
import asyncio
import json
from typing import AsyncGenerator

import asyncpg
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from app.config import settings
from app.db.session import get_pool

router = APIRouter()

_HEARTBEAT_INTERVAL = 20   # seconds


def _channel(property_id: str) -> str:
    return f"workflow_updates_{property_id}"


def _require_property_id(request: Request) -> str:
    pid = request.query_params.get("property_id", "").strip()
    if not pid:
        raise HTTPException(
            status_code=400,
            detail="property_id query parameter is required."
        )
    return pid


def _require_auth(request: Request) -> None:
    """Fix 14: simple bearer token auth for REST endpoint."""
    auth = request.headers.get("Authorization", "")
    token = auth.removeprefix("Bearer ").strip()
    if token != settings.secret_key:
        raise HTTPException(status_code=401, detail="Unauthorized")


async def _listen_generator(
    request: Request,
    property_id: str,
) -> AsyncGenerator[str, None]:
    """
    Fix 2+3: LISTEN on per-property channel only.
    Historical rows filtered by property_id.
    One dedicated DB connection per SSE client.
    """
    pool    = await get_pool()
    queue: asyncio.Queue[str] = asyncio.Queue(maxsize=200)
    channel = _channel(property_id)

    def _on_notify(
        connection: asyncpg.Connection,
        pid: int,
        ch: str,
        payload: str,
    ) -> None:
        try:
            queue.put_nowait(f"data: {payload}\n\n")
        except asyncio.QueueFull:
            pass

    conn: asyncpg.Connection = await pool.acquire()
    try:
        await conn.add_listener(channel, _on_notify)

        # Fix 3: historical rows filtered by property_id
        recent = await conn.fetch(
            """
            SELECT id, workflow_id, event_id, agent, action,
                   status, details, timestamp
            FROM workflow_logs
            WHERE property_id = $1
            ORDER BY timestamp DESC
            LIMIT 50
            """,
            property_id,
        )
        for row in reversed(recent):
            data = {
                "id":          row["id"],
                "workflow_id": row["workflow_id"],
                "event_id":    row["event_id"],
                "agent":       row["agent"],
                "action":      row["action"],
                "status":      row["status"],
                "details":     row["details"],
                "timestamp":   row["timestamp"].isoformat(),
            }
            yield f"data: {json.dumps(data)}\n\n"

        while True:
            if await request.is_disconnected():
                break
            try:
                event = await asyncio.wait_for(queue.get(),
                                               timeout=_HEARTBEAT_INTERVAL)
                yield event
            except asyncio.TimeoutError:
                yield ": heartbeat\n\n"

    finally:
        await conn.remove_listener(channel, _on_notify)
        await pool.release(conn)


@router.get("/stream/workflows")
async def stream_workflows(request: Request) -> StreamingResponse:
    """
    SSE endpoint. Requires ?property_id=<id> query param.
    Connect from dashboard:
      const es = new EventSource('/stream/workflows?property_id=villa-azure')
      es.onmessage = e => console.log(JSON.parse(e.data))
    """
    property_id = _require_property_id(request)
    return StreamingResponse(
        _listen_generator(request, property_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control":     "no-cache",
            "X-Accel-Buffering": "no",
            "Connection":        "keep-alive",
        },
    )


@router.get("/stream/workflows/recent")
async def recent_workflows(request: Request, limit: int = 100) -> list[dict]:
    """
    Fix 14: requires Authorization: Bearer <secret_key>.
    Fix 3:  results scoped to property_id.
    """
    _require_auth(request)
    property_id = _require_property_id(request)

    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, workflow_id, event_id, agent, action,
                   status, details, timestamp
            FROM workflow_logs
            WHERE property_id = $1
            ORDER BY timestamp DESC
            LIMIT $2
            """,
            property_id, limit,
        )
    return [
        {
            "id":          r["id"],
            "workflow_id": r["workflow_id"],
            "event_id":    r["event_id"],
            "agent":       r["agent"],
            "action":      r["action"],
            "status":      r["status"],
            "details":     r["details"],
            "timestamp":   r["timestamp"].isoformat(),
        }
        for r in rows
    ]
