"""
Webhook handlers — fixes applied:

Fix 10: _bootstrap_guest_profile removed. Now uses profile_resolver.resolve_guest_from_phone()
        which is the canonical, well-abstracted implementation with nationality→language mapping.
Fix 2:  pg_notify uses per-property channel (via worker enqueue — no direct notify here).
"""
from __future__ import annotations
import json
import uuid
from datetime import datetime

import arq
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse

from app.config import settings
from app.core.memory import AgentMemory
from app.core.profile_resolver import resolve_guest_from_phone  # Fix 10
from app.core.security import verify_webhotelier_signature, verify_whatsapp_token

router = APIRouter()


def _redis(request: Request) -> arq.ArqRedis:
    return request.app.state.arq_pool


def _memory(request: Request) -> AgentMemory:
    return request.app.state.memory


# ─── WebHotelier webhook ──────────────────────────────────────────────────────

@router.post("/webhook/webhotelier", status_code=202)
async def webhotelier_webhook(request: Request) -> dict:
    body      = await request.body()
    signature = request.headers.get("X-WebHotelier-Signature", "")

    if settings.webhotelier_webhook_secret:
        if not verify_webhotelier_signature(
            body, signature, settings.webhotelier_webhook_secret
        ):
            raise HTTPException(status_code=401, detail="Invalid signature")

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    event_id    = payload.get("event_id") or str(uuid.uuid4())
    workflow_id = str(uuid.uuid4())

    await _redis(request).enqueue_job(
        "run_booking_workflow",
        workflow_id=workflow_id,
        event_id=event_id,
        property_id=settings.property_id,
        payload=payload,
    )

    return {"status": "accepted", "workflow_id": workflow_id,
            "event_id": event_id}


# ─── WhatsApp verification ────────────────────────────────────────────────────

@router.get("/webhook/whatsapp")
async def whatsapp_verify(request: Request) -> PlainTextResponse:
    params    = request.query_params
    mode      = params.get("hub.mode")
    token     = params.get("hub.verify_token", "")
    challenge = params.get("hub.challenge", "")

    if mode == "subscribe" and verify_whatsapp_token(
        token, settings.whatsapp_webhook_verify_token
    ):
        return PlainTextResponse(challenge)
    raise HTTPException(status_code=403, detail="Verification failed")


# ─── WhatsApp inbound ─────────────────────────────────────────────────────────

@router.post("/webhook/whatsapp", status_code=202)
async def whatsapp_inbound(request: Request) -> dict:
    body = await request.body()
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    mem         = _memory(request)
    property_id = settings.property_id

    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})

            for msg in value.get("messages", []):
                if msg.get("type") != "text":
                    continue

                from_phone = msg.get("from", "")
                message_id = msg.get("id", "")
                text_body  = msg.get("text", {}).get("body", "")
                timestamp  = msg.get("timestamp", "")

                # Fix 10: use canonical profile_resolver, not inline duplicate
                existing = await mem.get_guest_profile(from_phone, property_id)
                if not existing:
                    resolved = await resolve_guest_from_phone(from_phone)
                    if resolved and resolved.get("name"):
                        await mem.upsert_guest_profile(
                            phone=from_phone,
                            property_id=property_id,
                            name=resolved["name"],
                            email=resolved.get("email"),
                            language=resolved.get("language", "en"),
                            nationality=resolved.get("nationality"),
                        )

                # Store inbound interaction for RAG
                await mem.store_interaction(
                    guest_phone=from_phone,
                    property_id=property_id,
                    reservation_id=None,
                    direction="inbound",
                    content=text_body,
                )

                await _redis(request).enqueue_job(
                    "run_whatsapp_reply_workflow",
                    workflow_id=str(uuid.uuid4()),
                    event_id=message_id,
                    property_id=property_id,
                    payload={
                        "event":        "whatsapp.message",
                        "guest_phone":  from_phone,
                        "message_id":   message_id,
                        "message_body": text_body,
                        "timestamp":    timestamp,
                    },
                )

    return {"status": "accepted"}


@router.get("/health")
async def health() -> dict:
    return {"status": "ok", "timestamp": datetime.utcnow().isoformat()}
