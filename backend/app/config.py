"""
Centralised settings — all env vars validated at startup.
"""
from __future__ import annotations
from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import field_validator
from typing import Any


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    environment: str = "development"
    secret_key:  str = "change-me-min-32-chars-in-production"
    property_id: str = "default"

    @field_validator("secret_key")
    @classmethod
    def _validate_secret_key(cls, v: str, info: Any) -> str:
        env = info.data.get("environment", "development")
        if env == "production" and v == "change-me-min-32-chars-in-production":
            raise ValueError(
                "SECRET_KEY must be set to a strong random value in production. "
                "Run: python3 -c \"import secrets; print(secrets.token_hex(32))\""
            )
        if len(v) < 32:
            raise ValueError("SECRET_KEY must be at least 32 characters")
        return v

    # Anthropic
    anthropic_api_key: str
    anthropic_model:   str = "claude-opus-4-6"

    # Voyage AI (async embeddings — replaces sentence-transformers in Stage 5)
    # Get free API key at https://dash.voyageai.com
    # Set VOYAGE_API_KEY= in .env to enable. Falls back to sentence-transformers if unset.
    voyage_api_key:    str = ""
    voyage_model:      str = "voyage-3"
    voyage_dimensions: int = 1024

    # Database
    database_url: str = "postgresql://vayancy:vayancy@postgres:5432/agentic_brain"

    # Redis
    redis_url: str = "redis://redis:6379/0"

    # ARQ worker
    worker_job_timeout: int = 300

    # WebHotelier
    webhotelier_api_key:        str = ""
    webhotelier_property_id:    str = ""
    webhotelier_webhook_secret: str = ""
    webhotelier_api_base:       str = "https://api.webhotelier.net/v2"

    # WhatsApp
    whatsapp_access_token:         str = ""
    whatsapp_phone_number_id:      str = ""
    whatsapp_webhook_verify_token: str = ""
    whatsapp_app_secret:           str = ""   # Meta App Secret for X-Hub-Signature-256 HMAC
    whatsapp_api_base:             str = "https://graph.facebook.com/v20.0"
    owner_whatsapp_phone:          str = ""

    # PriceLabs
    pricelabs_api_key:     str = ""
    pricelabs_property_id: str = ""
    pricelabs_api_base:    str = "https://api.pricelabs.co/v1"

    # Epsilon Net (Greek tax)
    epsilon_net_username:   str = ""
    epsilon_net_password:   str = ""
    epsilon_net_company_id: str = ""
    epsilon_net_api_url:    str = "https://api.epsilonnet.gr/v1"

    # TravelOS
    travelos_commission_pct: float = 5.0
    travelos_success_url:    str = "https://owners.vayancy.gr/booking/success"
    travelos_cancel_url:     str = "https://owners.vayancy.gr/booking/cancel"

    # Google Places API (property enrichment during onboarding)
    # Get key at https://console.cloud.google.com → Places API
    # Free tier: 28,500 requests/month
    google_places_api_key: str = ""

    # Email (Postmark — transactional confirmation + policy receipts)
    postmark_api_key:    str = ""   # Server API token from postmarkapp.com
    email_from_address:  str = "bookings@vayancy.gr"
    email_from_name:     str = "Vayancy Bookings"
    email_reply_to:      str = "support@vayancy.gr"

    # Property enrichment
    google_places_api_key: str = ""   # Google Places API key for property lookup

    # Stripe (exception payment flow — tenants with payment_required=True)
    stripe_secret_key:      str = ""   # sk_live_... or sk_test_...
    stripe_webhook_secret:  str = ""   # whsec_... from Stripe dashboard


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
