"""
Escalation handler — Item 4.

When the guest agent ends its output with ESCALATE: <reason>, this module:
1. Logs the escalation to the DB
2. Sends a WhatsApp message to the property owner's number
3. Marks the escalation as notified

Owner number is configured per property in settings.
"""
from __future__ import annotations
import os
import httpx
import structlog

log = structlog.get_logger()

_TOKEN    = os.getenv("WHATSAPP_ACCESS_TOKEN", "")
_PHONE_ID = os.getenv("WHATSAPP_PHONE_NUMBER_ID", "")
_API_BASE = os.getenv("WHATSAPP_API_BASE", "https://graph.facebook.com/v20.0")
OWNER_PHONE = os.getenv("OWNER_WHATSAPP_PHONE", "")   # E.164, e.g. +306912345678


def _headers() -> dict:
    return {"Authorization": f"Bearer {_TOKEN}", "Content-Type": "application/json"}


async def notify_owner(
    reason:        str,
    guest_phone:   str | None,
    guest_message: str | None,
    workflow_id:   str,
    property_id:   str,
) -> bool:
    """
    Send a WhatsApp message to the owner with the escalation details.
    Returns True if sent successfully.
    """
    if not OWNER_PHONE or not _TOKEN or not _PHONE_ID:
        log.warning("escalation_owner_not_configured",
                    workflow_id=workflow_id)
        return False

    guest_info = f"from {guest_phone}" if guest_phone else "unknown guest"
    last_msg   = f'\n\nGuest said: "{guest_message}"' if guest_message else ""

    body = (
        f"🔔 *Vayancy — Action Required*\n\n"
        f"Property: {property_id}\n"
        f"Guest: {guest_info}\n\n"
        f"Reason: {reason}"
        f"{last_msg}\n\n"
        f"Workflow ID: {workflow_id[:8]}"
    )

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(
                f"{_API_BASE}/{_PHONE_ID}/messages",
                headers=_headers(),
                json={
                    "messaging_product": "whatsapp",
                    "to": OWNER_PHONE,
                    "type": "text",
                    "text": {"body": body},
                },
            )
            r.raise_for_status()
            log.info("owner_notified", workflow_id=workflow_id,
                     reason=reason)
            return True
    except Exception as e:
        log.error("owner_notification_failed", error=str(e),
                  workflow_id=workflow_id)
        return False
