# AI_AGENT_GUIDE.md

This file provides guidance to AI coding agents when working with code in this repository.

## Project

DecisionDNA — an "AI Organizational Memory Engine". It ingests synthetic emails, meeting notes, and Jira tickets, then answers "why did we decide X?" questions with a reconstructed decision timeline, dissent capture, sources, and a confidence score. Built for the SparkAIthon hackathon (Aditi Consulting).

All application code lives in [decision-dna/](decision-dna/). The repo root only holds this file, the README, and an aggregate `requirements.txt`.

## Architecture

Six FastAPI microservices behind an API gateway, plus a React SPA and a standalone MCP server. Everything is orchestrated by [decision-dna/docker-compose.yml](decision-dna/docker-compose.yml).

| Service | Port | Role |
|---|---|---|
| api-gateway | 8000 | Routes to all services, IP rate-limiting (100/min, Redis-backed), CORS, JWT auth (signup/login + `require_auth` on every data route), Scalar docs at `/scalar` |
| ingestion-service | 8001 | Parses JSON emails/meetings/Jira → normalized docs; fans out to embedding + graph |
| embedding-service | 8002 | Chunks (`RecursiveCharacterTextSplitter`), embeds via OpenAI, upserts to Pinecone |
| graph-service | 8003 | Builds/queries the Neo4j knowledge graph (Person/Project/Decision/Meeting/Ticket/Email nodes) |
| query-service | 8004 | The core: a LangGraph 5-agent pipeline (Planner → Search → Timeline → Decision → Answer) |
| timeline-service | 8005 | Uses OpenAI to extract structured, dated timeline events from Pinecone hits |
| frontend | 8501 | React + TypeScript + Vite SPA (Tailwind v4, shadcn/ui): sign up/sign in, then Ask / Timeline / Graph / Ingest / Health |
| mcp-server | stdio | FastMCP server for live Jira status + SQLite history; run separately, not in compose |

Data path: raw JSON in `data/synthetic/{emails,meetings,jira}/` → ingestion normalizes → embedding (Pinecone vectors) + graph (Neo4j) → query-service orchestrates retrieval across both plus timeline-service to answer.

### Frontend (React + Vite + TypeScript)

`decision-dna/frontend/` is a Vite + React 19 + TypeScript SPA, not the Streamlit app it used to be (that history is why you may see stray references to `app.py`/`requirements.txt` in old branches or docs — the live code is 100% React now). Single-file app in [frontend/src/App.tsx](decision-dna/frontend/src/App.tsx); everything (auth screen, sidebar nav, all five tabs) lives in that one component, styled by [frontend/src/index.css](decision-dna/frontend/src/index.css) (CSS custom properties for the dark-purple theme, e.g. `--accent-purple: #8b5cf6`) plus Tailwind utility classes.

- **shadcn/ui is configured** (`components.json`, `new-york` style, `@/*` → `./src/*` alias in both `vite.config.ts` and `tsconfig.app.json`). Drop new shadcn components into `frontend/src/components/ui/` — that's already the correct default path, no setup needed. `frontend/src/lib/utils.ts` exports the `cn()` helper (`clsx` + `tailwind-merge`).
- **Auth is JWT, not session cookies.** The app posts to `/api/v1/auth/signup` and `/api/v1/auth/login` on the gateway; login returns a token that's stored in `localStorage` (`dna_token`) and sent as `Authorization: Bearer <token>` on every subsequent call via the `authFetch` wrapper in `App.tsx`. A `401` anywhere triggers an automatic logout. The gateway talks to a **native Postgres on the host** (not containerized — see gotcha below) for the `users` table.
- **Talking to the gateway:** `App.tsx` builds `GATEWAY_URL` as `http://${window.location.hostname}:8000` — it infers the gateway host from whatever host the page itself was loaded from, so it works both via `localhost` and via a LAN IP without extra config.
- **Dev vs. prod serving — both are hardcoded to port 8501, and they conflict.** `vite.config.ts` sets `server.port: 8501` with `strictPort: true` (Vite refuses to fall back to another port), matching the Dockerfile's `serve -s dist -l 8501`. You can't run `npm run dev` while the Docker `frontend` container is up — `docker compose stop frontend` first, then `npm run dev`, then `docker compose start frontend` when done. (The port is deliberately *not* 3000 — Vite's dev client has a hardcoded `3e3===+window.location.port` heuristic that misdetects the backend port if served on 3000.)
- **`frontend/.dockerignore` matters.** The Dockerfile does `npm install` then `COPY . .` then `npm run build`; without `.dockerignore` excluding `node_modules`/`dist`, the `COPY` overwrites the image's freshly-installed Linux `node_modules` with whatever's in your local (possibly Windows) `node_modules`, breaking `tsc`/`vite` inside the container.
- Package scripts: `npm run dev` (Vite dev server), `npm run build` (`tsc -b && vite build`), `npm run lint` (`oxlint`), `npm run preview`.

### Critical structural gotchas

- **`shared/` is NOT wired into the services.** Each service's `Dockerfile` does `COPY . .` from its own directory only, so `shared/` never reaches the containers, and no service imports it (`grep` for `from shared` returns nothing). Each `app/main.py` reads config **directly from `os.getenv(...)`** with its own inline defaults. Treat `shared/config/settings.py`, `shared/models/schemas.py`, and `shared/utils/helpers.py` as reference/aspirational scaffolding — editing them does not change running behavior. To change a service's config, edit that service's `main.py` and/or the `.env`.
- **The two context docs can drift from the code.** [decision-dna/AGENT_CONTEXT.md](decision-dna/AGENT_CONTEXT.md) and the README are useful for intent, but when they conflict with source, trust the source. The current LLM path uses OpenAI chat completions through `OPENAI_API_KEY`; Pinecone index defaults to `ai-asset` and embeddings use `1024` dimensions.
- **Neo4j has two possible targets.** `docker-compose.yml` runs a **local** Neo4j (`neo4j:5.15`, auth `neo4j/password123`), but `shared/config/settings.py` defaults to a **cloud Aura** URI. The actual connection is whatever `.env` (`NEO4J_URI`, `NEO4J_PASSWORD`) provides to each service — set this deliberately.
- **Duplicated synthetic data.** Both `data/synthetic/` and `scripts/data/synthetic/` exist. Compose mounts `./data` to `/app/data`, and ingestion reads `/app/data/synthetic`, so `decision-dna/data/synthetic/` is the one that matters at runtime.
- **Every data route requires a JWT, and auth needs two things `docker-compose.yml` doesn't fully provide on its own.** `require_auth` in `api-gateway/app/main.py` guards `/api/v1/ingest`, `/api/v1/query`, `/api/v1/timeline/*`, and `/api/v1/graph/*` — signup/login first via `/api/v1/auth/signup` / `/api/v1/auth/login`, then send `Authorization: Bearer <token>`. That needs (1) `JWT_SECRET` set in `.env` (empty → every auth call `503`s with "Auth is misconfigured"), and (2) a **native Postgres reachable at `POSTGRES_HOST`** (defaults to `host.docker.internal`, resolved via the `extra_hosts` entry already in compose) — the gateway auto-creates the `users` table on startup, but the Postgres *server* itself is not containerized and must already be running.

## Commands

All commands run from `decision-dna/`.

```bash
cd decision-dna

# 1. Configure — no .env.example is committed; create .env from the vars below
#    Required: OPENAI_API_KEY, PINECONE_API_KEY, NEO4J_PASSWORD, JWT_SECRET,
#               POSTGRES_HOST/PORT/DB/USER/PASSWORD (native Postgres for user accounts)
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

Access points after `docker-compose up`: UI http://localhost:8501 · Gateway http://localhost:8000 · Scalar docs http://localhost:8000/scalar · Neo4j browser http://localhost:7474.

Iterating on one service without a full rebuild:
```bash
docker-compose up --build query-service        # rebuild + restart just one
docker-compose logs -f query-service            # tail its logs
```
Uvicorn runs with `--reload` in every service `Dockerfile`, but the code is `COPY`-baked into the image (not bind-mounted), so source edits still require a rebuild of that service to take effect.

## Testing & linting

There is **no backend test suite and no CI** in this repo. The frontend has `oxlint` configured (`npm run lint` in `frontend/`) but nothing else. "Verifying" a change means exercising it against the running stack — e.g. an authenticated `POST /api/v1/query` through the gateway, or the React UI at `:8501` — not running tests.

## Adding to the query pipeline

The 5 agents in [query-service/app/main.py](decision-dna/services/query-service/app/main.py) are plain functions over a shared `AgentState` TypedDict, wired into a LangGraph `StateGraph`. To add or reorder a step: write a `def agent(state) -> AgentState` function, add it as a node, and update the edges. Agents append human-readable strings to `state["processing_steps"]`, which surface in the API response and UI.

## Reference details (consolidated from now-deleted docs)

The sections below were merged in from `README.md`, `AGENT_CONTEXT.md`, `GROQ_MIGRATION_CONTEXT.md`, `PROJECT_STRUCTURE.md`, `WINDOWS_DOCKER_SETUP.md`, and the root `README.md` — kept only where still accurate; anything that conflicted with the source (Streamlit UI, port 3000, port-8501-as-Streamlit, 3072-dim embeddings, Anthropic as the LLM, `.env.example`) was dropped rather than carried over. `AUDIT.md` and `OPTIMIZATION.md` were left as standalone files — they're detailed, occasion-specific reports (perf audit + the fixes applied), not everyday reference material, so they're better opened only when someone's actually working on performance.

### Gateway routes (api-gateway :8000)

- `POST /api/v1/auth/signup`, `POST /api/v1/auth/login` — JWT issuance, backed by native Postgres
- `POST /api/v1/ingest`, `GET /api/v1/ingest/status/{job_id}` — trigger/poll ingestion (auth required)
- `POST /api/v1/query` — ask a question (auth required)
- `GET /api/v1/timeline/{topic}?project=` — decision timeline for a topic (auth required)
- `GET /api/v1/graph/decisions`, `GET /api/v1/graph/entities/{entity}` — Neo4j reads (auth required)
- `GET /health` — fan-out health check across all services
- `GET /scalar` — OpenAPI docs UI

### Neo4j graph schema (graph-service :8003)

Nodes: `Person {name}`, `Project {name}`, `Decision {id, description, date, project}`, `Meeting {id, title, date, project}`, `Ticket {id, title, status, date}`, `Email {id, subject, date, project}`.
Relationships: `(Person)-[:ATTENDED]->(Meeting)`, `(Person)-[:INVOLVED_IN]->(Ticket)`, `(Person)-[:SENT_OR_RECEIVED]->(Email)`, `(Meeting)-[:PART_OF]->(Project)`, `(Meeting)-[:PRODUCED]->(Decision)`, `(Ticket)-[:PART_OF]->(Project)`.
Entry point is `app/graph_services_main.py` (not `app/main.py` — see the structural gotchas above); it returns `{rel, labels, m}` from `/entities/{name}`, matching what the frontend reads.

### Ingested document format

Every parser (email/meeting/Jira) normalizes into this shape before it's chunked and embedded:

```json
{
  "doc_id": "EMAIL-001",
  "doc_type": "email",
  "title": "Subject line",
  "content": "Combined text for embedding",
  "date": "2024-01-15T10:30:00",
  "participants": ["sender", "recipients"],
  "project": "CloudMigration",
  "tags": ["migration", "azure"],
  "source_path": "/app/data/...",
  "raw": { "...": "original parsed JSON" }
}
```

Raw source JSON per type: emails have `id/from/to/date/subject/body/project/tags`; meetings have `id/title/date/attendees/discussion/decisions/action_items/project/tags`; Jira tickets have `id/title/description/status/priority/reporter/assignee/created/labels/project/comments`.

### MCP server (Jira integration, stdio — `mcp-server/`)

Run standalone with `python mcp-server/server.py`, needs `JIRA_BASE_URL`, `JIRA_EMAIL`, `JIRA_API_TOKEN`, `JIRA_STATUS_DB_PATH`. Tools: `fetch_jira_ticket_status(ticket_key)`, `fetch_many_jira_ticket_statuses(ticket_keys)`, `get_stored_jira_ticket_status(ticket_key)`, `get_jira_ticket_status_history(ticket_key, limit)`. Persists to SQLite: a current-status table, status snapshots, and a status-transitions table (`from_status`, `to_status`, `changed_at`, `author`).

### LLM provider routing

Chat completions prefer Groq (`GROQ_API_KEY` set → `llama-3.3-70b-versatile` by default, ~95% cheaper) and fall back to OpenAI (`gpt-4o-mini`) when it isn't set. Embeddings always go to OpenAI — Groq has no embeddings endpoint. `app/llm.py` (per-service) auto-detects a retired/unavailable Groq model via the provider's `/models` catalogue and switches chat models automatically (`CHAT_MODEL_AUTO_FALLBACK=false` to disable); `/health` reports both the configured and effective chat model. This replaced an earlier bug where ambiguous `or`-chained env lookups could send a Groq key to the OpenAI endpoint — fixed in all three `provider_config.py` copies (embedding/query/timeline-service).

### Windows + Docker Desktop setup essentials

- `.\start.ps1` (from `decision-dna/`) automates the manual steps below: preflight checks, generates `corp-ca.crt` + a `.env` template if missing, builds, starts, polls `/health`. Flags: `-SkipBuild`, `-Down`.
- **Corporate TLS interception** (Cloudflare Gateway / Zscaler, common on corporate networks) breaks `pip install` and runtime Pinecone/OpenAI calls inside fresh containers with `CERTIFICATE_VERIFY_FAILED` unless a `corp-ca.crt` is copied into all 7 build contexts (`api-gateway`, `frontend`, and each of the 5 `services/*`). Gitignored; regenerated per machine, not committed. See the note in the audit — 7 copies to keep in sync, kept deliberately rather than deduplicated.
- Docker Desktop needs **8 GB+ RAM** allocated (Neo4j + 6 Python services + Redis); 4 GB thrashes. Give it WSL2 file sharing access to whichever drive the repo is on — a network share is slow to build from.
- Host port conflicts: don't edit `docker-compose.yml` — set `GATEWAY_HOST_PORT` / `REDIS_HOST_PORT` overrides in `.env` instead; the internal ports (and the UI, which talks over Docker's internal network) keep working either way.
- Common failure → cause: `COPY corp-ca.crt ... not found` → Section 3 skipped, create the file. `CERTIFICATE_VERIFY_FAILED` during `pip install` → CA not trusted in the build, rebuild with `--no-cache`. `pinecone ... 401` at startup → bad/missing `PINECONE_API_KEY` or the `ai-asset` index doesn't exist yet. `Bind for 0.0.0.0:XXXX failed` → another process holds that host port. Services show `running` in `docker compose ps` even after a startup crash because of `--reload` — always confirm with `docker compose logs <service>`.

### Environment variables (non-exhaustive, see `.env` on disk for the live set)

Required: `OPENAI_API_KEY`, `PINECONE_API_KEY`, `PINECONE_INDEX_NAME` (defaults `ai-asset`), `EMBEDDING_DIMENSIONS` (`1024`), `NEO4J_URI`/`NEO4J_PASSWORD`, `REDIS_URL`, `JWT_SECRET`, `POSTGRES_HOST`/`PORT`/`DB`/`USER`/`PASSWORD`. Optional: `GROQ_API_KEY`/`GROQ_CHAT_MODEL`/`GROQ_BASE_URL`, `OPENAI_CHAT_MODEL`, service URL overrides (`INGESTION_SERVICE_URL` etc., docker network names — don't change unless you know why), `JIRA_*` (MCP server only), `LLM_STUB_FALLBACK`, `QUERY_CACHE_TTL`/`TIMELINE_CACHE_TTL`, `DECISION_CONTEXT_CHUNKS`, `RETRIEVAL_MIN_SCORE`.
