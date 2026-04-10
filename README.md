# vayancy-agentic-brain

**Vayancy Agentic Brain** — Multi-agent AI system for luxury villa hospitality.

Built with LangGraph + MCP. Matches the architecture of owners.vayancy.gr.

### Quick Start
```bash
cp .env.example .env
docker-compose up --build
cat > backend/app/models.py << 'EOF'
from pydantic import BaseModel
from datetime import datetime
from typing import Literal

class WorkflowLog(BaseModel):
    id: str
    timestamp: datetime
    agent: Literal["supervisor", "guest", "revenue", "operations"]
    action: str
    status: Literal["thinking", "tool_call", "completed", "error"]
    details: str
    workflow_id: str
