"""
Agent Memory — pgvector + sentence-transformers (all-MiniLM-L6-v2).
Multi-tenant: every read/write is scoped by property_id.
"""
from __future__ import annotations
import asyncpg
from typing import List, Dict
from sentence_transformers import SentenceTransformer

embedder = SentenceTransformer('all-MiniLM-L6-v2')
EMBEDDING_DIM = 384


class AgentMemory:
    def __init__(self, db_pool: asyncpg.Pool):
        self.db = db_pool

    async def store_pattern(self, key: str, content: str, tier: str = "semantic") -> None:
        embedding = embedder.encode(content).tolist()
        await self.db.execute("""
            INSERT INTO reasoning_bank (key, content, embedding, tier)
            VALUES ($1, $2, $3::vector, $4)
            ON CONFLICT (key) DO UPDATE
                SET content = $2, embedding = $3::vector, tier = $4
        """, key, content, embedding, tier)

    async def retrieve_relevant(self, text: str, top_k: int = 3) -> List[Dict]:
        query_vector = embedder.encode(text).tolist()
        return await self.db.fetch("""
            SELECT content, 1 - (embedding <=> $1::vector) AS similarity
            FROM   reasoning_bank
            ORDER  BY embedding <=> $1::vector
            LIMIT  $2
        """, query_vector, top_k)

    async def store_interaction(
        self,
        guest_phone: str,
        property_id: str,
        reservation_id: str | None,
        direction: str,
        content: str,
    ) -> None:
        embedding = embedder.encode(content).tolist()
        await self.db.execute("""
            INSERT INTO guest_interactions
                (property_id, guest_phone, reservation_id, direction, content, embedding)
            VALUES ($1, $2, $3, $4, $5, $6::vector)
        """, property_id, guest_phone, reservation_id, direction, content, embedding)

    async def get_guest_profile(self, phone: str, property_id: str) -> Dict | None:
        row = await self.db.fetchrow(
            "SELECT * FROM guests WHERE phone=$1 AND property_id=$2", phone, property_id
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
        await self.db.execute("""
            INSERT INTO guests (property_id, phone, name, email, language, nationality)
            VALUES ($1, $2, $3, $4, $5, $6)
            ON CONFLICT (property_id, phone) DO UPDATE
                SET name        = COALESCE($3, guests.name),
                    email       = COALESCE($4, guests.email),
                    language    = COALESCE($5, guests.language),
                    nationality = COALESCE($6, guests.nationality),
                    updated_at  = NOW()
        """, property_id, phone, name, email, language, nationality)
