from fastapi import FastAPI, Request, HTTPException, Depends
from contextlib import asynccontextmanager
import asyncpg
import os
from slowapi import Limiter
from slowapi.util import get_remote_address
from prometheus_fastapi_instrumentator import Instrumentator

# Import all MCP servers
from app.mcp.whatsapp import mcp as whatsapp_mcp
from app.mcp.pms import mcp as pms_mcp
from app.mcp.pricelabs import mcp as pricelabs_mcp
from app.mcp.travel import mcp as travel_mcp
from app.mcp.hospitality import mcp as hospitality_mcp

from app.workflows.booking_workflow import run_booking_workflow

limiter = Limiter(key_func=get_remote_address)

db_pool = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global db_pool
    db_pool = await asyncpg.create_pool(dsn=os.getenv("DATABASE_URL"))
    await db_pool.execute("CREATE EXTENSION IF NOT EXISTS vector;")
    await db_pool.execute("""
        CREATE TABLE IF NOT EXISTS workflow_logs (
            id TEXT PRIMARY KEY,
            timestamp TIMESTAMPTZ NOT NULL,
            agent TEXT NOT NULL,
            action TEXT NOT NULL,
            status TEXT NOT NULL,
            details TEXT,
            workflow_id TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS reasoning_bank (
            key TEXT PRIMARY KEY,
            content TEXT,
            embedding vector(384),
            tier TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_reasoning_bank_embedding ON reasoning_bank USING hnsw (embedding vector_cosine_ops);
    """)
    yield
    await db_pool.close()

app = FastAPI(lifespan=lifespan, title="Vayancy Agentic Brain v11")
app.state.limiter = limiter

Instrumentator().instrument(app).expose(app)

# MCP mounts
app.mount("/mcp/whatsapp", whatsapp_mcp.sse_app())
app.mount("/mcp/pms", pms_mcp.sse_app())
app.mount("/mcp/pricelabs", pricelabs_mcp.sse_app())
app.mount("/mcp/travel", travel_mcp.sse_app())
app.mount("/mcp/hospitality", hospitality_mcp.sse_app())

@app.post("/webhook/webhotelier")
@limiter.limit("100/minute")
async def webhotelier_webhook(request: Request):
    payload = await request.json()
    result = await run_booking_workflow(payload, db_pool)
    return result

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000)
