"""
tests/test_tool_dedup.py

Covers:
  - _tool_call_key  — deterministic hashing of tool + args
  - _WRITE_TOOLS    — correct membership
  - dedup behaviour — second identical write-tool call returns cache, not real call
  - read tools      — never deduplicated (always re-executed)

Stage 4 fix: prevents duplicate create_folio, push_rate_overrides,
             send_text_message on LLM retry after network timeout.
"""
import pytest

from app.core.base_agent import _WRITE_TOOLS, _tool_call_key


# ── _tool_call_key ────────────────────────────────────────────────────────────

class TestToolCallKey:
    def test_same_tool_same_args_same_key(self):
        k1 = _tool_call_key("create_folio", {"reservation_id": "R001", "amount": 300.0})
        k2 = _tool_call_key("create_folio", {"reservation_id": "R001", "amount": 300.0})
        assert k1 == k2

    def test_different_tool_different_key(self):
        k1 = _tool_call_key("create_folio",   {"reservation_id": "R001"})
        k2 = _tool_call_key("issue_invoice",  {"reservation_id": "R001"})
        assert k1 != k2

    def test_different_args_different_key(self):
        k1 = _tool_call_key("create_folio", {"reservation_id": "R001"})
        k2 = _tool_call_key("create_folio", {"reservation_id": "R002"})
        assert k1 != k2

    def test_arg_order_irrelevant(self):
        """Keys must be stable regardless of dict ordering."""
        k1 = _tool_call_key("create_folio", {"a": 1, "b": 2})
        k2 = _tool_call_key("create_folio", {"b": 2, "a": 1})
        assert k1 == k2

    def test_key_is_short_hex_string(self):
        k = _tool_call_key("create_folio", {"amount": 300})
        assert len(k) == 16
        int(k, 16)  # raises ValueError if not valid hex

    def test_empty_args_produces_key(self):
        k = _tool_call_key("get_folio", {})
        assert isinstance(k, str) and len(k) == 16


# ── _WRITE_TOOLS membership ───────────────────────────────────────────────────

class TestWriteTools:
    def test_create_folio_is_write(self):
        assert "create_folio" in _WRITE_TOOLS

    def test_push_rate_overrides_is_write(self):
        assert "push_rate_overrides" in _WRITE_TOOLS

    def test_send_text_message_is_write(self):
        assert "send_text_message" in _WRITE_TOOLS

    def test_send_template_message_is_write(self):
        assert "send_template_message" in _WRITE_TOOLS

    def test_issue_invoice_is_write(self):
        assert "issue_invoice" in _WRITE_TOOLS

    def test_update_base_price_is_write(self):
        assert "update_base_price" in _WRITE_TOOLS

    def test_update_reservation_is_write(self):
        assert "update_reservation" in _WRITE_TOOLS

    def test_get_tools_not_in_write_set(self):
        """Read-only tools must NOT be in _WRITE_TOOLS — they should re-execute freely."""
        read_tools = [
            "get_reservation", "list_reservations", "get_availability",
            "get_current_rates", "get_market_data", "get_folio",
            "get_message_thread", "get_occupancy_stats",
        ]
        for tool in read_tools:
            assert tool not in _WRITE_TOOLS, f"{tool} should not be in _WRITE_TOOLS"


# ── Dedup behaviour (unit-level simulation) ───────────────────────────────────

class TestDedupBehaviour:
    """
    Simulate the dedup logic from base_agent.py without running the full loop.
    Validates that write tools are only called once per unique (tool, args) pair.
    """

    def _run_dedup_sim(self, calls: list[tuple[str, dict]]) -> dict:
        """
        Simulate the _fired_calls dict from the agent loop.
        Returns: {"executed": [...], "deduplicated": [...]}
        """
        _fired_calls: dict[str, str] = {}
        executed     = []
        deduplicated = []

        for tool_name, tool_input in calls:
            key = _tool_call_key(tool_name, tool_input)
            if tool_name in _WRITE_TOOLS and key in _fired_calls:
                deduplicated.append((tool_name, tool_input))
            else:
                # "Execute" the call — record it
                result = f"result_{tool_name}_{key[:4]}"
                executed.append((tool_name, tool_input))
                if tool_name in _WRITE_TOOLS:
                    _fired_calls[key] = result

        return {"executed": executed, "deduplicated": deduplicated}

    def test_duplicate_write_tool_deduped(self):
        calls = [
            ("create_folio", {"reservation_id": "R001", "amount": 300.0}),
            ("create_folio", {"reservation_id": "R001", "amount": 300.0}),  # retry
        ]
        result = self._run_dedup_sim(calls)
        assert len(result["executed"])     == 1
        assert len(result["deduplicated"]) == 1

    def test_different_args_both_execute(self):
        calls = [
            ("create_folio", {"reservation_id": "R001", "amount": 300.0}),
            ("create_folio", {"reservation_id": "R002", "amount": 500.0}),
        ]
        result = self._run_dedup_sim(calls)
        assert len(result["executed"])     == 2
        assert len(result["deduplicated"]) == 0

    def test_read_tool_never_deduped(self):
        calls = [
            ("get_reservation", {"reservation_id": "R001"}),
            ("get_reservation", {"reservation_id": "R001"}),
        ]
        result = self._run_dedup_sim(calls)
        assert len(result["executed"])     == 2
        assert len(result["deduplicated"]) == 0

    def test_mixed_read_write_correct_dedup(self):
        calls = [
            ("get_availability",   {"date_from": "2026-05-01"}),  # read — always executes
            ("push_rate_overrides", {"rates": "[...]", "dry_run": False}),  # write
            ("get_availability",   {"date_from": "2026-05-01"}),  # read — executes again
            ("push_rate_overrides", {"rates": "[...]", "dry_run": False}),  # write — deduped
        ]
        result = self._run_dedup_sim(calls)
        assert len(result["executed"])     == 3  # 2 reads + 1 write
        assert len(result["deduplicated"]) == 1  # 1 duplicate write

    def test_triple_retry_only_one_execution(self):
        args = {"reservation_id": "R001", "amount": 300.0}
        calls = [("create_folio", args)] * 4  # 1 original + 3 retries
        result = self._run_dedup_sim(calls)
        assert len(result["executed"])     == 1
        assert len(result["deduplicated"]) == 3

    def test_dedup_resets_between_workflows(self):
        """Each workflow run starts with an empty _fired_calls dict."""
        args = {"reservation_id": "R001", "amount": 300.0}
        # Simulate workflow 1
        r1 = self._run_dedup_sim([("create_folio", args)])
        # Simulate workflow 2 (fresh _fired_calls)
        r2 = self._run_dedup_sim([("create_folio", args)])
        # Both should execute — different workflow runs
        assert len(r1["executed"]) == 1
        assert len(r2["executed"]) == 1
