"""
Vayancy Insights Agent — backend/app/core/insights_agent.py

Stage 4 feature: text-to-SQL analyst agent.

The owner asks a natural language question:
  "Why did revenue drop last week?"
  "Which guests have stayed more than once?"
  "What is our average ADR in August vs September?"

The agent:
  1. Receives the question + full DB schema context
  2. Generates a safe, read-only SQL query
  3. Validates it (SELECT-only, no DDL/DML, row limit enforced)
  4. Executes it against the Vayancy Postgres DB
  5. Generates a plain-language narrative answer
  6. Returns question + sql + results + narrative + execution time

Security model:
  - Only SELECT statements are permitted
  - Hard row limit of 500 rows on all queries
  - Query timeout of 10 seconds
  - No access to: reasoning_bank embeddings, raw passwords, API keys
  - Columns with sensitive names are blocked (embedding, api_key, pms_config)
"""
from __future__ import annotations

import json
import re
import time
import uuid
from typing import Any

import anthropic
import asyncpg
import structlog

from app.config import settings

log = structlog.get_logger()

# ── Schema context fed to the LLM ────────────────────────────────────────────
# A concise, human-readable description of the schema.
# Updated manually when tables change. Embeddings and PII columns omitted.

SCHEMA_CONTEXT = """
Available tables in the Vayancy database (read-only access):

workflow_logs (id, workflow_id, event_id, property_id, agent, action, status, details JSONB, timestamp)
  — Every agent action ever taken. Use for: audit trail, agent performance, what happened when.

guests (id, property_id, phone, name, email, language, nationality, preferences, upsell_history JSONB, created_at, updated_at)
  — Guest profiles. Use for: repeat guest analysis, language distribution, preference trends.

guest_interactions (id, property_id, guest_phone, reservation_id, direction[inbound/outbound], channel, content, created_at)
  — Every WhatsApp message in/out. Use for: response time analysis, topic frequency, volume trends.
  NOTE: Do NOT select the embedding column — it contains raw vectors.

pricing_decisions (id, workflow_id, property_id, date_from, date_to, rates_applied JSONB, rationale, created_at)
  — Every rate change made by the Revenue Agent. Use for: pricing history, ADR trends, rationale audit.

escalations (id, property_id, workflow_id, guest_phone, reason, owner_notified, resolved, created_at)
  — Guest issues escalated to owner. Use for: issue frequency, resolution rate, problem patterns.

travelos_bookings (id, tenant_id, pms_booking_id, unit_id, check_in, check_out, guests INT, guest_name, guest_email, status, channel, commission_pct, created_at)
  — TravelOS direct bookings. Use for: booking volume, commission saved, channel performance.

travelos_properties (id, tenant_id, pms_unit_id, name, location, region, max_guests, bedrooms, bathrooms, amenities TEXT[], base_rate, currency, min_stay, active, created_at)
  — Property catalog. Use for: portfolio overview, amenity distribution, pricing benchmarks.

extension_property_metrics (property_id, category, data JSONB, updated_at)
  — Latest BDC metrics from Chrome Extension. Categories: analytics, ranking, promotions, rates, reviews, finance.
  — data JSONB shape varies by category. Example: data->>'adr', data->>'rank_position', data->>'occupancy_rate'

extension_snapshots (id, property_id, category, data JSONB, captured_at)
  — Historical BDC snapshots. Use for: ranking trends, ADR history, promotion performance over time.

hitl_decisions (id, property_id, workflow_id, agent, action_type, proposed_action JSONB, impact_summary, status[pending/approved/rejected], decision_note, created_at, decided_at)
  — Human-in-the-loop decisions. Use for: owner approval history, revenue decision audit.

Common query patterns:
  - Revenue last 30 days: SELECT date_trunc('day', created_at), COUNT(*) FROM travelos_bookings WHERE created_at > NOW()-INTERVAL '30 days' GROUP BY 1
  - Agent activity: SELECT agent, action, COUNT(*) FROM workflow_logs WHERE property_id=$X GROUP BY 1,2
  - Ranking trend: SELECT captured_at, data->>'rank_position' FROM extension_snapshots WHERE property_id=$X AND category='ranking' ORDER BY captured_at
  - Repeat guests: SELECT guest_email, COUNT(*) cnt FROM travelos_bookings GROUP BY guest_email HAVING COUNT(*)>1
"""

# ── SQL safety guard ──────────────────────────────────────────────────────────

_BLOCKED_KEYWORDS = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|TRUNCATE|GRANT|REVOKE"
    r"|EXECUTE|EXEC|COPY|VACUUM|ANALYZE|EXPLAIN\s+ANALYZE)\b",
    re.IGNORECASE,
)

_BLOCKED_COLUMNS = re.compile(
    r"\b(embedding|api_key|pms_config|password|secret)\b",
    re.IGNORECASE,
)

_MAX_ROWS    = 500
_QUERY_TIMEOUT = 10  # seconds


def _validate_sql(sql: str) -> str | None:
    """
    Returns None if safe, or an error string explaining why it was blocked.
    Enforces SELECT-only, blocks sensitive column access.
    """
    stripped = sql.strip()

    if not stripped.upper().startswith("SELECT"):
        return "Only SELECT statements are permitted."

    if _BLOCKED_KEYWORDS.search(stripped):
        m = _BLOCKED_KEYWORDS.search(stripped)
        return f"Blocked keyword detected: {m.group(0)}"

    if _BLOCKED_COLUMNS.search(stripped):
        m = _BLOCKED_COLUMNS.search(stripped)
        return f"Access to column '{m.group(0)}' is not permitted."

    return None


def _inject_limit(sql: str, max_rows: int = _MAX_ROWS) -> str:
    """
    Append LIMIT if not already present. Prevents unbounded result sets.
    """
    stripped = sql.strip().rstrip(";")
    if not re.search(r"\bLIMIT\b", stripped, re.IGNORECASE):
        return f"{stripped} LIMIT {max_rows}"
    return stripped


# Tables that carry property_id — queries against these without a property_id
# filter would expose every tenant's data to whoever calls /insights/query.
_PROPERTY_SCOPED_TABLES = {
    "workflow_logs", "guests", "guest_interactions", "pricing_decisions",
    "escalations", "hitl_decisions", "insights_queries",
    "extension_snapshots", "extension_property_metrics",
    "registered_properties",
}


def _enforce_property_scope(sql: str, property_id: str) -> tuple[str, str | None]:
    """
    Fix #4: ensure queries against property-scoped tables include a
    property_id filter. Without this, an authenticated caller could craft
    a question that exfiltrates data from other properties.

    Strategy: wrap the LLM-generated query as a subquery and apply the
    property_id filter at the outer level for tables that carry that column.
    If no property-scoped table is referenced, return SQL unchanged.

    Returns (final_sql, warning_message | None).
    """
    lower = sql.lower()
    referenced = [t for t in _PROPERTY_SCOPED_TABLES if t in lower]
    if not referenced:
        return sql, None

    # Check if any property_id filter already exists
    has_filter = bool(
        re.search(r"property_id", sql, re.IGNORECASE)
        and re.search(r"where", sql, re.IGNORECASE)
    )

    if has_filter:
        return sql, None

    # No property_id filter found — wrap to enforce tenant isolation
    # Wrap as subquery so LIMIT/ORDER BY from original query still work
    scoped = (
        f"SELECT * FROM ({sql.rstrip(';')}) AS _scoped "
        f"WHERE _scoped.property_id = '{property_id}'"
    )
    warning = (
        f"Automatically scoped to property_id='{property_id}'. "
        f"Tables referenced: {', '.join(referenced)}"
    )
    return scoped, warning


# ── Insights Agent ────────────────────────────────────────────────────────────

class InsightsAgent:
    """
    Stateless analyst. Each call is independent.
    Does not extend BaseAgent — it uses a different flow
    (no MCP, no tool loop — pure text-to-SQL execution).
    """

    SYSTEM_PROMPT = f"""
You are the Vayancy Insights Analyst — a data analyst embedded in a luxury
villa management system. Your job is to answer questions about property
performance by writing and explaining SQL queries.

## Database schema
{SCHEMA_CONTEXT}

## Your task
Given a natural language question, produce:
1. A valid, read-only PostgreSQL SELECT query that answers it
2. A plain-language narrative answer (2-5 sentences) interpreting the results
   as if explaining to a non-technical property owner

## Rules
- Only write SELECT statements. Never write INSERT, UPDATE, DELETE, DROP etc.
- Never access columns named: embedding, api_key, pms_config, password, secret
- Always include property_id = '<PROPERTY_ID>' in WHERE clauses when the table
  has a property_id column, to scope results to the owner's properties only
- Keep queries efficient — avoid SELECT * on large tables
- If the question cannot be answered from the available schema, say so clearly

## Output format
Respond with a JSON object exactly like this (no markdown, no extra text):
{{
  "sql": "<your SELECT query here>",
  "explanation": "<what this query does in plain English>",
  "narrative_template": "<2-5 sentence answer template using {{result_summary}} placeholder>"
}}
"""

    def __init__(self, db_pool: asyncpg.Pool) -> None:
        self._db   = db_pool
        self._llm  = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)

    async def query(
        self,
        question: str,
        property_id: str,
        query_id: str | None = None,
    ) -> dict[str, Any]:
        """
        Answer a natural language question about property performance.

        Returns:
            question, sql, validation_error, rows, row_count,
            narrative, execution_ms, query_id, error
        """
        qid   = query_id or str(uuid.uuid4())
        start = time.monotonic()

        result: dict[str, Any] = {
            "query_id":        qid,
            "question":        question,
            "property_id":     property_id,
            "sql":             None,
            "validation_error": None,
            "rows":            [],
            "row_count":       0,
            "narrative":       None,
            "execution_ms":    0,
            "error":           None,
        }

        # ── Step 1: Generate SQL via LLM ─────────────────────────────────────
        system = self.SYSTEM_PROMPT.replace("<PROPERTY_ID>", property_id)
        try:
            response = await self._llm.messages.create(
                model=settings.anthropic_model,
                max_tokens=1024,
                system=system,
                messages=[{"role": "user", "content": question}],
            )
            raw = response.content[0].text.strip()
        except Exception as e:
            result["error"] = f"LLM error: {e}"
            result["execution_ms"] = int((time.monotonic() - start) * 1000)
            return result

        # ── Step 2: Parse JSON response ───────────────────────────────────────
        try:
            # Strip possible markdown fences
            clean = re.sub(r"^```(?:json)?\s*|```$", "", raw, flags=re.MULTILINE).strip()
            parsed = json.loads(clean)
            sql                = parsed.get("sql", "").strip()
            narrative_template = parsed.get("narrative_template", "")
        except (json.JSONDecodeError, KeyError) as e:
            result["error"] = f"Could not parse LLM response as JSON: {e}. Raw: {raw[:200]}"
            result["execution_ms"] = int((time.monotonic() - start) * 1000)
            return result

        result["sql"] = sql

        # ── Step 3: Validate SQL ──────────────────────────────────────────────
        validation_err = _validate_sql(sql)
        if validation_err:
            result["validation_error"] = validation_err
            result["error"]            = f"SQL validation failed: {validation_err}"
            log.warning("insights_sql_blocked", reason=validation_err,
                        property_id=property_id, question=question[:80])
            result["execution_ms"] = int((time.monotonic() - start) * 1000)
            return result

        safe_sql = _inject_limit(sql)

        # Fix #4: enforce property_id scoping before execution.
        # Prevents cross-tenant data leakage from queries that omit the filter.
        safe_sql, scope_warning = _enforce_property_scope(safe_sql, property_id)
        if scope_warning:
            log.info("insights_property_scope_enforced",
                     property_id=property_id, warning=scope_warning)
            result["scope_warning"] = scope_warning

        # ── Step 4: Execute ───────────────────────────────────────────────────
        try:
            async with self._db.acquire() as conn:
                rows = await conn.fetch(safe_sql, timeout=_QUERY_TIMEOUT)
            rows_list = [dict(r) for r in rows]
            result["rows"]      = rows_list
            result["row_count"] = len(rows_list)
        except asyncpg.exceptions.PostgresError as e:
            result["error"] = f"Query error: {e}"
            result["execution_ms"] = int((time.monotonic() - start) * 1000)
            log.error("insights_query_failed", error=str(e), property_id=property_id)
            return result
        except Exception as e:
            result["error"] = f"Execution error: {e}"
            result["execution_ms"] = int((time.monotonic() - start) * 1000)
            return result

        # ── Step 5: Generate narrative ────────────────────────────────────────
        try:
            # Summarise results for narrative generation
            if rows_list:
                result_summary = json.dumps(rows_list[:10], default=str)
                if len(rows_list) > 10:
                    result_summary += f" ... ({len(rows_list)} total rows)"
            else:
                result_summary = "No results found."

            narrative_prompt = (
                f"Question: {question}\n\n"
                f"SQL result ({len(rows_list)} rows):\n{result_summary}\n\n"
                f"Write a 2-5 sentence plain-language answer for a property owner. "
                f"Be specific with numbers. Do not mention SQL."
            )

            narrative_resp = await self._llm.messages.create(
                model=settings.anthropic_model,
                max_tokens=400,
                system=(
                    "You are a hospitality analyst explaining data to a property owner. "
                    "Be concise, specific, and use actual numbers from the results."
                ),
                messages=[{"role": "user", "content": narrative_prompt}],
            )
            result["narrative"] = narrative_resp.content[0].text.strip()
        except Exception as e:
            # Narrative failure is non-fatal — return data without it
            log.warning("insights_narrative_failed", error=str(e))
            result["narrative"] = "Data retrieved successfully. See results for details."

        result["execution_ms"] = int((time.monotonic() - start) * 1000)
        log.info(
            "insights_query_complete",
            query_id=qid,
            row_count=result["row_count"],
            execution_ms=result["execution_ms"],
            property_id=property_id,
        )
        return result
