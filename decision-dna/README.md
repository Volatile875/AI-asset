# DecisionDNA — AI Organizational Memory Engine

> "ChatGPT for Company Knowledge + Decisions + History"

## Architecture Overview

```
┌──────────────────────────────────────────────────────────┐
│                     API GATEWAY :8000                    │
│              (Routes all incoming requests)              │
└──────┬───────────┬──────────┬──────────┬────────────────┘
       │           │          │          │
       ▼           ▼          ▼          ▼
┌──────────┐ ┌─────────┐ ┌────────┐ ┌──────────┐ ┌──────────────┐
│Ingestion │ │Embedding│ │ Graph  │ │  Query   │ │  Timeline    │
│:8001     │ │:8002    │ │:8003   │ │:8004     │ │:8005         │
└──────────┘ └─────────┘ └────────┘ └──────────┘ └──────────────┘
       │           │          │          │               │
       └───────────┴──────────┴──────────┴───────────────┘
                              │
                    ┌─────────▼──────────┐
                    │  Pinecone (Vector) │
                    │  Neo4j   (Graph)   │
                    │  Redis   (Cache)   │
                    └────────────────────┘
```

## Microservices

| Service | Port | Responsibility |
|---|---|---|
| api-gateway | 8000 | Route, auth, rate-limit |
| ingestion-service | 8001 | Parse emails, Jira, meetings |
| embedding-service | 8002 | Chunk text, generate vectors |
| graph-service | 8003 | Neo4j knowledge graph |
| query-service | 8004 | LangGraph agent, RAG pipeline |
| timeline-service | 8005 | Decision timeline builder |
| frontend | 3000 | Streamlit UI |
| mcp-server | stdio | Fetch Jira ticket status from Atlassian and store status history |

## Quick Start

```bash
# 1. Clone & setup
cp .env.example .env
# Fill in your API keys in .env

# 2. Generate synthetic data
python scripts/generate_data.py

# 3. Start all services
docker-compose up --build

# 4. Ingest data
python scripts/ingest_all.py

# 5. Open UI
open http://localhost:3000
```

## Jira MCP Server

The Jira MCP server lives in `mcp-server/`. It exposes tools that fetch Jira
ticket status from Atlassian, store the current status, and preserve status
transition timestamps in SQLite.

Required environment variables:

```bash
JIRA_BASE_URL=https://your-domain.atlassian.net
JIRA_EMAIL=your.email@company.com
JIRA_API_TOKEN=your_atlassian_api_token
JIRA_STATUS_DB_PATH=./mcp-server/data/jira_status_history.db
```

Run it as a stdio MCP server:

```bash
cd mcp-server
pip install -r requirements.txt
python server.py
```

## Tech Stack

- **LLM**: Claude Sonnet (claude-sonnet-4-6)
- **Embeddings**: OpenAI text-embedding-3-large
- **Vector DB**: Pinecone
- **Graph DB**: Neo4j
- **Framework**: FastAPI + LangChain + LangGraph
- **MCP**: Python MCP SDK
- **Cache**: Redis
- **UI**: Streamlit
- **Infra**: Docker Compose
