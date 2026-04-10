from mcp.server.fastmcp import FastMCP, Context

mcp = FastMCP("pricelabs")

@mcp.tool()
async def adjust_pricing(villa_id: str, new_price: float, reason: str, ctx: Context) -> str:
    return f"Price for {villa_id} adjusted to €{new_price} ({reason})"
