"""
Multi-Property Registry — backend/app/api/properties.py

Stage 5: Vayancy can now manage multiple villas from a single deployment.
Previously PROPERTY_ID was a single .env var, meaning one deploy = one villa.

This module adds:
  - A properties registry table (registered_properties)
  - Admin endpoints to register / list / deactivate properties
  - A routing helper used by webhooks.py to dispatch to the correct
    property_id based on the PMS property ID in the webhook payload

Architecture:
  WebHotelier sends webhook with property_id=12345
      ↓
  webhooks.py calls resolve_property_id("12345", "webhotelier")
      ↓
  Returns our internal property slug (e.g. "villa-azure")
      ↓
  All agents, SSE, memory scoped to that slug

Mount in main.py:
  from app.api.properties import router as properties_router
  app.include_router(properties_router, prefix="/properties")
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel

from app.config import settings
from app.db.session import get_pool

router  = APIRouter(tags=["properties"])
_bearer = HTTPBearer(auto_error=False)


def _require_admin(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> None:
    if not creds or creds.credentials != settings.secret_key:
        raise HTTPException(status_code=401, detail="Admin auth required")


# ── Schemas ───────────────────────────────────────────────────────────────────

class RegisterPropertyRequest(BaseModel):
    property_id:       str           # our internal slug e.g. "villa-azure"
    display_name:      str           # "Villa Azure — Mykonos"
    pms_type:          str = "webhotelier"
    pms_property_id:   str           # PMS-side ID e.g. "12345"
    owner_phone:       str = ""      # WhatsApp for escalations
    location:          str = ""
    active:            bool = True
    config:            dict = {}     # arbitrary per-property config


# ── DB helper (used by webhooks.py) ──────────────────────────────────────────

async def resolve_property_id(
    pms_property_id: str,
    pms_type: str = "webhotelier",
) -> str | None:
    """
    Resolve a PMS property ID to our internal property slug.

    Called by webhook handlers so that incoming WebHotelier events
    for property 12345 are automatically routed to "villa-azure"
    without hardcoding the mapping in .env.

    Falls back to settings.property_id if no mapping found (single-property
    compatibility — existing deployments keep working unchanged).
    """
    pool = await get_pool()
    row  = await pool.fetchrow(
        """
        SELECT property_id FROM registered_properties
        WHERE  pms_property_id = $1
          AND  pms_type        = $2
          AND  active          = TRUE
        """,
        pms_property_id, pms_type,
    )
    if row:
        return row["property_id"]

    # Fallback: single-property mode
    return settings.property_id


async def get_property_config(property_id: str) -> dict:
    """
    Return the full config dict for a property.
    Used by agents to get owner_phone, location, and other per-property settings.
    """
    pool = await get_pool()
    row  = await pool.fetchrow(
        "SELECT * FROM registered_properties WHERE property_id = $1 AND active = TRUE",
        property_id,
    )
    if not row:
        # Return defaults for single-property mode
        return {
            "property_id":     settings.property_id,
            "display_name":    settings.property_id,
            "owner_phone":     settings.owner_whatsapp_phone,
            "pms_type":        "webhotelier",
            "pms_property_id": settings.webhotelier_property_id,
            "location":        "",
            "config":          {},
        }
    return {
        "property_id":     row["property_id"],
        "display_name":    row["display_name"],
        "owner_phone":     row["owner_phone"] or settings.owner_whatsapp_phone,
        "pms_type":        row["pms_type"],
        "pms_property_id": row["pms_property_id"],
        "location":        row["location"],
        "config":          json.loads(row["config"]) if row["config"] else {},
    }


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.post("/register", dependencies=[Depends(_require_admin)])
async def register_property(body: RegisterPropertyRequest) -> dict:
    """
    Register a villa in the multi-property registry.
    Once registered, webhooks from this PMS property ID are automatically
    routed to the correct property slug.
    """
    pool = await get_pool()
    try:
        await pool.execute(
            """
            INSERT INTO registered_properties
                (property_id, display_name, pms_type, pms_property_id,
                 owner_phone, location, active, config)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
            ON CONFLICT (property_id) DO UPDATE SET
                display_name    = $2,
                pms_type        = $3,
                pms_property_id = $4,
                owner_phone     = $5,
                location        = $6,
                active          = $7,
                config          = $8,
                updated_at      = NOW()
            """,
            body.property_id,
            body.display_name,
            body.pms_type,
            body.pms_property_id,
            body.owner_phone,
            body.location,
            body.active,
            json.dumps(body.config),
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    return {
        "property_id":     body.property_id,
        "pms_property_id": body.pms_property_id,
        "status":          "registered",
        "message": (
            f"Webhooks from {body.pms_type} property {body.pms_property_id} "
            f"will now route to '{body.property_id}'."
        ),
    }


@router.get("/list", dependencies=[Depends(_require_admin)])
async def list_properties() -> dict:
    """List all registered properties."""
    pool = await get_pool()
    rows = await pool.fetch(
        "SELECT * FROM registered_properties ORDER BY created_at DESC"
    )
    return {
        "total": len(rows),
        "properties": [
            {
                "property_id":     r["property_id"],
                "display_name":    r["display_name"],
                "pms_type":        r["pms_type"],
                "pms_property_id": r["pms_property_id"],
                "location":        r["location"],
                "active":          r["active"],
                "created_at":      r["created_at"].isoformat(),
            }
            for r in rows
        ],
    }


@router.post("/{property_id}/deactivate", dependencies=[Depends(_require_admin)])
async def deactivate_property(property_id: str) -> dict:
    """Stop routing webhooks to this property."""
    pool = await get_pool()
    await pool.execute(
        "UPDATE registered_properties SET active=FALSE WHERE property_id=$1",
        property_id,
    )
    return {"property_id": property_id, "status": "deactivated"}
