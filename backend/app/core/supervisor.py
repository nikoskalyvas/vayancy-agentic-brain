from langgraph.graph import StateGraph, START, END
from langgraph.types import Send, RunnableConfig
from pydantic import BaseModel
from typing import Literal, List, TypedDict, Annotated
import operator
from anthropic import AsyncAnthropic
import uuid
from datetime import datetime, timezone
import asyncpg
import os
import json
from app.core.mcp_client import get_mcp_client
from app.core.memory import AgentMemory
from app.core.security import aidefence_guard, hook

anthropic = AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
memory = None

class AgentState(TypedDict):
    workflow_id: str
    payload: dict
    logs: Annotated[List[dict], operator.add]
    next: List[str] = []

class RoutingDecision(BaseModel):
    agents: List[Literal["guest", "revenue", "operations"]]
    reason: str
    parallel: bool

SYSTEM_PROMPT = """
You are the Cognitive Core of Vayancy Agentic Brain for ultra-luxury private villas in Greece.
Priorities: Guest delight > Revenue optimization > Operational excellence.
Tone: Warm, sophisticated, concierge-level. Never robotic.
"""

@hook("pre_supervisor")
async def supervisor_node(state: AgentState, config: RunnableConfig) -> AgentState:
    global memory
    db: asyncpg.Pool = config["configurable"]["db_pool"]
    if memory is None:
        memory = AgentMemory(db)

    payload = state["payload"]
    event_id = payload.get("event_id") or str(uuid.uuid4())

    # Idempotency check
    existing = await db.fetchval("SELECT 1 FROM workflow_logs WHERE details->>'event_id' = $1 LIMIT 1", event_id)
    if existing:
        print(f"🔁 Idempotency: Event {event_id} already processed")
        return {"next": []}

    if not await aidefence_guard(str(payload)):
        return {"next": []}

    payload_text = str(payload)
    past = await memory.retrieve_relevant(payload_text)

    memory_str = "\n".join([p["content"] for p in past]) if past else "No prior similar events."

    prompt = f"{SYSTEM_PROMPT}\n\nRelevant past events:\n{memory_str}\n\nNew event: {payload_text}\nDecide which agents should run."

    try:
        response = await anthropic.messages.create(
            model="claude-3-5-sonnet-20241022",
            max_tokens=600,
            messages=[{"role": "user", "content": prompt}],
            tools=[{"name": "route_workflow", "description": "Decide agents", "input_schema": RoutingDecision.model_json_schema()}],
            tool_choice={"type": "tool", "name": "route_workflow"},
            timeout=15.0
        )
    except Exception as e:
        print(f"⏰ LLM timeout or error: {e}")
        return {"next": []}

    tool_use = next((b for b in response.content if b.type == "tool_use"), None)
    if not tool_use:
        raise ValueError("Claude did not return tool_use block")

    decision: RoutingDecision = RoutingDecision.model_validate(tool_use.input)

    await db.execute(
        """INSERT INTO workflow_logs (id, timestamp, agent, action, status, details, workflow_id)
           VALUES ($1, $2, $3, $4, $5, $6, $7)""",
        str(uuid.uuid4()), datetime.now(timezone.utc), "supervisor",
        "routing_decision", "completed",
        json.dumps({"event_id": event_id, **payload}),
        state["workflow_id"]
    )

    await memory.store_pattern(f"workflow_{state['workflow_id']}", payload_text)

    return {"next": decision.agents if decision.parallel else [decision.agents[0]]}

def dynamic_router(state: AgentState):
    return [Send(agent, state) for agent in state.get("next", [])]

workflow = StateGraph(AgentState)
workflow.add_node("supervisor", supervisor_node)
workflow.add_node("guest", guest_agent_node)
workflow.add_node("revenue", revenue_agent_node)
workflow.add_node("operations", operations_agent_node)

workflow.add_edge(START, "supervisor")
workflow.add_conditional_edges("supervisor", dynamic_router)
workflow.add_edge("guest", END)
workflow.add_edge("revenue", END)
workflow.add_edge("operations", END)

agentic_brain = workflow.compile()

# Agent nodes
async def guest_agent_node(state: AgentState, config: RunnableConfig) -> AgentState:
    try:
        payload = state["payload"]
        async with get_mcp_client("http://backend:8000/mcp/whatsapp") as session:
            resp = await anthropic.messages.create(
                model="claude-3-5-sonnet-20241022",
                max_tokens=300,
                messages=[{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": f"Write luxury WhatsApp message: {payload}"}]
            )
            message = resp.content[0].text
            await session.call_tool("send_luxury_message", {"phone": payload.get("guest_phone"), "message": message})
        return {"logs": [{"agent": "guest", "status": "completed"}]}
    except Exception as e:
        return {"logs": [{"agent": "guest", "status": "error"}]}

async def revenue_agent_node(state: AgentState, config: RunnableConfig) -> AgentState:
    try:
        payload = state["payload"]
        async with get_mcp_client("http://backend:8000/mcp/pricelabs") as session:
            await session.call_tool("adjust_pricing", {"villa_id": payload.get("villa_id"), "new_price": 1250, "reason": "dynamic demand"})
        return {"logs": [{"agent": "revenue", "status": "completed"}]}
    except Exception as e:
        return {"logs": [{"agent": "revenue", "status": "error"}]}

async def operations_agent_node(state: AgentState, config: RunnableConfig) -> AgentState:
    try:
        payload = state["payload"]
        async with get_mcp_client("http://backend:8000/mcp/travel") as session:
            await session.call_tool("get_weather_forecast", {"location": payload.get("villa_location", "Santorini")})
        return {"logs": [{"agent": "operations", "status": "completed"}]}
    except Exception as e:
        return {"logs": [{"agent": "operations", "status": "error"}]}
