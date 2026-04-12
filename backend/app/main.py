"""
Vayancy Agentic Brain — main entry point.

Changes from live code:
  - DB init moved to session.py (cleaner, includes TravelOS tables)
  - TravelOS admin/owner API mounted at /travelos
  - app.state.db_pool exposed for dependency injection in routers
  - CORS updated to include PATCH method (needed for property updates)
"""
from __future__ import annotations
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from slowapi import Limiter
from slowapi.util import get_remote_address
from prometheus_fastapi_instrumentator import Instrumentator

from app.config import settings
from app.db.session import get_pool, close_pool, init_schema
from app.workflows.booking_workflow import run_booking_workflow

# MCP servers
from app.mcp.whatsapp  import mcp as whatsapp_mcp
from app.mcp.pms       import mcp as pms_mcp
from app.mcp.pricelabs import mcp as pricelabs_mcp
from app.mcp.travel    import mcp as travel_mcp
from app.mcp.hospitality import mcp as hospitality_mcp

# TravelOS API
from app.api.travelos import router as travelos_router

limiter = Limiter(key_func=get_remote_address)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Create all DB tables (original + TravelOS)
    await init_schema()
    pool = await get_pool()
    app.state.db_pool = pool   # exposed for travelos router

    yield

    await close_pool()


app = FastAPI(
    title="Vayancy Agentic Brain",
    version="7.0.0",
    lifespan=lifespan,
)

app.state.limiter = limiter
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://owners.vayancy.gr", "http://localhost:3000"],
    allow_methods=["GET", "POST", "PATCH", "DELETE"],
    allow_headers=["*"],
)

Instrumentator().instrument(app).expose(app)

# ── MCP servers (mounted in-process, same as live) ────────────────────────────
app.mount("/mcp/whatsapp",    whatsapp_mcp.sse_app())
app.mount("/mcp/pms",         pms_mcp.sse_app())
app.mount("/mcp/pricelabs",   pricelabs_mcp.sse_app())
app.mount("/mcp/travel",      travel_mcp.sse_app())
app.mount("/mcp/hospitality", hospitality_mcp.sse_app())

# ── TravelOS REST API ─────────────────────────────────────────────────────────
app.include_router(travelos_router, prefix="/travelos")


# ── Webhooks ──────────────────────────────────────────────────────────────────
@app.post("/webhook/webhotelier")
@limiter.limit("100/minute")
async def webhotelier_webhook(request: Request):
    payload = await request.json()
    pool    = request.app.state.db_pool
    result  = await run_booking_workflow(payload, pool)
    return result


@app.get("/health")
async def health():
    return {"status": "ok", "version": "7.0.0"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)
