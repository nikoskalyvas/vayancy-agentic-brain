"""
WebHotelier MCP Server — port 3001.
Issue 15 hardening: retry on 429/5xx with exponential backoff, structured errors.
"""
from __future__ import annotations
import asyncio
import json
import os
from datetime import date, timedelta

import httpx
import structlog
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("webhotelier")
log = structlog.get_logger()

_API_KEY  = os.getenv("WEBHOTELIER_API_KEY", "")
_PROP_ID  = os.getenv("WEBHOTELIER_PROPERTY_ID", "")
_API_BASE = os.getenv("WEBHOTELIER_API_BASE", "https://api.webhotelier.net/v2")

_MAX_RETRIES = 3
_RETRY_STATUSES = {429, 500, 502, 503, 504}


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {_API_KEY}",
        "Content-Type":  "application/json",
        "X-Property-Id": _PROP_ID,
    }


async def _request(method: str, path: str, **kwargs) -> dict | list:
    """HTTP client with retry + exponential backoff."""
    url = f"{_API_BASE}{path}"
    for attempt in range(_MAX_RETRIES):
        try:
            async with httpx.AsyncClient(timeout=12) as client:
                r = await client.request(method, url, headers=_headers(), **kwargs)
                if r.status_code in _RETRY_STATUSES and attempt < _MAX_RETRIES - 1:
                    wait = 2 ** attempt
                    log.warning("webhotelier_retry", status=r.status_code,
                                attempt=attempt + 1, wait=wait)
                    await asyncio.sleep(wait)
                    continue
                r.raise_for_status()
                return r.json()
        except httpx.TimeoutException:
            if attempt < _MAX_RETRIES - 1:
                await asyncio.sleep(2 ** attempt)
                continue
            raise
    raise RuntimeError("Max retries exceeded")


@mcp.tool()
async def get_reservation(reservation_id: str) -> str:
    """Fetch full reservation details including guest profile."""
    data = await _request("GET", f"/reservations/{reservation_id}")
    return json.dumps(data, ensure_ascii=False, indent=2)


@mcp.tool()
async def list_reservations(
    checkin_from: str,
    checkin_to:   str,
    status:       str = "confirmed",
) -> str:
    """List reservations within a date range (YYYY-MM-DD). status: confirmed|cancelled|all"""
    data = await _request("GET", "/reservations", params={
        "checkin_from": checkin_from, "checkin_to": checkin_to, "status": status,
    })
    return json.dumps(data, ensure_ascii=False)


@mcp.tool()
async def get_upcoming_checkouts(days_ahead: int = 1) -> str:
    """Return reservations checking out within the next N days."""
    today = date.today()
    until = today + timedelta(days=days_ahead)
    data  = await _request("GET", "/reservations", params={
        "checkout_from": str(today), "checkout_to": str(until), "status": "confirmed",
    })
    return json.dumps(data, ensure_ascii=False)


@mcp.tool()
async def update_reservation(reservation_id: str, fields_json: str) -> str:
    """Patch a reservation. fields_json: JSON object of fields to update."""
    data = json.loads(fields_json)
    await _request("PATCH", f"/reservations/{reservation_id}", json=data)
    return f"Reservation {reservation_id} updated: {list(data.keys())}"


@mcp.tool()
async def get_availability(date_from: str, date_to: str) -> str:
    """Return availability and base rates for a date range (YYYY-MM-DD)."""
    data = await _request("GET", "/availability", params={
        "from": date_from, "to": date_to, "property_id": _PROP_ID,
    })
    return json.dumps(data, ensure_ascii=False)


@mcp.tool()
async def get_occupancy_stats(month: str) -> str:
    """Return occupancy % and revenue for a month (YYYY-MM)."""
    data = await _request("GET", "/reports/occupancy", params={
        "month": month, "property_id": _PROP_ID,
    })
    return json.dumps(data, ensure_ascii=False)


if __name__ == "__main__":
    port = int(os.getenv("MCP_WEBHOTELIER_PORT", "3001"))
    mcp.run(transport="sse", host="0.0.0.0", port=port)
