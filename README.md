# Vayancy Agentic Brain v7

Multi-agent AI system for luxury villa hospitality + TravelOS MCP Server.

Built with LangGraph + FastMCP + asyncpg. Powers `owners.vayancy.gr`.

---

## What's in v7

| Component | Description |
|---|---|
| **Agentic Brain** | LangGraph supervisor routing guest/revenue/operations agents |
| **TravelOS MCP** | AI-native booking layer at `/mcp/hospitality/sse` |
| **TravelOS API** | Owner dashboard REST API at `/travelos/...` |
| **Prometheus** | Metrics at `:9090` |
| **Grafana** | Dashboards at `:3001` (admin / vayancy_grafana) |

---

## Quick Start

```bash
cp .env.example .env
# Fill in ANTHROPIC_API_KEY, WHATSAPP_TOKEN, PHONE_NUMBER_ID, WEBHOOK_SECRET

docker compose up --build -d

# Register first TravelOS tenant (run once after first deploy)
docker compose exec backend python scripts/onboard_tenant.py
```

---

## Services & Ports

| Service | Port | URL |
|---|---|---|
| Backend API | 8000 | http://localhost:8000 |
| PostgreSQL | 5432 | internal |
| Redis | 6379 | internal |
| Prometheus | 9090 | http://localhost:9090 |
| Grafana | 3001 | http://localhost:3001 |

---

## MCP Endpoints

All mounted inside the backend process:

| MCP Server | Path |
|---|---|
| WhatsApp | `/mcp/whatsapp/sse` |
| PMS | `/mcp/pms/sse` |
| PriceLabs | `/mcp/pricelabs/sse` |
| Travel | `/mcp/travel/sse` |
| **TravelOS Hospitality** | `/mcp/hospitality/sse` |

---

## TravelOS Owner API

```
POST   /travelos/admin/tenants           Register new owner (admin)
GET    /travelos/admin/tenants           List all owners (admin)
POST   /travelos/admin/tenants/{id}/deactivate
POST   /travelos/me/rotate-key           Rotate API key
POST   /travelos/me/properties           Add property to catalog
GET    /travelos/me/properties           My properties
PATCH  /travelos/me/properties/{id}      Update property
GET    /travelos/me/bookings             Booking dashboard
GET    /travelos/me/stats                Summary stats
```

Auth for admin endpoints: `Authorization: Bearer <SECRET_KEY>`
Auth for owner endpoints: `X-Tenant-Key: <api_key>`

---

## TravelOS Booking Flow (for AI agents)

```
search_properties(location, check_in, check_out, guests)
    → get_rate_details(unit_id, ...)
        → create_hold(unit_id, ..., guest_email)       ← 15min lock
            → create_booking(hold_id, guest_*, idempotency_key)
                → confirmed ✅ — no OTA commission
```

---

## Key Fixes vs Previous Version

- **Self-loop bug fixed**: agents no longer call `http://backend:8000/mcp/...` from inside the container. Tool functions are called directly.
- **Config centralised**: all env vars go through `app/config.py` (pydantic-settings), validated at startup.
- **DB init centralised**: `app/db/session.py` creates all tables including TravelOS.
- **Monitoring added**: Redis, Prometheus, Grafana now in docker-compose.
