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

    # MCP server URLs — override for Railway deployment
    mcp_webhotelier_url: str = "http://mcp-webhotelier:3001/sse"
    mcp_whatsapp_url:    str = "http://mcp-whatsapp:3002/sse"
    mcp_pricelabs_url:   str = "http://mcp-pricelabs:3003/sse"
    mcp_epsilonnet_url:  str = "http://mcp-epsilonnet:3004/sse"

    # Google Places API (property enrichment during onboarding)
    # Get key at https://console.cloud.google.com → Places API
    # Free tier: 28,500 requests/month
    google_places_api_key: str = ""

    # Email (Postmark — transactional confirmation + policy receipts)
    postmark_api_key:    str = ""   # Server API token from postmarkapp.com
    email_from_address:  str = "bookings@vayancy.gr"
    email_from_name:     str = "Vayancy Bookings"
    email_reply_to:      str = "support@vayancy.gr"

    # Stripe (exception payment flow — tenants with payment_required=True)
    stripe_secret_key:      str = ""   # sk_live_... or sk_test_...
    stripe_webhook_secret:  str = ""   # whsec_... from Stripe dashboard

    # HostHub (villa availability & pricing for public villa search)
    hosthub_api_key:               str = ""   # Settings → API Key in HostHub dashboard
    hosthub_api_base:              str = "https://app.hosthub.com/api/2019-03-01"
    # HostHub rental IDs (same as Pricelabs listing IDs)
    hosthub_rental_adaman_nicoleta: str = ""  # Adaman Villas – Nicoleta unit
    hosthub_rental_adaman_maria:    str = ""  # Adaman Villas – Maria unit
    hosthub_rental_nidri:           str = ""  # Nidri Hills Villa
    hosthub_rental_boat:            str = ""  # Boat Villa
    # HostHub default rate-plan IDs (for pricing)
    hosthub_rate_plan_adaman_nicoleta: str = ""
    hosthub_rate_plan_adaman_maria:    str = ""
    hosthub_rate_plan_nidri:           str = ""
    hosthub_rate_plan_boat:            str = ""

    # iCal feed URLs — used by refresh_hosthub_cache cron to populate availability cache.
    # These are the HostHub/Booking.com/mphb iCal export URLs (tokens in the URL act as auth).
    # HostHub properties (Lefkada)
    ical_boat:            str = ""   # https://app.hosthub.com/rentals/422841/icalendar/...
    ical_nidri:           str = ""   # https://app.hosthub.com/rentals/372945/icalendar/...
    ical_adaman_nicoleta: str = ""   # https://app.hosthub.com/rentals/758329/icalendar/...
    ical_adaman_maria:    str = ""   # https://app.hosthub.com/rentals/971651/icalendar/...
    # Ktima Bird Paradise — two units from Booking.com
    ical_ktima_3bed:      str = ""   # https://ical.booking.com/v1/export?t=...
    ical_ktima_2bed:      str = ""   # https://ical.booking.com/v1/export?t=...
    # Garden House (Crete) — mphb.ics feed from gardenhouse.gr
    ical_garden_house:    str = "https://www.gardenhouse.gr/?feed=mphb.ics&accommodation_id=2006"


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
