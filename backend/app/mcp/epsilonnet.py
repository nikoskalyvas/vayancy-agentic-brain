"""
Epsilon Net MCP Server — port 3004.
Issue 15: retry, full audit logging on every write (legal compliance),
          structured error returns, VAT validation.
"""
from __future__ import annotations
import asyncio
import base64
import json
import os

import httpx
import structlog
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("epsilonnet")
log = structlog.get_logger()

_USERNAME   = os.getenv("EPSILON_NET_USERNAME", "")
_PASSWORD   = os.getenv("EPSILON_NET_PASSWORD", "")
_COMPANY_ID = os.getenv("EPSILON_NET_COMPANY_ID", "")
_API_URL    = os.getenv("EPSILON_NET_API_URL", "https://api.epsilonnet.gr/v1")

VAT_RATES   = {"accommodation": 0.13, "food": 0.24, "reduced": 0.06}
_MAX_RETRIES    = 3
_RETRY_STATUSES = {429, 500, 502, 503, 504}


def _headers() -> dict:
    creds = base64.b64encode(f"{_USERNAME}:{_PASSWORD}".encode()).decode()
    return {
        "Authorization": f"Basic {creds}",
        "Content-Type":  "application/json",
        "X-Company-Id":  _COMPANY_ID,
    }


async def _request(method: str, path: str, **kwargs) -> dict:
    url = f"{_API_URL}{path}"
    for attempt in range(_MAX_RETRIES):
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.request(method, url, headers=_headers(), **kwargs)
                if r.status_code in _RETRY_STATUSES and attempt < _MAX_RETRIES - 1:
                    wait = int(r.headers.get("Retry-After", 2 ** attempt))
                    log.warning("epsilonnet_retry", status=r.status_code,
                                path=path, wait=wait)
                    await asyncio.sleep(wait)
                    continue
                r.raise_for_status()
                return r.json()
        except httpx.TimeoutException:
            if attempt < _MAX_RETRIES - 1:
                await asyncio.sleep(2 ** attempt)
                continue
            log.error("epsilonnet_timeout", path=path, attempt=attempt)
            raise
    raise RuntimeError(f"Epsilon Net: max retries exceeded for {path}")


# Stage 4 completion: amount floor + ceiling + per-reservation dedup
_FOLIO_MIN_EUR = 1.0       # below €1 net is clearly bad data
_FOLIO_MAX_EUR = 50_000.0  # above €50k/stay is clearly bad data


@mcp.tool()
async def create_folio(
    reservation_id: str,
    guest_name:     str,
    guest_afm:      str,
    checkin_date:   str,
    checkout_date:  str,
    total_amount:   float,
    currency:       str = "EUR",
    notes:          str = "",
) -> str:
    """
    Create a Greek tax-compliant accommodation folio.
    guest_afm: Greek tax number (ΑΦΜ). Use '000000000' for foreign guests.
    total_amount: net amount (pre-VAT) in EUR.
    VAT 13% applied automatically.
    This is a legally required operation — every failure is logged.

    Stage 4 compliance fixes:
      - Amount validation: rejects ≤0, negative, or implausible values
      - Idempotency: duplicate call for same reservation_id returns cached result
    """
    # ── Amount validation (compliance risk if skipped) ────────────────────────
    if total_amount is None or total_amount <= 0:
        err = f"Invalid total_amount={total_amount}: must be positive. Folio blocked."
        log.error("folio_amount_invalid", reservation_id=reservation_id,
                  total_amount=total_amount, reason="non_positive")
        return json.dumps({"error": err, "reservation_id": reservation_id,
                           "status": "validation_failed"})

    if total_amount < _FOLIO_MIN_EUR:
        err = (f"total_amount={total_amount} is below minimum €{_FOLIO_MIN_EUR}. "
               f"Likely a data error. Folio blocked.")
        log.error("folio_amount_too_low", reservation_id=reservation_id,
                  total_amount=total_amount, min=_FOLIO_MIN_EUR)
        return json.dumps({"error": err, "reservation_id": reservation_id,
                           "status": "validation_failed"})

    if total_amount > _FOLIO_MAX_EUR:
        err = (f"total_amount={total_amount} exceeds ceiling €{_FOLIO_MAX_EUR}. "
               f"Requires manual review. Folio blocked.")
        log.error("folio_amount_too_high", reservation_id=reservation_id,
                  total_amount=total_amount, max=_FOLIO_MAX_EUR)
        return json.dumps({"error": err, "reservation_id": reservation_id,
                           "status": "validation_failed"})

    # ── Idempotency: check if folio already exists for this reservation ────────
    # Prevents duplicate Greek tax documents on agent retry.
    try:
        existing = await _request("GET", "/folios",
                                  params={"reference": reservation_id, "limit": 1})
        existing_list = existing if isinstance(existing, list) else existing.get("data", [])
        if existing_list:
            cached = existing_list[0]
            log.info("folio_idempotent_return", reservation_id=reservation_id,
                     folio_id=cached.get("id"))
            return json.dumps({
                "folio_id":    cached.get("id"),
                "status":      "already_exists",
                "message":     "Folio already exists for this reservation. Returning cached.",
                "gross_amount": cached.get("gross_amount"),
            })
    except Exception:
        pass  # If check fails, proceed — better to attempt than to silently skip

    vat   = round(total_amount * VAT_RATES["accommodation"], 2)
    gross = round(total_amount + vat, 2)

    payload = {
        "document_type": "accommodation_folio",
        "reference":     reservation_id,
        "guest":         {"name": guest_name, "afm": guest_afm},
        "service_period": {"from": checkin_date, "to": checkout_date},
        "lines": [{
            "description":  f"Accommodation {checkin_date}–{checkout_date}",
            "category":     1,
            "net_amount":   total_amount,
            "vat_rate":     VAT_RATES["accommodation"],
            "vat_amount":   vat,
            "gross_amount": gross,
        }],
        "currency": currency,
        "notes":    notes,
    }

    try:
        data = await _request("POST", "/folios", json=payload)
        folio_id = data.get("id")
        log.info("folio_created", folio_id=folio_id,
                 reservation_id=reservation_id, gross=gross)
        return json.dumps({
            "folio_id":    folio_id,
            "net_amount":  total_amount,
            "vat_amount":  vat,
            "gross_amount": gross,
            "status":      "created",
        })
    except Exception as e:
        # Compliance: log every folio failure — never silently drop
        log.error("folio_creation_failed", reservation_id=reservation_id,
                  error=str(e))
        return json.dumps({"error": str(e), "reservation_id": reservation_id,
                           "status": "failed"})


@mcp.tool()
async def issue_invoice(folio_id: str) -> str:
    """Issue a final tax invoice (Τιμολόγιο) from a folio."""
    try:
        data = await _request("POST", f"/folios/{folio_id}/invoice")
        log.info("invoice_issued", folio_id=folio_id,
                 invoice_number=data.get("invoice_number"))
        return json.dumps({
            "invoice_number": data.get("invoice_number"),
            "pdf_url":        data.get("pdf_url"),
            "issued_at":      data.get("issued_at"),
        })
    except Exception as e:
        log.error("invoice_failed", folio_id=folio_id, error=str(e))
        return json.dumps({"error": str(e), "folio_id": folio_id})


@mcp.tool()
async def get_folio(folio_id: str) -> str:
    """Retrieve a folio by ID."""
    data = await _request("GET", f"/folios/{folio_id}")
    return json.dumps(data, ensure_ascii=False)


@mcp.tool()
async def list_outstanding_folios() -> str:
    """List all folios that have not yet had invoices issued."""
    data = await _request("GET", "/folios", params={"status": "pending"})
    return json.dumps(data, ensure_ascii=False)


@mcp.tool()
async def calculate_vat(net_amount: float, service_type: str = "accommodation") -> str:
    """
    Calculate Greek VAT breakdown.
    service_type: accommodation (13%) | food (24%) | reduced (6%)
    """
    rate = VAT_RATES.get(service_type, 0.13)
    vat  = round(net_amount * rate, 2)
    return json.dumps({
        "net_amount":  net_amount,
        "vat_rate":    rate,
        "vat_amount":  vat,
        "gross_amount": round(net_amount + vat, 2),
    })


if __name__ == "__main__":
    port = int(os.getenv("MCP_EPSILONNET_PORT", "3004"))
    mcp.run(transport="sse", host="0.0.0.0", port=port)
