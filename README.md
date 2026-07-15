# AI-asset

decision-dna/
├── api-gateway/          ← PORT 8000 — Central router + rate limiter
├── services/
│   ├── ingestion-service/  ← PORT 8001 — Parses emails/meetings/Jira
│   ├── embedding-service/  ← PORT 8002 — Chunks + embeds → Pinecone
│   ├── graph-service/      ← PORT 8003 — Neo4j knowledge graph
│   ├── query-service/      ← PORT 8004 — LangGraph 5-agent brain ⭐
│   └── timeline-service/   ← PORT 8005 — Decision timeline builder ⭐
├── shared/               ← Pydantic models, config, utils (shared by all)
├── frontend/             ← Streamlit UI (5 pages)
├── scripts/              ← generate_data.py + ingest_all.py
└── docker-compose.yml    ← One command to run everything
# 🧬 DecisionDNA — AI Organizational Memory Engine

> *"Why did we reject Vendor X? Who raised the security concern? What decisions led to this bug?"*
> DecisionDNA answers all of it — instantly.

[![Python](https://img.shields.io/badge/Python-3.11-blue)](https://python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.111-green)](https://fastapi.tiangolo.com)
[![LangGraph](https://img.shields.io/badge/LangGraph-0.1-orange)](https://langchain-ai.github.io/langgraph)
[![Claude](https://img.shields.io/badge/LLM-Claude%20Sonnet-purple)](https://anthropic.com)
[![Docker](https://img.shields.io/badge/Docker-Compose-blue)](https://docker.com)

---

## The Problem It Solves

Every company hemorrhages knowledge daily. When employees leave, change teams, or get promoted — the *why* behind decisions disappears. Meeting notes exist but nobody reads them. Jira tickets exist but context is missing. New engineers repeat old mistakes.

**Existing tools (Confluence, Notion, SharePoint) store information. They do NOT capture:**
- Why a decision was made
- Who disagreed and what their concern was
- Whether the decision aged well or caused problems later

**DecisionDNA does all three.**

---

## What Makes It Unique

| Feature | Confluence / Notion | DecisionDNA |
|---|---|---|
| Stores documents | ✅ | ✅ |
| Semantic search | ❌ | ✅ |
| Decision timeline | ❌ | ✅ |
| Captures dissent | ❌ | ✅ |
| Links outcomes to decisions | ❌ | ✅ |
| Confidence scoring | ❌ | ✅ |
| Works across emails + Jira + meetings | ❌ | ✅ |

---

## Architecture

```
┌──────────────────────────────────────────────────────────────┐
│                    Streamlit UI  :3000                        │
└───────────────────────────┬──────────────────────────────────┘
                            │
┌───────────────────────────▼──────────────────────────────────┐
│                   API Gateway  :8000                          │
│          (Routing · Rate Limiting · Health Checks)            │
└──┬──────────┬─────────────┬──────────────┬────────────────┬──┘
   │          │             │              │                │
   ▼          ▼             ▼              ▼                ▼
:8001      :8002          :8003          :8004            :8005
Ingest   Embedding       Graph          Query           Timeline
Service   Service        Service        Service          Service
   │          │             │              │                │
   │      Pinecone        Neo4j        LangGraph        Pinecone
   │      (Vectors)       (Graph)      (5 Agents)       (Search)
   │                                       │
   └───────────────────────────────────────┘
                    Redis (Cache)
```

### The 5-Agent LangGraph Pipeline

When you ask *"Why did we migrate to Azure?"*, the query service runs:

```
Your Question
     │
     ▼
┌─────────────────┐
│ 1. Planner      │  Breaks question into sub-tasks
└────────┬────────┘
         ▼
┌─────────────────┐
│ 2. Search       │  Queries Pinecone vector DB
└────────┬────────┘
         ▼
┌─────────────────┐
│ 3. Timeline     │  Calls Timeline Service → chronological events
└────────┬────────┘
         ▼
┌─────────────────┐
│ 4. Decision     │  Analyzes dissent, risks, outcomes with Claude
└────────┬────────┘
         ▼
┌─────────────────┐
│ 5. Answer       │  Generates final answer + sources + confidence
└─────────────────┘
```

---

## Sample Output

**Question:** *"Why did we reject Vendor X?"*

```
✅ Answer
Vendor X was rejected in Q1 2024 primarily due to pricing that exceeded
budget by 40%, unacceptable SLA terms (99.5% vs required 99.9%), and
missing GDPR compliance certification. Anjali argued for a second chance
but was outvoted in MTG-034.

📅 Decision Timeline
• Jan 15, 2024  [DISCUSSION]  Initial vendor evaluation kicked off
• Jan 22, 2024  [MEETING]     Vendor X presented proposal — MTG-031
• Jan 28, 2024  ⚠️ [CONCERN]  Anjali flagged SLA terms as insufficient
• Feb 01, 2024  ✅ [DECISION]  Vendor X formally rejected — MTG-034
• Feb 05, 2024  [ACTION]      Open tender issued for alternatives

🎯 Outcome: Vendor Y was selected 6 weeks later at 25% lower cost.
📊 Confidence: 87%
📎 Sources: [EMAIL-023] [MTG-031] [MTG-034] [PROJ-089]
```

---

## Tech Stack

| Layer | Technology |
|---|---|
| LLM | Claude Sonnet (`claude-sonnet-4-6`) |
| Embeddings | OpenAI `text-embedding-3-large` |
| Vector DB | Pinecone (serverless) |
| Graph DB | Neo4j 5.x |
| Agent Framework | LangGraph |
| RAG Framework | LangChain |
| API Services | FastAPI + Uvicorn |
| Cache | Redis 7 |
| UI | Streamlit |
| Infrastructure | Docker Compose |
| Language | Python 3.11 |

---

## Project Structure

```
decision-dna/
├── 📄 .env.example
├── 📄 docker-compose.yml
│
├── 📁 api-gateway/            # PORT 8000
│   ├── app/main.py
│   ├── requirements.txt
│   └── Dockerfile
│
├── 📁 services/
│   ├── ingestion-service/     # PORT 8001 — Parse raw documents
│   │   ├── app/main.py
│   │   └── app/parsers/
│   │       ├── email_parser.py
│   │       ├── meeting_parser.py
│   │       └── jira_parser.py
│   │
│   ├── embedding-service/     # PORT 8002 — Chunk + embed → Pinecone
│   │   └── app/main.py
│   │
│   ├── graph-service/         # PORT 8003 — Neo4j knowledge graph
│   │   └── app/main.py
│   │
│   ├── query-service/         # PORT 8004 — LangGraph 5-agent brain ⭐
│   │   └── app/main.py
│   │
│   └── timeline-service/      # PORT 8005 — Decision timeline builder ⭐
│       └── app/main.py
│
├── 📁 shared/                 # Shared across all services
│   ├── models/schemas.py      # All Pydantic models
│   ├── config/settings.py     # Centralized config
│   └── utils/helpers.py       # Logger, HTTP client, date utils
│
├── 📁 frontend/               # PORT 3000 — Streamlit UI
│   └── app.py                 # Ask · Timeline · Graph · Ingest · Health
│
├── 📁 data/synthetic/         # Generated test data
│   ├── emails/
│   ├── meetings/
│   └── jira/
│
└── 📁 scripts/
    ├── generate_data.py       # Generates 250 synthetic documents
    └── ingest_all.py          # Triggers ingestion pipeline
```

---

## Quick Start

### Prerequisites

- Docker + Docker Compose
- Python 3.11+
- API Keys: Anthropic, OpenAI, Pinecone

### Step 1 — Configure environment

```bash
git clone https://github.com/your-org/decision-dna.git
cd decision-dna
cp .env.example .env
# Open .env and fill in your API keys
```

### Step 2 — Generate synthetic data

```bash
pip install faker
python scripts/generate_data.py
# Creates 250 synthetic emails, meetings, and Jira tickets
# Output: data/synthetic/
```

### Step 3 — Start all services

```bash
docker-compose up --build
# Wait ~60 seconds for Neo4j to initialize
```

### Step 4 — Ingest data

```bash
pip install httpx
python scripts/ingest_all.py
# Triggers ingestion → embedding → graph build pipeline
# Takes 2-5 minutes depending on document count
```

### Step 5 — Open the UI

```
http://localhost:3000
```

---

## API Reference

All requests go through the API Gateway at `http://localhost:8000`.

### Ask a Question
```http
POST /api/v1/query
Content-Type: application/json

{
  "question": "Why did we reject Vendor X?",
  "project_filter": "DataPlatform"
}
```

**Response:**
```json
{
  "question": "Why did we reject Vendor X?",
  "answer": "Vendor X was rejected due to...",
  "timeline": {
    "topic": "Vendor X rejection",
    "events": [...],
    "outcome_assessment": "...",
    "confidence_score": 0.87
  },
  "sources": [...],
  "confidence_score": 0.87,
  "processing_steps": [
    "Planner: Generated 3 sub-tasks",
    "Search: Retrieved 12 chunks",
    "Timeline: Built chronological sequence",
    "Decision Agent: Analyzed decisions and dissent",
    "Answer Agent: Generated final response"
  ]
}
```

### Ingest Documents
```http
POST /api/v1/ingest
{
  "data_dir": "/app/data/synthetic",
  "trigger_embedding": true,
  "trigger_graph": true
}
```

### Get Decision Timeline
```http
GET /api/v1/timeline/{topic}?project=CloudMigration
```

### Health Check
```http
GET /health
```

---

## Data Formats

### Email JSON
```json
{
  "id": "EMAIL-001",
  "from": "ravi.sharma@company.com",
  "to": ["priya.patel@company.com"],
  "date": "2024-01-15T10:30:00",
  "subject": "Azure Migration Concerns",
  "body": "I have concerns about vendor lock-in...",
  "project": "CloudMigration",
  "tags": ["migration", "risk"]
}
```

### Meeting JSON
```json
{
  "id": "MTG-001",
  "title": "Cloud Migration Planning",
  "date": "2024-01-22T14:00:00",
  "attendees": ["Ravi Sharma", "Priya Patel"],
  "discussion": "The team debated...",
  "decisions": ["Proceed with Azure Functions"],
  "action_items": ["Security audit by Feb 15"],
  "project": "CloudMigration"
}
```

### Jira Ticket JSON
```json
{
  "id": "PROJ-101",
  "title": "API Timeout after migration",
  "description": "30% of requests timing out...",
  "status": "Open",
  "priority": "Critical",
  "reporter": "Ravi Sharma",
  "assignee": "Alex Johnson",
  "created": "2024-03-01T09:00:00",
  "comments": [{"author": "...", "date": "...", "body": "..."}],
  "project": "CloudMigration",
  "labels": ["migration", "performance"]
}
```

---

## Environment Variables

| Variable | Description | Required |
|---|---|---|
| `ANTHROPIC_API_KEY` | Claude API key | ✅ |
| `OPENAI_API_KEY` | For embeddings | ✅ |
| `PINECONE_API_KEY` | Vector database | ✅ |
| `PINECONE_ENVIRONMENT` | Pinecone region | ✅ |
| `NEO4J_URI` | Graph DB connection | ✅ |
| `NEO4J_PASSWORD` | Graph DB password | ✅ |
| `REDIS_URL` | Cache connection | ✅ |

---

## Roadmap

- [ ] Microsoft Graph API integration (real Outlook + Teams)
- [ ] Jira REST API live connector
- [ ] Slack connector
- [ ] Confluence connector
- [ ] Decision confidence trend over time
- [ ] Team-level knowledge gap analysis
- [ ] Export decision reports to PDF
- [ ] Role-based access control

---

## Contributing

1. Fork the repo
2. Create a feature branch: `git checkout -b feature/your-feature`
3. Commit changes: `git commit -m 'Add feature'`
4. Push: `git push origin feature/your-feature`
5. Open a Pull Request

---

## License

MIT License — see [LICENSE](LICENSE) for details.

---

*Built for SparkAIthon Hackathon — Aditi Consulting*
