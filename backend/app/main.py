"""
Vayancy Agentic Brain — FastAPI application entry point.

MCP servers run as separate processes on ports 3001-3005.
TravelOS Admin API is mounted at /travelos.
"""
from __future__ import annotations
from contextlib import asynccontextmanager

import arq
import structlog
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.core.memory import AgentMemory
from app.db.session import get_pool, close_pool, init_schema
from app.api.webhooks import router as webhook_router
from app.api.stream   import router as stream_router
from app.api.travelos import router as travelos_router
from app.api.insights    import router as insights_router    # Stage 4
from app.api.hitl        import router as hitl_router        # Stage 4
from app.api.properties  import router as properties_router  # Stage 5
from app.api.payments       import router as payments_router       # Stripe
from app.api.dispute_defense import router as dispute_router        # Dispute prevention
from app.api.auth import auth_router, owner_router                   # Owner self-service

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
    version="5.2.0",
    lifespan=lifespan,
)

_limiter = Limiter(key_func=get_remote_address)
app.state.limiter = _limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://owners.vayancy.gr", "http://localhost:3000", "http://localhost:3001"],
    allow_methods=["GET", "POST", "PATCH", "DELETE"],
    allow_headers=["Authorization", "Content-Type", "X-Tenant-Key", "X-Hub-Signature-256", "X-WebHotelier-Signature"],
)

app.include_router(webhook_router)
app.include_router(stream_router)
app.include_router(travelos_router, prefix="/travelos")
app.include_router(insights_router, prefix="/insights")  # Stage 4
app.include_router(hitl_router,       prefix="/hitl")        # Stage 4
app.include_router(properties_router, prefix="/properties")  # Stage 5
app.include_router(payments_router,   prefix="/payments")      # Stripe
app.include_router(dispute_router,    prefix="/dispute")       # Dispute prevention
app.include_router(auth_router,       prefix="/auth")           # Owner auth
app.include_router(owner_router,      prefix="/owner")          # Owner dashboard

# Route reference:
#
# TravelOS
#   GET  /travelos/admin/tenants
#   POST /travelos/admin/tenants
#   POST /travelos/me/rotate-key
#   POST /travelos/me/properties
#   GET  /travelos/me/properties
#   PATCH /travelos/me/properties/{id}
#   GET  /travelos/me/bookings
#   GET  /travelos/me/stats
#
# Insights (Stage 4)
#   POST /insights/query              — natural language → SQL → narrative
#   GET  /insights/history            — last N queries
#
# HITL (Stage 4)
#   GET  /hitl/pending                — revenue decisions awaiting approval
#   GET  /hitl/{id}                   — single decision detail
#   POST /hitl/{id}/approve           — owner approves → re-enqueued
#   POST /hitl/{id}/reject            — owner rejects → recorded
#   GET  /hitl/history/all            — full decision history


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=8000,
        reload=settings.environment == "development",
    )
