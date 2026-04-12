"""
Vayancy TravelOS MCP Server — port 3005.

The AI-facing facade. Claude, ChatGPT, Gemini, or any MCP-compatible agent
calls this single endpoint to search, price, hold, and book villas.

Search architecture (two-phase):
  Phase 1 — DB catalog lookup (fast, no PMS calls)
             Filter by location, guests, amenities, price
  Phase 2 — Parallel PMS availability check on candidates
             Only confirmed-available properties are returned

This means search is fast and the PMS is only hit for real candidates.
"""
from __future__ import annotations

import asyncio
import json
import os
import uuid
from datetime import datetime, timezone, timedelta

import asyncpg
import structlog
from mcp.server.fastmcp import FastMCP, Context

from app.mcp.adapters import WebHotelierAdapter, PMSAdapter
from app.config import settings

log = structlog.get_logger()

mcp = FastMCP(
    "vayancy-travelos",
    description=(
        "Vayancy TravelOS — search, price, hold, and book villas and hotels "
        "across Greece directly through AI. Real-time PMS availability. "
        "No OTA intermediaries. Direct booking, owner as merchant of record."
    ),
)

HOLD_TTL = 15  # minutes

# ── DB pool ───────────────────────────────────────────────────────────────────

_pool: asyncpg.Pool | None = None


async def _get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            settings.database_url, min_size=2, max_size=10, command_timeout=20
        )
    return _pool


# ── Auth ──────────────────────────────────────────────────────────────────────

async def _resolve_tenant(key: str) -> dict | None:
    pool = await _get_pool()
    row  = await pool.fetchrow(
        "SELECT * FROM travelos_tenants WHERE api_key=$1 AND active=TRUE", key
    )
    return dict(row) if row else None


def _get_tenant_key(ctx: Context) -> str | None:
    try:
        h = ctx.request_context.request.headers  # type: ignore
        return h.get("x-tenant-key") or h.get("X-Tenant-Key")
    except Exception:
        return None


async def _auth(ctx: Context) -> dict:
    key = _get_tenant_key(ctx)
    if not key:
        raise PermissionError("Missing X-Tenant-Key header")
    tenant = await _resolve_tenant(key)
    if not tenant:
        raise PermissionError("Invalid or inactive tenant key")
    return tenant


def _build_adapter(tenant: dict) -> PMSAdapter:
    cfg = tenant["pms_config"]
    if tenant["pms_type"] == "webhotelier":
        return WebHotelierAdapter(
            api_key=cfg["api_key"],
            property_id=cfg["property_id"],
            api_base=cfg.get("api_base", "https://api.webhotelier.net/v2"),
        )
    raise ValueError(f"Unsupported PMS: {tenant['pms_type']}")


# ── Catalog helpers ───────────────────────────────────────────────────────────

async def _catalog_search(
    location:  str,
    guests:    int,
    max_price: float | None,
    amenities: list[str],
) -> list[dict]:
    """
    Phase 1: fast DB-only search across all active properties.
    Returns property rows from travelos_properties.
    """
    pool = await _get_pool()

    amenity_filter = ""
    params: list = [guests]

    if amenities:
        params.append(amenities)
        amenity_filter = f"AND amenities @> ${len(params)}::text[]"

    price_filter = ""
    if max_price:
        params.append(max_price)
        price_filter = f"AND (base_rate IS NULL OR base_rate <= ${len(params)})"

    # location is matched via full-text search on location+region+address
    params.append(location)
    location_filter = f"""
        AND to_tsvector('english', location || ' ' || COALESCE(region,'') || ' ' || COALESCE(address,''))
            @@ plainto_tsquery('english', ${len(params)})
    """

    query = f"""
        SELECT
            p.id, p.tenant_id, p.pms_unit_id, p.name, p.slug,
            p.description, p.location, p.region, p.max_guests,
            p.bedrooms, p.bathrooms, p.amenities, p.base_rate,
            p.currency, p.min_stay, p.photos, p.latitude, p.longitude,
            t.pms_type, t.pms_config
        FROM  travelos_properties p
        JOIN  travelos_tenants    t ON t.id = p.tenant_id
        WHERE p.active     = TRUE
          AND t.active     = TRUE
          AND p.max_guests >= $1
          {amenity_filter}
          {price_filter}
          {location_filter}
        ORDER BY p.base_rate ASC NULLS LAST
        LIMIT 20
    """
    rows = await pool.fetch(query, *params)
    return [dict(r) for r in rows]


async def _check_single_property(
    prop: dict, check_in: str, check_out: str, guests: int
) -> dict | None:
    """Check live PMS availability for one property. Returns enriched dict or None."""
    try:
        adapter = WebHotelierAdapter(
            api_key=prop["pms_config"]["api_key"],
            property_id=prop["pms_config"]["property_id"],
            api_base=prop["pms_config"].get("api_base", "https://api.webhotelier.net/v2"),
        )
        result = await adapter.check_availability(
            check_in=check_in,
            check_out=check_out,
            guests=guests,
            unit_id=prop["pms_unit_id"],
        )
        if not result.available:
            return None

        # Use live rate if available, fall back to catalog rate
        live_unit  = result.units[0] if result.units else {}
        live_rate  = live_unit.get("base_rate") or prop.get("base_rate")

        return {
            "property_id":  str(prop["id"]),
            "unit_id":      prop["pms_unit_id"],
            "name":         prop["name"],
            "location":     prop["location"],
            "region":       prop.get("region"),
            "max_guests":   prop["max_guests"],
            "bedrooms":     prop.get("bedrooms"),
            "bathrooms":    prop.get("bathrooms"),
            "amenities":    prop.get("amenities") or [],
            "base_rate":    float(live_rate) if live_rate else None,
            "currency":     result.currency or prop.get("currency", "EUR"),
            "min_stay":     prop.get("min_stay", 1),
            "photos":       prop.get("photos") or [],
            "latitude":     float(prop["latitude"])  if prop.get("latitude")  else None,
            "longitude":    float(prop["longitude"]) if prop.get("longitude") else None,
            "description":  prop.get("description"),
        }
    except Exception as e:
        log.warning("pms_availability_check_failed", unit_id=prop["pms_unit_id"], error=str(e))
        return None


# ── MCP Tools ─────────────────────────────────────────────────────────────────

@mcp.tool()
async def search_properties(
    location:   str,
    check_in:   str,
    check_out:  str,
    guests:     int,
    max_price:  float | None = None,
    amenities:  str | None   = None,
    ctx: Context = None,
) -> str:
    """
    Search available villas and hotels on the Vayancy platform.

    Two-phase search: first finds matching properties from the catalog,
    then checks real-time PMS availability in parallel. Only returns
    properties that are genuinely available for those exact dates.

    Args:
        location:  City, area, or island (e.g. "Mykonos", "Santorini", "Rhodes")
        check_in:  Arrival date YYYY-MM-DD
        check_out: Departure date YYYY-MM-DD
        guests:    Number of guests
        max_price: Optional max total price in EUR
        amenities: Optional comma-separated filters: pool, sea_view, ac, bbq, wifi

    Returns list of available properties with pricing and details.
    """
    tenant = await _auth(ctx)

    amenity_list = [a.strip().lower() for a in amenities.split(",")] if amenities else []

    # Phase 1: catalog search (fast, DB only)
    candidates = await _catalog_search(
        location=location,
        guests=guests,
        max_price=max_price,
        amenities=amenity_list,
    )

    if not candidates:
        return json.dumps({
            "location": location, "check_in": check_in, "check_out": check_out,
            "guests": guests, "properties_found": 0, "properties": [],
            "message": "No properties found matching your criteria.",
        })

    # Phase 2: parallel PMS availability check
    tasks = [
        _check_single_property(p, check_in, check_out, guests)
        for p in candidates
    ]
    results = await asyncio.gather(*tasks, return_exceptions=False)
    available = [r for r in results if r is not None]

    return json.dumps({
        "location":         location,
        "check_in":         check_in,
        "check_out":        check_out,
        "guests":           guests,
        "properties_found": len(available),
        "properties":       available,
    }, ensure_ascii=False, default=str)


@mcp.tool()
async def check_availability(
    unit_id:   str,
    check_in:  str,
    check_out: str,
    guests:    int,
    ctx: Context = None,
) -> str:
    """
    Check real-time availability for a specific villa.

    Args:
        unit_id:   Unit ID from search_properties results
        check_in:  Arrival date YYYY-MM-DD
        check_out: Departure date YYYY-MM-DD
        guests:    Number of guests
    """
    tenant  = await _auth(ctx)
    adapter = _build_adapter(tenant)
    result  = await adapter.check_availability(
        check_in=check_in, check_out=check_out, guests=guests, unit_id=unit_id
    )
    return json.dumps({
        "unit_id": unit_id, "available": result.available,
        "check_in": check_in, "check_out": check_out,
        "guests": guests, "error": result.error,
    })


@mcp.tool()
async def get_rate_details(
    unit_id:   str,
    check_in:  str,
    check_out: str,
    guests:    int,
    ctx: Context = None,
) -> str:
    """
    Get full pricing breakdown for a specific villa and dates.

    Args:
        unit_id:   Unit ID from search_properties results
        check_in:  Arrival date YYYY-MM-DD
        check_out: Departure date YYYY-MM-DD
        guests:    Number of guests

    Returns nightly rate, cleaning fee, taxes, total, cancellation policy.
    """
    tenant  = await _auth(ctx)
    adapter = _build_adapter(tenant)
    rate    = await adapter.get_rate_details(
        unit_id=unit_id, check_in=check_in, check_out=check_out, guests=guests
    )
    return json.dumps({
        "unit_id":   rate.unit_id,
        "check_in":  rate.check_in,
        "check_out": rate.check_out,
        "nights":    rate.nights,
        "currency":  rate.currency,
        "breakdown": {
            "base_rate_per_night": rate.base_rate,
            "subtotal":            rate.total_price,
            "cleaning_fee":        rate.cleaning_fee,
            "tax":                 rate.tax_amount,
        },
        "gross_total":         rate.gross_total,
        "cancellation_policy": rate.cancellation_policy,
        "min_stay_nights":     rate.min_stay,
        "error":               rate.error,
    })


@mcp.tool()
async def create_hold(
    unit_id:     str,
    check_in:    str,
    check_out:   str,
    guests:      int,
    guest_email: str,
    ctx: Context = None,
) -> str:
    """
    Place a 15-minute hold on a villa to lock inventory while the guest
    completes payment. Always call before create_booking.

    Args:
        unit_id:     Unit ID from search_properties
        check_in:    Arrival date YYYY-MM-DD
        check_out:   Departure date YYYY-MM-DD
        guests:      Number of guests
        guest_email: Guest email to associate with hold

    Returns hold_id and expiry. Pass hold_id to create_booking.
    """
    tenant  = await _auth(ctx)
    pool    = await _get_pool()
    adapter = _build_adapter(tenant)

    avail = await adapter.check_availability(
        check_in=check_in, check_out=check_out, guests=guests, unit_id=unit_id
    )
    if not avail.available:
        return json.dumps({
            "success": False,
            "error": f"Unit {unit_id} is not available for {check_in} – {check_out}",
        })

    # Block duplicate holds on same unit/dates
    existing = await pool.fetchrow(
        """
        SELECT id FROM travelos_holds
        WHERE  unit_id=$1 AND check_in=$2 AND check_out=$3
          AND  status='active' AND expires_at > NOW()
        """,
        unit_id, check_in, check_out,
    )
    if existing:
        return json.dumps({
            "success": False,
            "error":   "Unit already held for those dates. Try again shortly.",
        })

    hold_id = str(uuid.uuid4())
    expires = datetime.now(timezone.utc) + timedelta(minutes=HOLD_TTL)

    await pool.execute(
        """
        INSERT INTO travelos_holds
            (id, tenant_id, unit_id, check_in, check_out,
             guests, guest_email, expires_at, status)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,'active')
        """,
        hold_id, tenant["id"], unit_id, check_in, check_out,
        guests, guest_email, expires,
    )

    log.info("hold_created", hold_id=hold_id, unit_id=unit_id)

    return json.dumps({
        "success":   True,
        "hold_id":   hold_id,
        "unit_id":   unit_id,
        "check_in":  check_in,
        "check_out": check_out,
        "expires_at": expires.isoformat(),
        "message":   f"Hold active for {HOLD_TTL} min. Call create_booking with this hold_id.",
    })


@mcp.tool()
async def create_booking(
    hold_id:         str,
    guest_name:      str,
    guest_email:     str,
    guest_phone:     str,
    idempotency_key: str,
    notes:           str = "",
    ctx: Context = None,
) -> str:
    """
    Confirm a booking using an active hold. Creates reservation in the PMS.
    The property owner is the merchant of record — no OTA involved.

    Args:
        hold_id:         Hold ID from create_hold (must be active, not expired)
        guest_name:      Full name of primary guest
        guest_email:     Guest email
        guest_phone:     Guest phone (international format, e.g. +306912345678)
        idempotency_key: Unique key from caller — prevents double-booking on retry
        notes:           Optional special requests

    Returns booking_id and PMS confirmation number.
    """
    tenant = await _auth(ctx)
    pool   = await _get_pool()

    hold = await pool.fetchrow(
        """
        SELECT * FROM travelos_holds
        WHERE  id=$1 AND tenant_id=$2 AND status='active' AND expires_at > NOW()
        """,
        hold_id, tenant["id"],
    )
    if not hold:
        return json.dumps({
            "success": False,
            "error":   "Hold not found, expired, or already used. Call create_hold again.",
        })

    # Idempotency guard
    existing = await pool.fetchrow(
        "SELECT * FROM travelos_bookings WHERE idempotency_key=$1", idempotency_key
    )
    if existing:
        log.info("idempotent_return", key=idempotency_key)
        return json.dumps({
            "success":        True,
            "booking_id":     str(existing["id"]),
            "pms_booking_id": existing["pms_booking_id"],
            "status":         existing["status"],
            "message":        "Booking already exists (idempotent return)",
        })

    adapter    = _build_adapter(tenant)
    booking_id = str(uuid.uuid4())

    result = await adapter.create_booking(
        unit_id=hold["unit_id"],
        check_in=hold["check_in"],
        check_out=hold["check_out"],
        guests=hold["guests"],
        guest_name=guest_name,
        guest_email=guest_email,
        guest_phone=guest_phone,
        idempotency_key=idempotency_key,
        notes=notes,
        hold_ref=hold_id,
    )

    if result.success:
        await pool.execute(
            "UPDATE travelos_holds SET status='consumed' WHERE id=$1", hold_id
        )
        await pool.execute(
            """
            INSERT INTO travelos_bookings
                (id, tenant_id, idempotency_key, pms_booking_id,
                 unit_id, check_in, check_out, guests,
                 guest_name, guest_email, guest_phone,
                 status, channel, notes, created_at)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,
                    'confirmed','travelos_mcp',$12,NOW())
            """,
            booking_id, tenant["id"], idempotency_key, result.pms_booking_id,
            hold["unit_id"], hold["check_in"], hold["check_out"], hold["guests"],
            guest_name, guest_email, guest_phone, notes,
        )
        log.info("booking_confirmed", booking_id=booking_id, pms_id=result.pms_booking_id)
        return json.dumps({
            "success":        True,
            "booking_id":     booking_id,
            "pms_booking_id": result.pms_booking_id,
            "unit_id":        hold["unit_id"],
            "check_in":       hold["check_in"],
            "check_out":      hold["check_out"],
            "guest_name":     guest_name,
            "status":         "confirmed",
            "channel":        "travelos_mcp",
            "message":        "Booking confirmed. No commission charged to OTA.",
        })
    else:
        log.error("booking_failed", error=result.error, hold_id=hold_id)
        return json.dumps({
            "success": False,
            "error":   result.error,
            "message": "Booking failed in PMS. Hold still active — you may retry.",
        })


@mcp.tool()
async def cancel_hold(hold_id: str, ctx: Context = None) -> str:
    """
    Release an active hold without booking. Call if guest decides not to proceed.

    Args:
        hold_id: Hold ID from create_hold
    """
    tenant = await _auth(ctx)
    pool   = await _get_pool()
    result = await pool.execute(
        """
        UPDATE travelos_holds SET status='cancelled'
        WHERE id=$1 AND tenant_id=$2 AND status='active'
        """,
        hold_id, tenant["id"],
    )
    released = result != "UPDATE 0"
    return json.dumps({
        "success": released,
        "hold_id": hold_id,
        "message": "Hold released." if released else "Hold not found or already expired.",
    })


@mcp.tool()
async def get_booking_details(booking_id: str, ctx: Context = None) -> str:
    """
    Retrieve full details of a Vayancy booking.

    Args:
        booking_id: Vayancy booking ID returned by create_booking
    """
    tenant = await _auth(ctx)
    pool   = await _get_pool()

    row = await pool.fetchrow(
        "SELECT * FROM travelos_bookings WHERE id=$1 AND tenant_id=$2",
        booking_id, tenant["id"],
    )
    if not row:
        return json.dumps({"error": f"Booking {booking_id} not found"})

    adapter = _build_adapter(tenant)
    live    = await adapter.get_booking_details(row["pms_booking_id"])

    return json.dumps({
        "booking_id":     booking_id,
        "pms_booking_id": row["pms_booking_id"],
        "status":         live.status if not live.error else row["status"],
        "unit_id":        row["unit_id"],
        "unit_name":      live.unit_name,
        "check_in":       row["check_in"],
        "check_out":      row["check_out"],
        "guests":         row["guests"],
        "guest_name":     row["guest_name"],
        "guest_email":    row["guest_email"],
        "guest_phone":    row["guest_phone"],
        "total_amount":   live.total_amount,
        "currency":       live.currency,
        "channel":        row["channel"],
        "notes":          row["notes"],
        "created_at":     row["created_at"].isoformat() if row["created_at"] else None,
    }, ensure_ascii=False, default=str)


@mcp.tool()
async def cancel_booking(
    booking_id: str, reason: str = "", ctx: Context = None
) -> str:
    """
    Cancel a confirmed booking.

    Args:
        booking_id: Vayancy booking ID from create_booking
        reason:     Cancellation reason
    """
    tenant = await _auth(ctx)
    pool   = await _get_pool()

    row = await pool.fetchrow(
        "SELECT * FROM travelos_bookings WHERE id=$1 AND tenant_id=$2",
        booking_id, tenant["id"],
    )
    if not row:
        return json.dumps({"error": f"Booking {booking_id} not found"})

    adapter = _build_adapter(tenant)
    result  = await adapter.cancel_booking(row["pms_booking_id"], reason)

    if result.get("success"):
        await pool.execute(
            "UPDATE travelos_bookings SET status='cancelled' WHERE id=$1", booking_id
        )
        log.info("booking_cancelled", booking_id=booking_id)

    return json.dumps({
        "booking_id":     booking_id,
        "pms_booking_id": row["pms_booking_id"],
        "success":        result.get("success", False),
        "status":         "cancelled" if result.get("success") else "cancel_failed",
        "error":          result.get("error"),
    })


@mcp.tool()
async def get_guest_profile(guest_email: str, ctx: Context = None) -> str:
    """
    Look up a guest's booking history on Vayancy.

    Args:
        guest_email: Guest email address
    """
    tenant = await _auth(ctx)
    pool   = await _get_pool()

    bookings = await pool.fetch(
        """
        SELECT id, pms_booking_id, unit_id, check_in, check_out,
               guests, status, created_at
        FROM   travelos_bookings
        WHERE  tenant_id=$1 AND guest_email=$2
        ORDER  BY created_at DESC LIMIT 10
        """,
        tenant["id"], guest_email,
    )

    return json.dumps({
        "guest_email":    guest_email,
        "total_bookings": len(bookings),
        "bookings": [
            {
                "booking_id":     str(b["id"]),
                "pms_booking_id": b["pms_booking_id"],
                "unit_id":        b["unit_id"],
                "check_in":       b["check_in"],
                "check_out":      b["check_out"],
                "guests":         b["guests"],
                "status":         b["status"],
                "booked_at":      b["created_at"].isoformat() if b["created_at"] else None,
            }
            for b in bookings
        ],
    }, ensure_ascii=False, default=str)


if __name__ == "__main__":
    port = int(os.getenv("MCP_HOSPITALITY_PORT", "3005"))
    mcp.run(transport="sse", host="0.0.0.0", port=port)
