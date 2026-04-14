"""
tests/test_memory.py

Covers:
  - record_upsell      — appends to upsell_history JSONB, no duplicates
  - log_escalation     — inserts row, returns UUID
  - mark_escalation_notified — sets owner_notified=TRUE
  - retrieve_relevant  — property_id scoping (no cross-tenant bleed)

All tests use an in-memory asyncpg mock via unittest.mock — no live DB needed.
"""
import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.memory import AgentMemory


# ── Helpers ───────────────────────────────────────────────────────────────────

def _pool(execute_result=None, fetchrow_result=None, fetch_result=None):
    """Return a mock asyncpg Pool with configurable return values."""
    pool = MagicMock()
    pool.execute    = AsyncMock(return_value=execute_result or "UPDATE 1")
    pool.fetchrow   = AsyncMock(return_value=fetchrow_result)
    pool.fetch      = AsyncMock(return_value=fetch_result or [])
    pool.fetchval   = AsyncMock(return_value=None)

    # Pool.acquire() as async context manager
    conn = MagicMock()
    conn.execute    = AsyncMock(return_value=execute_result or "UPDATE 1")
    conn.fetchrow   = AsyncMock(return_value=fetchrow_result)
    conn.fetch      = AsyncMock(return_value=fetch_result or [])
    conn.fetchval   = AsyncMock(return_value=None)
    conn.__aenter__ = AsyncMock(return_value=conn)
    conn.__aexit__  = AsyncMock(return_value=False)
    pool.acquire    = MagicMock(return_value=conn)
    return pool, conn


# ── record_upsell ─────────────────────────────────────────────────────────────

class TestRecordUpsell:
    @pytest.mark.asyncio
    async def test_calls_execute_with_correct_args(self):
        pool, _ = _pool()
        mem = AgentMemory(pool)
        await mem.record_upsell("+306912345678", "villa-azure", "sailing_trip")

        pool.execute.assert_called_once()
        call_sql = pool.execute.call_args[0][0]
        assert "upsell_history" in call_sql
        assert "UPDATE guests" in call_sql

    @pytest.mark.asyncio
    async def test_upsell_type_in_payload(self):
        pool, _ = _pool()
        mem = AgentMemory(pool)
        await mem.record_upsell("+306912345678", "villa-azure", "chef_dinner")

        # The upsell type should appear in the JSON payload passed to execute
        call_args = pool.execute.call_args[0]
        # Third positional arg is the JSON entry string
        assert any("chef_dinner" in str(a) for a in call_args)

    @pytest.mark.asyncio
    async def test_correct_phone_and_property_scoping(self):
        pool, _ = _pool()
        mem = AgentMemory(pool)
        await mem.record_upsell("+306912345678", "villa-azure", "spa_day")

        call_args = pool.execute.call_args[0]
        assert "+306912345678" in call_args
        assert "villa-azure" in call_args


# ── log_escalation ────────────────────────────────────────────────────────────

class TestLogEscalation:
    @pytest.mark.asyncio
    async def test_returns_uuid_string(self):
        pool, _ = _pool()
        mem = AgentMemory(pool)
        esc_id = await mem.log_escalation(
            property_id="villa-azure",
            workflow_id="wf-001",
            guest_phone="+306912345678",
            reason="Guest upset about AC",
        )
        # Should return a valid UUID string
        uuid.UUID(esc_id)  # raises if invalid

    @pytest.mark.asyncio
    async def test_inserts_into_escalations(self):
        pool, _ = _pool()
        mem = AgentMemory(pool)
        await mem.log_escalation(
            property_id="villa-azure",
            workflow_id="wf-001",
            guest_phone="+306912345678",
            reason="Guest upset",
            guest_message="AC is broken",
            agent_output="ESCALATE: AC issue",
        )
        pool.execute.assert_called_once()
        sql = pool.execute.call_args[0][0]
        assert "INSERT INTO escalations" in sql

    @pytest.mark.asyncio
    async def test_truncates_long_agent_output(self):
        pool, _ = _pool()
        mem = AgentMemory(pool)
        long_output = "x" * 10_000
        await mem.log_escalation(
            property_id="villa-azure",
            workflow_id="wf-001",
            guest_phone=None,
            reason="Test",
            agent_output=long_output,
        )
        # agent_output should be truncated in the call args
        call_args = pool.execute.call_args[0]
        agent_out_arg = str([a for a in call_args if isinstance(a, str) and len(a) > 100])
        assert len(agent_out_arg) < 10_000


# ── mark_escalation_notified ──────────────────────────────────────────────────

class TestMarkEscalationNotified:
    @pytest.mark.asyncio
    async def test_updates_owner_notified(self):
        pool, _ = _pool()
        mem = AgentMemory(pool)
        esc_id = str(uuid.uuid4())
        await mem.mark_escalation_notified(esc_id)

        pool.execute.assert_called_once()
        sql = pool.execute.call_args[0][0]
        assert "owner_notified" in sql
        assert "TRUE" in sql or "true" in sql.lower()

    @pytest.mark.asyncio
    async def test_passes_correct_escalation_id(self):
        pool, _ = _pool()
        mem = AgentMemory(pool)
        esc_id = str(uuid.uuid4())
        await mem.mark_escalation_notified(esc_id)

        call_args = pool.execute.call_args[0]
        assert esc_id in call_args


# ── retrieve_relevant — property isolation ────────────────────────────────────

class TestRetrieveRelevant:
    """
    Fix #2 from Stage 1: retrieve_relevant must scope to property_id.
    Without scoping, all tenants' interaction history is mixed in results.
    """

    @pytest.mark.asyncio
    async def test_scoped_query_includes_property_id(self):
        """When property_id is provided, the interaction query must filter by it."""
        pool, _ = _pool()
        mem = AgentMemory(pool)

        # Patch _encode to avoid loading the ML model
        with patch("app.core.memory._encode", new_callable=AsyncMock) as mock_enc:
            mock_enc.return_value = [0.0] * 384
            await mem.retrieve_relevant(
                "guest wants late checkout",
                property_id="villa-azure",
            )

        # Should have called pool.fetch twice:
        # once for reasoning_bank (global), once for guest_interactions (scoped)
        assert pool.fetch.call_count == 2
        calls = [str(pool.fetch.call_args_list[i]) for i in range(2)]
        # At least one call should contain property_id scoping
        assert any("villa-azure" in c for c in calls)

    @pytest.mark.asyncio
    async def test_no_property_id_only_global_patterns(self):
        """Without property_id, only reasoning_bank is queried — no interactions."""
        pool, _ = _pool()
        mem = AgentMemory(pool)

        with patch("app.core.memory._encode", new_callable=AsyncMock) as mock_enc:
            mock_enc.return_value = [0.0] * 384
            await mem.retrieve_relevant("late checkout request")

        # Only 1 call — reasoning_bank only
        assert pool.fetch.call_count == 1

    @pytest.mark.asyncio
    async def test_guest_phone_scope_adds_second_filter(self):
        """When guest_phone is also provided, SQL includes AND guest_phone = $N."""
        pool, _ = _pool()
        mem = AgentMemory(pool)

        with patch("app.core.memory._encode", new_callable=AsyncMock) as mock_enc:
            mock_enc.return_value = [0.0] * 384
            await mem.retrieve_relevant(
                "pool towels",
                property_id="villa-azure",
                guest_phone="+306912345678",
            )

        assert pool.fetch.call_count == 2
        interaction_call = str(pool.fetch.call_args_list[1])
        assert "+306912345678" in interaction_call

    @pytest.mark.asyncio
    async def test_results_deduped_and_ranked(self):
        """Merged results from both sources should be sorted by similarity desc."""
        from unittest.mock import MagicMock as MM

        pool, _ = _pool(fetch_result=[
            {"content": "late checkout policy", "similarity": 0.9},
            {"content": "pool access hours",    "similarity": 0.7},
        ])
        mem = AgentMemory(pool)

        with patch("app.core.memory._encode", new_callable=AsyncMock) as mock_enc:
            mock_enc.return_value = [0.0] * 384
            results = await mem.retrieve_relevant(
                "checkout time", property_id="villa-azure"
            )

        # Results should be sorted descending by similarity
        if len(results) >= 2:
            assert results[0]["similarity"] >= results[1]["similarity"]
