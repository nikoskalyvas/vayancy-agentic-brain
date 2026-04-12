from __future__ import annotations
import asyncpg
import uuid
from app.core.supervisor import agentic_brain


async def run_booking_workflow(payload: dict, db_pool: asyncpg.Pool) -> dict:
    workflow_id   = str(uuid.uuid4())
    initial_state = {
        "workflow_id": workflow_id,
        "payload":     payload,
        "logs":        [],
        "next":        [],
    }
    result = await agentic_brain.ainvoke(
        initial_state,
        config={"configurable": {"db_pool": db_pool}},
    )
    return {"workflow_id": workflow_id, "status": "completed", "result": result}
