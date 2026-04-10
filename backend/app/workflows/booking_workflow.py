from app.core.supervisor import agentic_brain
import uuid

async def run_booking_workflow(payload: dict, db_pool: asyncpg.Pool):
    workflow_id = str(uuid.uuid4())
    initial_state = {"workflow_id": workflow_id, "payload": payload, "logs": []}
    result = await agentic_brain.ainvoke(initial_state, config={"configurable": {"db_pool": db_pool}})
    return {"workflow_id": workflow_id, "status": "completed", "result": result}
