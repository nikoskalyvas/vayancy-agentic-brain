"""
WhatsApp MCP Server — port 3002.
Issue 15: retry on 429, structured error return, rate limit awareness,
          raise_for_status on all calls.
"""
from __future__ import annotations
import asyncio
import json
import os

import asyncpg
import httpx
import structlog
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("whatsapp")
log = structlog.get_logger()

_TOKEN    = os.getenv("WHATSAPP_ACCESS_TOKEN", "")
_PHONE_ID = os.getenv("WHATSAPP_PHONE_NUMBER_ID", "")
_API_BASE = os.getenv("WHATSAPP_API_BASE", "https://graph.facebook.com/v20.0")
_DB_URL   = os.getenv("DATABASE_URL", "")

_MAX_RETRIES = 3


def _headers() -> dict:
    return {"Authorization": f"Bearer {_TOKEN}", "Content-Type": "application/json"}


async def _post(payload: dict) -> dict:
    """POST to WhatsApp API with retry on 429."""
    if not _TOKEN or not _PHONE_ID:
        return {"error": "WhatsApp credentials not configured"}

    url = f"{_API_BASE}/{_PHONE_ID}/messages"
    for attempt in range(_MAX_RETRIES):
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(url, headers=_headers(), json=payload)

            if r.status_code == 429:
                retry_after = int(r.headers.get("Retry-After", 2 ** attempt))
                log.warning("whatsapp_rate_limited", retry_after=retry_after,
                            attempt=attempt + 1)
                if attempt < _MAX_RETRIES - 1:
                    await asyncio.sleep(retry_after)
                    continue
            r.raise_for_status()
            return r.json()
    raise RuntimeError("WhatsApp: max retries exceeded")


@mcp.tool()
async def send_text_message(to: str, body: str) -> str:
    """
    Send a free-form text message (within 24h customer service window).
    to: E.164 phone number e.g. +447911123456
    """
    data = await _post({
        "messaging_product": "whatsapp",
        "to":   to,
        "type": "text",
        "text": {"body": body, "preview_url": False},
    })
    if "error" in data:
        log.error("whatsapp_send_failed", error=data["error"], to=to)
        return f"ERROR: {data['error']}"
    msg_id = data.get("messages", [{}])[0].get("id", "unknown")
    log.info("whatsapp_sent", to=to, message_id=msg_id)
    return f"Sent. Message ID: {msg_id}"


@mcp.tool()
async def send_template_message(
    to:              str,
    template_name:   str,
    language_code:   str,
    parameters_json: str,
) -> str:
    """
    Send an approved WhatsApp template (works outside 24h window).
    parameters_json: JSON array e.g. '[{"type":"text","text":"John"}]'
    Templates: welcome_booking, checkin_reminder, checkout_followup,
               modification_confirmed, cancellation_confirmed
    """
    params = json.loads(parameters_json)
    data   = await _post({
        "messaging_product": "whatsapp",
        "to":   to,
        "type": "template",
        "template": {
            "name":     template_name,
            "language": {"code": language_code},
            "components": [{"type": "body", "parameters": params}],
        },
    })
    if "error" in data:
        return f"ERROR: {data['error']}"
    msg_id = data.get("messages", [{}])[0].get("id", "unknown")
    return f"Template '{template_name}' sent. ID: {msg_id}"


@mcp.tool()
async def get_message_thread(guest_phone: str, limit: int = 20) -> str:
    """Return recent conversation thread with a guest from the database."""
    if not _DB_URL:
        return "[]"
    try:
        conn = await asyncpg.connect(_DB_URL)
        rows = await conn.fetch(
            """
            SELECT direction, content, created_at
            FROM guest_interactions
            WHERE guest_phone = $1
            ORDER BY created_at DESC
            LIMIT $2
            """,
            guest_phone, limit,
        )
        await conn.close()
        thread = [
            {"direction": r["direction"], "content": r["content"],
             "at": str(r["created_at"])}
            for r in reversed(rows)
        ]
        return json.dumps(thread, ensure_ascii=False)
    except Exception as e:
        log.error("get_thread_failed", error=str(e))
        return json.dumps({"error": str(e), "thread": []})


@mcp.tool()
async def mark_message_read(message_id: str) -> str:
    """Mark a WhatsApp message as read."""
    data = await _post({
        "messaging_product": "whatsapp",
        "status":     "read",
        "message_id": message_id,
    })
    if "error" in data:
        return f"ERROR: {data['error']}"
    return f"Message {message_id} marked as read."


if __name__ == "__main__":
    port = int(os.getenv("MCP_WHATSAPP_PORT", "3002"))
    mcp.run(transport="sse", host="0.0.0.0", port=port)
