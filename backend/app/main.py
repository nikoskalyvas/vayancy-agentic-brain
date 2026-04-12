"""
Vayancy Agentic Brain — FastAPI application entry point.

MCP servers run as separate processes on ports 3001-3005.
TravelOS Admin API is mounted at /travelos.
"""
from __future__ import annotations
from contextlib import asynccontextmanager

import arq
import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.core.memory import AgentMemory
from app.db.session import get_pool, close_pool, init_schema
from app.api.webhooks import router as webhook_router
from app.api.stream import router as stream_router
from app.api.travelos import router as travelos_router   # ← NEW

log = structlog.get_logger()


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("startup_begin", environment=settings.environment)

    await init_schema()   # creates all tables including TravelOS tables
    pool = await get_pool()

    app.state.memory   = AgentMemory(pool)
    app.state.db_pool  = pool   # ← exposed for travelos router dependency

    app.state.arq_pool = await arq.create_pool(
        arq.connections.RedisSettings.from_dsn(settings.redis_url)
    )

    log.info("startup_complete")
    yield

    await app.state.arq_pool.close()
    await close_pool()
    log.info("shutdown_complete")


app = FastAPI(
    title="Vayancy Agentic Brain",
    version="3.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://owners.vayancy.gr", "http://localhost:3000"],
    allow_methods=["GET", "POST", "PATCH", "DELETE"],
    allow_headers=["*"],
)

app.include_router(webhook_router)
app.include_router(stream_router)
app.include_router(travelos_router, prefix="/travelos")   # ← NEW

# Docs note:
#   GET  /travelos/admin/tenants          — list all tenants (admin)
#   POST /travelos/admin/tenants          — register new tenant (admin)
#   POST /travelos/me/rotate-key          — rotate API key (owner)
#   POST /travelos/me/properties          — register property in catalog (owner)
#   GET  /travelos/me/properties          — list my properties (owner)
#   PATCH /travelos/me/properties/{id}    — update property (owner)
#   GET  /travelos/me/bookings            — booking dashboard (owner)
#   GET  /travelos/me/stats               — summary stats (owner)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=8000,
        reload=settings.environment == "development",
    )
