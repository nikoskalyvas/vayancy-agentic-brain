"""
Database session — single pool, all table creation in one place.
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
    """Create all tables on startup. Safe to call multiple times."""
    pool = await get_pool()
    async with pool.acquire() as conn:

        await conn.execute("CREATE EXTENSION IF NOT EXISTS vector;")

        # ── Core tables ───────────────────────────────────────────────────────
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS workflow_logs (
                id          TEXT PRIMARY KEY,
                workflow_id TEXT NOT NULL,
                event_id    TEXT,
                property_id TEXT NOT NULL DEFAULT 'default',
                agent       TEXT NOT NULL,
                action      TEXT NOT NULL,
                status      TEXT NOT NULL,
                details     JSONB,
                timestamp   TIMESTAMPTZ DEFAULT NOW()
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_workflow_logs_event_id
                ON workflow_logs (event_id) WHERE event_id IS NOT NULL;
            CREATE INDEX IF NOT EXISTS idx_workflow_logs_property
                ON workflow_logs (property_id, timestamp DESC);
        """)

        # Fix #1: dimension is driven by settings — 384 for sentence-transformers,
        # 1024 for Voyage AI. Using hardcoded vector(384) crashes at INSERT when
        # Voyage AI is active. We read the configured dim at schema creation time.
        _emb_dim = settings.voyage_dimensions if settings.voyage_api_key else 384
        await conn.execute(f"""
            CREATE TABLE IF NOT EXISTS reasoning_bank (
                key       TEXT PRIMARY KEY,
                content   TEXT,
                embedding vector({_emb_dim}),
                tier      TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_reasoning_bank_embedding
                ON reasoning_bank USING hnsw (embedding vector_cosine_ops)
                WITH (m = 16, ef_construction = 64);
        """)

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS guests (
                id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                property_id    TEXT NOT NULL DEFAULT 'default',
                phone          TEXT NOT NULL,
                name           TEXT,
                email          TEXT,
                language       TEXT DEFAULT 'en',
                nationality    TEXT,
                preferences    TEXT,
                upsell_history JSONB DEFAULT '[]',
                created_at     TIMESTAMPTZ DEFAULT NOW(),
                updated_at     TIMESTAMPTZ DEFAULT NOW(),
                UNIQUE (property_id, phone)
            );
            CREATE INDEX IF NOT EXISTS idx_guests_property_phone
                ON guests (property_id, phone);
        """)

        await conn.execute(f"""
            CREATE TABLE IF NOT EXISTS guest_interactions (
                id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                property_id    TEXT NOT NULL DEFAULT 'default',
                guest_phone    TEXT,
                reservation_id TEXT,
                direction      TEXT CHECK (direction IN ('inbound','outbound')),
                channel        TEXT DEFAULT 'whatsapp',
                content        TEXT NOT NULL,
                embedding      vector({_emb_dim}),
                created_at     TIMESTAMPTZ DEFAULT NOW()
            );
            CREATE INDEX IF NOT EXISTS idx_guest_interactions_embedding
                ON guest_interactions
                USING hnsw (embedding vector_cosine_ops)
                WITH (m = 16, ef_construction = 64);
            CREATE INDEX IF NOT EXISTS idx_guest_interactions_property_phone
                ON guest_interactions (property_id, guest_phone);
        """)

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS pricing_decisions (
                id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                workflow_id   TEXT,
                property_id   TEXT NOT NULL DEFAULT 'default',
                date_from     DATE,
                date_to       DATE,
                rates_applied JSONB,
                rationale     TEXT,
                created_at    TIMESTAMPTZ DEFAULT NOW()
            );
            CREATE INDEX IF NOT EXISTS idx_pricing_property
                ON pricing_decisions (property_id, created_at DESC);
        """)

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS escalations (
                id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                property_id    TEXT NOT NULL DEFAULT 'default',
                workflow_id    TEXT,
                guest_phone    TEXT,
                reason         TEXT NOT NULL,
                guest_message  TEXT,
                agent_output   TEXT,
                owner_notified BOOLEAN DEFAULT FALSE,
                resolved       BOOLEAN DEFAULT FALSE,
                created_at     TIMESTAMPTZ DEFAULT NOW()
            );
            CREATE INDEX IF NOT EXISTS idx_escalations_property
                ON escalations (property_id, resolved, created_at DESC);
        """)

        # ── TravelOS tables ───────────────────────────────────────────────────
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS travelos_tenants (
                id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                name               TEXT NOT NULL,
                api_key            TEXT NOT NULL UNIQUE,
                pms_type           TEXT NOT NULL DEFAULT 'webhotelier',
                pms_config         JSONB NOT NULL DEFAULT '{}',
                active             BOOLEAN DEFAULT TRUE,
                payment_required   BOOLEAN DEFAULT FALSE,
                stripe_account_id  TEXT DEFAULT NULL,
                commission_pct     NUMERIC(5,2) DEFAULT 5.00,
                created_at         TIMESTAMPTZ DEFAULT NOW()
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
                slug        TEXT UNIQUE NOT NULL,
                location    TEXT NOT NULL,
                region      TEXT,
                address     TEXT,
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
                created_at  TIMESTAMPTZ DEFAULT NOW(),
                updated_at  TIMESTAMPTZ DEFAULT NOW()
            );
            CREATE INDEX IF NOT EXISTS idx_travelos_properties_location
                ON travelos_properties USING gin(
                    to_tsvector('english',
                        location || ' ' || COALESCE(region,'') || ' ' || COALESCE(address,''))
                );
            CREATE INDEX IF NOT EXISTS idx_travelos_properties_amenities
                ON travelos_properties USING gin(amenities);
            CREATE INDEX IF NOT EXISTS idx_travelos_properties_guests
                ON travelos_properties (max_guests, active);
            CREATE INDEX IF NOT EXISTS idx_travelos_properties_slug
                ON travelos_properties (slug);
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
                id              UUID PRIMARY KEY,
                tenant_id       UUID NOT NULL REFERENCES travelos_tenants(id),
                idempotency_key TEXT NOT NULL UNIQUE,
                pms_booking_id  TEXT NOT NULL,
                unit_id         TEXT NOT NULL,
                check_in        TEXT NOT NULL,
                check_out       TEXT NOT NULL,
                guests          INT  NOT NULL,
                guest_name      TEXT NOT NULL,
                guest_email     TEXT NOT NULL,
                guest_phone     TEXT,
                status          TEXT NOT NULL DEFAULT 'confirmed',
                channel         TEXT DEFAULT 'travelos_mcp',
                commission_pct  NUMERIC(5,2) DEFAULT 5.00,
                notes           TEXT,
                created_at      TIMESTAMPTZ DEFAULT NOW()
            );
            CREATE INDEX IF NOT EXISTS idx_travelos_bookings_tenant
                ON travelos_bookings (tenant_id, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_travelos_bookings_idempotency
                ON travelos_bookings (idempotency_key);
            CREATE INDEX IF NOT EXISTS idx_travelos_bookings_guest
                ON travelos_bookings (tenant_id, guest_email);
        """)
        # ── Stage 4: HITL decisions table ────────────────────────────────────
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS hitl_decisions (
                id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                property_id     TEXT NOT NULL,
                workflow_id     TEXT NOT NULL,
                agent           TEXT NOT NULL DEFAULT 'revenue',
                action_type     TEXT NOT NULL,
                proposed_action JSONB NOT NULL,
                impact_summary  TEXT,
                status          TEXT NOT NULL DEFAULT 'pending'
                                CHECK (status IN ('pending','approved','rejected')),
                decision_note   TEXT,
                created_at      TIMESTAMPTZ DEFAULT NOW(),
                decided_at      TIMESTAMPTZ
            );
            CREATE INDEX IF NOT EXISTS idx_hitl_decisions_property_status
                ON hitl_decisions (property_id, status, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_hitl_decisions_workflow
                ON hitl_decisions (workflow_id);
        """)

        # ── Stage 4: Insights query history ──────────────────────────────────
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS insights_queries (
                id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                property_id   TEXT NOT NULL,
                question      TEXT NOT NULL,
                sql_generated TEXT,
                row_count     INT  DEFAULT 0,
                narrative     TEXT,
                execution_ms  INT  DEFAULT 0,
                error         TEXT,
                asked_at      TIMESTAMPTZ DEFAULT NOW()
            );
            CREATE INDEX IF NOT EXISTS idx_insights_queries_property
                ON insights_queries (property_id, asked_at DESC);
        """)

        # ── Extension tables (from Phase 1 extension build) ───────────────────
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS extension_snapshots (
                id          TEXT PRIMARY KEY,
                property_id TEXT NOT NULL,
                category    TEXT NOT NULL,
                data        JSONB NOT NULL,
                captured_at TEXT NOT NULL,
                created_at  TIMESTAMPTZ DEFAULT NOW()
            );
            CREATE INDEX IF NOT EXISTS idx_ext_snapshots_property
                ON extension_snapshots (property_id, captured_at DESC);
            CREATE INDEX IF NOT EXISTS idx_ext_snapshots_category
                ON extension_snapshots (property_id, category, captured_at DESC);
        """)

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS extension_property_metrics (
                property_id TEXT NOT NULL,
                category    TEXT NOT NULL,
                data        JSONB NOT NULL,
                updated_at  TIMESTAMPTZ DEFAULT NOW(),
                PRIMARY KEY (property_id, category)
            );
            CREATE INDEX IF NOT EXISTS idx_ext_metrics_property
                ON extension_property_metrics (property_id);
        """)

        # ── Stripe payment sessions ───────────────────────────────────────────
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS stripe_payments (
                id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                hold_id             TEXT NOT NULL REFERENCES travelos_holds(id),
                tenant_id           UUID NOT NULL REFERENCES travelos_tenants(id),
                stripe_session_id   TEXT NOT NULL UNIQUE,
                stripe_payment_intent TEXT,
                amount_total        NUMERIC(10,2) NOT NULL,
                commission_amount   NUMERIC(10,2) NOT NULL,
                currency            TEXT NOT NULL DEFAULT 'eur',
                status              TEXT NOT NULL DEFAULT 'pending'
                                    CHECK (status IN ('pending','paid','expired','failed')),
                guest_email         TEXT NOT NULL,
                idempotency_key     TEXT NOT NULL,
                metadata            JSONB DEFAULT '{}',
                created_at          TIMESTAMPTZ DEFAULT NOW(),
                paid_at             TIMESTAMPTZ
            );
            CREATE INDEX IF NOT EXISTS idx_stripe_payments_session
                ON stripe_payments (stripe_session_id);
            CREATE INDEX IF NOT EXISTS idx_stripe_payments_hold
                ON stripe_payments (hold_id);
            CREATE INDEX IF NOT EXISTS idx_stripe_payments_tenant
                ON stripe_payments (tenant_id, created_at DESC);
        """)

        # ── Dispute prevention: delivery proofs ───────────────────────────────
        # Every guest-facing communication stored with WA message ID + timestamp.
        # Used as evidence in Stripe dispute responses.
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS booking_evidence (
                id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                booking_ref     TEXT NOT NULL,  -- travelos_bookings.id OR pms_booking_id
                property_id     TEXT NOT NULL,
                guest_email     TEXT NOT NULL,
                guest_phone     TEXT,
                evidence_type   TEXT NOT NULL
                                CHECK (evidence_type IN (
                                    'booking_contract',    -- WhatsApp T&Cs + guest YES reply
                                    'contract_accepted',   -- Guest reply confirming acceptance
                                    'checkin_instructions',-- Property access details sent
                                    'checkin_delivered',   -- Confirmed delivered (read receipt)
                                    'welcome_sent',        -- Welcome message
                                    'payment_receipt',     -- Stripe payment confirmation / email receipt
                                    'preauth_captured',    -- Pre-auth captured at check-in
                                    'communication'        -- Any other timestamped contact
                                )),
                channel         TEXT DEFAULT 'whatsapp' CHECK (channel IN ('whatsapp','email','stripe','manual')),
                content_summary TEXT NOT NULL,  -- brief description (no PII)
                wa_message_id   TEXT,           -- WhatsApp message ID for read receipt proof
                stripe_charge   TEXT,           -- Stripe charge/payment_intent ID
                metadata        JSONB DEFAULT '{}',
                created_at      TIMESTAMPTZ DEFAULT NOW()
            );
            CREATE INDEX IF NOT EXISTS idx_evidence_booking
                ON booking_evidence (booking_ref, evidence_type, created_at);
            CREATE INDEX IF NOT EXISTS idx_evidence_property
                ON booking_evidence (property_id, guest_email);
        """)
        # ── Stage 5: Multi-property registry ─────────────────────────────────
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS registered_properties (
                property_id     TEXT PRIMARY KEY,
                display_name    TEXT NOT NULL,
                pms_type        TEXT NOT NULL DEFAULT 'webhotelier',
                pms_property_id TEXT NOT NULL,
                owner_phone     TEXT DEFAULT '',
                location        TEXT DEFAULT '',
                active          BOOLEAN DEFAULT TRUE,
                config          JSONB DEFAULT '{}',
                created_at      TIMESTAMPTZ DEFAULT NOW(),
                updated_at      TIMESTAMPTZ DEFAULT NOW()
            );
            CREATE INDEX IF NOT EXISTS idx_reg_properties_pms
                ON registered_properties (pms_property_id, pms_type, active);
        """)

