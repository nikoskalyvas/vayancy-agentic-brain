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

import structlog
log = structlog.get_logger()

import arq
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse

from app.config import settings
from app.core.memory import AgentMemory
from app.core.profile_resolver import resolve_guest_from_phone  # Fix 10
from app.core.security import verify_webhotelier_signature, verify_whatsapp_token, verify_whatsapp_hmac, aidefence_guard
from app.api.properties import resolve_property_id

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

    # Stage 5: multi-property routing
    # Resolve PMS property ID to our internal slug.
    # Falls back to settings.property_id for single-property deployments.
    pms_pid     = str(payload.get("property_id") or payload.get("hotel_id") or "")
    property_id = await resolve_property_id(pms_pid, "webhotelier") if pms_pid else settings.property_id

    await _redis(request).enqueue_job(
        "run_booking_workflow",
        workflow_id=workflow_id,
        event_id=event_id,
        property_id=property_id,
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

    # Fix #5: verify Meta X-Hub-Signature-256 HMAC before processing.
    # If WHATSAPP_APP_SECRET is set, reject any request that fails verification.
    # Without this, any actor who discovers the URL can POST arbitrary payloads.
    if settings.whatsapp_app_secret:
        sig = request.headers.get("X-Hub-Signature-256", "")
        if not verify_whatsapp_hmac(body, sig, settings.whatsapp_app_secret):
            log.warning("whatsapp_hmac_failed",
                        ip=request.client.host if request.client else "unknown")
            raise HTTPException(status_code=401, detail="Invalid signature")

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

                # Fix #4: guard runs on the raw message text BEFORE enqueueing.
                # Previously the guard ran in the supervisor on the serialized
                # JSON payload — an injection string inside a JSON value would
                # still be caught by the regex, but only by accident.
                # Failing here: log + skip this message, still return 202 so
                # WhatsApp does not retry. We do NOT store the interaction
                # (keeps injection attempts out of the RAG index entirely).
                if not await aidefence_guard(text_body):
                    log.warning(
                        "injection_blocked_at_webhook",
                        from_phone=from_phone,
                        message_id=message_id,
                        snippet=text_body[:80],
                    )
                    continue

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
