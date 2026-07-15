# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

DecisionDNA — an "AI Organizational Memory Engine". It ingests synthetic emails, meeting notes, and Jira tickets, then answers "why did we decide X?" questions with a reconstructed decision timeline, dissent capture, sources, and a confidence score. Built for the SparkAIthon hackathon (Aditi Consulting).

All application code lives in [decision-dna/](decision-dna/). The repo root only holds this file, the README, and an aggregate `requirements.txt`.

## Architecture

Six FastAPI microservices behind an API gateway, plus a Streamlit UI and a standalone MCP server. Everything is orchestrated by [decision-dna/docker-compose.yml](decision-dna/docker-compose.yml).

| Service | Port | Role |
|---|---|---|
| api-gateway | 8000 | Routes to all services, IP rate-limiting (100/min, Redis-backed), CORS, Scalar docs at `/scalar` |
| ingestion-service | 8001 | Parses JSON emails/meetings/Jira → normalized docs; fans out to embedding + graph |
| embedding-service | 8002 | Chunks (`RecursiveCharacterTextSplitter`), embeds via OpenAI, upserts to Pinecone |
| graph-service | 8003 | Builds/queries the Neo4j knowledge graph (Person/Project/Decision/Meeting/Ticket/Email nodes) |
| query-service | 8004 | The core: a LangGraph 5-agent pipeline (Planner → Search → Timeline → Decision → Answer) |
| timeline-service | 8005 | Uses Claude to extract structured, dated timeline events from Pinecone hits |
| frontend | 3000 | Streamlit UI: Ask / Timeline / Graph / Ingest / Health |
| mcp-server | stdio | FastMCP server for live Jira status + SQLite history; run separately, not in compose |

Data path: raw JSON in `data/synthetic/{emails,meetings,jira}/` → ingestion normalizes → embedding (Pinecone vectors) + graph (Neo4j) → query-service orchestrates retrieval across both plus timeline-service to answer.

### Critical structural gotchas

- **`shared/` is NOT wired into the services.** Each service's `Dockerfile` does `COPY . .` from its own directory only, so `shared/` never reaches the containers, and no service imports it (`grep` for `from shared` returns nothing). Each `app/main.py` reads config **directly from `os.getenv(...)`** with its own inline defaults. Treat `shared/config/settings.py`, `shared/models/schemas.py`, and `shared/utils/helpers.py` as reference/aspirational scaffolding — editing them does not change running behavior. To change a service's config, edit that service's `main.py` and/or the `.env`.
- **The two context docs drift from the code.** [decision-dna/AGENT_CONTEXT.md](decision-dna/AGENT_CONTEXT.md) and the README are useful for intent but out of date on specifics. When they conflict with source, trust the source. Known drifts: LLM model is `claude-sonnet-4-6` (hardcoded in service `main.py` files, e.g. [query-service](decision-dna/services/query-service/app/main.py)); Pinecone index defaults to `ai-asset` and embeddings use `1024` dimensions (not `decision-dna` / `3072` as docs claim).
- **Neo4j has two possible targets.** `docker-compose.yml` runs a **local** Neo4j (`neo4j:5.15`, auth `neo4j/password123`), but `shared/config/settings.py` defaults to a **cloud Aura** URI. The actual connection is whatever `.env` (`NEO4J_URI`, `NEO4J_PASSWORD`) provides to each service — set this deliberately.
- **Duplicated synthetic data.** Both `data/synthetic/` and `scripts/data/synthetic/` exist. Compose mounts `./data` to `/app/data`, and ingestion reads `/app/data/synthetic`, so `decision-dna/data/synthetic/` is the one that matters at runtime.

## Commands

All commands run from `decision-dna/`.

```bash
cd decision-dna

# 1. Configure — no .env.example is committed; create .env from the vars below
#    Required: ANTHROPIC_API_KEY, OPENAI_API_KEY, PINECONE_API_KEY, NEO4J_PASSWORD
#    Plus PINECONE_INDEX_NAME, EMBEDDING_DIMENSIONS, service URLs (see AGENT_CONTEXT.md)

# 2. Generate synthetic data (100 emails, 50 meetings, 100 Jira tickets → data/synthetic/)
pip install faker
python scripts/generate_data.py

# 3. Start the whole stack (wait ~60s for Neo4j to become healthy)
docker-compose up --build

# 4. Ingest — triggers ingest → embed → graph, then polls to completion
pip install httpx
python scripts/ingest_all.py

# Optional: seed Neo4j directly from local data (bypasses the service pipeline)
python scripts/seed_neo4j.py            # seed
python scripts/seed_neo4j.py --clear    # wipe first
python scripts/seed_neo4j.py --dump-cypher data/decisiondna_seed.cypher

# Run the MCP server standalone (Jira integration; needs JIRA_* env vars)
python mcp-server/server.py
```

Access points after `docker-compose up`: UI http://localhost:3000 · Gateway http://localhost:8000 · Scalar docs http://localhost:8000/scalar · Neo4j browser http://localhost:7474.

Iterating on one service without a full rebuild:
```bash
docker-compose up --build query-service        # rebuild + restart just one
docker-compose logs -f query-service            # tail its logs
```
Uvicorn runs with `--reload` in every service `Dockerfile`, but the code is `COPY`-baked into the image (not bind-mounted), so source edits still require a rebuild of that service to take effect.

## Testing & linting

There is **no test suite, no linter config, and no CI** in this repo. "Verifying" a change means exercising it against the running stack — e.g. `POST /api/v1/query` through the gateway or the Streamlit UI — not running tests.

## Adding to the query pipeline

The 5 agents in [query-service/app/main.py](decision-dna/services/query-service/app/main.py) are plain functions over a shared `AgentState` TypedDict, wired into a LangGraph `StateGraph`. To add or reorder a step: write a `def agent(state) -> AgentState` function, add it as a node, and update the edges. Agents append human-readable strings to `state["processing_steps"]`, which surface in the API response and UI.
