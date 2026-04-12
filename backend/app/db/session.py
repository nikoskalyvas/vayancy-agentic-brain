"""
Database session management.
Single pool, created once at startup, shared across the app.
All table creation happens here — including TravelOS tables.
"""
from __future__ import annotations
import asyncpg
from app.config import settings

_pool: asyncpg.Pool | None = None


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            settings.database_url,
            min_size=2,
            max_size=15,
            command_timeout=30,
        )
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool:
        await _pool.close()
        _pool = None


async def init_schema() -> None:
    """Create all tables. Safe to call multiple times (IF NOT EXISTS)."""
    pool = await get_pool()
    async with pool.acquire() as conn:

        await conn.execute("CREATE EXTENSION IF NOT EXISTS vector;")

        # ── Original tables ───────────────────────────────────────────────────
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS workflow_logs (
                id          TEXT PRIMARY KEY,
                timestamp   TIMESTAMPTZ NOT NULL,
                agent       TEXT NOT NULL,
                action      TEXT NOT NULL,
                status      TEXT NOT NULL,
                details     TEXT,
                workflow_id TEXT NOT NULL
            );
        """)

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS reasoning_bank (
                key       TEXT PRIMARY KEY,
                content   TEXT,
                embedding vector(384),
                tier      TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_reasoning_bank_embedding
                ON reasoning_bank USING hnsw (embedding vector_cosine_ops);
        """)

        # ── TravelOS tables ───────────────────────────────────────────────────
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS travelos_tenants (
                id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                name         TEXT NOT NULL,
                api_key      TEXT NOT NULL UNIQUE,
                pms_type     TEXT NOT NULL DEFAULT 'webhotelier',
                pms_config   JSONB NOT NULL DEFAULT '{}',
                active       BOOLEAN DEFAULT TRUE,
                created_at   TIMESTAMPTZ DEFAULT NOW()
            );
            CREATE INDEX IF NOT EXISTS idx_travelos_tenants_key
                ON travelos_tenants (api_key);
        """)

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS travelos_properties (
                id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                tenant_id   UUID NOT NULL REFERENCES travelos_tenants(id) ON DELETE CASCADE,
                pms_unit_id TEXT NOT NULL,
                name        TEXT NOT NULL,
                location    TEXT NOT NULL,
                region      TEXT,
                max_guests  INT  NOT NULL DEFAULT 2,
                bedrooms    INT  DEFAULT 1,
                bathrooms   INT  DEFAULT 1,
                amenities   TEXT[] DEFAULT '{}',
                base_rate   NUMERIC(10,2),
                currency    TEXT DEFAULT 'EUR',
                min_stay    INT  DEFAULT 1,
                description TEXT,
                photos      JSONB DEFAULT '[]',
                latitude    NUMERIC(10,7),
                longitude   NUMERIC(10,7),
                active      BOOLEAN DEFAULT TRUE,
                created_at  TIMESTAMPTZ DEFAULT NOW()
            );
            CREATE INDEX IF NOT EXISTS idx_travelos_properties_location
                ON travelos_properties USING gin(
                    to_tsvector('english', location || ' ' || COALESCE(region,''))
                );
            CREATE INDEX IF NOT EXISTS idx_travelos_properties_amenities
                ON travelos_properties USING gin(amenities);
            CREATE INDEX IF NOT EXISTS idx_travelos_properties_guests
                ON travelos_properties (max_guests, active);
        """)

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS travelos_holds (
                id          UUID PRIMARY KEY,
                tenant_id   UUID NOT NULL REFERENCES travelos_tenants(id),
                unit_id     TEXT NOT NULL,
                check_in    TEXT NOT NULL,
                check_out   TEXT NOT NULL,
                guests      INT  NOT NULL,
                guest_email TEXT NOT NULL,
                expires_at  TIMESTAMPTZ NOT NULL,
                status      TEXT NOT NULL DEFAULT 'active',
                created_at  TIMESTAMPTZ DEFAULT NOW()
            );
            CREATE INDEX IF NOT EXISTS idx_travelos_holds_unit
                ON travelos_holds (unit_id, check_in, check_out, status);
            CREATE INDEX IF NOT EXISTS idx_travelos_holds_expiry
                ON travelos_holds (expires_at, status);
        """)

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS travelos_bookings (
                id               UUID PRIMARY KEY,
                tenant_id        UUID NOT NULL REFERENCES travelos_tenants(id),
                idempotency_key  TEXT NOT NULL UNIQUE,
                pms_booking_id   TEXT NOT NULL,
                unit_id          TEXT NOT NULL,
                check_in         TEXT NOT NULL,
                check_out        TEXT NOT NULL,
                guests           INT  NOT NULL,
                guest_name       TEXT NOT NULL,
                guest_email      TEXT NOT NULL,
                guest_phone      TEXT,
                status           TEXT NOT NULL DEFAULT 'confirmed',
                channel          TEXT DEFAULT 'travelos_mcp',
                commission_pct   NUMERIC(5,2) DEFAULT 5.00,
                notes            TEXT,
                created_at       TIMESTAMPTZ DEFAULT NOW()
            );
            CREATE INDEX IF NOT EXISTS idx_travelos_bookings_tenant
                ON travelos_bookings (tenant_id, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_travelos_bookings_idempotency
                ON travelos_bookings (idempotency_key);
            CREATE INDEX IF NOT EXISTS idx_travelos_bookings_guest
                ON travelos_bookings (tenant_id, guest_email);
        """)
