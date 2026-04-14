"""
Supervisor — fixed self-loop bug.
Agents call MCP tool functions directly — no external HTTP ports needed.
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
import structlog

from app.config import settings
from app.core.memory import AgentMemory
from app.core.security import aidefence_guard, hook
from app.core.revenue_agent import RevenueAgent
from app.core.guest_agent import GuestAgent
from app.core.operations_agent import OperationsAgent

# MCP server URLs (separate Docker processes)
_WH_URL = "http://mcp-webhotelier:3001/sse"
_WA_URL = "http://mcp-whatsapp:3002/sse"
_PL_URL = "http://mcp-pricelabs:3003/sse"
_EN_URL = "http://mcp-epsilonnet:3004/sse"

# Direct imports — no HTTP self-loop, no external ports
from app.mcp.whatsapp  import send_luxury_message as _send_whatsapp
from app.mcp.pricelabs import adjust_pricing as _adjust_pricing
from app.mcp.travel    import get_weather_forecast as _get_weather

log      = structlog.get_logger()
anthropic = AsyncAnthropic(api_key=settings.anthropic_api_key)
_memory: AgentMemory | None = None


def _notify_channel(property_id: str) -> str:
    return f"workflow_updates_{property_id}"


class AgentState(TypedDict):
    workflow_id: str
    payload:     dict
    logs:        Annotated[List[dict], operator.add]
    next:        List[str]


class RoutingDecision(BaseModel):
    agents: List[Literal["guest", "revenue", "operations"]]
    reason: str


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

    # Idempotency check
    existing = await db.fetchval(
        "SELECT 1 FROM workflow_logs WHERE details::jsonb->>'event_id' = $1 LIMIT 1",
        event_id,
    )
    if existing:
        return {"next": [], "logs": []}

    if not await aidefence_guard(str(payload)):
        return {"next": [], "logs": []}

    # Fix #2: scope retrieval to this property (+ guest if available)
    # Without property_id, the vector search pulled from all tenants data.
    payload_text = str(payload)
    _pid         = payload.get("property_id") or settings.property_id
    _phone       = payload.get("guest_phone")
    past         = await _memory.retrieve_relevant(
        payload_text,
        property_id=_pid,
        guest_phone=_phone,
    )
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
                "name":         "route_workflow",
                "description":  "Decide which agents to run",
                "input_schema": RoutingDecision.model_json_schema(),
            }],
            tool_choice={"type": "tool", "name": "route_workflow"},
            timeout=15.0,
        )
    except Exception as e:
        log.error("supervisor_llm_error", error=str(e))
        return {"next": [], "logs": []}

    tool_use = next((b for b in response.content if b.type == "tool_use"), None)
    if not tool_use:
        return {"next": [], "logs": []}

    decision = RoutingDecision.model_validate(tool_use.input)

    await db.execute(
        """
        INSERT INTO workflow_logs
            (id, workflow_id, event_id, property_id, agent, action, status, details)
        VALUES (gen_random_uuid()::text, $1, $2, $3, 'supervisor', 'routing_decision', 'completed', $4)
        """,
        state["workflow_id"],
        event_id,
        settings.property_id,
        json.dumps({"event_id": event_id, **payload}),
    )
    await _memory.store_pattern(f"workflow_{state['workflow_id']}", payload_text)

    return {
        "next": decision.agents,
        "logs": [{"agent": "supervisor", "status": "completed"}],
    }


def dynamic_router(state: AgentState):
    return [Send(agent, state) for agent in state.get("next", [])]


async def guest_agent_node(state: AgentState, config: RunnableConfig) -> dict:
    """
    Fix #2: replaced stub with full GuestAgent (BaseAgent + MCP + memory).
    Previously called Claude directly with a plain string prompt — no MCP tools,
    no profile injection, no directive parsing, no escalation handling.
    """
    db_pool     = config["configurable"]["db_pool"]
    memory      = config["configurable"]["memory"]
    payload     = state["payload"]
    property_id = payload.get("property_id") or settings.property_id
    guest_phone = payload.get("guest_phone")
    memory_ctx  = state.get("memory_context", "")

    event = payload.get("event", "unknown")
    user_message = (
        f"Event: {event}\n"
        f"Reservation ID: {payload.get('reservation_id', 'N/A')}\n"
        f"Full payload: {json.dumps(payload)}\n\n"
        f"Guest interaction history:\n{memory_ctx or 'No prior history.'}\n\n"
        "Handle this event as the Vayancy luxury concierge."
    )
    if payload.get("message_body"):
        user_message = (
            f"Inbound WhatsApp message from {guest_phone}:\n"
            f"{payload['message_body']}\n\n"
            f"Conversation history:\n{memory_ctx or 'No prior history.'}"
        )

    try:
        agent  = GuestAgent(webhotelier_url=_WH_URL, whatsapp_url=_WA_URL)
        result = await agent.run(
            user_message,
            workflow_id=state["workflow_id"],
            property_id=property_id,
            db_pool=db_pool,
            memory=memory,
            guest_phone=guest_phone,
        )
        return {"logs": [result]}
    except Exception as e:
        log.error("guest_agent_node_failed", error=str(e), workflow_id=state["workflow_id"])
        return {"logs": [{"agent": "guest", "status": "error", "details": str(e)}]}


async def revenue_agent_node(state: AgentState, config: RunnableConfig) -> dict:
    db_pool     = config["configurable"]["db_pool"]
    memory      = config["configurable"]["memory"]
    payload     = state["payload"]
    property_id = payload.get("property_id") or settings.property_id

    # Stage 4: Extension → Brain loop
    # Fetch live BDC metrics from Chrome Extension snapshots and inject
    # into the agent context so it can factor in real-time ranking + ADR.
    from app.core.base_agent import fetch_extension_context, RevenueAgent as _RAClass
    ext_ctx = await fetch_extension_context(db_pool, property_id)

    user_message = (
        f"Booking event: {json.dumps(payload)}\n\n"
        "Review current pricing and occupancy. Adjust rates for the next "
        "60 days where warranted. Always dry_run=True first. "
        "Provide a clear PRICING SUMMARY at the end."
    )
    if ext_ctx:
        user_message += f"\n\n{ext_ctx}"

    try:
        agent  = RevenueAgent(webhotelier_url=_WH_URL, pricelabs_url=_PL_URL)
        # Fix #3: if this is an approved HITL re-enqueue, skip the HITL threshold
        # check so the agent executes the action without creating a new pending decision.
        result = await agent.run(
            user_message,
            workflow_id=state["workflow_id"],
            property_id=property_id,
            db_pool=db_pool,
            memory=memory,
            skip_hitl=bool(payload.get("hitl_approved", False)),
        )
        return {"logs": [result]}
    except Exception as e:
        return {"logs": [{"agent": "revenue", "status": "error", "details": str(e)}]}


async def operations_agent_node(state: AgentState, config: RunnableConfig) -> dict:
    """
    Fix #2: replaced stub with full OperationsAgent (BaseAgent + MCP + memory).
    Previously called _get_weather("Santorini") — a mock tool returning a weather
    string. No cleaning dispatch, no folio creation, no escalation.
    """
    db_pool     = config["configurable"]["db_pool"]
    memory      = config["configurable"]["memory"]
    payload     = state["payload"]
    property_id = payload.get("property_id") or settings.property_id
    event       = payload.get("event", "")

    if "checkout" in event:
        instruction = (
            "1. Dispatch cleaning team via WhatsApp.\n"
            "2. Create the Epsilon Net folio (13% VAT).\n"
            "3. Flag URGENT if cleaning window < 6 hours."
        )
    elif "checkin" in event:
        instruction = (
            "Confirm property is ready. Check open maintenance tasks. "
            "Send readiness message to staff."
        )
    else:
        instruction = "Determine what operational actions are needed and execute them."

    user_message = f"Operations event: {json.dumps(payload)}\n\n{instruction}"

    try:
        agent  = OperationsAgent(
            webhotelier_url=_WH_URL, whatsapp_url=_WA_URL, epsilonnet_url=_EN_URL
        )
        result = await agent.run(
            user_message,
            workflow_id=state["workflow_id"],
            property_id=property_id,
            db_pool=db_pool,
            memory=memory,
        )
        return {"logs": [result]}
    except Exception as e:
        log.error("operations_agent_node_failed", error=str(e), workflow_id=state["workflow_id"])
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
