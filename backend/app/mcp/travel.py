from mcp.server.fastmcp import FastMCP, Context

mcp = FastMCP("travel")

@mcp.tool()
async def get_weather_forecast(location: str, ctx: Context = None) -> str:
    """Get weather forecast for a location."""
    return f"Weather in {location}: 28°C, sunny, perfect for villa guests."

@mcp.tool()
async def get_transfer_options(from_location: str, to_location: str, ctx: Context = None) -> str:
    """Get transfer options between two locations."""
    return f"Transfer from {from_location} to {to_location}: Private car €120, Taxi €80, Bus €15."
