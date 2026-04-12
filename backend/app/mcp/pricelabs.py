"""
PriceLabs MCP Server — port 3003.
Issue 15: retry, backoff, dry_run guard, structured error returns.
"""
from __future__ import annotations
import asyncio
import json
import os

import httpx
import structlog
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("pricelabs")
log = structlog.get_logger()

_API_KEY  = os.getenv("PRICELABS_API_KEY", "")
_PROP_ID  = os.getenv("PRICELABS_PROPERTY_ID", "")
_API_BASE = os.getenv("PRICELABS_API_BASE", "https://api.pricelabs.co/v1")

_MAX_RETRIES    = 3
_RETRY_STATUSES = {429, 500, 502, 503, 504}
_RATE_FLOOR     = 150.0   # EUR — hard floor, never go below
_RATE_CEILING   = 2500.0  # EUR — hard ceiling


def _headers() -> dict:
    return {"X-API-Key": _API_KEY, "Content-Type": "application/json"}


async def _request(method: str, path: str, **kwargs) -> dict | list:
    url = f"{_API_BASE}{path}"
    for attempt in range(_MAX_RETRIES):
        try:
            async with httpx.AsyncClient(timeout=12) as client:
                r = await client.request(method, url, headers=_headers(), **kwargs)
                if r.status_code in _RETRY_STATUSES and attempt < _MAX_RETRIES - 1:
                    wait = int(r.headers.get("Retry-After", 2 ** attempt))
                    log.warning("pricelabs_retry", status=r.status_code, wait=wait)
                    await asyncio.sleep(wait)
                    continue
                r.raise_for_status()
                return r.json()
        except httpx.TimeoutException:
            if attempt < _MAX_RETRIES - 1:
                await asyncio.sleep(2 ** attempt)
                continue
            raise
    raise RuntimeError("PriceLabs: max retries exceeded")


@mcp.tool()
async def get_current_rates(date_from: str, date_to: str) -> str:
    """Fetch current nightly rates for a date range (YYYY-MM-DD)."""
    data = await _request("GET", f"/listings/{_PROP_ID}/calendar", params={
        "start_date": date_from, "end_date": date_to,
    })
    return json.dumps(data, ensure_ascii=False)


@mcp.tool()
async def get_market_data(location: str, date_from: str, date_to: str) -> str:
    """Fetch competitor market rate data for a location and date range."""
    data = await _request("GET", "/market-data", params={
        "location": location, "start_date": date_from, "end_date": date_to,
    })
    return json.dumps(data, ensure_ascii=False)


@mcp.tool()
async def get_min_stay_rules() -> str:
    """Return current minimum-stay rules."""
    data = await _request("GET", f"/listings/{_PROP_ID}/min-stay")
    return json.dumps(data, ensure_ascii=False)


@mcp.tool()
async def push_rate_overrides(date_rates_json: str, dry_run: bool = True) -> str:
    """
    Push nightly rate overrides. date_rates_json: JSON array of {date, rate}.
    Rates outside €150–€2500 are clamped with a warning.
    dry_run=True (default): preview without committing.
    ALWAYS call with dry_run=True first.
    """
    rates = json.loads(date_rates_json)

    # Guard: clamp rates to valid range
    clamped = []
    for item in rates:
        original = item.get("rate", 0)
        clamped_rate = max(_RATE_FLOOR, min(_RATE_CEILING, float(original)))
        if clamped_rate != original:
            log.warning("rate_clamped", date=item.get("date"),
                        original=original, clamped=clamped_rate)
        clamped.append({**item, "rate": clamped_rate})

    if dry_run:
        return json.dumps({
            "dry_run":     True,
            "would_apply": clamped,
            "count":       len(clamped),
            "note":        "Call with dry_run=False to commit.",
        })

    await _request("POST", f"/listings/{_PROP_ID}/calendar",
                   json={"overrides": clamped})
    log.info("rates_applied", count=len(clamped))
    return json.dumps({"applied": True, "count": len(clamped)})


@mcp.tool()
async def update_base_price(new_base_price: float, rationale: str) -> str:
    """Update the base price for the listing (EUR/night). Min €150, max €2500."""
    clamped = max(_RATE_FLOOR, min(_RATE_CEILING, new_base_price))
    if clamped != new_base_price:
        log.warning("base_price_clamped", original=new_base_price, clamped=clamped)
    await _request("PATCH", f"/listings/{_PROP_ID}", json={"base_price": clamped})
    return f"Base price set to €{clamped}/night. Reason: {rationale}"


@mcp.tool()
async def set_min_stay(date_from: str, date_to: str, min_nights: int) -> str:
    """Set minimum stay requirement for a date range."""
    await _request("POST", f"/listings/{_PROP_ID}/min-stay", json={
        "start_date": date_from, "end_date": date_to, "min_nights": min_nights,
    })
    return f"Min stay {min_nights}n: {date_from} → {date_to}"


if __name__ == "__main__":
    port = int(os.getenv("MCP_PRICELABS_PORT", "3003"))
    mcp.run(transport="sse", host="0.0.0.0", port=port)
