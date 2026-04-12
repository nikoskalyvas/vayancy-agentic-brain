"""
Supervisor — fixes applied:

Fix 2:  pg_notify uses per-property channel workflow_updates_{property_id}.
Fix 7:  parallel=false no longer silently drops agents after the first.
        All returned agents are always dispatched — the graph only supports
        parallel Send() anyway. The `parallel` field is removed from the
        routing schema to eliminate the ambiguity entirely.
Fix 11: db_pool and memory threaded from config into every agent.run() call.
"""
from __future__ import annotations
import json
from datetime import datetime
from typing import Annotated, Any, TypedDict, Literal, List
import operator

import asyncpg
import anthropic
import structlog
from langgraph.graph import StateGraph, START, END
from langgraph.types import Send, RunnableConfig
from pydantic import BaseModel

from app.config import settings
from app.core.memory import AgentMemory
from app.core.security import aidefence_guard
from app.core.guest_agent import GuestAgent
from app.core.revenue_agent import RevenueAgent
from app.core.operations_agent import OperationsAgent

log = structlog.get_logger()

_llm = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)

_WH_URL = "http://mcp-webhotelier:3001/sse"
_WA_URL = "http://mcp-whatsapp:3002/sse"
_PL_URL = "http://mcp-pricelabs:3003/sse"
_EN_URL = "http://mcp-epsilonnet:3004/sse"


def _notify_channel(property_id: str) -> str:
    """Fix 2: per-property channel name prevents cross-tenant SSE leakage."""
    return f"workflow_updates_{property_id}"


# ─── State ────────────────────────────────────────────────────────────────────

class AgentState(TypedDict):
    workflow_id:    str
    event_id:       str
    property_id:    str
    payload:        dict[str, Any]
    memory_context: str
    guest_phone:    str | None
    agents_to_run:  list[str]
    results:        Annotated[list[dict], operator.add]
    retry_agents:   list[str]


# Fix 7: removed `parallel` field — all returned agents always dispatched
class RoutingDecision(BaseModel):
    agents: List[Literal["guest", "revenue", "operations"]]
    reason: str


# ─── Audit helper ─────────────────────────────────────────────────────────────

async def _audit(
    conn:        asyncpg.Connection,
    workflow_id: str,
    event_id:    str | None,
    property_id: str,
    agent:       str,
    action:      str,
    status:      str,
    details:     dict,
) -> None:
    row_id = await conn.fetchval(
        """
        INSERT INTO workflow_logs
            (id, workflow_id, event_id, property_id, agent, action, status, details)
        VALUES
            (gen_random_uuid()::text, $1, $2, $3, $4, $5, $6, $7)
        RETURNING id
        """,
        workflow_id, event_id, property_id, agent, action, status,
        json.dumps(details),
    )
    # Fix 2: per-property channel
    await conn.execute(
        "SELECT pg_notify($1, $2)",
        _notify_channel(property_id),
        json.dumps({
            "id":          row_id,
            "workflow_id": workflow_id,
            "property_id": property_id,
            "agent":       agent,
            "action":      action,
            "status":      status,
            "details":     details,
            "timestamp":   datetime.utcnow().isoformat(),
        }),
    )


# ─── Supervisor node ──────────────────────────────────────────────────────────

async def supervisor_node(state: AgentState, config: RunnableConfig) -> dict:
    db:          asyncpg.Pool = config["configurable"]["db_pool"]
    memory:      AgentMemory  = config["configurable"]["memory"]
    property_id: str          = state["property_id"]
    event_id:    str          = state["event_id"]
    payload:     dict         = state["payload"]

    existing = await db.fetchval(
        "SELECT 1 FROM workflow_logs WHERE event_id = $1 LIMIT 1", event_id
    )
    if existing:
        log.info("idempotency_skip", event_id=event_id)
        return {"agents_to_run": [], "results": []}

    if not await aidefence_guard(json.dumps(payload)):
        log.warning("guard_blocked", event_id=event_id)
        return {"agents_to_run": [], "results": []}

    guest_phone = payload.get("guest_phone") or payload.get("from")

    past = await memory.retrieve_relevant(
        json.dumps(payload), property_id=property_id, guest_phone=guest_phone
    )
    memory_context = "\n".join(r["content"] for r in past) if past else ""

    profile_summary = ""
    if guest_phone:
        profile = await memory.get_guest_profile(guest_phone, property_id)
        if profile:
            profile_summary = (
                f"Guest: {profile.get('name')} | "
                f"Language: {profile.get('language')} | "
                f"Preferences: {profile.get('preferences') or 'none'}"
            )

    try:
        response = await _llm.messages.create(
            model=settings.anthropic_model,
            max_tokens=512,
            system=(
                "You are the Cognitive Core of Vayancy — a luxury villa management AI.\n"
                "Return ALL agents needed for this event. They all run in parallel.\n"
                "Routing rules:\n"
                "- booking.confirmed                → [guest, operations, revenue]\n"
                "- booking.modified / cancelled     → [guest]\n"
                "- guest.checkout                   → [operations]\n"
                "- guest.checkin                    → [operations, guest]\n"
                "- whatsapp.message                 → [guest]\n"
                "- scheduled.pricing_review         → [revenue]\n"
                "- scheduled.checkout_dispatch      → [operations]"
            ),
            tools=[{
                "name": "route_workflow",
                "description": "Return the list of agents to dispatch.",
                "input_schema": RoutingDecision.model_json_schema(),
            }],
            tool_choice={"type": "tool", "name": "route_workflow"},
            messages=[{
                "role": "user",
                "content": (
                    f"Event: {json.dumps(payload)}\n"
                    f"Guest: {profile_summary or 'unknown'}\n"
                    f"Context:\n{memory_context or 'none'}"
                ),
            }],
        )
    except Exception as e:
        log.error("supervisor_llm_failed", error=str(e))
        return {
            "agents_to_run": ["guest"],
            "memory_context": memory_context,
            "guest_phone":   guest_phone,
            "retry_agents":  [],
        }

    tool_block = next(
        (b for b in response.content if b.type == "tool_use"), None
    )
    if not tool_block:
        return {
            "agents_to_run": ["guest"],
            "memory_context": memory_context,
            "guest_phone":   guest_phone,
            "retry_agents":  [],
        }

    decision = RoutingDecision.model_validate(tool_block.input)
    # Fix 7: always use ALL returned agents — no silent drops
    agents = list(decision.agents)

    async with db.acquire() as conn:
        await _audit(
            conn, state["workflow_id"], event_id, property_id,
            "supervisor", "routing", "completed",
            {"agents": agents, "reason": decision.reason},
        )

    log.info("supervisor_routed", agents=agents,
             workflow_id=state["workflow_id"], property_id=property_id)

    return {
        "agents_to_run":  agents,
        "memory_context": memory_context,
        "guest_phone":    guest_phone,
        "retry_agents":   [],
    }


# ─── Re-evaluation ────────────────────────────────────────────────────────────

async def re_evaluate_node(state: AgentState, config: RunnableConfig) -> dict:
    failed          = [r["agent"] for r in state.get("results", [])
                       if not r.get("success", True)]
    already_retried = state.get("retry_agents", [])
    to_retry        = [a for a in failed if a not in already_retried]

    if to_retry:
        log.warning("re_evaluating_failed_agents", agents=to_retry)
        return {"agents_to_run": to_retry,
                "retry_agents":  already_retried + to_retry}
    return {"agents_to_run": []}


def should_re_evaluate(state: AgentState) -> str:
    failed          = [r["agent"] for r in state.get("results", [])
                       if not r.get("success", True)]
    already_retried = state.get("retry_agents", [])
    if any(a not in already_retried for a in failed):
        return "re_evaluate"
    return END


def route_to_agents(state: AgentState) -> list[Send] | str:
    agents = state.get("agents_to_run", [])
    if not agents:
        return END
    return [Send(agent, state) for agent in agents]


# ─── Agent nodes — Fix 11: pass db_pool + memory into agent.run() ─────────────

async def guest_agent_node(state: AgentState, config: RunnableConfig) -> dict:
    db_pool     = config["configurable"]["db_pool"]
    memory      = config["configurable"]["memory"]
    payload     = state["payload"]
    property_id = state["property_id"]
    guest_phone = state.get("guest_phone")
    memory_ctx  = state.get("memory_context", "")

    user_message = (
        f"Event: {payload.get('event', 'unknown')}\n"
        f"Reservation ID: {payload.get('reservation_id', 'N/A')}\n"
        f"Full payload: {json.dumps(payload)}\n\n"
        f"Guest interaction history:\n{memory_ctx or 'No prior history.'}\n\n"
        "Handle this event as the Vayancy luxury concierge."
    )
    agent  = GuestAgent(webhotelier_url=_WH_URL, whatsapp_url=_WA_URL)
    result = await agent.run(
        user_message,
        workflow_id=state["workflow_id"],
        property_id=property_id,
        db_pool=db_pool,
        memory=memory,
        guest_phone=guest_phone,
    )
    return {"results": [result]}


async def revenue_agent_node(state: AgentState, config: RunnableConfig) -> dict:
    db_pool     = config["configurable"]["db_pool"]
    memory      = config["configurable"]["memory"]
    payload     = state["payload"]
    property_id = state["property_id"]

    user_message = (
        f"Booking event: {json.dumps(payload)}\n\n"
        "Review current pricing and occupancy. Adjust rates for the next "
        "60 days where warranted. Always dry_run=True first. "
        "Provide a clear PRICING SUMMARY at the end."
    )
    agent  = RevenueAgent(webhotelier_url=_WH_URL, pricelabs_url=_PL_URL)
    result = await agent.run(
        user_message,
        workflow_id=state["workflow_id"],
        property_id=property_id,
        db_pool=db_pool,
        memory=memory,
    )
    return {"results": [result]}


async def operations_agent_node(state: AgentState, config: RunnableConfig) -> dict:
    db_pool     = config["configurable"]["db_pool"]
    memory      = config["configurable"]["memory"]
    payload     = state["payload"]
    property_id = state["property_id"]
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
    return {"results": [result]}


# ─── Graph ────────────────────────────────────────────────────────────────────

def build_graph() -> Any:
    graph = StateGraph(AgentState)

    graph.add_node("supervisor",  supervisor_node)
    graph.add_node("re_evaluate", re_evaluate_node)
    graph.add_node("guest",       guest_agent_node)
    graph.add_node("revenue",     revenue_agent_node)
    graph.add_node("operations",  operations_agent_node)

    graph.add_edge(START, "supervisor")
    graph.add_conditional_edges("supervisor", route_to_agents)

    for node in ("guest", "revenue", "operations"):
        graph.add_conditional_edges(
            node, should_re_evaluate,
            {"re_evaluate": "re_evaluate", END: END},
        )

    graph.add_conditional_edges("re_evaluate", route_to_agents)
    return graph.compile()


agentic_brain = build_graph()
