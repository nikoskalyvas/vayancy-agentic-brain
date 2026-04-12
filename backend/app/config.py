"""
Centralised settings — all env vars validated at startup.
"""
from __future__ import annotations
from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    environment: str = "development"
    secret_key:  str = "change-me-min-32-chars-in-production"
    property_id: str = "default"

    # Anthropic
    anthropic_api_key: str
    anthropic_model:   str = "claude-3-5-sonnet-20241022"

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


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
