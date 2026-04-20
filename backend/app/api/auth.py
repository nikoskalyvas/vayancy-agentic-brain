"""
Auth & Owner API — backend/app/api/auth.py

Endpoints:
  POST /auth/signup                    — create account, send verification email
  GET  /auth/verify                    — verify email via token
  POST /auth/login                     — returns JWT
  GET  /auth/me                        — current user info
  POST /auth/onboarding/test-webhotelier — validate WH credentials
  POST /auth/onboarding/test-whatsapp  — validate WA credentials
  POST /auth/onboarding/complete       — creates tenant, first property, returns new JWT

  GET  /owner/dashboard                — aggregated stats for owner (JWT auth)
  GET  /owner/bookings                 — recent bookings
  GET  /owner/hitl                     — pending HITL decisions
  POST /owner/hitl/{id}/approve        — approve decision
  POST /owner/hitl/{id}/reject         — reject decision
  GET  /owner/activity                 — recent agent events in plain language
  GET  /owner/integrations             — PMS + WhatsApp health status
  PATCH /owner/settings                — update agent configuration

Mount in main.py:
  from app.api.auth import router as auth_router, owner_router
  app.include_router(auth_router,  prefix="/auth")
  app.include_router(owner_router, prefix="/owner")
"""
from __future__ import annotations

import json
import secrets
import uuid
from datetime import datetime, timezone
from typing import Any

import httpx
import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from slowapi import Limiter
from slowapi.util import get_remote_address
from pydantic import BaseModel, EmailStr

from app.config import settings
from app.core.auth_utils import (
    create_jwt, get_current_owner, get_current_user,
    generate_verify_token, hash_password, verify_password,
)
from app.core.email import send_email
from app.db.session import get_pool

auth_router  = APIRouter(tags=["auth"])
owner_router = APIRouter(tags=["owner"], dependencies=[Depends(get_current_owner)])
log = structlog.get_logger()


# ── Schemas ───────────────────────────────────────────────────────────────────

class SignupRequest(BaseModel):
    name:     str
    email:    EmailStr
    password: str


class LoginRequest(BaseModel):
    email:    EmailStr
    password: str


class TestWebHotelierRequest(BaseModel):
    api_key:     str
    property_id: str
    api_base:    str = "https://api.webhotelier.net/v2"


class TestWhatsAppRequest(BaseModel):
    access_token:    str
    phone_number_id: str


class OnboardingCompleteRequest(BaseModel):
    # WebHotelier
    wh_api_key:     str
    wh_property_id: str
    wh_api_base:    str = "https://api.webhotelier.net/v2"
    # WhatsApp
    wa_access_token:    str
    wa_phone_number_id: str
    # First property
    property_name:     str
    property_location: str
    max_guests:        int
    bedrooms:          int   = 1
    bathrooms:         int   = 1
    base_rate:         float | None = None
    amenities:         list[str]    = []
    # Commission
    commission_pct:    float = 5.0


class SettingsUpdateRequest(BaseModel):
    hitl_threshold_pct:   float | None = None
    pricing_floor_eur:    float | None = None
    pricing_ceiling_eur:  float | None = None
    owner_whatsapp_phone: str | None   = None


class HITLNoteRequest(BaseModel):
    note: str = ""


# ── Verification email helper ─────────────────────────────────────────────────

async def _send_verify_email(email: str, name: str, token: str) -> None:
    base = settings.travelos_success_url.replace("/booking/success", "")
    verify_url = f"{base}/verify?token={token}"
    subject = "Verify your Vayancy account"
    html = f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="font-family:Arial,sans-serif;max-width:520px;margin:0 auto;padding:20px;color:#1a1a1a">
  <div style="background:#0a0a0a;padding:18px 20px;border-radius:8px 8px 0 0">
    <h1 style="color:#c8a96e;margin:0;font-size:20px">Vayancy</h1>
  </div>
  <div style="border:1px solid #e5e5e5;border-top:none;padding:24px;border-radius:0 0 8px 8px">
    <h2 style="margin:0 0 12px">Hi {name},</h2>
    <p>Click the button below to verify your email address and start setting up your property.</p>
    <a href="{verify_url}" style="display:inline-block;background:#c8a96e;color:#0a0a0a;text-decoration:none;padding:12px 24px;border-radius:6px;font-weight:bold;margin:16px 0">
      Verify my email
    </a>
    <p style="font-size:13px;color:#666;margin-top:16px">
      Or copy this link:<br>
      <span style="font-family:monospace;font-size:12px">{verify_url}</span>
    </p>
    <p style="font-size:12px;color:#999;margin-top:20px">
      This link expires in 24 hours. If you didn't sign up, ignore this email.
    </p>
  </div>
</body>
</html>"""
    await send_email(email, subject, html, tag="verification")


# ── Auth endpoints ────────────────────────────────────────────────────────────

@auth_router.post("/signup")
async def signup(request: Request, body: SignupRequest) -> dict:
    """Create a new owner account. Sends verification email."""
    pool = await get_pool()

    # Check duplicate
    existing = await pool.fetchrow(
        "SELECT id FROM owner_users WHERE email = $1", body.email
    )
    if existing:
        raise HTTPException(status_code=409, detail="An account with this email already exists")

    # Validate password
    if len(body.password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters")

    user_id      = str(uuid.uuid4())
    password_hash = hash_password(body.password)
    verify_token  = generate_verify_token()

    await pool.execute(
        """
        INSERT INTO owner_users
            (id, name, email, password_hash, verified, verify_token,
             onboarding_complete, created_at)
        VALUES ($1,$2,$3,$4,FALSE,$5,FALSE,NOW())
        """,
        user_id, body.name.strip(), body.email.lower(),
        password_hash, verify_token,
    )

    # Send verification email (no-op if Postmark not configured)
    try:
        await _send_verify_email(body.email, body.name, verify_token)
    except Exception as e:
        log.warning("verify_email_failed", email=body.email, error=str(e))

    log.info("owner_signup", user_id=user_id, email=body.email)
    return {
        "message": "Account created. Check your email to verify.",
        "email":   body.email,
    }


@auth_router.get("/verify")
async def verify_email(token: str) -> dict:
    """Verify email address from link. Returns JWT."""
    pool = await get_pool()
    user = await pool.fetchrow(
        "SELECT * FROM owner_users WHERE verify_token = $1", token
    )
    if not user:
        raise HTTPException(status_code=400, detail="Invalid or expired verification link")
    if user["verified"]:
        raise HTTPException(status_code=400, detail="Email already verified")

    await pool.execute(
        "UPDATE owner_users SET verified=TRUE, verify_token=NULL WHERE id=$1",
        user["id"],
    )
    log.info("owner_verified", user_id=str(user["id"]))

    jwt_token = create_jwt(
        user_id=str(user["id"]),
        email=user["email"],
        name=user["name"],
        tenant_id=None,
        tenant_api_key=None,
    )
    return {
        "message":             "Email verified",
        "access_token":        jwt_token,
        "onboarding_complete": user["onboarding_complete"],
    }


@auth_router.post("/login")
async def login(request: Request, body: LoginRequest) -> dict:
    """Login with email + password. Returns JWT."""
    pool = await get_pool()
    user = await pool.fetchrow(
        "SELECT * FROM owner_users WHERE email = $1", body.email.lower()
    )
    if not user or not verify_password(body.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Incorrect email or password")
    if not user["verified"]:
        raise HTTPException(
            status_code=403,
            detail="Please verify your email first. Check your inbox.",
        )

    # Get tenant API key if onboarded
    tenant_api_key = None
    if user["tenant_id"]:
        row = await pool.fetchrow(
            "SELECT api_key FROM travelos_tenants WHERE id=$1", user["tenant_id"]
        )
        if row:
            tenant_api_key = row["api_key"]

    jwt_token = create_jwt(
        user_id=str(user["id"]),
        email=user["email"],
        name=user["name"],
        tenant_id=str(user["tenant_id"]) if user["tenant_id"] else None,
        tenant_api_key=tenant_api_key,
    )
    log.info("owner_login", user_id=str(user["id"]))
    return {
        "access_token":        jwt_token,
        "name":                user["name"],
        "email":               user["email"],
        "onboarding_complete": user["onboarding_complete"],
    }


@auth_router.get("/me")
async def me(user: dict = Depends(get_current_user)) -> dict:
    """Return current user info from JWT."""
    return {
        "user_id":             user["sub"],
        "email":               user["email"],
        "name":                user["name"],
        "tenant_id":           user.get("tenant_id"),
        "onboarding_complete": bool(user.get("tenant_id")),
    }


# ── Onboarding ────────────────────────────────────────────────────────────────

# ── Enrichment schemas ────────────────────────────────────────────────────────

class EnrichFromWebsiteRequest(BaseModel):
    website_url:    str
    wh_api_key:     str | None = None
    wh_property_id: str | None = None


class EnrichFromBDCRequest(BaseModel):
    bdc_url: str


class EnrichFromPhotosRequest(BaseModel):
    photos_b64: list[str]  # base64-encoded images, max 4

@auth_router.post("/onboarding/test-webhotelier")
async def test_webhotelier(
    body: TestWebHotelierRequest,
    user: dict = Depends(get_current_user),
) -> dict:
    """Test WebHotelier credentials by making a real API call."""
    from datetime import date, timedelta
    today = date.today()
    tomorrow = today + timedelta(days=1)

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                f"{body.api_base.rstrip('/')}/availability",
                headers={
                    "Authorization": f"Bearer {body.api_key}",
                    "Content-Type":  "application/json",
                    "X-Property-Id": body.property_id,
                },
                params={
                    "from":  str(today),
                    "to":    str(tomorrow),
                    "property_id": body.property_id,
                },
            )
        if r.status_code == 401:
            return {"success": False, "error": "Invalid API key"}
        if r.status_code == 404:
            return {"success": False, "error": "Property ID not found"}
        if r.status_code >= 500:
            return {"success": False, "error": "WebHotelier server error — try again"}
        return {"success": True, "message": "Connection successful"}
    except httpx.TimeoutException:
        return {"success": False, "error": "Connection timed out"}
    except Exception as e:
        return {"success": False, "error": str(e)}


@auth_router.post("/onboarding/test-whatsapp")
async def test_whatsapp(
    body: TestWhatsAppRequest,
    user: dict = Depends(get_current_user),
) -> dict:
    """Test WhatsApp Business credentials."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                f"https://graph.facebook.com/v20.0/{body.phone_number_id}",
                headers={"Authorization": f"Bearer {body.access_token}"},
                params={"fields": "display_phone_number,verified_name"},
            )
        if r.status_code == 401:
            return {"success": False, "error": "Invalid access token"}
        if r.status_code == 404:
            return {"success": False, "error": "Phone number ID not found"}
        data = r.json()
        if "error" in data:
            return {"success": False, "error": data["error"].get("message", "Unknown error")}
        return {
            "success":      True,
            "phone_number": data.get("display_phone_number", ""),
            "name":         data.get("verified_name", ""),
            "message":      "WhatsApp Business account connected",
        }
    except httpx.TimeoutException:
        return {"success": False, "error": "Connection timed out"}
    except Exception as e:
        return {"success": False, "error": str(e)}


@auth_router.post("/onboarding/enrich")
async def enrich_property(
    body: EnrichFromWebsiteRequest,
    user: dict = Depends(get_current_user),
) -> dict:
    """
    Step 1 of property auto-population pipeline.
    Given a website URL (+ optional WH credentials), runs:
      website → schema.org + OG tags
      Google Places → structured address + location
      WebHotelier API → full property data (if credentials provided)

    Returns pre-filled PropertyData with completeness score.
    Frontend shows a confirmation screen — owner edits anything wrong.
    """
    from app.core.property_enrichment import run_enrichment_pipeline
    result = await run_enrichment_pipeline(
        website_url=body.website_url,
        wh_api_key=body.wh_api_key,
        wh_property_id=body.wh_property_id,
    )
    return result


@auth_router.post("/onboarding/enrich-bdc")
async def enrich_from_bdc(
    body: EnrichFromBDCRequest,
    user: dict = Depends(get_current_user),
) -> dict:
    """
    Step 2 — fill gaps from Booking.com public listing.
    Called if the owner pastes their BDC listing URL.
    Only fills fields still empty after the website scrape.
    """
    from app.core.property_enrichment import enrich_from_bdc as _bdc
    data = await _bdc(body.bdc_url)
    return data.to_dict()


@auth_router.post("/onboarding/enrich-photos")
async def enrich_from_photos(
    body: EnrichFromPhotosRequest,
    user: dict = Depends(get_current_user),
) -> dict:
    """
    Step 3 (optional) — Claude vision analyses uploaded photos.
    Identifies bedrooms, bathrooms, amenities from visual inspection.
    Returns amenity list and room counts to fill any remaining gaps.
    Max 4 photos, base64 encoded.
    """
    from app.core.property_enrichment import enrich_from_photos as _vision
    data = await _vision(body.photos_b64)
    return data.to_dict()


@auth_router.post("/onboarding/complete")
async def onboarding_complete(
    body: OnboardingCompleteRequest,
    user: dict = Depends(get_current_user),
) -> dict:
    """
    Create tenant + first property. Link to owner account. Return new JWT.
    Called after both connection tests pass and property details are filled.
    """
    pool = await get_pool()
    user_id = user["sub"]

    # Check not already onboarded
    existing = await pool.fetchrow(
        "SELECT tenant_id FROM owner_users WHERE id=$1", user_id
    )
    if existing and existing["tenant_id"]:
        # Already onboarded — refresh JWT
        row = await pool.fetchrow(
            "SELECT api_key FROM travelos_tenants WHERE id=$1", existing["tenant_id"]
        )
        jwt_token = create_jwt(
            user_id=user_id,
            email=user["email"],
            name=user["name"],
            tenant_id=str(existing["tenant_id"]),
            tenant_api_key=row["api_key"] if row else None,
        )
        return {
            "message":      "Already onboarded",
            "access_token": jwt_token,
        }

    # Create tenant
    api_key    = secrets.token_urlsafe(32)
    tenant_id  = str(uuid.uuid4())
    pms_config = json.dumps({
        "api_key":     body.wh_api_key,
        "property_id": body.wh_property_id,
        "api_base":    body.wh_api_base,
    })
    owner_config = json.dumps({
        "wa_access_token":    body.wa_access_token,
        "wa_phone_number_id": body.wa_phone_number_id,
        "hitl_threshold_pct":  25.0,
        "pricing_floor_eur":   150.0,
        "pricing_ceiling_eur": 2500.0,
    })

    await pool.execute(
        """
        INSERT INTO travelos_tenants
            (id, name, api_key, pms_type, pms_config, active,
             payment_required, commission_pct, owner_config)
        VALUES ($1,$2,$3,'webhotelier',$4,TRUE,FALSE,$5,$6)
        """,
        tenant_id,
        user["name"],
        api_key,
        pms_config,
        body.commission_pct,
        owner_config,
    )

    # Create first property
    prop_id  = str(uuid.uuid4())
    raw_slug = f"{body.property_name}-{body.property_location}".lower()
    slug     = "".join(c if c.isalnum() else "-" for c in raw_slug).strip("-")
    # Ensure slug uniqueness
    count = await pool.fetchval(
        "SELECT COUNT(*) FROM travelos_properties WHERE slug LIKE $1",
        f"{slug}%"
    )
    if count and count > 0:
        slug = f"{slug}-{count}"

    await pool.execute(
        """
        INSERT INTO travelos_properties
            (id, tenant_id, pms_unit_id, name, slug, location,
             max_guests, bedrooms, bathrooms, amenities, base_rate, active)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,TRUE)
        """,
        prop_id, tenant_id, body.wh_property_id,
        body.property_name, slug, body.property_location,
        body.max_guests, body.bedrooms, body.bathrooms,
        body.amenities, body.base_rate,
    )

    # Link user to tenant
    await pool.execute(
        """
        UPDATE owner_users
        SET tenant_id=$2, onboarding_complete=TRUE
        WHERE id=$1
        """,
        user_id, tenant_id,
    )

    jwt_token = create_jwt(
        user_id=user_id,
        email=user["email"],
        name=user["name"],
        tenant_id=tenant_id,
        tenant_api_key=api_key,
    )

    log.info("onboarding_complete", user_id=user_id, tenant_id=tenant_id)
    return {
        "message":        "Setup complete. Your property is live.",
        "access_token":   jwt_token,
        "tenant_id":      tenant_id,
    }


# ── Owner endpoints (require JWT + onboarding complete) ───────────────────────

def _translate_action(action: str) -> str:
    """Convert internal agent action strings to owner-friendly language."""
    m = {
        "tool:send_text_message":     "Guest message sent via WhatsApp",
        "tool:send_template_message": "Welcome message sent to guest",
        "tool:get_reservation":       "Reservation details checked",
        "tool:push_rate_overrides":   "Rates updated",
        "tool:update_base_price":     "Base price updated",
        "tool:create_folio":          "Greek tax invoice created",
        "tool:issue_invoice":         "Invoice issued",
        "tool:get_upcoming_checkouts":"Checkout schedule checked",
        "tool:get_availability":      "Availability checked",
        "tool:get_current_rates":     "Current rates checked",
        "tool:get_occupancy_stats":   "Occupancy data analysed",
        "tool:get_market_data":       "Market benchmark data fetched",
        "routing_decision":           "Booking event processed",
        "workflow_status":            "Workflow completed",
        "error":                      "Action failed — logged for review",
    }
    for key, label in m.items():
        if key in action:
            return label
    return action.replace("tool:", "").replace("_", " ").capitalize()


@owner_router.get("/dashboard")
async def owner_dashboard(user: dict = Depends(get_current_owner)) -> dict:
    """Aggregated dashboard data for the owner."""
    pool       = await get_pool()
    tenant_id  = user["tenant_id"]

    # Stats
    stats = await pool.fetchrow(
        """
        SELECT
            COUNT(*) FILTER (WHERE status='confirmed') AS confirmed,
            COUNT(*) FILTER (WHERE status='confirmed'
                AND created_at > NOW()-INTERVAL '30 days') AS this_month,
            COUNT(DISTINCT guest_email) AS unique_guests,
            COUNT(*) FILTER (WHERE status='confirmed'
                AND created_at > NOW()-INTERVAL '30 days'
                AND channel='travelos_mcp') AS direct_bookings
        FROM travelos_bookings WHERE tenant_id=$1
        """,
        tenant_id,
    )

    # Recent bookings
    bookings = await pool.fetch(
        """
        SELECT b.id, b.guest_name, b.guest_email, b.check_in, b.check_out,
               b.guests, b.status, b.channel, b.created_at,
               p.name AS property_name
        FROM   travelos_bookings b
        LEFT   JOIN travelos_properties p
               ON p.pms_unit_id=b.unit_id AND p.tenant_id=b.tenant_id
        WHERE  b.tenant_id=$1 AND b.status='confirmed'
        ORDER  BY b.created_at DESC LIMIT 5
        """,
        tenant_id,
    )

    # Pending HITL
    # Get properties for this tenant to scope HITL
    props = await pool.fetch(
        "SELECT property_id FROM registered_properties rp "
        "JOIN travelos_properties tp ON tp.pms_unit_id=rp.pms_property_id "
        "WHERE tp.tenant_id=$1",
        tenant_id,
    )
    # Fallback: use property_id from first property's slug
    first_prop = await pool.fetchrow(
        "SELECT slug FROM travelos_properties WHERE tenant_id=$1 LIMIT 1",
        tenant_id,
    )
    prop_id_filter = first_prop["slug"] if first_prop else settings.property_id

    hitl = await pool.fetch(
        """
        SELECT id, action_type, impact_summary, proposed_action,
               created_at, status
        FROM hitl_decisions
        WHERE property_id=$1 AND status='pending'
        ORDER BY created_at DESC LIMIT 5
        """,
        prop_id_filter,
    )

    # Recent agent activity (translated)
    activity = await pool.fetch(
        """
        SELECT agent, action, status, details, timestamp
        FROM workflow_logs
        WHERE property_id=$1
        ORDER BY timestamp DESC LIMIT 15
        """,
        prop_id_filter,
    )

    # Commission savings vs OTA (17%)
    savings_row = await pool.fetchrow(
        """
        SELECT SUM(
            (d2-d1)::int *
            COALESCE(p.base_rate, 200) *
            (0.17 - b.commission_pct/100)
        ) AS saved
        FROM (
            SELECT b.*,
                   b.check_in::date  AS d1,
                   b.check_out::date AS d2
            FROM travelos_bookings b
            WHERE b.tenant_id=$1 AND b.status='confirmed'
        ) b
        LEFT JOIN travelos_properties p
               ON p.pms_unit_id=b.unit_id AND p.tenant_id=$1
        """,
        tenant_id,
    )

    return {
        "stats": {
            "confirmed_bookings": stats["confirmed"] or 0,
            "this_month":         stats["this_month"] or 0,
            "unique_guests":      stats["unique_guests"] or 0,
            "direct_bookings":    stats["direct_bookings"] or 0,
            "commission_saved_eur": round(float(savings_row["saved"] or 0), 2),
        },
        "recent_bookings": [
            {
                "booking_id":    str(r["id"]),
                "guest_name":    r["guest_name"],
                "property_name": r["property_name"] or "Your property",
                "check_in":      r["check_in"],
                "check_out":     r["check_out"],
                "guests":        r["guests"],
                "status":        r["status"],
                "channel":       r["channel"],
                "booked_at":     r["created_at"].isoformat() if r["created_at"] else None,
            }
            for r in bookings
        ],
        "pending_hitl": [
            {
                "id":             str(h["id"]),
                "action_type":    h["action_type"],
                "impact_summary": h["impact_summary"],
                "proposed":       json.loads(h["proposed_action"]) if h["proposed_action"] else {},
                "created_at":     h["created_at"].isoformat(),
            }
            for h in hitl
        ],
        "recent_activity": [
            {
                "agent":     a["agent"],
                "action":    _translate_action(a["action"]),
                "status":    a["status"],
                "timestamp": a["timestamp"].isoformat(),
            }
            for a in activity
        ],
    }


@owner_router.get("/bookings")
async def owner_bookings(
    user:   dict = Depends(get_current_owner),
    limit:  int  = 20,
    status: str  = "confirmed",
) -> dict:
    """All bookings for this owner's tenant."""
    pool      = await get_pool()
    tenant_id = user["tenant_id"]

    rows = await pool.fetch(
        """
        SELECT b.*, p.name AS property_name
        FROM   travelos_bookings b
        LEFT   JOIN travelos_properties p
               ON p.pms_unit_id=b.unit_id AND p.tenant_id=b.tenant_id
        WHERE  b.tenant_id=$1
          AND  ($2='all' OR b.status=$2)
        ORDER  BY b.created_at DESC LIMIT $3
        """,
        tenant_id, status, limit,
    )
    return {
        "bookings": [
            {
                "booking_id":    str(r["id"]),
                "pms_booking_id": r["pms_booking_id"],
                "property_name": r["property_name"] or r["unit_id"],
                "guest_name":    r["guest_name"],
                "guest_email":   r["guest_email"],
                "check_in":      r["check_in"],
                "check_out":     r["check_out"],
                "guests":        r["guests"],
                "status":        r["status"],
                "channel":       r["channel"],
                "created_at":    r["created_at"].isoformat() if r["created_at"] else None,
            }
            for r in rows
        ],
        "total": len(rows),
    }


@owner_router.get("/hitl")
async def owner_hitl(user: dict = Depends(get_current_owner)) -> dict:
    """Pending HITL decisions for this owner."""
    pool = await get_pool()
    first_prop = await pool.fetchrow(
        "SELECT slug FROM travelos_properties WHERE tenant_id=$1 LIMIT 1",
        user["tenant_id"],
    )
    prop_id = first_prop["slug"] if first_prop else settings.property_id

    rows = await pool.fetch(
        """
        SELECT id, agent, action_type, proposed_action, impact_summary,
               status, decision_note, created_at, decided_at
        FROM hitl_decisions
        WHERE property_id=$1
        ORDER BY created_at DESC LIMIT 50
        """,
        prop_id,
    )
    return {
        "decisions": [
            {
                "id":             str(r["id"]),
                "agent":          r["agent"],
                "action_type":    r["action_type"],
                "proposed":       json.loads(r["proposed_action"]) if r["proposed_action"] else {},
                "impact_summary": r["impact_summary"],
                "status":         r["status"],
                "decision_note":  r["decision_note"],
                "created_at":     r["created_at"].isoformat(),
                "decided_at":     r["decided_at"].isoformat() if r["decided_at"] else None,
            }
            for r in rows
        ],
        "pending": sum(1 for r in rows if r["status"] == "pending"),
    }


@owner_router.post("/hitl/{decision_id}/approve")
async def owner_approve_hitl(
    decision_id: str,
    body:        HITLNoteRequest,
    user:        dict = Depends(get_current_owner),
) -> dict:
    """Approve a HITL decision."""
    pool = await get_pool()
    row  = await pool.fetchrow(
        "SELECT * FROM hitl_decisions WHERE id=$1 AND status='pending'",
        uuid.UUID(decision_id),
    )
    if not row:
        raise HTTPException(status_code=404, detail="Decision not found or already resolved")

    await pool.execute(
        "UPDATE hitl_decisions SET status='approved', decision_note=$2, decided_at=NOW() WHERE id=$1",
        uuid.UUID(decision_id), body.note or "Approved by owner",
    )
    log.info("owner_hitl_approved", decision_id=decision_id, user=user["email"])
    return {"status": "approved", "decision_id": decision_id}


@owner_router.post("/hitl/{decision_id}/reject")
async def owner_reject_hitl(
    decision_id: str,
    body:        HITLNoteRequest,
    user:        dict = Depends(get_current_owner),
) -> dict:
    """Reject a HITL decision."""
    pool = await get_pool()
    row  = await pool.fetchrow(
        "SELECT * FROM hitl_decisions WHERE id=$1 AND status='pending'",
        uuid.UUID(decision_id),
    )
    if not row:
        raise HTTPException(status_code=404, detail="Decision not found or already resolved")

    await pool.execute(
        "UPDATE hitl_decisions SET status='rejected', decision_note=$2, decided_at=NOW() WHERE id=$1",
        uuid.UUID(decision_id), body.note or "Rejected by owner",
    )
    return {"status": "rejected", "decision_id": decision_id}


@owner_router.get("/activity")
async def owner_activity(
    user:  dict = Depends(get_current_owner),
    limit: int  = 30,
) -> dict:
    """Recent agent activity in plain English."""
    pool = await get_pool()
    first_prop = await pool.fetchrow(
        "SELECT slug FROM travelos_properties WHERE tenant_id=$1 LIMIT 1",
        user["tenant_id"],
    )
    prop_id = first_prop["slug"] if first_prop else settings.property_id

    rows = await pool.fetch(
        """
        SELECT agent, action, status, details, timestamp
        FROM workflow_logs
        WHERE property_id=$1
        ORDER BY timestamp DESC LIMIT $2
        """,
        prop_id, limit,
    )
    return {
        "events": [
            {
                "agent":     r["agent"],
                "action":    _translate_action(r["action"]),
                "status":    r["status"],
                "timestamp": r["timestamp"].isoformat(),
            }
            for r in rows
        ]
    }


@owner_router.get("/integrations")
async def owner_integrations(user: dict = Depends(get_current_owner)) -> dict:
    """Return integration health status for this owner."""
    pool = await get_pool()
    tenant = await pool.fetchrow(
        "SELECT * FROM travelos_tenants WHERE id=$1", user["tenant_id"]
    )
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")

    pms_config   = json.loads(tenant["pms_config"]) if tenant["pms_config"] else {}
    owner_config = json.loads(tenant["owner_config"]) if tenant.get("owner_config") else {}

    # Quick WebHotelier health ping
    wh_ok = False
    try:
        from datetime import date, timedelta
        today    = date.today()
        tomorrow = today + timedelta(days=1)
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get(
                f"{pms_config.get('api_base','https://api.webhotelier.net/v2')}/availability",
                headers={
                    "Authorization": f"Bearer {pms_config.get('api_key','')}",
                    "X-Property-Id": pms_config.get("property_id", ""),
                },
                params={"from": str(today), "to": str(tomorrow)},
            )
            wh_ok = r.status_code < 500
    except Exception:
        pass

    # WhatsApp health ping
    wa_ok = False
    wa_phone = ""
    try:
        wa_token = owner_config.get("wa_access_token", "")
        wa_pid   = owner_config.get("wa_phone_number_id", "")
        if wa_token and wa_pid:
            async with httpx.AsyncClient(timeout=5) as client:
                r = await client.get(
                    f"https://graph.facebook.com/v20.0/{wa_pid}",
                    headers={"Authorization": f"Bearer {wa_token}"},
                    params={"fields": "display_phone_number"},
                )
                if r.status_code == 200:
                    wa_ok    = True
                    wa_phone = r.json().get("display_phone_number", "")
    except Exception:
        pass

    return {
        "webhotelier": {
            "connected":   wh_ok,
            "property_id": pms_config.get("property_id", ""),
            "status":      "connected" if wh_ok else "error",
        },
        "whatsapp": {
            "connected":    wa_ok,
            "phone_number": wa_phone,
            "status":       "connected" if wa_ok else "error",
        },
        "ai_agents": {
            "connected": True,
            "model":     settings.anthropic_model,
            "status":    "active",
        },
        "voyage_ai": {
            "connected": bool(settings.voyage_api_key and settings.voyage_api_key != "placeholder-use-real-key-for-embeddings"),
            "status":    "active" if settings.voyage_api_key else "not configured",
        },
    }


@owner_router.patch("/settings")
async def update_settings(
    body: SettingsUpdateRequest,
    user: dict = Depends(get_current_owner),
) -> dict:
    """Update owner/agent configuration."""
    pool   = await get_pool()
    tenant = await pool.fetchrow(
        "SELECT owner_config FROM travelos_tenants WHERE id=$1", user["tenant_id"]
    )
    config = json.loads(tenant["owner_config"]) if tenant and tenant["owner_config"] else {}

    if body.hitl_threshold_pct is not None:
        config["hitl_threshold_pct"] = body.hitl_threshold_pct
    if body.pricing_floor_eur is not None:
        config["pricing_floor_eur"] = body.pricing_floor_eur
    if body.pricing_ceiling_eur is not None:
        config["pricing_ceiling_eur"] = body.pricing_ceiling_eur
    if body.owner_whatsapp_phone is not None:
        config["owner_whatsapp_phone"] = body.owner_whatsapp_phone

    await pool.execute(
        "UPDATE travelos_tenants SET owner_config=$2 WHERE id=$1",
        user["tenant_id"], json.dumps(config),
    )
    return {"message": "Settings updated", "config": config}
