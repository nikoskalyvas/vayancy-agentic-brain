"""
conftest.py — shared fixtures for Vayancy test suite.

Run tests:
  docker compose exec api pytest tests/ -v

Or locally (requires test deps):
  pip install pytest pytest-asyncio pytest-httpx
  pytest tests/ -v
"""
import pytest


@pytest.fixture
def property_id() -> str:
    return "villa-azure-test"


@pytest.fixture
def other_property_id() -> str:
    return "villa-blue-test"
