"""
Security utilities.

FIX (issue 8): The original aidefence_guard blocked words like "system" and "dan"
which are legitimate in booking payloads (sound system, guest named Dan).
The guard now targets only actual prompt-injection patterns — imperative commands
directed at an AI, not nouns that happen to appear in the text.
"""
from __future__ import annotations
import re
import hmac
import hashlib
import structlog

log = structlog.get_logger()

# Only match imperative injection commands, not nouns
_INJECTION_PATTERNS = [
    r"ignore\s+(all\s+)?(previous|prior|above)\s+instructions",
    r"disregard\s+(all\s+)?(previous|prior|above)\s+instructions",
    r"forget\s+(all\s+)?(previous|prior|above)\s+instructions",
    r"you\s+are\s+now\s+(a\s+)?(?:DAN|jailbreak)",
    r"pretend\s+you\s+have\s+no\s+(rules|restrictions|guidelines)",
    r"act\s+as\s+if\s+you\s+(have\s+no|are\s+not)",
]

_COMPILED = [re.compile(p, re.IGNORECASE) for p in _INJECTION_PATTERNS]


async def aidefence_guard(input_text: str) -> bool:
    """
    Returns True (safe to proceed) or False (injection detected).
    Only fires on actual prompt injection patterns, not on common words.
    """
    for pattern in _COMPILED:
        if pattern.search(input_text):
            log.warning("injection_attempt_detected", snippet=input_text[:120])
            return False
    return True


def verify_webhotelier_signature(body: bytes, signature_header: str, secret: str) -> bool:
    """
    Verify WebHotelier webhook HMAC-SHA256 signature.
    FIX (issue 4): this is now actually called in the webhook handler.
    signature_header format: "sha256=<hex_digest>"
    """
    if not signature_header.startswith("sha256="):
        return False
    expected = hmac.new(
        secret.encode(),
        body,
        hashlib.sha256,
    ).hexdigest()
    received = signature_header[len("sha256="):]
    return hmac.compare_digest(expected, received)


def verify_whatsapp_token(token: str, expected: str) -> bool:
    """Verify WhatsApp webhook verification challenge token."""
    return hmac.compare_digest(token, expected)
