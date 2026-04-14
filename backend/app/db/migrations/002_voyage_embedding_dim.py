"""
Migration 002 — pgvector: rebuild embedding columns + HNSW index for Voyage AI.

Stage 5: Switching from sentence-transformers (384-dim) to Voyage AI voyage-3
(1024-dim) requires rebuilding all embedding columns and indexes.

WARNING: This drops all existing embeddings. They cannot be reused across
dimensions. The reasoning_bank and guest_interactions tables will need to be
re-embedded from source text after running this migration.

Run ONLY when switching to Voyage AI for the first time:
  docker compose exec api python -m app.db.migrations.002_voyage_embedding_dim

Safe to run multiple times (checks current dim before altering).
"""
from __future__ import annotations
import asyncio
import sys

sys.path.insert(0, "/app")

from app.config import settings

NEW_DIM = settings.voyage_dimensions  # 1024 for voyage-3


async def run() -> None:
    import asyncpg
    conn = await asyncpg.connect(settings.database_url)
    print(f"Connected. Migrating to {NEW_DIM}-dim embeddings (Voyage AI)...")

    # Check current dim
    current = await conn.fetchval("""
        SELECT atttypmod FROM pg_attribute
        JOIN pg_class ON pg_class.oid = pg_attribute.attrelid
        WHERE pg_class.relname = 'reasoning_bank'
          AND pg_attribute.attname = 'embedding'
    """)

    if current and current == NEW_DIM:
        print(f"  ✓ Already at {NEW_DIM}-dim. Nothing to do.")
        await conn.close()
        return

    print(f"  Current dim: {current or 'unknown'} → Target: {NEW_DIM}")
    print("  WARNING: All existing embeddings will be cleared.")

    # 1. Drop HNSW indexes (required before ALTER COLUMN)
    await conn.execute("DROP INDEX IF EXISTS idx_reasoning_bank_embedding;")
    await conn.execute("DROP INDEX IF EXISTS idx_guest_interactions_embedding;")
    print("  ✓ Dropped HNSW indexes")

    # 2. Clear and resize reasoning_bank embeddings
    await conn.execute("""
        ALTER TABLE reasoning_bank
        ALTER COLUMN embedding TYPE vector(%d)
        USING NULL::vector(%d);
    """ % (NEW_DIM, NEW_DIM))
    print(f"  ✓ reasoning_bank.embedding → vector({NEW_DIM})")

    # 3. Clear and resize guest_interactions embeddings
    await conn.execute("""
        ALTER TABLE guest_interactions
        ALTER COLUMN embedding TYPE vector(%d)
        USING NULL::vector(%d);
    """ % (NEW_DIM, NEW_DIM))
    print(f"  ✓ guest_interactions.embedding → vector({NEW_DIM})")

    # 4. Rebuild HNSW indexes at new dimension
    await conn.execute(f"""
        CREATE INDEX idx_reasoning_bank_embedding
        ON reasoning_bank
        USING hnsw (embedding vector_cosine_ops)
        WITH (m = 16, ef_construction = 64);
    """)
    await conn.execute(f"""
        CREATE INDEX idx_guest_interactions_embedding
        ON guest_interactions
        USING hnsw (embedding vector_cosine_ops)
        WITH (m = 16, ef_construction = 64);
    """)
    print(f"  ✓ HNSW indexes rebuilt at {NEW_DIM}-dim")

    await conn.close()
    print(f"\nMigration 002 complete. All embeddings cleared — they will be")
    print(f"re-generated at {NEW_DIM}-dim on next interaction store call.")


if __name__ == "__main__":
    asyncio.run(run())
