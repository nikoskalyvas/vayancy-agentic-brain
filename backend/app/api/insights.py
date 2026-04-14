"""
Insights API — backend/app/api/insights.py

Endpoints:
  POST /insights/query   — answer a natural language question
  GET  /insights/history — last N queries for this property

Auth: Bearer <SECRET_KEY>

Mount in main.py:
  from app.api.insights import router as insights_router
  app.include_router(insights_router, prefix="/insights")
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel

from app.config import settings
from app.core.insights_agent import InsightsAgent
from app.db.session import get_pool

router  = APIRouter(tags=["insights"])
_bearer = HTTPBearer(auto_error=False)


def _require_auth(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> None:
    if not creds or creds.credentials != settings.secret_key:
        raise HTTPException(status_code=401, detail="Unauthorized")


# ── Schemas ───────────────────────────────────────────────────────────────────

class InsightsQueryRequest(BaseModel):
    question:    str
    property_id: str | None = None  # defaults to settings.property_id


class InsightsQueryResponse(BaseModel):
    query_id:         str
    question:         str
    property_id:      str
    sql:              str | None
    validation_error: str | None
    row_count:        int
    rows:             list[dict]
    narrative:        str | None
    execution_ms:     int
    error:            str | None
    asked_at:         str


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.post("/query", response_model=InsightsQueryResponse)
async def insights_query(
    body: InsightsQueryRequest,
    request: Request,
    _: None = Depends(_require_auth),
) -> InsightsQueryResponse:
    """
    Answer a natural language question about property performance.

    Examples:
      "How many bookings did we have last month?"
      "What is our average ADR vs this time last year?"
      "Which guests have stayed more than once?"
      "Why did revenue drop last week?"
      "What promotions are currently active on Booking.com?"
      "Show me escalations that weren't resolved in the last 30 days"
    """
    if not body.question.strip():
        raise HTTPException(status_code=400, detail="question is required")

    property_id = (body.property_id or settings.property_id).strip()
    pool        = await get_pool()
    agent       = InsightsAgent(db_pool=pool)
    query_id    = str(uuid.uuid4())

    result = await agent.query(
        question=body.question,
        property_id=property_id,
        query_id=query_id,
    )

    # Persist to query history
    try:
        await pool.execute(
            """
            INSERT INTO insights_queries
                (id, property_id, question, sql_generated, row_count,
                 narrative, execution_ms, error, asked_at)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,NOW())
            """,
            query_id,
            property_id,
            body.question,
            result.get("sql"),
            result.get("row_count", 0),
            result.get("narrative"),
            result.get("execution_ms", 0),
            result.get("error"),
        )
    except Exception:
        pass  # History is best-effort — don't fail the response

    return InsightsQueryResponse(
        query_id=result["query_id"],
        question=result["question"],
        property_id=result["property_id"],
        sql=result.get("sql"),
        validation_error=result.get("validation_error"),
        row_count=result.get("row_count", 0),
        rows=result.get("rows", []),
        narrative=result.get("narrative"),
        execution_ms=result.get("execution_ms", 0),
        error=result.get("error"),
        asked_at=datetime.now(timezone.utc).isoformat(),
    )


@router.get("/history")
async def insights_history(
    request: Request,
    property_id: str | None = None,
    limit: int = 20,
    _: None = Depends(_require_auth),
) -> dict:
    """Return recent insight queries for this property."""
    pid  = (property_id or settings.property_id).strip()
    pool = await get_pool()

    try:
        rows = await pool.fetch(
            """
            SELECT id, question, sql_generated, row_count,
                   narrative, execution_ms, error, asked_at
            FROM   insights_queries
            WHERE  property_id = $1
            ORDER  BY asked_at DESC
            LIMIT  $2
            """,
            pid, limit,
        )
    except Exception:
        # Table may not exist yet on first boot
        rows = []

    return {
        "property_id": pid,
        "queries": [
            {
                "query_id":     str(r["id"]),
                "question":     r["question"],
                "sql":          r["sql_generated"],
                "row_count":    r["row_count"],
                "narrative":    r["narrative"],
                "execution_ms": r["execution_ms"],
                "error":        r["error"],
                "asked_at":     r["asked_at"].isoformat() if r["asked_at"] else None,
            }
            for r in rows
        ],
        "total": len(rows),
    }
