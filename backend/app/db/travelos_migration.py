"""
TravelOS DB migration — add to init_schema() in db/session.py.

Tables:
  travelos_tenants    — property owners / API keys
  travelos_properties — searchable property catalog (location, amenities, etc.)
  travelos_holds      — 15-min inventory holds (Postgres-backed, PMS-agnostic)
  travelos_bookings   — bookings made through TravelOS MCP channel
"""

TRAVELOS_TENANTS = """
    CREATE TABLE IF NOT EXISTS travelos_tenants (
        id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        name         TEXT NOT NULL,
        api_key      TEXT NOT NULL UNIQUE,
        property_ids TEXT[] DEFAULT '{}',
        pms_type     TEXT NOT NULL DEFAULT 'webhotelier',
        pms_config   JSONB NOT NULL DEFAULT '{}',
        active       BOOLEAN DEFAULT TRUE,
        created_at   TIMESTAMPTZ DEFAULT NOW()
    );
    CREATE INDEX IF NOT EXISTS idx_travelos_tenants_key
        ON travelos_tenants (api_key);
    CREATE INDEX IF NOT EXISTS idx_travelos_tenants_active
        ON travelos_tenants (active);
"""

# The property catalog is what makes search_properties fast.
# Every registered property is stored here with searchable metadata.
# The PMS is only called for real-time availability once candidates are found.
TRAVELOS_PROPERTIES = """
    CREATE TABLE IF NOT EXISTS travelos_properties (
        id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        tenant_id       UUID NOT NULL REFERENCES travelos_tenants(id) ON DELETE CASCADE,
        pms_unit_id     TEXT NOT NULL,         -- ID in the PMS (room_type_id etc.)
        name            TEXT NOT NULL,
        slug            TEXT UNIQUE,           -- URL-friendly name, auto-generated
        description     TEXT,
        location        TEXT NOT NULL,         -- searchable: "Mykonos", "Santorini"
        region          TEXT,                  -- broader area: "Cyclades", "Crete"
        country         TEXT DEFAULT 'GR',
        address         TEXT,
        latitude        NUMERIC(10,7),
        longitude       NUMERIC(10,7),
        max_guests      INT NOT NULL DEFAULT 2,
        bedrooms        INT DEFAULT 1,
        bathrooms       INT DEFAULT 1,
        amenities       TEXT[] DEFAULT '{}',   -- pool, sea_view, ac, bbq, wifi, etc.
        base_rate       NUMERIC(10,2),         -- cached nightly rate (for quick filter)
        currency        TEXT DEFAULT 'EUR',
        min_stay        INT DEFAULT 1,
        photos          JSONB DEFAULT '[]',    -- [{url, caption}]
        active          BOOLEAN DEFAULT TRUE,
        created_at      TIMESTAMPTZ DEFAULT NOW(),
        updated_at      TIMESTAMPTZ DEFAULT NOW()
    );
    CREATE INDEX IF NOT EXISTS idx_travelos_properties_location
        ON travelos_properties USING gin (to_tsvector('english', location || ' ' || COALESCE(region,'') || ' ' || COALESCE(address,'')));
    CREATE INDEX IF NOT EXISTS idx_travelos_properties_amenities
        ON travelos_properties USING gin (amenities);
    CREATE INDEX IF NOT EXISTS idx_travelos_properties_guests
        ON travelos_properties (max_guests, active);
    CREATE INDEX IF NOT EXISTS idx_travelos_properties_tenant
        ON travelos_properties (tenant_id, active);
"""

TRAVELOS_HOLDS = """
    CREATE TABLE IF NOT EXISTS travelos_holds (
        id           UUID PRIMARY KEY,
        tenant_id    UUID NOT NULL REFERENCES travelos_tenants(id),
        unit_id      TEXT NOT NULL,
        check_in     TEXT NOT NULL,
        check_out    TEXT NOT NULL,
        guests       INT  NOT NULL,
        guest_email  TEXT NOT NULL,
        expires_at   TIMESTAMPTZ NOT NULL,
        status       TEXT NOT NULL DEFAULT 'active',
        created_at   TIMESTAMPTZ DEFAULT NOW()
    );
    CREATE INDEX IF NOT EXISTS idx_travelos_holds_unit_dates
        ON travelos_holds (unit_id, check_in, check_out, status);
    CREATE INDEX IF NOT EXISTS idx_travelos_holds_tenant
        ON travelos_holds (tenant_id, created_at DESC);
    CREATE INDEX IF NOT EXISTS idx_travelos_holds_expiry
        ON travelos_holds (expires_at, status);
"""

TRAVELOS_BOOKINGS = """
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
    CREATE INDEX IF NOT EXISTS idx_travelos_bookings_guest
        ON travelos_bookings (tenant_id, guest_email);
    CREATE INDEX IF NOT EXISTS idx_travelos_bookings_idempotency
        ON travelos_bookings (idempotency_key);
    CREATE INDEX IF NOT EXISTS idx_travelos_bookings_status
        ON travelos_bookings (status, created_at DESC);
"""
