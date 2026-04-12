# Vayancy Agentic Brain

Multi-agent AI system for luxury villa hospitality. Connects to WebHotelier, WhatsApp Business, PriceLabs, and Epsilon Net — coordinating them autonomously via Claude.

---

## Architecture

```
WebHotelier webhook → API (FastAPI) → Redis queue → Worker
                                                      ↓
                                              LangGraph Supervisor
                                              ↙       ↓       ↘
                                         Guest    Revenue   Operations
                                           ↓        ↓          ↓
                                        WA MCP  PL MCP      EN MCP
                                        WH MCP  WH MCP      WH MCP
                                                            WA MCP
```

**8 Docker services:**

| Service | Role | Port |
|---|---|---|
| `postgres` | Primary DB + pgvector | 5432 |
| `redis` | ARQ task queue | 6379 |
| `mcp-webhotelier` | WebHotelier MCP server | 3001 |
| `mcp-whatsapp` | WhatsApp MCP server | 3002 |
| `mcp-pricelabs` | PriceLabs MCP server | 3003 |
| `mcp-epsilonnet` | Epsilon Net MCP server | 3004 |
| `api` | FastAPI + webhook handlers | 8000 |
| `worker` | ARQ worker + cron jobs | — |
| `dashboard` | Next.js owner dashboard | 3000 |

---

## Quick Start

### 1. Prerequisites

- Docker + Docker Compose
- Anthropic API key
- Voyage AI API key (free tier sufficient): [dash.voyageai.com](https://dash.voyageai.com)

### 2. Configure

```bash
cp .env.example .env
```

Edit `.env` and fill in:

```env
PROPERTY_ID=villa-azure          # unique slug for your property

ANTHROPIC_API_KEY=sk-ant-...
VOYAGE_API_KEY=pa-...

WEBHOTELIER_API_KEY=...
WEBHOTELIER_PROPERTY_ID=...
WEBHOTELIER_WEBHOOK_SECRET=...   # set in WebHotelier portal

WHATSAPP_ACCESS_TOKEN=...
WHATSAPP_PHONE_NUMBER_ID=...
WHATSAPP_WEBHOOK_VERIFY_TOKEN=vayancy-verify
OWNER_WHATSAPP_PHONE=+306912345678

PRICELABS_API_KEY=...
PRICELABS_PROPERTY_ID=...

EPSILON_NET_USERNAME=...
EPSILON_NET_PASSWORD=...
EPSILON_NET_COMPANY_ID=...

POSTGRES_PASSWORD=choose_strong_password
REDIS_PASSWORD=choose_strong_password
SECRET_KEY=choose_32_char_secret
```

### 3. Run

```bash
docker-compose up --build
```

Wait for all services to be healthy (~30 seconds on first run).

- **Dashboard:** http://localhost:3000
- **API health:** http://localhost:8000/health

---

## WebHotelier Webhook Setup

In your WebHotelier portal, set the webhook URL to:
```
https://your-domain.com/webhook/webhotelier
```

Set the webhook secret to match `WEBHOTELIER_WEBHOOK_SECRET` in your `.env`.

Events handled: `booking.confirmed`, `booking.modified`, `booking.cancelled`,
`booking.noshow`, `guest.checkin`, `guest.checkout`

---

## WhatsApp Webhook Setup

In the Meta Developer portal, set:
- **Webhook URL:** `https://your-domain.com/webhook/whatsapp`
- **Verify token:** value of `WHATSAPP_WEBHOOK_VERIFY_TOKEN`
- **Subscribe to:** `messages` webhook field

---

## Scheduled Jobs

The ARQ worker runs two cron jobs automatically:

| Job | Schedule | Agent |
|---|---|---|
| Pricing review | Every 4 hours | Revenue |
| Checkout dispatch | Daily 08:00 | Operations |

---

## Dashboard SSE Stream

The owner dashboard connects to:
```
GET /stream/workflows?property_id=<PROPERTY_ID>
```

This is a Server-Sent Events stream — events arrive instantly via PostgreSQL
`LISTEN/NOTIFY`, zero polling. Each event is scoped to your `property_id`.

REST fallback (requires `Authorization: Bearer <SECRET_KEY>`):
```
GET /stream/workflows/recent?property_id=<PROPERTY_ID>&limit=100
```

---

## Multi-Tenant

Each deployment is isolated by `PROPERTY_ID`. All DB rows, memory retrieval,
SSE events, and agent context are scoped to this value. To add a second property:

1. Deploy a second instance with a different `PROPERTY_ID`
2. They can share the same PostgreSQL and Redis (rows are isolated by `property_id`)

---

## Agent Directives

The guest agent can emit structured directives in its output that are
automatically parsed and acted upon:

| Directive | Effect |
|---|---|
| `PROFILE_UPDATE: <preference>` | Persists guest preference to DB |
| `UPSELL_SENT: <type>` | Records upsell in history — never offered again |
| `ESCALATE: <reason>` | Logs escalation, sends WhatsApp to owner |

---

## File Structure

```
backend/app/
├── config.py              Settings (pydantic-settings)
├── models.py              Domain types
├── main.py                FastAPI app + startup
├── core/
│   ├── base_agent.py      Agentic loop shared by all agents
│   ├── guest_agent.py     Luxury concierge + upsell engine
│   ├── revenue_agent.py   Dynamic pricing logic
│   ├── operations_agent.py Cleaning dispatch + folio creation
│   ├── supervisor.py      LangGraph orchestrator
│   ├── memory.py          pgvector + Voyage AI embeddings
│   ├── security.py        HMAC verification + injection guard
│   ├── escalation.py      Owner WhatsApp notification
│   └── profile_resolver.py WebHotelier → guest profile bootstrap
├── mcp/
│   ├── webhotelier.py     WebHotelier API (reservations, availability)
│   ├── whatsapp.py        WhatsApp Business Cloud API
│   ├── pricelabs.py       PriceLabs API (rates, market data)
│   └── epsilonnet.py      Epsilon Net ERP (folios, invoices, VAT)
├── api/
│   ├── webhooks.py        Webhook handlers (WebHotelier + WhatsApp)
│   └── stream.py          SSE + REST log endpoints
├── queue/
│   └── worker.py          ARQ worker + cron jobs
└── db/
    └── session.py         PostgreSQL pool + schema init
```

---

## Key Design Decisions

**Why separate MCP processes?**
Each MCP server runs as its own Docker service. Agents connect to them over SSE.
This avoids the self-loopback problem (mounting + calling in the same process)
and makes integrations independently deployable and restartable.

**Why Voyage AI for embeddings?**
Voyage AI's `voyage-3` model produces 1024-dim embeddings with strong semantic
accuracy for hospitality text. The sync client is wrapped in `asyncio.to_thread()`
so it never blocks the event loop.

**Why HNSW over IVFFlat?**
IVFFlat requires existing data to build centroids — it produces a degenerate index
on first deploy against an empty table. HNSW has no such requirement.

**Why per-property pg_notify channels?**
`workflow_updates_{property_id}` ensures each SSE client only receives events
for their own property. Cross-tenant data leakage via SSE is architecturally impossible.

---

## License

MIT
