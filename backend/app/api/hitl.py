"""
HITL (Human-in-the-Loop) API — backend/app/api/hitl.py

Stage 4: Revenue decisions above a threshold pause for owner approval
before executing. The revenue agent emits HITL_REQUIRED: <json> in its
output. base_agent.py intercepts this and inserts a pending decision here.

Endpoints:
  GET  /hitl/pending              — all pending decisions for this property
  GET  /hitl/{decision_id}        — single decision detail
  POST /hitl/{decision_id}/approve — owner approves, action is re-enqueued
  POST /hitl/{decision_id}/reject  — owner rejects, records note

Auth: Bearer <SECRET_KEY>

Mount in main.py:
  from app.api.hitl import router as hitl_router
  app.include_router(hitl_router, prefix="/hitl")
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

import arq
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel

from app.config import settings
from app.db.session import get_pool

router  = APIRouter(tags=["hitl"])
_bearer = HTTPBearer(auto_error=False)


def _require_auth(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> None:
    if not creds or creds.credentials != settings.secret_key:
        raise HTTPException(status_code=401, detail="Unauthorized")


def _arq(request: Request) -> arq.ArqRedis:
    return request.app.state.arq_pool


# ── Schemas ───────────────────────────────────────────────────────────────────

class DecisionNote(BaseModel):
    note: str = ""


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/pending")
async def list_pending(
    request: Request,
    property_id: str | None = None,
    _: None = Depends(_require_auth),
) -> dict:
    """
    List all pending HITL decisions awaiting owner approval.
    Sorted by created_at DESC — most urgent first.
    """
    pid  = (property_id or settings.property_id).strip()
    pool = await get_pool()

    rows = await pool.fetch(
        """
        SELECT id, property_id, workflow_id, agent, action_type,
               proposed_action, impact_summary, status, created_at
        FROM   hitl_decisions
        WHERE  property_id = $1 AND status = 'pending'
        ORDER  BY created_at DESC
        """,
        pid,
    )

    return {
        "property_id":   pid,
        "pending_count": len(rows),
        "decisions": [
            {
                "decision_id":    str(r["id"]),
                "workflow_id":    r["workflow_id"],
                "agent":          r["agent"],
                "action_type":    r["action_type"],
                "proposed_action": json.loads(r["proposed_action"])
                                   if r["proposed_action"] else {},
                "impact_summary": r["impact_summary"],
                "status":         r["status"],
                "created_at":     r["created_at"].isoformat(),
            }
            for r in rows
        ],
    }


@router.get("/{decision_id}")
async def get_decision(
    decision_id: str,
    request: Request,
    _: None = Depends(_require_auth),
) -> dict:
    """Get full details of a specific HITL decision."""
    pool = await get_pool()
    row  = await pool.fetchrow(
        "SELECT * FROM hitl_decisions WHERE id = $1",
        uuid.UUID(decision_id),
    )
    if not row:
        raise HTTPException(status_code=404, detail="Decision not found")

    return {
        "decision_id":    str(row["id"]),
        "property_id":    row["property_id"],
        "workflow_id":    row["workflow_id"],
        "agent":          row["agent"],
        "action_type":    row["action_type"],
        "proposed_action": json.loads(row["proposed_action"])
                           if row["proposed_action"] else {},
        "impact_summary": row["impact_summary"],
        "status":         row["status"],
        "decision_note":  row["decision_note"],
        "created_at":     row["created_at"].isoformat(),
        "decided_at":     row["decided_at"].isoformat() if row["decided_at"] else None,
    }


@router.post("/{decision_id}/approve")
async def approve_decision(
    decision_id: str,
    body: DecisionNote,
    request: Request,
    _: None = Depends(_require_auth),
) -> dict:
    """
    Owner approves a pending revenue decision.

    The proposed action is re-enqueued as a new workflow with
    hitl_approved=True so the revenue agent executes it immediately
    without pausing again.
    """
    pool = await get_pool()
    row  = await pool.fetchrow(
        "SELECT * FROM hitl_decisions WHERE id = $1 AND status = 'pending'",
        uuid.UUID(decision_id),
    )
    if not row:
        raise HTTPException(
            status_code=404,
            detail="Decision not found or already resolved",
        )

    # Mark as approved
    await pool.execute(
        """
        UPDATE hitl_decisions
        SET status='approved', decision_note=$2, decided_at=NOW()
        WHERE id=$1
        """,
        uuid.UUID(decision_id),
        body.note or "Approved by owner",
    )

    # Re-enqueue the action with hitl_approved=True
    proposed = json.loads(row["proposed_action"]) if row["proposed_action"] else {}
    new_workflow_id = str(uuid.uuid4())

    await _arq(request).enqueue_job(
        "run_booking_workflow",
        workflow_id=new_workflow_id,
        event_id=f"hitl-approved-{decision_id}",
        property_id=row["property_id"],
        payload={
            "event":          "hitl.approved_action",
            "action_type":    row["action_type"],
            "proposed_action": proposed,
            "hitl_approved":  True,
            "original_workflow_id": row["workflow_id"],
            "decision_id":    decision_id,
            "approved_by":    "owner",
            "approved_at":    datetime.now(timezone.utc).isoformat(),
            "note":           body.note,
        },
    )

    return {
        "decision_id":      decision_id,
        "status":           "approved",
        "new_workflow_id":  new_workflow_id,
        "message":          "Decision approved. Action re-enqueued for execution.",
    }


@router.post("/{decision_id}/reject")
async def reject_decision(
    decision_id: str,
    body: DecisionNote,
    request: Request,
    _: None = Depends(_require_auth),
) -> dict:
    """
    Owner rejects a pending revenue decision.
    Records the rejection note. No action is taken.
    """
    pool = await get_pool()
    row  = await pool.fetchrow(
        "SELECT * FROM hitl_decisions WHERE id = $1 AND status = 'pending'",
        uuid.UUID(decision_id),
    )
    if not row:
        raise HTTPException(
            status_code=404,
            detail="Decision not found or already resolved",
        )

    await pool.execute(
        """
        UPDATE hitl_decisions
        SET status='rejected', decision_note=$2, decided_at=NOW()
        WHERE id=$1
        """,
        uuid.UUID(decision_id),
        body.note or "Rejected by owner",
    )

    return {
        "decision_id": decision_id,
        "status":      "rejected",
        "note":        body.note,
        "message":     "Decision rejected. No action will be taken.",
    }


@router.get("/history/all")
async def decision_history(
    request: Request,
    property_id: str | None = None,
    limit: int = 50,
    _: None = Depends(_require_auth),
) -> dict:
    """Full HITL decision history — pending + approved + rejected."""
    pid  = (property_id or settings.property_id).strip()
    pool = await get_pool()

    rows = await pool.fetch(
        """
        SELECT id, agent, action_type, impact_summary,
               status, decision_note, created_at, decided_at
        FROM   hitl_decisions
        WHERE  property_id = $1
        ORDER  BY created_at DESC
        LIMIT  $2
        """,
        pid, limit,
    )

    return {
        "property_id": pid,
        "total":       len(rows),
        "decisions": [
            {
                "decision_id":   str(r["id"]),
                "agent":         r["agent"],
                "action_type":   r["action_type"],
                "impact_summary": r["impact_summary"],
                "status":        r["status"],
                "decision_note": r["decision_note"],
                "created_at":    r["created_at"].isoformat(),
                "decided_at":    r["decided_at"].isoformat() if r["decided_at"] else None,
            }
            for r in rows
        ],
    }
