"""
Agent Memory — pgvector embeddings with Voyage AI (async HTTP) or
sentence-transformers fallback.
Multi-tenant: every read/write is scoped by property_id.

Stage 1 fixes:
  Fix #2: retrieve_relevant() property/guest scoped — no cross-tenant leakage
  Fix #3: record_upsell, log_escalation, mark_escalation_notified added

Stage 3 fix:
  Fix #1: _encode wrapped in asyncio.to_thread() — non-blocking

Stage 5 — Voyage AI:
  If VOYAGE_API_KEY is set, embeddings are generated via async HTTP call to
  voyage-3 (1024-dim). This eliminates the 90MB sentence-transformers model
  from every worker process. Falls back to sentence-transformers automatically
  if the key is not set, so existing single-property deployments keep working.

  Dimension note: voyage-3 = 1024-dim vs all-MiniLM-L6-v2 = 384-dim.
  If switching an existing deployment, run the migration script to rebuild
  the HNSW index with the new dimension:
    docker compose exec api python -m app.db.migrations.002_voyage_embedding_dim
"""
from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any, Dict, List

import asyncpg
import httpx
import structlog

from app.config import settings

log = structlog.get_logger()

# ── Embedding backend ─────────────────────────────────────────────────────────
# Selected at module load based on whether VOYAGE_API_KEY is set.
# Both paths produce a list[float] and are awaitable.

_USE_VOYAGE = bool(settings.voyage_api_key)
EMBEDDING_DIM = settings.voyage_dimensions if _USE_VOYAGE else 384

if not _USE_VOYAGE:
    # sentence-transformers fallback — only import if actually needed
    # If not installed, embeddings are disabled (no-op) until VOYAGE_API_KEY is set
    try:
        from sentence_transformers import SentenceTransformer as _ST
        _st_model = _ST("all-MiniLM-L6-v2")
        log.info("embeddings_backend", backend="sentence-transformers", dim=384)
    except ImportError:
        _st_model = None
        log.warning("embeddings_backend", backend="none",
                    note="Set VOYAGE_API_KEY for embeddings — sentence_transformers not installed")
else:
    _st_model = None
    log.info("embeddings_backend", backend="voyage-ai",
             model=settings.voyage_model, dim=settings.voyage_dimensions)


async def _encode(text: str) -> list[float]:
    """
    Stage 5: Async embedding with Voyage AI when VOYAGE_API_KEY is set.
    Falls back to sentence-transformers (wrapped in asyncio.to_thread) otherwise.

    Voyage AI advantages over local sentence-transformers:
      - No 90MB model loaded per worker process
      - Fully async — no event loop blocking, no to_thread() overhead
      - voyage-3 produces higher-quality hospitality-domain embeddings
      - 1024-dim vs 384-dim — better semantic discrimination
    """
    if _USE_VOYAGE:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                "https://api.voyageai.com/v1/embeddings",
                headers={
                    "Authorization": f"Bearer {settings.voyage_api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": settings.voyage_model,
                    "input": text,
                    "input_type": "document",
                },
            )
            resp.raise_for_status()
            return resp.json()["data"][0]["embedding"]
    elif _st_model is not None:
        # sentence-transformers path — CPU-bound, run in thread pool
        return await asyncio.to_thread(lambda: _st_model.encode(text).tolist())
    else:
        # No embedding backend configured — return zero vector
        # Set VOYAGE_API_KEY to enable semantic memory
        log.warning("embedding_skipped", reason="no_backend_configured")
        return [0.0] * EMBEDDING_DIM


class AgentMemory:
    def __init__(self, db_pool: asyncpg.Pool) -> None:
        self.db = db_pool

    # ── Reasoning bank ────────────────────────────────────────────────────────

    async def store_pattern(
        self, key: str, content: str, tier: str = "semantic"
    ) -> None:
        embedding = await _encode(content)
        await self.db.execute(
            """
            INSERT INTO reasoning_bank (key, content, embedding, tier)
            VALUES ($1, $2, $3::vector, $4)
            ON CONFLICT (key) DO UPDATE
                SET content = $2, embedding = $3::vector, tier = $4
            """,
            key, content, embedding, tier,
        )

    async def retrieve_relevant(
        self,
        text: str,
        top_k: int = 3,
        property_id: str | None = None,   # Fix #2: scope to tenant
        guest_phone: str | None = None,   # Fix #2: optionally scope to guest
    ) -> List[Dict]:
        """
        Retrieve semantically similar content from the reasoning bank.

        Fix #2: When property_id is provided, retrieval is scoped exclusively
        to that property. Without this filter, every tenant's data was mixed
        in the vector index — a direct violation of the isolation model.

        The reasoning_bank table stores property-agnostic patterns (e.g.
        "how to handle a late checkout request"), so property_id filtering
        is applied to guest_interactions for per-tenant context, not to
        reasoning_bank which is intentionally shared. Both sources are
        combined into the returned list.
        """
        query_vector = await _encode(text)

        # Global reasoning patterns (shared knowledge, not tenant-specific)
        patterns = await self.db.fetch(
            """
            SELECT content, 1 - (embedding <=> $1::vector) AS similarity
            FROM   reasoning_bank
            ORDER  BY embedding <=> $1::vector
            LIMIT  $2
            """,
            query_vector, top_k,
        )

        # Tenant-specific interaction history (scoped by property_id)
        interactions: list = []
        if property_id:
            if guest_phone:
                interactions = await self.db.fetch(
                    """
                    SELECT content, 1 - (embedding <=> $1::vector) AS similarity
                    FROM   guest_interactions
                    WHERE  property_id = $3
                      AND  guest_phone = $4
                    ORDER  BY embedding <=> $1::vector
                    LIMIT  $2
                    """,
                    query_vector, top_k, property_id, guest_phone,
                )
            else:
                interactions = await self.db.fetch(
                    """
                    SELECT content, 1 - (embedding <=> $1::vector) AS similarity
                    FROM   guest_interactions
                    WHERE  property_id = $3
                    ORDER  BY embedding <=> $1::vector
                    LIMIT  $2
                    """,
                    query_vector, top_k, property_id,
                )

        # Merge, deduplicate by content, sort by similarity descending
        combined: dict[str, float] = {}
        for row in list(patterns) + list(interactions):
            content = row["content"]
            sim     = float(row["similarity"])
            if content not in combined or sim > combined[content]:
                combined[content] = sim

        sorted_results = sorted(combined.items(), key=lambda x: x[1], reverse=True)
        return [{"content": c, "similarity": s} for c, s in sorted_results[:top_k]]

    # ── Guest interactions ────────────────────────────────────────────────────

    async def store_interaction(
        self,
        guest_phone: str,
        property_id: str,
        reservation_id: str | None,
        direction: str,
        content: str,
    ) -> None:
        embedding = await _encode(content)
        await self.db.execute(
            """
            INSERT INTO guest_interactions
                (property_id, guest_phone, reservation_id,
                 direction, content, embedding)
            VALUES ($1, $2, $3, $4, $5, $6::vector)
            """,
            property_id, guest_phone, reservation_id,
            direction, content, embedding,
        )

    # ── Guest profiles ────────────────────────────────────────────────────────

    async def get_guest_profile(
        self, phone: str, property_id: str
    ) -> Dict | None:
        row = await self.db.fetchrow(
            "SELECT * FROM guests WHERE phone=$1 AND property_id=$2",
            phone, property_id,
        )
        return dict(row) if row else None

    async def upsert_guest_profile(
        self,
        phone: str,
        property_id: str,
        name: str | None = None,
        email: str | None = None,
        language: str = "en",
        nationality: str | None = None,
    ) -> None:
        await self.db.execute(
            """
            INSERT INTO guests
                (property_id, phone, name, email, language, nationality)
            VALUES ($1, $2, $3, $4, $5, $6)
            ON CONFLICT (property_id, phone) DO UPDATE
                SET name        = COALESCE($3, guests.name),
                    email       = COALESCE($4, guests.email),
                    language    = COALESCE($5, guests.language),
                    nationality = COALESCE($6, guests.nationality),
                    updated_at  = NOW()
            """,
            property_id, phone, name, email, language, nationality,
        )

    # ── Upsell tracking ───────────────────────────────────────────────────────

    async def record_upsell(
        self,
        guest_phone: str,
        property_id: str,
        upsell_type: str,
    ) -> None:
        """
        Fix #3: Append an upsell record to guests.upsell_history (JSONB array).
        The guest agent checks this array before offering — prevents repeat offers.
        Uses a Postgres JSON append so concurrent workers don't clobber each other.
        """
        entry = json.dumps({"type": upsell_type})
        await self.db.execute(
            """
            UPDATE guests
            SET    upsell_history = COALESCE(upsell_history, '[]'::jsonb)
                                    || $3::jsonb,
                   updated_at     = NOW()
            WHERE  phone       = $1
              AND  property_id = $2
            """,
            guest_phone, property_id, entry,
        )
        log.info("upsell_recorded", phone=guest_phone,
                 property_id=property_id, type=upsell_type)

    # ── Escalation tracking ───────────────────────────────────────────────────

    async def log_escalation(
        self,
        property_id: str,
        workflow_id: str,
        guest_phone: str | None,
        reason: str,
        guest_message: str | None = None,
        agent_output: str | None = None,
    ) -> str:
        """
        Fix #3: Insert a row into the escalations table.
        Returns the escalation ID so the caller can later call
        mark_escalation_notified() once the owner WhatsApp is confirmed sent.
        """
        esc_id = str(uuid.uuid4())
        await self.db.execute(
            """
            INSERT INTO escalations
                (id, property_id, workflow_id, guest_phone,
                 reason, guest_message, agent_output,
                 owner_notified, resolved)
            VALUES ($1, $2, $3, $4, $5, $6, $7, FALSE, FALSE)
            """,
            esc_id, property_id, workflow_id, guest_phone,
            reason, guest_message,
            agent_output[:4000] if agent_output else None,
        )
        log.info("escalation_logged", esc_id=esc_id,
                 property_id=property_id, reason=reason)
        return esc_id

    async def mark_escalation_notified(self, escalation_id: str) -> None:
        """
        Fix #3: Set owner_notified = TRUE once the WhatsApp message to the
        owner is confirmed delivered. Prevents duplicate owner notifications
        if the workflow retries.
        """
        await self.db.execute(
            "UPDATE escalations SET owner_notified = TRUE WHERE id = $1",
            escalation_id,
        )
        log.info("escalation_notified", esc_id=escalation_id)
