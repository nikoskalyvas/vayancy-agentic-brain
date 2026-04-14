"""
BaseAgent — fixes applied:

Fix 4:  Escalation now uses memory.log_escalation() + memory.mark_escalation_notified()
        instead of a direct INSERT INTO escalations (duplicate code path removed).
Fix 8:  PROFILE_UPDATE regex simplified — no phone extraction needed,
        guest_phone is already known.  Pattern: "PROFILE_UPDATE: <preference>"
Fix 9:  Upsell SQL replaced with memory.record_upsell() — single code path.
Fix 11: db_pool and memory injected as parameters instead of re-calling get_pool().
"""
from __future__ import annotations
import hashlib
import json
import re
import time
from abc import ABC, abstractmethod
from typing import Any

import anthropic
import asyncpg
import structlog

from app.config import settings
from app.core.mcp_client import MultiMCPClient

log = structlog.get_logger()

MAX_ITERATIONS       = 12
AGENT_TIMEOUT_SECONDS = 60  # Fix #5: wall-clock limit per agent run

# Fix 8: simplified — no phone group, just the preference
_PROFILE_RE  = re.compile(r"PROFILE_UPDATE:\s*(.+)",  re.IGNORECASE)
_ESCALATE_RE = re.compile(r"ESCALATE:\s*(.+)",        re.IGNORECASE)
_UPSELL_RE   = re.compile(r"UPSELL_SENT:\s*(.+)",     re.IGNORECASE)
_HITL_RE     = re.compile(r"HITL_REQUIRED:\s*(.+)",   re.IGNORECASE)

# Per-property pg_notify channel — Fix 2/3: SSE isolation
def _notify_channel(property_id: str) -> str:
    return f"workflow_updates_{property_id}"


# Tools whose calls must be deduplicated within a workflow run.
# Read-only tools (get_*, list_*, calculate_*) are intentionally excluded —
# dedup only matters for write operations that cause side effects.
_WRITE_TOOLS = {
    "create_folio",
    "issue_invoice",
    "push_rate_overrides",
    "update_base_price",
    "set_min_stay",
    "update_reservation",
    "send_text_message",
    "send_template_message",
}


def _tool_call_key(tool_name: str, tool_input: dict) -> str:
    """
    Deterministic hash key for a tool + arguments pair.
    Used to detect duplicate tool calls within the same workflow run.
    Sorted JSON ensures argument order doesn't create false misses.
    """
    payload = json.dumps({"t": tool_name, "i": tool_input}, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


async def fetch_extension_context(
    db_pool: "asyncpg.Pool",
    property_id: str,
) -> str:
    """
    Stage 4: Extension → Brain loop.

    Fetch the latest Booking.com metrics captured by the Chrome Extension
    and format them as a compact context string for the revenue agent.

    Returns empty string if no extension data exists yet (extension not
    installed or never synced) — agent runs normally without it.
    """
    try:
        rows = await db_pool.fetch(
            """
            SELECT category, data, updated_at
            FROM   extension_property_metrics
            WHERE  property_id = $1
            ORDER  BY category
            """,
            property_id,
        )
        if not rows:
            return ""

        import json as _json
        sections = ["## Live Booking.com data (from Chrome Extension)"]
        for row in rows:
            cat  = row["category"]
            data = _json.loads(row["data"]) if isinstance(row["data"], str) else dict(row["data"])
            updated = row["updated_at"].strftime("%Y-%m-%d %H:%M") if row["updated_at"] else "unknown"

            if cat == "analytics":
                adr = data.get("adr")
                occ = data.get("occupancy_rate")
                sections.append(
                    f"Analytics (as of {updated}): "
                    f"ADR={f'€{adr:.0f}' if adr else 'N/A'}, "
                    f"Occupancy={f'{occ*100:.1f}%' if occ else 'N/A'}"
                )
            elif cat == "ranking":
                pos   = data.get("rank_position")
                total = data.get("rank_total")
                ctr   = data.get("ctr_30d")
                score = data.get("page_score")
                sections.append(
                    f"BDC Ranking (as of {updated}): "
                    f"Position={f'{pos}/{total}' if pos else 'N/A'}, "
                    f"CTR={f'{ctr*100:.2f}%' if ctr else 'N/A'}, "
                    f"Page Score={score or 'N/A'}"
                )
            elif cat == "promotions":
                active = [k for k, v in data.items() if isinstance(v, dict) and v.get("active")]
                if active:
                    sections.append(
                        f"Active BDC Promotions: {', '.join(active)}"
                    )
            elif cat == "visibility":
                if data.get("booster_active"):
                    sections.append(
                        f"Visibility Booster: ACTIVE at {data.get('booster_percentage', '?')}%"
                    )

        return "\n".join(sections) if len(sections) > 1 else ""

    except Exception as e:
        import structlog as _sl
        _sl.get_logger().warning("extension_context_fetch_failed", error=str(e))
        return ""


class BaseAgent(ABC):

    name: str = "base"

    @property
    @abstractmethod
    def system_prompt(self) -> str: ...

    @property
    @abstractmethod
    def mcp_server_urls(self) -> list[str]: ...

    async def run(
        self,
        user_message: str,
        workflow_id:  str,
        property_id:  str,
        db_pool:      asyncpg.Pool,        # Fix 11: threaded in, not re-fetched
        memory:       Any,                 # AgentMemory — Any to avoid circular
        guest_phone:  str | None = None,
        skip_hitl:    bool = False,        # Fix #3: set True on HITL re-enqueue
    ) -> dict[str, Any]:
        from app.core.escalation import notify_owner

        client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
        start  = time.monotonic()
        channel = _notify_channel(property_id)

        # ── Inject guest profile into system prompt ───────────────────────────
        system = self.system_prompt
        if guest_phone:
            async with db_pool.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    SELECT name, language, preferences, upsell_history
                    FROM guests
                    WHERE property_id = $1 AND phone = $2
                    """,
                    property_id, guest_phone,
                )
            if row:
                already_offered = [
                    item["type"] for item in (row["upsell_history"] or [])
                ]
                system += (
                    f"\n\n## Current guest profile"
                    f"\nName: {row['name']}"
                    f"\nLanguage: {row['language']}"
                    f"\nPreferences: {row['preferences'] or 'None recorded yet'}"
                    f"\nUpsells already offered (DO NOT repeat): "
                    f"{', '.join(already_offered) or 'none'}"
                )

        # ── Agentic loop ──────────────────────────────────────────────────────
        async with MultiMCPClient(self.mcp_server_urls) as mcp:
            tools    = await mcp.list_tools()
            messages: list[dict[str, Any]] = [
                {"role": "user", "content": user_message}
            ]
            final_text:     str  = ""
            tool_calls_log: list = []
            success:        bool = True
            error_msg:      str | None = None
            # Stage 4: tool-call dedup — maps hash → cached result string
            # Prevents duplicate write operations (create_folio, push_rate_overrides etc.)
            # if the LLM retries the same call due to a transient timeout.
            _fired_calls: dict[str, str] = {}

            for _ in range(MAX_ITERATIONS):
                # Fix #5: wall-clock timeout — checked at the start of every
                # iteration. If an MCP server hangs mid-call and the LLM keeps
                # retrying, this ensures we return a partial result and mark
                # the job failed rather than spinning until ARQ's 300s limit
                # kills the process ungracefully.
                elapsed = time.monotonic() - start
                if elapsed > AGENT_TIMEOUT_SECONDS:
                    success   = False
                    error_msg = (
                        f"Agent timeout after {elapsed:.1f}s "
                        f"({len(tool_calls_log)} tool calls completed). "
                        f"Returning partial result."
                    )
                    log.warning(
                        "agent_timeout",
                        agent=self.name,
                        elapsed_s=round(elapsed, 1),
                        timeout_s=AGENT_TIMEOUT_SECONDS,
                        tool_calls_completed=len(tool_calls_log),
                        workflow_id=workflow_id,
                    )
                    break

                try:
                    response = await client.messages.create(
                        model=settings.anthropic_model,
                        max_tokens=4096,
                        system=system,
                        tools=tools if tools else [],
                        messages=messages,
                    )
                except anthropic.APIError as e:
                    success   = False
                    error_msg = str(e)
                    log.error("llm_api_error", agent=self.name, error=error_msg)
                    break

                assistant_content = list(response.content)
                for block in assistant_content:
                    if block.type == "text":
                        final_text = block.text

                messages.append({"role": "assistant", "content": assistant_content})

                if response.stop_reason == "end_turn":
                    break

                if response.stop_reason == "tool_use":
                    tool_results = []
                    for block in assistant_content:
                        if block.type != "tool_use":
                            continue

                        # Stage 4: tool call dedup
                        # For write-side tools, check if we already fired this
                        # exact call in this workflow run. If yes, return the
                        # cached result — prevents duplicate tax folios,
                        # duplicate WhatsApp messages, and double rate pushes
                        # when the LLM retries after a transient timeout.
                        _call_key = _tool_call_key(block.name, block.input)
                        if block.name in _WRITE_TOOLS and _call_key in _fired_calls:
                            result_text = _fired_calls[_call_key]
                            log.warning(
                                "tool_call_deduplicated",
                                tool=block.name,
                                key=_call_key,
                                agent=self.name,
                                workflow_id=workflow_id,
                            )
                        else:
                            result_text = await mcp.call_tool(block.name, block.input)
                            if block.name in _WRITE_TOOLS:
                                _fired_calls[_call_key] = result_text

                        tool_calls_log.append({
                            "tool":   block.name,
                            "input":  block.input,
                            "output": result_text[:800],
                        })

                        detail = {
                            "tool":   block.name,
                            "input":  block.input,
                            "output": result_text[:400],
                        }
                        async with db_pool.acquire() as conn:
                            row_id = await conn.fetchval(
                                """
                                INSERT INTO workflow_logs
                                    (id, workflow_id, property_id, agent,
                                     action, status, details)
                                VALUES
                                    (gen_random_uuid()::text, $1, $2, $3,
                                     $4, 'completed', $5)
                                RETURNING id
                                """,
                                workflow_id, property_id, self.name,
                                f"tool:{block.name}", json.dumps(detail),
                            )
                            # Fix 2: per-property channel
                            await conn.execute(
                                "SELECT pg_notify($1, $2)",
                                channel,
                                json.dumps({
                                    "id":          row_id,
                                    "workflow_id": workflow_id,
                                    "property_id": property_id,
                                    "agent":       self.name,
                                    "action":      f"tool:{block.name}",
                                    "status":      "completed",
                                    "details":     detail,
                                }),
                            )

                        tool_results.append({
                            "type":        "tool_result",
                            "tool_use_id": block.id,
                            "content":     result_text,
                        })

                    messages.append({"role": "user", "content": tool_results})
                else:
                    break

        # ── Parse directives ──────────────────────────────────────────────────

        # Fix 8: simplified regex — use known guest_phone, no phone extraction
        if final_text and guest_phone:
            for match in _PROFILE_RE.finditer(final_text):
                pref = match.group(1).strip()
                async with db_pool.acquire() as conn:
                    await conn.execute(
                        """
                        UPDATE guests SET preferences = $1, updated_at = NOW()
                        WHERE property_id = $2 AND phone = $3
                        """,
                        pref, property_id, guest_phone,
                    )
                log.info("profile_updated", phone=guest_phone)

        # Fix 9: use memory.record_upsell() — single code path, no inline SQL
        if final_text and guest_phone:
            for match in _UPSELL_RE.finditer(final_text):
                upsell_type = match.group(1).strip()
                await memory.record_upsell(guest_phone, property_id, upsell_type)
                log.info("upsell_recorded", type=upsell_type, phone=guest_phone)

        # Stage 4: HITL — revenue decisions requiring owner approval
        # Intercept HITL_REQUIRED directive before executing any high-impact action.
        # The proposed action is stored in hitl_decisions as 'pending'.
        # It will be re-enqueued by POST /hitl/{id}/approve when owner approves.
        # Fix #3: skip_hitl=True is set when re-enqueueing an approved decision.
        # Without this guard, the revenue agent re-evaluates the same action,
        # hits the threshold again, and emits HITL_REQUIRED — infinite loop.
        if final_text and self.name == "revenue" and not skip_hitl:
            hitl_match = _HITL_RE.search(final_text)
            if hitl_match:
                import json as _json
                hitl_raw = hitl_match.group(1).strip()
                try:
                    hitl_data = _json.loads(hitl_raw)
                except Exception:
                    hitl_data = {"raw": hitl_raw}

                await db_pool.execute(
                    """
                    INSERT INTO hitl_decisions
                        (property_id, workflow_id, agent, action_type,
                         proposed_action, impact_summary, status)
                    VALUES ($1, $2, $3, $4, $5, $6, 'pending')
                    """,
                    property_id,
                    workflow_id,
                    self.name,
                    hitl_data.get("action_type", "rate_change"),
                    _json.dumps(hitl_data.get("proposed_action", hitl_data)),
                    hitl_data.get("impact_summary", hitl_raw[:200]),
                )
                log.info(
                    "hitl_decision_created",
                    action_type=hitl_data.get("action_type"),
                    impact=hitl_data.get("impact_summary", "")[:80],
                    workflow_id=workflow_id,
                    property_id=property_id,
                )

        # Fix 4: use memory.log_escalation() + mark_escalation_notified()
        if final_text:
            esc_match = _ESCALATE_RE.search(final_text)
            if esc_match:
                reason    = esc_match.group(1).strip()
                guest_msg = None
                if guest_phone:
                    async with db_pool.acquire() as conn:
                        row = await conn.fetchrow(
                            """
                            SELECT content FROM guest_interactions
                            WHERE property_id = $1
                              AND guest_phone  = $2
                              AND direction    = 'inbound'
                            ORDER BY created_at DESC LIMIT 1
                            """,
                            property_id, guest_phone,
                        )
                    guest_msg = row["content"] if row else None

                # Fix 4: single canonical code path via AgentMemory
                esc_id = await memory.log_escalation(
                    property_id=property_id,
                    workflow_id=workflow_id,
                    guest_phone=guest_phone,
                    reason=reason,
                    guest_message=guest_msg,
                    agent_output=final_text[:2000],
                )
                sent = await notify_owner(
                    reason=reason,
                    guest_phone=guest_phone,
                    guest_message=guest_msg,
                    workflow_id=workflow_id,
                    property_id=property_id,
                )
                if sent:
                    await memory.mark_escalation_notified(esc_id)

                log.warning("escalation_triggered", reason=reason,
                            owner_notified=sent, workflow_id=workflow_id)

        duration_ms = int((time.monotonic() - start) * 1000)
        return {
            "agent":       self.name,
            "output":      final_text,
            "tool_calls":  tool_calls_log,
            "duration_ms": duration_ms,
            "success":     success,
            "error":       error_msg,
        }
