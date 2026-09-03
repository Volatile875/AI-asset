# DecisionDNA — Cleanup & Latency Audit

_Read from source at `C:\project\AI_asset`, 21 Aug 2026. Call counts, token budgets, dependency
lists and file sizes are taken from the code and the tree. Wall-clock durations are **not**
measured — `logs/` contains PowerShell transcripts of `start.ps1`, not application traces._

**Headline:** every `/api/v1/query` makes **five LLM generations strictly one after another**
(~5,300 `max_tokens` on the critical path), the service **blocks its own event loop** while doing
it so only one query runs at a time, and all of that reasoning currently happens over a corpus of
**three emails**.

---

## Part 1 — What is making it slow

Ranked by wall-clock impact.

### L1 · Five LLM generations on one strict chain — **critical**
`query-service/app/main.py`, `build_graph()`

The LangGraph edges are a straight line: `planner → search → timeline_step → decision → answer`.
Response time is the sum of all five generations plus every embedding and Pinecone round trip in
between.

Two of those dependencies are not real:

- `timeline_agent` reads only `state["question"]`. It never needed to wait for planner or search —
  it could start at t=0. It is also the widest block on the path (2,000 + 300 `max_tokens` inside
  timeline-service).
- `answer_agent`'s prompt contains **only** `state["decision_analysis"]`. It re-narrates what the
  decision agent already wrote, at a *larger* token budget (1,500 vs 1,000) than the analysis itself.

**Fix:** fan the timeline node out from the entry point to run concurrently with planner + search,
joining before `decision`. Merge decision + answer into one call that emits the structured block and
the prose answer together. Critical path: five generations → two.

### L2 · The service can only serve one query at a time — **critical**
`query-service` `POST /query`; `timeline-service` `GET /timeline/{topic}`

The route is `async def` but the body calls the **synchronous** `agent_graph.invoke(...)`. Every
blocking OpenAI/Groq/Pinecone call inside the pipeline runs on the event loop thread, so a second
request — and even `GET /health` — waits for the first to finish. `timeline-service` has the same
shape: `async def build_timeline` calling sync `search_pinecone()` and `generate_text()`.

This compounds everything else here: a 30 s pipeline becomes a 60 s wait for user two rather than a
parallel 30.

**Fix:** convert nodes to `async def` and `await agent_graph.ainvoke(...)`; as a one-line stopgap,
`starlette.concurrency.run_in_threadpool`. Same for `build_timeline`.

### L3 · Retrieval is running against three documents — **critical**
`data/synthetic/` vs `scripts/data/synthetic/`

Compose mounts `./data` → `/app/data`; ingestion reads `/app/data/synthetic`. That file
(`data/synthetic/emails/emails.json`, 2 KB) contains **3 emails**. The full generated corpus —
69 KB emails / 52 KB meetings / 90 KB Jira — sits in `scripts/data/synthetic/`, which nothing reads.

Cause: `generate_data.py` sets `OUTPUT_DIR = Path("data/synthetic")`, resolved against the working
directory. Run from `scripts/`, it creates a second tree — and that is the one with the real data.

The pipeline pays full price for five generations while reasoning over almost nothing, producing
vague low-confidence answers that read like the model struggling.

**Fix:** move the populated files into `decision-dna/data/synthetic/`, delete `scripts/data/`,
re-ingest, and make the path absolute:
`Path(__file__).resolve().parent.parent / "data" / "synthetic"`.

### L4 · The planner multiplies every retrieval cost after it — **high**
`planner_agent`, `search_agent`

One LLM call splits the question into 2–4 sub-tasks. `search_agent` then issues **one
`embed_query` HTTP call per sub-task**, then one Pinecone query per sub-task — serially. One question
becomes up to four embedding round trips and four vector searches.

The payoff is thin: results are merged, deduped and truncated to top-15 anyway, and when the JSON
fails to parse the code silently falls back to `[question]` with no measurable loss.

**Fix:** one `embed_documents(sub_tasks)` batch call; fire Pinecone queries with `asyncio.gather`.
For a few-hundred-document corpus, consider dropping the planner node entirely.

### L5 · Health polling fans out serially, every 15 s, per browser tab — **high**
`api-gateway` `gateway_health()`; `frontend/src/App.tsx:79`

`setInterval(fetchHealth, 15000)` on mount. The gateway walks all five services in a `for` loop with
a 5 s timeout each — worst case 25 s per poll. Because of L2, query-service's `/health` reliably
burns the full 5 s while a query is running.

**Fix:** `asyncio.gather` the probes, drop the timeout to ~1.5 s, cache in Redis for 10 s, raise the
client interval to 60 s.

### L6 · A new HTTP client, thread and event loop per request — **high**
`api-gateway proxy()`; `query-service timeline_agent`

`proxy()` opens `async with httpx.AsyncClient(timeout=120)` on every call — no connection pool, fresh
TCP handshake each time. Ingestion and the timeline agent do the same.

`timeline_agent` goes further: because it is a sync LangGraph node called from a running loop, it
builds a `ThreadPoolExecutor(max_workers=1)` and calls `asyncio.run` on a new event loop — a thread,
a loop and an HTTP client constructed and torn down for one GET.

**Fix:** one module-level `AsyncClient` per service, created on startup and closed on shutdown. Make
`timeline_agent` natively async and delete the thread-pool workaround (unnecessary once L2 is fixed).

### L7 · Chunk text stored twice, shipped whole into every prompt — **high**
`embedding-service chunk_document()`; `query-service decision_agent`

Each Pinecone vector's metadata carries both `content` (full 800-char chunk) **and**
`content_preview` (first 200 chars). Every search runs `include_metadata=True`, so each query pulls
roughly twice the metadata bytes it needs — timeline-service at `top_k=20`.

`decision_agent` then concatenates up to **15 full chunks** (~12 KB ≈ 3,000 input tokens) with no
reranking and no relevance floor.

**Fix:** drop `content_preview` from stored metadata (or keep only the preview and pull full text
from Redis — ingestion already caches `doc:{id}` for 24 h). Trim the decision prompt to the top 6–8
chunks or cut off below a score threshold.

### L8 · Nothing on the read path is cached — **high**

Redis is running, healthchecked, and used only for rate-limit counters and ingest job status. Ask
the same question twice and the whole five-call pipeline runs twice.

`GET /timeline/{topic}?project=` is a pure function of its two arguments and sits on the widest part
of the trace.

**Fix:** cache timeline responses keyed on `(topic, project)`, 1 h TTL. Cache query answers keyed on
a hash of `(question, project_filter)`. Optionally memoize query embeddings by text hash.

### L9 · Every container runs uvicorn `--reload`, which can never fire — **medium**
all six `Dockerfile` `CMD`s

`--reload` starts a `watchfiles` supervisor that continuously stat-walks `/app`, doubles the process
count per container and pins each service to one worker. The code is `COPY`-baked into the image, not
bind-mounted, so nothing it watches ever changes. Pure overhead.

**Fix:** drop `--reload`; add `--workers 2`+ for these IO-bound services — but fix L2 first, or extra
workers just move the queue.

### L10 · One transient error downgrades the process permanently — and poisons the index — **critical**
`*/app/fallbacks.py`, `generate_text()`, `_embed_texts()`

On **any** exception, `generate_text()` reassigns the global `openai_client` to
`FallbackOpenAIClient` — permanently, for the life of the process, with no path back. One timeout or
429 and the service returns canned strings ("Local fallback response generated because the external
AI service was unavailable") at HTTP 200 forever. It will look fast and be worthless.

The embedding side is worse: `_embed_texts()` swaps in `FallbackEmbeddings` — deterministic sparse
hash vectors with no semantic meaning — and the next line **upserts them into the real Pinecone
index**. Those don't expire. Retrieval degrades permanently rather than for the outage.
`GROQ_MIGRATION_CONTEXT.md` records exactly this run (`retrieved 0 unique chunks`) and logs it as a
success.

**Fix:** retry with backoff, then 503 — don't silently substitute stub output. Never upsert fallback
vectors into the real index (write nowhere, or a separate namespace). Surface `degraded` in the
response body the UI renders, not behind a 200.

### L11 · graph-service crash-loops when Neo4j is late, and its Cypher is broken anyway — **high**
`graph-service/app/main.py`

Unlike the other five services, it raises out of the startup hook when `verify_connectivity()` fails.
With `restart: unless-stopped` that becomes a crash loop, and `api-gateway`'s `depends_on` lists it
with no `condition`, so the gateway comes up pointing at a flapping service → 5 s health timeouts and
503s.

Separately, the live file's Cypher has lost its parameter markers:
`MERGE (d:Document {id: })` and `WHERE d.project = `. Those are syntax errors — `/build-graph`,
`/decisions` and `/project-timeline` cannot succeed. The repaired version with correct `$param`
Cypher sits unused in the same folder as `graph_services_main.py`.

**Fix:** log and continue on startup like the others; add `condition: service_started` to the
gateway's dependency. Promote `graph_services_main.py` to `main.py`; delete the broken one.

### L12 · Smaller taxes worth collecting — **low**

- **Rate limiter = 2 Redis round trips per request** (`INCR` + `EXPIRE`) on every route. A Lua script
  or a pipelined pair makes it one.
- **120 s proxy timeout, no client counterpart.** Frontend `fetch` calls use no `AbortController`, so
  a stuck pipeline holds a gateway connection for two minutes. With a single-worker query-service, a
  few stuck queries stall everyone.
- **Fragile confidence parsing.** `float(line.split(":")[1].strip())` breaks on any second colon in
  that line, silently leaving the previous value rather than the intended 0.5 default.
- **Six copies of the tracing middleware** — identical `trace_requests` pasted into all six services.

---

## Part 2 — Safe to remove

None of these are on the request path, so deleting them won't make a query faster — but several are
actively misleading, and the unused dependencies do slow builds and cold starts.

| Path | What it is | Verdict |
|---|---|---|
| `shared/` | Never imported. Each Dockerfile does `COPY . .` from its own dir, so it never reaches a container; every service reads config from `os.getenv`. `shared/utils/provider_config.py` is the **pre-fix** version with the ambiguous `or` chaining that `GROQ_MIGRATION_CONTEXT.md` documents as the cause of the Groq 401. | Delete |
| `graph-service/app/graph_services_main.py` | 17.5 KB alternate implementation, never imported (Dockerfile runs `app.main:app`). It is also the *working* one. | Promote, delete the other |
| `scripts/data/synthetic/` | 211 KB duplicate corpus from running `generate_data.py` in the wrong dir. Holds the real 250-doc dataset (see L3). | Move, then delete |
| `data/decisiondna_seed_check.cypher` | Byte-identical to `decisiondna_seed.cypher` — both exactly 10,263 bytes. | Delete |
| `frontend/requirements.txt` | Placeholder whose entire content says Python requirements are no longer used. | Delete |
| `requirements.txt` (root) | Referenced by no Dockerfile. Still pins `anthropic==0.28.0` and `streamlit==1.35.0` — neither imported anywhere. | Trim or delete |
| `services/graph-service/requirements.txt` | Installs `langchain`, `langchain-openai`, `pinecone-client`, `openai`, `tiktoken`. The service imports **none** — only `neo4j`, `fastapi`, `pydantic`. Hundreds of MB of image and a slower cold start for nothing. | Strip 5 deps |
| embedding / query / timeline requirements | `langchain-community` unused; `langchain` pulled in for `RecursiveCharacterTextSplitter` alone (now `langchain-text-splitters`); `tiktoken` never imported directly. `pydantic-settings` and `python-dotenv` unused across all six — nothing reads `settings.py`, and compose's `env_file` handles the environment. | Strip |
| `*/app/fallbacks.py`, `*/app/provider_config.py` | Four copies each (3 services + `shared/`), already diverged. Any fix to L10 must be applied three times and will drift again. | Consolidate |
| `logs/` | 46 PowerShell transcripts of `start.ps1`, ~200 KB, back to July. Gitignored; also contain the machine username and full paths. | Delete |
| `{api-gateway,services/…}/`, `services/{ingestion-service,…}/` | Literal directories named with brace-expansion syntax — residue of `mkdir -p a/{b,c}` run in PowerShell, which doesn't expand braces. Empty; they confuse every recursive tool. | Delete |
| `.mypy_cache/`, `venv/`, `frontend/index.html.vite-backup` | 1,351 cache entries and a local virtualenv inside a tree that runs entirely in Docker, plus a stray Vite backup. Gitignored, but they dominate listings and slow editor indexing. | Delete |
| `test_query.py` (root), `tests/test_fallbacks.py` | A one-off script and a single test file, no runner, no CI. `CLAUDE.md` states there is no backend test suite. | Formalize or delete |
| `PROJECT_STRUCTURE.md` | Documents a Streamlit frontend on port 3000, an uncommitted `.env.example`, and Anthropic Claude as the LLM. All wrong; contradicts the accurate `CLAUDE.md`. | Rewrite or delete |
| `.env` | `OPENAI_API_KEY` declared twice. `GROQ_EMBEDDING_MODEL` set but Groq has no embeddings endpoint — read only by the dead `shared/` config. `SECRET_KEY` unused (auth uses `JWT_SECRET`). `NEO4J_HOST` read by no code. | Prune |
| `corp-ca.crt` ×7 | Copied into all seven build contexts. Gitignored and regenerated by `start.ps1`, so defensible — but seven copies of a secret-adjacent file and seven contexts to keep in sync. | Keep, note it |

---

## Part 3 — Where to start

Ordered by return per hour, not severity.

1. **Point ingestion at the real corpus** (L3) — ~20 min. Nothing else matters if retrieval has three
   documents to search.
2. **Stop blocking the event loop** (L2, L6) — 1–3 hrs. `run_in_threadpool` as a stopgap, then convert
   properly. The difference between one concurrent user and many.
3. **Collapse the chain from five generations to two** (L1, L4) — 2–4 hrs. Parallel timeline, merged
   decision+answer, batched sub-task embeddings.
4. **Clean up the cheap overheads** (L5, L6, L9) — ~1 hr. Drop `--reload`, hoist the `httpx` clients,
   parallelize the health fan-out, back the frontend poll off to 60 s.
5. **Cache timeline and query results in Redis** (L8) — ~2 hrs.
6. **Put a floor under the fallback ladder** (L10) — ~2 hrs.
7. **Delete the dead weight** (L11, Part 2) — ~2 hrs. Resolve the graph-service duplicate first; trim
   unused dependencies while you're in there.

---

## Measuring it

The services already emit per-request timings with correlation ids
(`← POST /query 200 …ms (rid=…)`). Capturing one `docker compose logs` run across a few real
questions turns the estimates above into measured numbers — worth doing before and after step 3.
