from fastapi import FastAPI, Request, HTTPException, Depends
from contextlib import asynccontextmanager
import asyncpg
import os
import hmac
import hashlib
from slowapi import Limiter
from slowapi.util import get_remote_address
from prometheus_fastapi_instrumentator import Instrumentator

# Import all MCP servers
from app.mcp.whatsapp import mcp as whatsapp_mcp
from app.mcp.pms import mcp as pms_mcp
from app.mcp.pricelabs import mcp as pricelabs_mcp
from app.mcp.travel import mcp as travel_mcp

from app.workflows.booking_workflow import run_booking_workflow

limiter = Limiter(key_func=get_remote_address)

db_pool = None

async def verify_webhook(request: Request):
    signature = request.headers.get("X-Webhook-Signature")
    if not signature:
        raise HTTPException(401, "Missing signature")
    body = await request.body()
    secret = os.getenv("WEBHOOK_SECRET", "").encode()
    expected = hmac.new(secret, body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise HTTPException(401, "Invalid signature")
    return True

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
        CREATE INDEX IF NOT EXISTS idx_reasoning_bank_embedding 
        ON reasoning_bank USING hnsw (embedding vector_cosine_ops);
    """)
    yield
    await db_pool.close()

app = FastAPI(lifespan=lifespan, title="Vayancy Agentic Brain v11")
app.state.limiter = limiter

Instrumentator().instrument(app).expose(app)

app.mount("/mcp/whatsapp", whatsapp_mcp.sse_app())
app.mount("/mcp/pms", pms_mcp.sse_app())
app.mount("/mcp/pricelabs", pricelabs_mcp.sse_app())
app.mount("/mcp/travel", travel_mcp.sse_app())

@app.post("/webhook/webhotelier")
@limiter.limit("100/minute")
async def webhotelier_webhook(request: Request, verified: bool = Depends(verify_webhook)):
    payload = await request.json()
    result = await run_booking_workflow(payload, db_pool)
    return result

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000)
