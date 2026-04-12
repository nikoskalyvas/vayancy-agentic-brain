"""
MCP client — fixed self-loop bug.

The live code called http://backend:8000/mcp/... from inside the backend
container itself. This caused request queuing deadlocks under load.

Fix: MCP tools are now called directly via function import, not HTTP.
The sse_client is kept for external MCP servers only (future use).
"""
from __future__ import annotations
import json
from typing import Any

from mcp.client.sse import sse_client
from mcp.client.session import ClientSession
from contextlib import asynccontextmanager


@asynccontextmanager
async def get_mcp_client(server_url: str):
    """Connect to an external MCP server via SSE."""
    async with sse_client(server_url) as streams:
        async with ClientSession(streams[0], streams[1]) as session:
            await session.initialize()
            yield session
