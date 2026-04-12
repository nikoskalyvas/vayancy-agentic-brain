"""
Profile resolver — Item 2.

When a WhatsApp message arrives from an unknown phone number, this module
queries WebHotelier for a matching active reservation, extracts the guest's
name and language, and upserts the guest profile.

This runs before the guest agent so that every reply — even the first one —
is personalised with the guest's name.
"""
from __future__ import annotations
import os
import httpx
import structlog

log = structlog.get_logger()

_API_KEY  = os.getenv("WEBHOTELIER_API_KEY", "")
_PROP_ID  = os.getenv("WEBHOTELIER_PROPERTY_ID", "")
_API_BASE = os.getenv("WEBHOTELIER_API_BASE", "https://api.webhotelier.net/v2")

# ISO 639-1 codes inferred from common nationalities
_NATIONALITY_LANG: dict[str, str] = {
    "GR": "el", "DE": "de", "FR": "fr", "RU": "ru",
    "IT": "it", "ES": "es", "NL": "nl", "PL": "pl",
    "GB": "en", "US": "en", "AU": "en", "CA": "en",
}


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {_API_KEY}",
        "Content-Type":  "application/json",
        "X-Property-Id": _PROP_ID,
    }


async def resolve_guest_from_phone(phone: str) -> dict | None:
    """
    Query WebHotelier for a confirmed reservation matching this phone number.
    Returns a dict with name, email, language, nationality — or None.
    """
    if not _API_KEY:
        return None
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            r = await client.get(
                f"{_API_BASE}/reservations",
                headers=_headers(),
                params={"guest_phone": phone, "status": "confirmed",
                        "limit": 1},
            )
            r.raise_for_status()
            data = r.json()

        reservations = data.get("data") or data.get("reservations") or []
        if not reservations:
            return None

        res   = reservations[0]
        guest = res.get("guest", {})
        name  = guest.get("name") or guest.get("full_name") or ""
        email = guest.get("email", "")
        nat   = (guest.get("nationality") or "").upper()
        lang  = _NATIONALITY_LANG.get(nat, "en")

        return {
            "name":          name,
            "email":         email or None,
            "language":      lang,
            "nationality":   nat or None,
            "reservation_id": res.get("id"),
        }

    except Exception as e:
        log.warning("profile_resolver_failed", phone=phone, error=str(e))
        return None
