from mcp.server.fastmcp import FastMCP, Context
import httpx

mcp = FastMCP("whatsapp")

@mcp.tool()
async def send_luxury_message(phone: str, message: str, ctx: Context) -> str:
    phone_number_id = ctx.env.get("PHONE_NUMBER_ID")
    if not phone_number_id:
        return "❌ PHONE_NUMBER_ID not configured"
    async with httpx.AsyncClient() as client:
        payload = {"messaging_product": "whatsapp", "to": phone, "type": "text", "text": {"body": message}}
        await client.post(f"https://graph.facebook.com/v20.0/{phone_number_id}/messages",
                          json=payload, headers={"Authorization": f"Bearer {ctx.env.get('WHATSAPP_TOKEN')}"})
    return f"✅ Luxury message sent to {phone}"
