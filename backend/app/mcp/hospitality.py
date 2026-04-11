from mcp.server.fastmcp import FastMCP, Context
import uuid
from datetime import datetime, timedelta

mcp = FastMCP("hospitality")

@mcp.tool()
async def check_availability(tenant_id: str, check_in: str, check_out: str, villa_id: str = None, ctx: Context = None) -> dict:
    if not tenant_id:
        return {"error": "tenant_id is required"}
    return {
        "tenant_id": tenant_id,
        "available": True,
        "villas": [
            {"villa_id": villa_id or "V001", "name": "Villa Azure", "price": 1250}
        ]
    }

@mcp.tool()
async def create_hold(tenant_id: str, villa_id: str, check_in: str, check_out: str, guest_name: str, guest_email: str, ctx: Context = None) -> dict:
    hold_id = f"HOLD-{uuid.uuid4().hex[:10].upper()}"
    expires_at = (datetime.utcnow() + timedelta(minutes=15)).isoformat()
    return {
        "status": "hold_created",
        "hold_id": hold_id,
        "tenant_id": tenant_id,
        "villa_id": villa_id,
        "expires_at": expires_at
    }

@mcp.tool()
async def create_booking(tenant_id: str, hold_id: str, guest_name: str, guest_email: str, guest_phone: str, idempotency_key: str, ctx: Context = None) -> dict:
    booking_id = f"BK-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}"
    return {
        "status": "confirmed",
        "booking_id": booking_id,
        "tenant_id": tenant_id,
        "guest_name": guest_name
    }

@mcp.tool()
async def get_guest_profile(tenant_id: str, guest_email: str = None, guest_phone: str = None, ctx: Context = None) -> dict:
    if not tenant_id:
        return {"error": "tenant_id is required"}
    return {
        "tenant_id": tenant_id,
        "name": "Alexander Voss",
        "loyalty_tier": "Platinum",
        "preferences": "Ocean view, private chef"
    }

@mcp.tool()
async def cancel_booking(tenant_id: str, booking_id: str, reason: str = None, ctx: Context = None) -> dict:
    if not tenant_id or not booking_id:
        return {"error": "tenant_id and booking_id are required"}
    return {
        "booking_id": booking_id,
        "tenant_id": tenant_id,
        "status": "cancelled"
    }
