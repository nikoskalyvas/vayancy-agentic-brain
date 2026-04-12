"""
MCP Client — connects to MCP servers running as separate processes.

FIX (issue 5): Agents no longer call http://backend:8000/mcp/... which was a
self-looping HTTP call inside the same process. Each MCP server now runs as
its own process on its own port. Agents connect to those ports.

MCP server ports:
  webhotelier  → 3001
  whatsapp     → 3002
  pricelabs    → 3003
  epsilonnet   → 3004
"""
from __future__ import annotations
import json
from typing import Any

from mcp import ClientSession
from mcp.client.sse import sse_client
from mcp.types import Tool as MCPTool


def _to_anthropic_tool(tool: MCPTool) -> dict:
    return {
        "name": tool.name,
        "description": tool.description or "",
        "input_schema": tool.inputSchema or {"type": "object", "properties": {}},
    }


class MCPClient:
    """Single-server MCP client using SSE transport."""

    def __init__(self, url: str) -> None:
        self.url = url
        self._session: ClientSession | None = None
        self._cm = None
        self._tool_map: dict[str, dict] = {}

    async def __aenter__(self) -> "MCPClient":
        self._cm = sse_client(self.url)
        read, write = await self._cm.__aenter__()
        self._session = ClientSession(read, write)
        await self._session.__aenter__()
        await self._session.initialize()
        result = await self._session.list_tools()
        self._tool_map = {t.name: _to_anthropic_tool(t) for t in result.tools}
        return self

    async def __aexit__(self, *args: Any) -> None:
        if self._session:
            await self._session.__aexit__(*args)
        if self._cm:
            await self._cm.__aexit__(*args)

    async def list_tools(self) -> list[dict]:
        return list(self._tool_map.values())

    async def call_tool(self, name: str, arguments: dict) -> str:
        result = await self._session.call_tool(name, arguments)
        parts = []
        for item in result.content:
            if hasattr(item, "text"):
                parts.append(item.text)
            else:
                parts.append(json.dumps(item, default=str))
        return "\n".join(parts) if parts else "Tool returned no output."


class MultiMCPClient:
    """Connects to multiple MCP servers, merges their tool lists."""

    def __init__(self, server_urls: list[str]) -> None:
        self._urls = server_urls
        self._clients: list[MCPClient] = []
        self._tool_server_map: dict[str, MCPClient] = {}

    async def __aenter__(self) -> "MultiMCPClient":
        for url in self._urls:
            client = MCPClient(url)
            await client.__aenter__()
            self._clients.append(client)
            for name, tool in client._tool_map.items():
                self._tool_server_map[name] = client
        return self

    async def __aexit__(self, *args: Any) -> None:
        for client in self._clients:
            try:
                await client.__aexit__(*args)
            except Exception:
                pass

    async def list_tools(self) -> list[dict]:
        tools = []
        for client in self._clients:
            tools.extend(await client.list_tools())
        return tools

    async def call_tool(self, name: str, arguments: dict) -> str:
        client = self._tool_server_map.get(name)
        if not client:
            return f"ERROR: Tool '{name}' not found in any connected MCP server."
        return await client.call_tool(name, arguments)
