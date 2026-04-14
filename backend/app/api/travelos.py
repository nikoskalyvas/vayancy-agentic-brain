"""
TravelOS Admin API — backend/app/api/travelos.py

Endpoints used by owners.vayancy.gr to:
  - Register as a TravelOS tenant (connect their PMS)
  - Manage their property listings
  - View TravelOS bookings and commission savings
  - Rotate API keys

Auth: Bearer token from settings.secret_key for admin endpoints.
      Tenant API key in X-Tenant-Key header for owner self-service endpoints.

Mount in main.py:
  from app.api.travelos import router as travelos_router
  app.include_router(travelos_router, prefix="/travelos")
"""
from __future__ import annotations

import json
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, EmailStr

from app.config import settings
from app.db.session import get_pool

router  = APIRouter(tags=["travelos"])
_bearer = HTTPBearer(auto_error=False)


# ── Auth helpers ──────────────────────────────────────────────────────────────

def _require_admin(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> None:
    if not creds or creds.credentials != settings.secret_key:
        raise HTTPException(status_code=401, detail="Admin auth required")


async def _resolve_tenant_key(request: Request) -> dict:
    key  = request.headers.get("X-Tenant-Key") or request.headers.get("x-tenant-key")
    if not key:
        raise HTTPException(status_code=401, detail="Missing X-Tenant-Key header")
    pool = request.app.state.db_pool if hasattr(request.app.state, "db_pool") else await get_pool()
    row  = await pool.fetchrow(
        "SELECT * FROM travelos_tenants WHERE api_key=$1 AND active=TRUE", key
    )
    if not row:
        raise HTTPException(status_code=401, detail="Invalid or inactive tenant key")
    return dict(row)


# ── Pydantic schemas ──────────────────────────────────────────────────────────

class RegisterTenantRequest(BaseModel):
    name:              str            # "Villa Azure - Nikos Papadopoulos"
    pms_type:          str = "webhotelier"
    pms_api_key:       str
    pms_property_id:   str
    pms_api_base:      str = "https://api.webhotelier.net/v2"
    # Payment model
    # payment_required=False → default flow: AI books directly, owner invoiced monthly
    # payment_required=True  → exception flow: Stripe checkout between hold and booking
    payment_required:  bool = False
    stripe_account_id: str | None = None   # owner's Stripe Connect account ID
    commission_pct:    float = 5.0         # override global default per owner


class RegisterPropertyRequest(BaseModel):
    pms_unit_id:  str
    name:         str
    description:  str | None = None
    location:     str             # "Mykonos"
    region:       str | None = None
    address:      str | None = None
    latitude:     float | None = None
    longitude:    float | None = None
    max_guests:   int
    bedrooms:     int = 1
    bathrooms:    int = 1
    amenities:    list[str] = []  # ["pool", "sea_view", "ac", "wifi"]
    base_rate:    float | None = None
    currency:     str = "EUR"
    min_stay:     int = 1
    photos:       list[dict] = [] # [{"url": "...", "caption": "..."}]


class UpdatePropertyRequest(BaseModel):
    name:        str | None = None
    description: str | None = None
    location:    str | None = None
    region:      str | None = None
    max_guests:  int | None = None
    amenities:   list[str] | None = None
    base_rate:   float | None = None
    min_stay:    int | None = None
    active:      bool | None = None


# ── Tenant management (admin only) ────────────────────────────────────────────

@router.post("/admin/tenants", dependencies=[Depends(_require_admin)])
async def register_tenant(body: RegisterTenantRequest, request: Request) -> dict:
    """
    Register a new property owner as a TravelOS tenant.
    Called by the Vayancy onboarding flow when an owner connects their PMS.
    """
    pool    = await get_pool()
    api_key = secrets.token_urlsafe(32)

    pms_config = {
        "api_key":     body.pms_api_key,
        "property_id": body.pms_property_id,
        "api_base":    body.pms_api_base,
    }

    try:
        row = await pool.fetchrow(
            """
            INSERT INTO travelos_tenants
                (name, api_key, pms_type, pms_config, active,
                 payment_required, stripe_account_id, commission_pct)
            VALUES ($1, $2, $3, $4, TRUE, $5, $6, $7)
            RETURNING id, name, created_at
            """,
            body.name,
            api_key,
            body.pms_type,
            json.dumps(pms_config),
            body.payment_required,
            body.stripe_account_id,
            body.commission_pct,
        )
    except asyncpg.UniqueViolationError:
        raise HTTPException(status_code=409, detail="Tenant already exists")

    return {
        "tenant_id":        str(row["id"]),
        "name":             row["name"],
        "api_key":          api_key,
        "payment_required": body.payment_required,
        "commission_pct":   body.commission_pct,
        "payment_mode":     "stripe_checkout" if body.payment_required else "invoice_monthly",
        "created_at":       row["created_at"].isoformat(),
        "message":          "Tenant registered. Store the api_key — it will not be shown again.",
    }


@router.get("/admin/tenants", dependencies=[Depends(_require_admin)])
async def list_tenants(request: Request) -> dict:
    """List all registered tenants (without API keys)."""
    pool = await get_pool()
    rows = await pool.fetch(
        """
        SELECT t.id, t.name, t.pms_type, t.active, t.created_at,
               COUNT(p.id) AS property_count,
               COUNT(b.id) AS booking_count
        FROM   travelos_tenants   t
        LEFT   JOIN travelos_properties p ON p.tenant_id = t.id AND p.active = TRUE
        LEFT   JOIN travelos_bookings   b ON b.tenant_id = t.id AND b.status = 'confirmed'
        GROUP  BY t.id
        ORDER  BY t.created_at DESC
        """
    )
    return {
        "tenants": [
            {
                "id":             str(r["id"]),
                "name":           r["name"],
                "pms_type":       r["pms_type"],
                "active":         r["active"],
                "property_count": r["property_count"],
                "booking_count":  r["booking_count"],
                "created_at":     r["created_at"].isoformat(),
            }
            for r in rows
        ],
        "total": len(rows),
    }


@router.post("/admin/tenants/{tenant_id}/deactivate", dependencies=[Depends(_require_admin)])
async def deactivate_tenant(tenant_id: str, request: Request) -> dict:
    """Deactivate a tenant (removes them from search, blocks new bookings)."""
    pool = await get_pool()
    await pool.execute(
        "UPDATE travelos_tenants SET active=FALSE WHERE id=$1", uuid.UUID(tenant_id)
    )
    return {"tenant_id": tenant_id, "status": "deactivated"}


# ── Key rotation (owner self-service) ─────────────────────────────────────────

@router.post("/me/rotate-key")
async def rotate_api_key(request: Request) -> dict:
    """
    Rotate the tenant's TravelOS API key.
    Old key is immediately invalidated.
    """
    tenant  = await _resolve_tenant_key(request)
    pool    = await get_pool()
    new_key = secrets.token_urlsafe(32)

    await pool.execute(
        "UPDATE travelos_tenants SET api_key=$1 WHERE id=$2",
        new_key, tenant["id"],
    )

    return {
        "new_api_key": new_key,
        "message":     "Old key invalidated immediately. Update your configuration.",
    }


# ── Property management (owner self-service) ──────────────────────────────────

@router.post("/me/properties")
async def register_property(body: RegisterPropertyRequest, request: Request) -> dict:
    """
    Register a property in the TravelOS catalog.
    Once registered, it becomes discoverable by AI platforms via search_properties.
    """
    tenant = await _resolve_tenant_key(request)
    pool   = await get_pool()

    # Auto-generate slug from name + location
    raw_slug = f"{body.name}-{body.location}".lower()
    slug     = "".join(c if c.isalnum() else "-" for c in raw_slug).strip("-")

    row = await pool.fetchrow(
        """
        INSERT INTO travelos_properties
            (tenant_id, pms_unit_id, name, slug, description,
             location, region, address, latitude, longitude,
             max_guests, bedrooms, bathrooms, amenities,
             base_rate, currency, min_stay, photos, active)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,
                $11,$12,$13,$14,$15,$16,$17,$18,TRUE)
        ON CONFLICT (slug) DO UPDATE SET updated_at=NOW()
        RETURNING id, name, slug, created_at
        """,
        tenant["id"],
        body.pms_unit_id,
        body.name,
        slug,
        body.description,
        body.location,
        body.region,
        body.address,
        body.latitude,
        body.longitude,
        body.max_guests,
        body.bedrooms,
        body.bathrooms,
        body.amenities,
        body.base_rate,
        body.currency,
        body.min_stay,
        json.dumps(body.photos),
    )

    return {
        "property_id": str(row["id"]),
        "name":        row["name"],
        "slug":        row["slug"],
        "created_at":  row["created_at"].isoformat(),
        "message":     "Property is now discoverable via TravelOS AI search.",
    }


@router.get("/me/properties")
async def list_my_properties(request: Request) -> dict:
    """List all properties registered by this tenant."""
    tenant = await _resolve_tenant_key(request)
    pool   = await get_pool()

    rows = await pool.fetch(
        """
        SELECT p.*, COUNT(b.id) AS total_bookings
        FROM   travelos_properties p
        LEFT   JOIN travelos_bookings b ON b.unit_id = p.pms_unit_id
                                      AND b.tenant_id = p.tenant_id
                                      AND b.status = 'confirmed'
        WHERE  p.tenant_id = $1
        GROUP  BY p.id
        ORDER  BY p.created_at DESC
        """,
        tenant["id"],
    )

    return {
        "properties": [
            {
                "property_id":    str(r["id"]),
                "pms_unit_id":    r["pms_unit_id"],
                "name":           r["name"],
                "location":       r["location"],
                "region":         r.get("region"),
                "max_guests":     r["max_guests"],
                "bedrooms":       r.get("bedrooms"),
                "amenities":      list(r["amenities"]) if r["amenities"] else [],
                "base_rate":      float(r["base_rate"]) if r["base_rate"] else None,
                "currency":       r["currency"],
                "active":         r["active"],
                "total_bookings": r["total_bookings"],
            }
            for r in rows
        ],
        "total": len(rows),
    }


@router.patch("/me/properties/{property_id}")
async def update_property(
    property_id: str, body: UpdatePropertyRequest, request: Request
) -> dict:
    """Update a property listing (rate, amenities, active status, etc.)."""
    tenant = await _resolve_tenant_key(request)
    pool   = await get_pool()

    updates = {
        k: v for k, v in body.model_dump(exclude_none=True).items()
    }
    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")

    set_clauses = ", ".join(f"{k}=${i+3}" for i, k in enumerate(updates))
    values      = list(updates.values())

    await pool.execute(
        f"""
        UPDATE travelos_properties
        SET    {set_clauses}, updated_at=NOW()
        WHERE  id=$1 AND tenant_id=$2
        """,
        uuid.UUID(property_id), tenant["id"], *values,
    )

    return {"property_id": property_id, "updated_fields": list(updates.keys())}


# ── Bookings dashboard (owner self-service) ───────────────────────────────────

@router.get("/me/bookings")
async def list_my_bookings(
    request: Request,
    status:  str = "confirmed",
    limit:   int = 50,
) -> dict:
    """
    List bookings made through TravelOS for this tenant.
    Shows channel, commission saved vs OTA, and PMS reference.
    """
    tenant = await _resolve_tenant_key(request)
    pool   = await get_pool()

    rows = await pool.fetch(
        """
        SELECT b.*, p.name AS property_name, p.base_rate
        FROM   travelos_bookings   b
        LEFT   JOIN travelos_properties p
            ON p.pms_unit_id = b.unit_id AND p.tenant_id = b.tenant_id
        WHERE  b.tenant_id = $1
          AND  ($2 = 'all' OR b.status = $2)
        ORDER  BY b.created_at DESC
        LIMIT  $3
        """,
        tenant["id"], status, limit,
    )

    # Calculate commission savings (vs Booking.com 17%)
    bookings_out = []
    total_saved  = 0.0

    for r in rows:
        # Rough estimate: nights × base_rate
        try:
            d1     = datetime.fromisoformat(r["check_in"])
            d2     = datetime.fromisoformat(r["check_out"])
            nights = (d2 - d1).days
            base   = float(r["base_rate"]) if r["base_rate"] else 0
            est_total   = nights * base
            ota_fee     = round(est_total * 0.17, 2)  # Booking.com ~17%
            our_fee     = round(est_total * float(r["commission_pct"]) / 100, 2)
            saved       = round(ota_fee - our_fee, 2)
            total_saved += saved
        except Exception:
            ota_fee = our_fee = saved = 0

        bookings_out.append({
            "booking_id":     str(r["id"]),
            "pms_booking_id": r["pms_booking_id"],
            "property_name":  r["property_name"] or r["unit_id"],
            "check_in":       r["check_in"],
            "check_out":      r["check_out"],
            "guests":         r["guests"],
            "guest_name":     r["guest_name"],
            "guest_email":    r["guest_email"],
            "status":         r["status"],
            "channel":        r["channel"],
            "ota_fee_avoided": ota_fee,
            "vayancy_fee":    our_fee,
            "commission_saved": saved,
            "created_at":     r["created_at"].isoformat() if r["created_at"] else None,
        })

    return {
        "bookings":           bookings_out,
        "total":              len(bookings_out),
        "total_commission_saved_eur": round(total_saved, 2),
    }


@router.get("/me/stats")
async def my_stats(request: Request) -> dict:
    """Dashboard summary stats for this tenant."""
    tenant = await _resolve_tenant_key(request)
    pool   = await get_pool()

    stats = await pool.fetchrow(
        """
        SELECT
            COUNT(*) FILTER (WHERE status='confirmed')  AS confirmed_bookings,
            COUNT(*) FILTER (WHERE status='cancelled')  AS cancelled_bookings,
            COUNT(*) FILTER (WHERE
                status='confirmed'
                AND created_at > NOW() - INTERVAL '30 days'
            ) AS bookings_last_30d,
            COUNT(DISTINCT guest_email) AS unique_guests
        FROM travelos_bookings
        WHERE tenant_id = $1
        """,
        tenant["id"],
    )

    props = await pool.fetchrow(
        "SELECT COUNT(*) AS total FROM travelos_properties WHERE tenant_id=$1 AND active=TRUE",
        tenant["id"],
    )

    return {
        "confirmed_bookings":  stats["confirmed_bookings"],
        "cancelled_bookings":  stats["cancelled_bookings"],
        "bookings_last_30d":   stats["bookings_last_30d"],
        "unique_guests":       stats["unique_guests"],
        "active_properties":   props["total"],
        "channel":             "travelos_mcp",
    }
