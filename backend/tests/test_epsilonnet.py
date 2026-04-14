"""
tests/test_epsilonnet.py

Covers the amount validation guards added in Stage 4 completion (Fix #1).
Tests run against the validation logic only — no real Epsilon Net API calls.
The create_folio function is tested for its early-return validation paths
by mocking the _request function so the network is never hit.
"""
import json
from unittest.mock import AsyncMock, patch

import pytest


class TestFolioAmountValidation:
    """
    Test the three validation paths in create_folio:
      - total_amount <= 0   → validation_failed
      - total_amount < 1.0  → validation_failed (below floor)
      - total_amount > 50000 → validation_failed (above ceiling)
      - valid amount → proceeds to _request (mocked)
    """

    @pytest.mark.asyncio
    async def test_zero_amount_rejected(self):
        from app.mcp.epsilonnet import create_folio
        result = json.loads(await create_folio(
            reservation_id="R001", guest_name="Test Guest",
            guest_afm="000000000", checkin_date="2026-05-01",
            checkout_date="2026-05-05", total_amount=0.0,
        ))
        assert result["status"] == "validation_failed"
        assert "positive" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_negative_amount_rejected(self):
        from app.mcp.epsilonnet import create_folio
        result = json.loads(await create_folio(
            reservation_id="R002", guest_name="Test Guest",
            guest_afm="000000000", checkin_date="2026-05-01",
            checkout_date="2026-05-05", total_amount=-150.0,
        ))
        assert result["status"] == "validation_failed"

    @pytest.mark.asyncio
    async def test_below_floor_rejected(self):
        from app.mcp.epsilonnet import create_folio
        result = json.loads(await create_folio(
            reservation_id="R003", guest_name="Test Guest",
            guest_afm="000000000", checkin_date="2026-05-01",
            checkout_date="2026-05-05", total_amount=0.50,
        ))
        assert result["status"] == "validation_failed"
        assert "minimum" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_above_ceiling_rejected(self):
        from app.mcp.epsilonnet import create_folio
        result = json.loads(await create_folio(
            reservation_id="R004", guest_name="Test Guest",
            guest_afm="000000000", checkin_date="2026-05-01",
            checkout_date="2026-05-05", total_amount=99_999.99,
        ))
        assert result["status"] == "validation_failed"
        assert "ceiling" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_valid_amount_proceeds(self):
        """Valid amount should reach _request — mock it to avoid network."""
        from app.mcp.epsilonnet import create_folio
        mock_response = {"id": "folio-123", "gross_amount": 339.0}

        with patch("app.mcp.epsilonnet._request", new_callable=AsyncMock) as mock_req:
            # Mock idempotency check (GET folios) → empty list
            # Mock create folio (POST /folios) → success
            mock_req.side_effect = [
                {"data": []},     # GET /folios (idempotency check)
                mock_response,    # POST /folios (create)
            ]
            result = json.loads(await create_folio(
                reservation_id="R005", guest_name="Nikos Papadopoulos",
                guest_afm="123456789", checkin_date="2026-05-01",
                checkout_date="2026-05-05", total_amount=300.0,
            ))
        assert result["status"] == "created"
        assert result["folio_id"] == "folio-123"

    @pytest.mark.asyncio
    async def test_idempotent_return_for_existing_folio(self):
        """Same reservation_id called twice returns cached result."""
        from app.mcp.epsilonnet import create_folio
        existing_folio = {"id": "folio-existing", "gross_amount": 339.0}

        with patch("app.mcp.epsilonnet._request", new_callable=AsyncMock) as mock_req:
            mock_req.return_value = {"data": [existing_folio]}
            result = json.loads(await create_folio(
                reservation_id="R006", guest_name="Test",
                guest_afm="000000000", checkin_date="2026-05-01",
                checkout_date="2026-05-05", total_amount=300.0,
            ))
        assert result["status"] == "already_exists"
        assert result["folio_id"] == "folio-existing"
