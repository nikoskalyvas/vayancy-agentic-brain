from __future__ import annotations
from mcp.server.fastmcp import FastMCP, Context

mcp = FastMCP("travel")


async def get_weather_forecast(location: str) -> str:
    """Direct callable — used by operations agent without HTTP."""
    return f"Weather in {location}: 28°C, sunny, perfect for villa guests."


@mcp.tool()
async def get_weather_forecast_tool(location: str, ctx: Context = None) -> str:
    return await get_weather_forecast(location)
