"""
TravelOS — First Tenant Onboarding Script.

Run this once after deploy to register yourself (or the first owner)
as a TravelOS tenant. This generates the API key they use to call
the hospitality MCP server.

Usage:
    docker compose exec backend python scripts/onboard_tenant.py
"""
import asyncio
import json
import os
import secrets
import sys

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import asyncpg


async def main():
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        print("ERROR: DATABASE_URL not set")
        sys.exit(1)

    print("\n=== Vayancy TravelOS — Tenant Onboarding ===\n")
    name       = input("Owner name (e.g. 'Nikos - Villa Azure'): ").strip()
    wh_api_key = input("WebHotelier API key: ").strip()
    wh_prop_id = input("WebHotelier Property ID: ").strip()

    api_key    = secrets.token_urlsafe(32)
    pms_config = json.dumps({
        "api_key":     wh_api_key,
        "property_id": wh_prop_id,
        "api_base":    "https://api.webhotelier.net/v2",
    })

    pool = await asyncpg.create_pool(db_url, min_size=1, max_size=2)
    row  = await pool.fetchrow(
        """
        INSERT INTO travelos_tenants (name, api_key, pms_type, pms_config, active)
        VALUES ($1, $2, 'webhotelier', $3, TRUE)
        RETURNING id, name
        """,
        name, api_key, pms_config,
    )
    await pool.close()

    print(f"\n✅ Tenant registered!")
    print(f"   ID      : {row['id']}")
    print(f"   Name    : {row['name']}")
    print(f"   API Key : {api_key}")
    print("\n⚠️  Save this API key — it will not be shown again.")
    print("\nMCP endpoint: http://localhost:8000/mcp/hospitality/sse")
    print("Use header:   X-Tenant-Key: <api_key>")


if __name__ == "__main__":
    asyncio.run(main())
