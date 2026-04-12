"""
Supervisor — fixed self-loop bug from live code.

Live code called http://backend:8000/mcp/whatsapp from inside the backend.
Fix: agents now import and call MCP tool functions directly.
"""
from __future__ import annotations

from langgraph.graph import StateGraph, START, END
from langgraph.types import Send, RunnableConfig
from pydantic import BaseModel
from typing import Literal, List, TypedDict, Annotated
import operator
from anthropic import AsyncAnthropic
import uuid
from datetime import datetime, timezone
import asyncpg
import json

from app.config import settings
from app.core.memory import AgentMemory
from app.core.security import aidefence_guard, hook

# Direct imports — no HTTP self-loop
from app.mcp.whatsapp import send_luxury_message as _send_whatsapp
from app.mcp.pricelabs import adjust_pricing as _adjust_pricing
from app.mcp.travel import get_weather_forecast as _get_weather

anthropic = AsyncAnthropic(api_key=settings.anthropic_api_key)
_memory: AgentMemory | None = None


class AgentState(TypedDict):
    workflow_id: str
    payload:     dict
    logs:        Annotated[List[dict], operator.add]
    next:        List[str]


class RoutingDecision(BaseModel):
    agents:   List[Literal["guest", "revenue", "operations"]]
    reason:   str
    parallel: bool


SYSTEM_PROMPT = """
You are the Cognitive Core of Vayancy Agentic Brain for ultra-luxury private villas in Greece.
Priorities: Guest delight > Revenue optimization > Operational excellence.
Tone: Warm, sophisticated, concierge-level. Never robotic.
"""


@hook("pre_supervisor")
async def supervisor_node(state: AgentState, config: RunnableConfig) -> dict:
    global _memory
    db: asyncpg.Pool = config["configurable"]["db_pool"]
    if _memory is None:
        _memory = AgentMemory(db)

    payload  = state["payload"]
    event_id = payload.get("event_id") or str(uuid.uuid4())

    # Idempotency
    existing = await db.fetchval(
        "SELECT 1 FROM workflow_logs WHERE details::jsonb->>'event_id' = $1 LIMIT 1",
        event_id,
    )
    if existing:
        return {"next": [], "logs": []}

    if not await aidefence_guard(str(payload)):
        return {"next": [], "logs": []}

    payload_text = str(payload)
    past         = await _memory.retrieve_relevant(payload_text)
    memory_str   = "\n".join(p["content"] for p in past) or "No prior similar events."

    prompt = (
        f"{SYSTEM_PROMPT}\n\n"
        f"Relevant past events:\n{memory_str}\n\n"
        f"New event: {payload_text}\n"
        f"Decide which agents should run."
    )

    try:
        response = await anthropic.messages.create(
            model=settings.anthropic_model,
            max_tokens=600,
            messages=[{"role": "user", "content": prompt}],
            tools=[{
                "name":        "route_workflow",
                "description": "Decide which agents to run",
                "input_schema": RoutingDecision.model_json_schema(),
            }],
            tool_choice={"type": "tool", "name": "route_workflow"},
            timeout=15.0,
        )
    except Exception as e:
        print(f"⏰ Supervisor LLM error: {e}")
        return {"next": [], "logs": []}

    tool_use = next((b for b in response.content if b.type == "tool_use"), None)
    if not tool_use:
        return {"next": [], "logs": []}

    decision = RoutingDecision.model_validate(tool_use.input)

    await db.execute(
        """
        INSERT INTO workflow_logs (id, timestamp, agent, action, status, details, workflow_id)
        VALUES ($1, $2, 'supervisor', 'routing_decision', 'completed', $3, $4)
        """,
        str(uuid.uuid4()),
        datetime.now(timezone.utc),
        json.dumps({"event_id": event_id, **payload}),
        state["workflow_id"],
    )
    await _memory.store_pattern(f"workflow_{state['workflow_id']}", payload_text)

    agents = decision.agents if decision.parallel else [decision.agents[0]]
    return {"next": agents, "logs": [{"agent": "supervisor", "status": "completed"}]}


def dynamic_router(state: AgentState):
    return [Send(agent, state) for agent in state.get("next", [])]


# ── Agent nodes — call tools directly, no HTTP self-loop ──────────────────────

async def guest_agent_node(state: AgentState, config: RunnableConfig) -> dict:
    try:
        payload = state["payload"]
        resp    = await anthropic.messages.create(
            model=settings.anthropic_model,
            max_tokens=300,
            messages=[
                {"role": "user", "content": (
                    f"{SYSTEM_PROMPT}\n\n"
                    f"Write a luxury concierge WhatsApp message for: {payload}"
                )},
            ],
        )
        message = resp.content[0].text
        # Call directly — no HTTP
        result  = await _send_whatsapp(
            phone=payload.get("guest_phone", ""),
            message=message,
        )
        return {"logs": [{"agent": "guest", "status": "completed", "details": result}]}
    except Exception as e:
        return {"logs": [{"agent": "guest", "status": "error", "details": str(e)}]}


async def revenue_agent_node(state: AgentState, config: RunnableConfig) -> dict:
    try:
        payload = state["payload"]
        result  = await _adjust_pricing(
            villa_id=payload.get("villa_id", "unknown"),
            new_price=float(payload.get("suggested_price", 1250)),
            reason="dynamic demand adjustment",
        )
        return {"logs": [{"agent": "revenue", "status": "completed", "details": result}]}
    except Exception as e:
        return {"logs": [{"agent": "revenue", "status": "error", "details": str(e)}]}


async def operations_agent_node(state: AgentState, config: RunnableConfig) -> dict:
    try:
        payload = state["payload"]
        result  = await _get_weather(
            location=payload.get("villa_location", "Santorini"),
        )
        return {"logs": [{"agent": "operations", "status": "completed", "details": result}]}
    except Exception as e:
        return {"logs": [{"agent": "operations", "status": "error", "details": str(e)}]}


# ── Graph ─────────────────────────────────────────────────────────────────────

workflow = StateGraph(AgentState)
workflow.add_node("supervisor",  supervisor_node)
workflow.add_node("guest",       guest_agent_node)
workflow.add_node("revenue",     revenue_agent_node)
workflow.add_node("operations",  operations_agent_node)

workflow.add_edge(START, "supervisor")
workflow.add_conditional_edges("supervisor", dynamic_router)
workflow.add_edge("guest",      END)
workflow.add_edge("revenue",    END)
workflow.add_edge("operations", END)

agentic_brain = workflow.compile()
