from __future__ import annotations
from mcp.server.fastmcp import FastMCP, Context

mcp = FastMCP("pricelabs")


async def adjust_pricing(villa_id: str, new_price: float, reason: str) -> str:
    """Direct callable — used by revenue agent without HTTP."""
    return f"Price for {villa_id} adjusted to €{new_price} ({reason})"


@mcp.tool()
async def adjust_pricing_tool(villa_id: str, new_price: float, reason: str, ctx: Context = None) -> str:
    return await adjust_pricing(villa_id, new_price, reason)
