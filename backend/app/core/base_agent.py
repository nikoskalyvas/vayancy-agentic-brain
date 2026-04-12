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

MAX_ITERATIONS = 12

# Fix 8: simplified — no phone group, just the preference
_PROFILE_RE  = re.compile(r"PROFILE_UPDATE:\s*(.+)", re.IGNORECASE)
_ESCALATE_RE = re.compile(r"ESCALATE:\s*(.+)",       re.IGNORECASE)
_UPSELL_RE   = re.compile(r"UPSELL_SENT:\s*(.+)",    re.IGNORECASE)

# Per-property pg_notify channel — Fix 2/3: SSE isolation
def _notify_channel(property_id: str) -> str:
    return f"workflow_updates_{property_id}"


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

            for _ in range(MAX_ITERATIONS):
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

                        result_text = await mcp.call_tool(block.name, block.input)
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
