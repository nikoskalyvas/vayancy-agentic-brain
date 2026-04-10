from mcp.server.fastmcp import FastMCP, Context

mcp = FastMCP("pms")

@mcp.tool()
async def get_booking_details(booking_id: str, ctx: Context) -> dict:
    return {"booking_id": booking_id, "guest_name": "Alexander Voss", "villa": "Villa Azure"}

@mcp.tool()
async def update_booking_status(booking_id: str, new_status: str, ctx: Context) -> str:
    return f"Booking {booking_id} updated to {new_status}"
