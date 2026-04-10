from mcp.client.sse import sse_client
from mcp.client.session import ClientSession
from contextlib import asynccontextmanager

@asynccontextmanager
async def get_mcp_client(server_url: str):
    async with sse_client(server_url) as streams:
        async with ClientSession(streams[0], streams[1]) as session:
            await session.initialize()
            yield session
