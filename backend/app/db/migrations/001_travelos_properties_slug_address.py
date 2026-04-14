"""
Migration 001 — travelos_properties: add slug + address columns.

Fix #7: The CREATE TABLE in session.py didn't include slug or address,
but travelos.py inserts both and uses ON CONFLICT (slug).
This migration adds them to existing deployments safely.

Run manually ONCE on any deployment that was started before this fix:
  docker compose exec api python -m app.db.migrations.001_travelos_properties_slug_address

Safe to run multiple times (all operations are idempotent).
"""
from __future__ import annotations
import asyncio
import sys

import asyncpg

sys.path.insert(0, "/app")
from app.config import settings


async def run() -> None:
    conn = await asyncpg.connect(settings.database_url)
    print("Connected. Running migration 001...")

    # Add slug column if missing
    await conn.execute("""
        ALTER TABLE travelos_properties
        ADD COLUMN IF NOT EXISTS slug TEXT;
    """)
    print("  ✓ slug column present")

    # Back-fill slug for any existing rows that have NULL
    updated = await conn.execute("""
        UPDATE travelos_properties
        SET slug = REPLACE(LOWER(name || '-' || location), ' ', '-')
        WHERE slug IS NULL
    """)
    print(f"  ✓ back-filled slug for {updated.split()[-1]} rows")

    # Add unique constraint if missing
    exists = await conn.fetchval("""
        SELECT 1 FROM pg_constraint
        WHERE conname = 'travelos_properties_slug_key'
    """)
    if not exists:
        # Deduplicate before adding constraint
        await conn.execute("""
            UPDATE travelos_properties p
            SET slug = p.slug || '-' || SUBSTR(p.id::TEXT, 1, 8)
            WHERE EXISTS (
                SELECT 1 FROM travelos_properties p2
                WHERE p2.slug = p.slug AND p2.id != p.id AND p2.id < p.id
            )
        """)
        await conn.execute("""
            ALTER TABLE travelos_properties
            ADD CONSTRAINT travelos_properties_slug_key UNIQUE (slug)
        """)
        print("  ✓ unique constraint on slug added")
    else:
        print("  ✓ unique constraint on slug already present")

    # Add address column if missing
    await conn.execute("""
        ALTER TABLE travelos_properties
        ADD COLUMN IF NOT EXISTS address TEXT;
    """)
    print("  ✓ address column present")

    # Add updated_at column if missing (also added in new schema)
    await conn.execute("""
        ALTER TABLE travelos_properties
        ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT NOW();
    """)
    print("  ✓ updated_at column present")

    # Rebuild location GIN index to include address
    await conn.execute("""
        DROP INDEX IF EXISTS idx_travelos_properties_location;
        CREATE INDEX idx_travelos_properties_location
            ON travelos_properties USING gin(
                to_tsvector('english',
                    location || ' ' || COALESCE(region,'') || ' ' || COALESCE(address,''))
            );
    """)
    print("  ✓ location GIN index rebuilt (now includes address)")

    await conn.close()
    print("\nMigration 001 complete.")


if __name__ == "__main__":
    asyncio.run(run())
