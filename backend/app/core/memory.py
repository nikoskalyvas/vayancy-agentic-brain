import asyncpg
from typing import List, Dict
from sentence_transformers import SentenceTransformer

embedder = SentenceTransformer('all-MiniLM-L6-v2')

class AgentMemory:
    def __init__(self, db_pool: asyncpg.Pool):
        self.db = db_pool

    async def store_pattern(self, key: str, content: str, tier: str = "semantic"):
        embedding = embedder.encode(content).tolist()
        await self.db.execute("""
            INSERT INTO reasoning_bank (key, content, embedding, tier)
            VALUES ($1, $2, $3::vector, $4)
            ON CONFLICT (key) DO UPDATE SET content = $2, embedding = $3::vector, tier = $4
        """, key, content, embedding, tier)

    async def retrieve_relevant(self, text: str, top_k: int = 3) -> List[Dict]:
        query_vector = embedder.encode(text).tolist()
        return await self.db.fetch("""
            SELECT content, 1 - (embedding <=> $1::vector) as similarity
            FROM reasoning_bank
            ORDER BY embedding <=> $1::vector
            LIMIT $2
        """, query_vector, top_k)
