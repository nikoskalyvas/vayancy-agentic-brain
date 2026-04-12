from __future__ import annotations
import httpx
from mcp.server.fastmcp import FastMCP, Context
from app.config import settings

mcp = FastMCP("whatsapp")


async def send_luxury_message(phone: str, message: str) -> str:
    """Direct callable — used by supervisor without HTTP round-trip."""
    if not settings.phone_number_id:
        return "❌ PHONE_NUMBER_ID not configured"
    async with httpx.AsyncClient() as client:
        payload = {
            "messaging_product": "whatsapp",
            "to":   phone,
            "type": "text",
            "text": {"body": message},
        }
        await client.post(
            f"https://graph.facebook.com/v20.0/{settings.phone_number_id}/messages",
            json=payload,
            headers={"Authorization": f"Bearer {settings.whatsapp_token}"},
        )
    return f"✅ Message sent to {phone}"


@mcp.tool()
async def send_luxury_message_tool(phone: str, message: str, ctx: Context = None) -> str:
    """MCP-exposed version of send_luxury_message."""
    return await send_luxury_message(phone, message)
