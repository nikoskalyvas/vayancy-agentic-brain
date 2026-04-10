from mcp.server.fastmcp import FastMCP, Context

mcp = FastMCP("travel")

@mcp.tool()
async def get_weather_forecast(location: str, ctx: Context) -> str:
    return f"Weather in {location}: 28°C, sunny, perfect for villa guests."
