"""
Centralised settings — replaces scattered os.getenv() calls throughout the codebase.
All env vars are validated at startup via pydantic-settings.
"""
from __future__ import annotations
from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    environment: str = "development"

    # Anthropic
    anthropic_api_key: str
    anthropic_model:   str = "claude-3-5-sonnet-20241022"

    # Database
    database_url: str = "postgresql://vayancy:vayancy@postgres:5432/agentic_brain"

    # Redis (for ARQ worker + hold expiry cron)
    redis_url: str = "redis://redis:6379/0"

    # WhatsApp
    whatsapp_token:      str = ""
    phone_number_id:     str = ""
    webhook_secret:      str = ""

    # WebHotelier
    webhotelier_api_key:       str = ""
    webhotelier_property_id:   str = ""
    webhotelier_api_base:      str = "https://api.webhotelier.net/v2"

    # Epsilon Net (Greek tax)
    epsilon_net_username:   str = ""
    epsilon_net_password:   str = ""
    epsilon_net_company_id: str = ""
    epsilon_net_api_url:    str = "https://api.epsilonnet.gr/v1"

    # PriceLabs
    pricelabs_api_key:    str = ""
    pricelabs_property_id: str = ""

    # App
    secret_key:              str = "change-me-min-32-chars-in-production"
    property_id:             str = "default"
    worker_job_timeout:      int = 300

    # TravelOS
    travelos_commission_pct: float = 5.0


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
