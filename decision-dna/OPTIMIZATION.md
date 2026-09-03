# LangGraph pipeline optimisation — engineering report

**Date:** 21 Aug 2026 · **Scope:** `decision-dna/` · **Constraint:** no functional change to the API contract.

Headline: the `/api/v1/query` critical path went from **5 sequential LLM generations to 2**, the
service went from **1 concurrent query to N**, and the `/query` response is byte-compatible —
same keys, same `sources` shape, same `processing_steps` strings.

---

## A. Files inspected

**Read in full before any edit:**

| Area | Files |
|---|---|
| query-service | `app/main.py`, `app/fallbacks.py`, `app/provider_config.py`, `requirements.txt`, `Dockerfile` |
| timeline-service | `app/main.py`, `app/fallbacks.py`, `app/provider_config.py`, `requirements.txt`, `Dockerfile` |
| embedding-service | `app/main.py`, `app/fallbacks.py`, `app/provider_config.py`, `requirements.txt`, `Dockerfile` |
| graph-service | `app/main.py`, `app/graph_services_main.py`, `requirements.txt`, `Dockerfile` |
| ingestion-service | `app/main.py`, `app/parsers/{__init__,email_parser,meeting_parser,jira_parser}.py`, `Dockerfile` |
| api-gateway | `app/main.py`, `requirements.txt`, `Dockerfile` |
| frontend | `src/App.tsx`, `vite.config.ts`, `package.json`, `Dockerfile` |
| orchestration | `docker-compose.yml`, `.env`, `start.ps1` |
| scripts | `generate_data.py`, `ingest_all.py`, `seed_neo4j.py` |
| shared (dead) | `config/settings.py`, `models/schemas.py`, `utils/{helpers,fallbacks,provider_config}.py` |
| tests / docs | `tests/test_fallbacks.py`, `test_query.py`, `CLAUDE.md`, `AGENT_CONTEXT.md`, `GROQ_MIGRATION_CONTEXT.md`, `PROJECT_STRUCTURE.md` |
| data | `data/synthetic/**`, `scripts/data/synthetic/**` |

**Library versions were verified empirically, not assumed.** A venv pinned to the exact
`requirements.txt` versions (`langgraph==0.1.5`, `langchain-openai==0.1.8`, `openai==1.30.1`,
`pinecone-client==3.2.2`, `fastapi==0.111.0`) established:

- `START` / `END` import from `langgraph.graph`. ✅
- Fan-out from `START` requires **at least one `Annotated[..., reducer]` state channel**, otherwise
  `add_edge` raises `ValueError: Already found path for node '__start__'`.
- Parallel branches genuinely overlap under `ainvoke` (two 0.4 s sleeps completed in 0.41 s).
- A node in a parallel branch **must return a partial dict**. Returning the whole state — the old
  code's style — raises `InvalidUpdateError` even for unchanged keys.
- **`.invoke()` cannot drive async nodes** (`KeyError <Future ...>`), so converting nodes to
  coroutines forces `ainvoke`.
- `OpenAIEmbeddings` has `aembed_documents` / `aembed_query`; `AsyncOpenAI.chat.completions.create`
  returns an awaitable; **`pinecone-client` 3.2.2 has no async API** (async Pinecone arrives in v5),
  so its calls are offloaded with `asyncio.to_thread`.

No dependency was upgraded. Two services gained `redis==5.0.4`, the pin already used by
api-gateway and ingestion-service, for the read-path cache.

---

## B. Diagnosis verification

### Confirmed

| ID | Finding | Evidence |
|---|---|---|
| L1 | 5 sequential LLM generations | `build_graph()` was a straight chain; measured 5 calls / 5300 `max_tokens` |
| L1a | `timeline_agent` reads only `question` + `project_filter` | no other state key referenced — a false dependency |
| L1b | `answer_agent` re-narrates `decision_analysis` | its prompt contained only that field; chunks were used solely for `sources` |
| L2 | Event loop blocked | `async def query()` calling sync `agent_graph.invoke()`; same shape in `build_timeline` |
| L3 | Corpus almost empty | mounted `data/synthetic` held 3 emails / 3 meetings / 5 tickets; the real 250 docs sat in `scripts/data/synthetic/` |
| L4 | Planner multiplies retrieval | one `embed_query` HTTP call **and** one Pinecone query per sub-task, serially |
| L5 | Serial health fan-out | `for name, url in SERVICES.items()` with `timeout=5`; frontend polled every 15 s |
| L6 | Per-request HTTP clients | `async with httpx.AsyncClient(...)` inside `proxy()`, the timeline agent and the ingest job |
| L6a | Thread + event loop per request | `timeline_agent` built a `ThreadPoolExecutor(max_workers=1)` and called `asyncio.run` |
| L7 | Duplicate chunk metadata | `chunk_document` stored `content` **and** `content_preview`; 15 full chunks reached the prompt |
| L8 | No read-path cache | Redis used only for rate-limit counters and job state |
| L9 | Production `--reload` | present in all six service Dockerfiles |
| L10 | Permanent fallback downgrade | `generate_text` / `_embed_texts` reassigned the module global on any exception, with no recovery |
| L10a | Fallback vectors reached the real index | `_embed_texts` swapped in hash vectors and the next line upserted them into Pinecone |
| L12 | Two Redis round trips per request; no client timeout; fragile confidence parsing | `INCR` + `EXPIRE`; no `AbortController`; `float(line.split(":")[1])` |

### Incorrect / outdated in the diagnosis

**L11 was backwards, and this matters.** The diagnosis claimed `graph-service/app/main.py` was live
with broken Cypher and `graph_services_main.py` was an unused repaired copy. The Dockerfile actually
runs:

```
CMD ["uvicorn", "app.graph_services_main:app", ...]
```

So the **repaired module is already the entry point**. `graph_services_main.py` also already:

- logs and continues when Neo4j is unreachable (`# Don't raise — let health check report the problem`)
  — the crash-loop was fixed;
- carries correct `$param` Cypher throughout;
- builds the `Person / Project / Decision / Meeting / Ticket / Email` node model and returns
  `{rel, labels, m}` from `/entities/{name}` — exactly what `App.tsx` reads. The stale `app/main.py`
  returns `{entity, relationship, other}` over generic `:Document` nodes, which the frontend cannot
  render.

**No change was made to graph-service beyond removing `--reload`.** `app/main.py` is dead code that
should be deleted (see §G). Tests now pin the entry point, the Cypher parameters, the non-fatal
startup and the frontend-compatible node model so nobody "repairs" the CMD back to the broken file.

**L12 (duplicated tracing middleware)** — left alone deliberately. Extracting it would need a shared
package, and each service's Dockerfile does `COPY . .` from its own directory, so a top-level package
never reaches the container. Per the brief, not worth new coupling.

### Already fixed before this work

- Groq/OpenAI provider routing (`provider_config.py` splits `chat_*` from `embedding_*`). The old
  ambiguous `or`-chained version survives only in the unused `shared/utils/provider_config.py`.
- graph-service startup resilience and Cypher (see above).

### Found during the audit, not in the diagnosis

1. **`tests/test_fallbacks.py` was already failing** (`KeyError: 'api_key'`) — it asserted the
   pre-Groq-fix config keys. Baseline was 2 passed / 1 failed.
2. **`scripts/ingest_all.py` could no longer ingest.** `/api/v1/ingest` is behind `require_auth`,
   and the script sent no `Authorization` header, so every run would 401.
3. **`ingest_all.py`'s preflight advertised the poisoning behaviour** as acceptable ("Will use local
   FallbackEmbeddings ... into the real Pinecone index") and proceeded.
4. **A langgraph 0.1.5 trap that the first implementation walked straight into** — see §D.

---

## C. Changes made

### query-service

| File · function | Change | Why |
|---|---|---|
| `app/main.py` · `build_graph()` | `START` fans out to `retrieve` and `timeline_step`; both join `synthesize` | the timeline branch never depended on retrieval |
| `app/main.py` · `AgentState` | `processing_steps` is `Annotated[List[str], operator.add]` | parallel branches both append; langgraph also requires an annotated channel to allow fan-out |
| `app/main.py` · every node | return **partial** dicts, not the whole state | full-state returns raise `InvalidUpdateError` in parallel branches |
| `app/main.py` · `retrieve_agent()` | new: composes `planner_agent` → `search_agent` | planner→search is a real data dependency; keeping them one node also keeps both branches one superstep deep (§D) |
| `app/main.py` · `synthesis_agent()` | replaces `decision_agent` + `answer_agent` with one structured JSON call | the answer agent only re-narrated the decision agent's output |
| `app/main.py` · `search_agent()` | one `aembed_documents(sub_tasks)` batch; Pinecone queries via `asyncio.gather` + `asyncio.to_thread` | was one embed HTTP call and one query per sub-task, serially |
| `app/main.py` · `timeline_agent()` | native async node on the shared client | removes the `ThreadPoolExecutor` + `asyncio.run` workaround |
| `app/main.py` · `query()` | `await agent_graph.ainvoke(...)` | `.invoke()` cannot drive async nodes, and it blocked the loop |
| `app/main.py` · `generate_text()` | bounded retry via `app/llm.py`, raises `LLMUnavailable` → 503 | no more permanent global downgrade |
| `app/main.py` · `parse_confidence()` | regex-based, tolerant of extra colons, clamped | `float(line.split(":")[1])` crashed on `CONFIDENCE: score: 0.7` |
| `app/main.py` · `select_context_chunks()` | top-k cap (`DECISION_CONTEXT_CHUNKS`, default 8) + optional score floor | 15 full chunks reached the prompt with no relevance filter |
| `app/main.py` · `chunk_text_of()` | reads `content` or `content_preview` | backward compatible with already-indexed vectors |
| `app/main.py` · lifespan | one `httpx.AsyncClient`, one Redis client | was a new client per request |
| `app/llm.py` | **new** — `achat()` with bounded exponential backoff + jitter, and `is_retryable()` error classification | a 4xx from the provider (bad model id, bad key, no access) is a config error: it fails on the first attempt with a message naming the model and the env var, instead of burning the retry budget |
| `app/cache.py` | **new** — corpus-version-namespaced Redis cache | |

### timeline-service

| File · function | Change | Why |
|---|---|---|
| `app/main.py` · `extract_timeline()` | one call returning `{events, outcome, confidence}`, replacing `extract_events_with_openai` + `assess_outcome` | the second call summarised what the first had just produced |
| `app/main.py` · `search_pinecone()` | `async def`, `aembed_query`, Pinecone via `asyncio.to_thread` | the async route was calling sync helpers on the loop thread |
| `app/main.py` · `normalise_timeline_payload()` | accepts the merged object **or** the legacy bare array | tolerant of model drift |
| `app/main.py` · `chunk_preview_of()` | derives the preview from `content` when absent | metadata slimming |
| `app/main.py` · `build_timeline()` | Redis cache on `(topic, project)` | pure function of its arguments; widest block on the trace |
| `app/main.py` · `generate_text()` | bounded retry, no permanent downgrade | |
| `app/{llm,cache}.py` | **new** (per-service copies) | `shared/` never reaches a container |

### embedding-service

| File · function | Change | Why |
|---|---|---|
| `app/main.py` · `assert_upsert_allowed()` | **new hard rule**: fallback vectors may only be written to a `FallbackIndex`, never a real Pinecone index. No env override. | hash vectors have no TTL — they degrade retrieval permanently, not for the outage |
| `app/main.py` · `_embed_texts()` | bounded retry (transient only), then 503; returns `(vectors, used_fallback)` instead of mutating a global | same classification: a rejected model id or key fails immediately, and nothing reaches the index |
| `app/main.py` · `chunk_document()` | drops `content_preview` from stored metadata | it was a second copy of the first 200 chars of `content`, returned on every search |
| `app/main.py` · `embed_and_upsert()` / `/selftest` | async; guard enforced before every write | |

### api-gateway

| File · function | Change | Why |
|---|---|---|
| `app/main.py` · lifespan | replaces `on_event`; owns one `httpx.AsyncClient` (200 conns / 40 keep-alive) | `proxy()` built a fresh client per call |
| `app/main.py` · `gateway_health()` | `asyncio.gather` fan-out, 1.5 s per-probe timeout, 10 s in-process TTL cache | was serial with a 5 s timeout each — 25 s worst case, polled per browser tab |
| `app/main.py` · `rate_limit()` | one pipeline (`INCR` + `EXPIRE ... NX`); tolerates Redis failure | two round trips per request; `NX` reproduces `if current == 1` and closes a race |
| `app/main.py` · `proxy()` | uses the shared client | |

### ingestion-service

- Lifespan-scoped `httpx.AsyncClient` instead of one per job.
- `INCR dna:corpus_version` after a successful ingest — the invalidation signal both read caches key on.
- A failing `embed-batch` now marks the job **failed** instead of reporting "completed".

### scripts, frontend, Docker

- `generate_data.py` — `DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parent.parent / "data" / "synthetic"`,
  plus `--out` with a warning when it isn't the mounted tree. **Root cause fixed**, verified by running
  the script from `scripts/` and from `/`.
- `ingest_all.py` — `DNA_TOKEN` or `DNA_USER`/`DNA_PASSWORD` auth; aborts before ingest when the
  embedding provider is down rather than describing index poisoning as acceptable.
- `App.tsx` — `fetchWithTimeout` with `AbortController` (query 90 s, timeline 60 s, auth/short 15 s,
  health 10 s) and a friendly `RequestTimeoutError`; health poll 15 s → 60 s.
- All six service Dockerfiles — `--reload` removed. **`--workers` was deliberately not added**: the
  services hold module-level singletons and a per-process health cache, so multi-worker safety needs
  its own review now that async correctness is in place.

### Data

`data/synthetic/{emails,meetings,jira}/` now holds the generated 250-document corpus.
The 11 hand-written demo documents moved to `<kind>/seed/`, which the parsers' non-recursive
`glob("*.json")` does not pick up — preserved on disk, out of the ingest path, and no longer
colliding with the generated `EMAIL-001` / `MTG-001` / `PROJ-001` IDs. Nothing was deleted.

---

## D. Final LangGraph topology

```
                    ┌── retrieve ────────┐
                    │   planner (LLM)    │
  START ────────────┤   embed batch      ├──► synthesize (LLM) ──► END
                    │   search ×N concur │
                    │                    │
                    └── timeline_step ───┘
                        (timeline-service:
                         embed → search → 1 LLM)
```

**Why `retrieve` is one node, not `planner → search`.** langgraph 0.1.5 does not defer a node until
every inbound edge has fired; it advances in supersteps and runs a node once per superstep in which
any inbound edge triggers it. With branches of unequal depth:

```
superstep 1: planner, timeline_step
superstep 2: search        <- from planner
             synthesize    <- from timeline_step, which already finished
superstep 3: synthesize    <- from search   ← runs a SECOND time
```

The first implementation had exactly this shape and `synthesize` executed twice — an extra 2000-token
generation and duplicated `processing_steps`. `test_exactly_two_llm_generations_on_the_critical_path`
caught it (3 calls instead of 2). Collapsing planner+search into one node makes both branches one
superstep deep. `test_synthesis_runs_exactly_once` is the regression guard.

Critical path now: `max(retrieve, timeline)` then `synthesize` — **2 generations**. The planner's
generation still happens, but it is fully hidden behind the longer timeline branch, so it costs no
wall-clock and its retrieval-quality contribution is kept (`PLANNER_ENABLED=false` disables it).

---

## E. Performance comparison

Reproduce with `python bench/compare.py`.

**Method.** Both pipelines run against identical deterministic stand-ins for Groq/OpenAI/Pinecone,
over the real restored corpus, driving the real code paths. Counts and character totals are
**exact**. Wall-clock is **simulated** under `bench/model.py`
(chat = 0.25 s + 1.2 ms/token ≈ 830 tok/s; embedding = 80 ms; Pinecone = 60 ms) — read the ratios,
not the seconds. No real provider was called, so these are not production latencies.

| Metric | Before | After | Change |
|---|---:|---:|---:|
| LLM generations (total) | 5 | 3 | −40% |
| **LLM generations on the critical path** | **5** | **2** | **−60%** |
| LLM `max_tokens` budget | 5,300 | 4,500 | −15% |
| Prompt characters sent | 15,299 | 10,933 | −29% |
| Prompt tokens (est., chars ÷ 4) | 3,825 | 2,733 | −29% |
| Embedding HTTP requests | 4 | 2 | −50% |
| Texts embedded | 4 | 4 | same |
| Pinecone queries | 4 | 4 | same (now concurrent) |
| Chunks retrieved (basis for `sources`) | 15 | 15 | same |
| Chunks in the synthesis prompt | 15 | 8 | −47% |
| Wall clock, 1 query | 8.19 s | 5.46 s | −33% |
| **Wall clock, 4 concurrent queries** | **32.76 s** | **5.47 s** | **−83%** |
| Concurrent queries making progress | 1 | N | — |
| `sources` returned | 5 | 5 | same |
| `confidence_score` | 0.78 | 0.78 | same |
| Response keys identical | — | ✅ | |
| `processing_steps` identical | — | ✅ | |

Four concurrent queries finish in the time of one, which is the `.invoke()` → `ainvoke()` fix showing
up. Not measured here: the Redis caches (a repeat question skips the pipeline entirely) and the
gateway/HTTP-client changes, which sit outside the pipeline.

---

## F. Tests

`python -m pytest tests/ -q` → **82 passed, 0 failed** (was 2 passed / 1 failed).

| Area | Coverage |
|---|---|
| Graph topology | timeline independent of retrieval; declared topology matches the compiled graph; decision+answer merged; **synthesis runs exactly once** |
| Async execution | `ainvoke` drives the graph; exactly 2 generations in query-service |
| Concurrency | branches overlap (two 0.3 s branches finish in < 0.5 s); 4 concurrent Pinecone searches in < 0.4 s; two timeline searches overlap |
| Fallback behaviour | transient failure retried then succeeds; **the client is never permanently replaced**; retry budget bounded; exhaustion → 503, not a fake 200; stub mode is opt-in; backoff exponential, capped, jittered |
| Embedding safety | **fallback vectors never upserted into a real index** (index left empty); guard enforced at the write; stub-index path still works; retries before giving up; metadata no longer duplicates chunk text |
| Data ingestion | `generate_data.py` output anchored to the repo not the CWD, matching the compose mount; parsers discover 250 docs with 0 ID collisions; curated seed preserved and excluded |
| Retrieval | sub-tasks embedded in one batch; top-k cap without touching `sources`; score threshold never empties the prompt; both metadata shapes readable |
| API contract | `/query` keys unchanged; `processing_steps` strings unchanged; `sources` shape unchanged; non-JSON synthesis still answers |
| Timeline | one LLM call not two; response model unchanged; events sorted; empty corpus short-circuits with no LLM call; legacy array shape accepted |
| Graph service | entry point pinned to `graph_services_main`; Cypher parameters present; startup non-fatal; node model matches `App.tsx` |
| Health / limiter | probes concurrent; per-service state reported; unreachable ≠ raised; TTL cache; one Redis round trip; 100/window semantics preserved; limiter failure doesn't reject traffic |
| Confidence parsing | `0.85`, `0.5`, `invalid`, `text: 0.7`, case, clamping, absent |

**Not run:** `npm run lint` / `tsc -b` against the full frontend, `docker compose up`, and any call to
a real provider. `docker-compose.yml` parses and all build contexts resolve; `App.tsx` type-checks
clean apart from module resolution (`node_modules` is absent in this environment).

---

## H. Provider resilience (added 21 Aug 2026, after live runs)

Two production failures exposed the same class of gap: the code treated every
provider error as one thing.

**A retired model id is not an outage.** Groq removed the Llama families from its
catalogue, so the pinned `llama-3.3-70b-versatile` began returning
`404 model_not_found` on every request. `app/llm.py` now recognises that specific
rejection, reads the provider's live `/models` catalogue once per process, and
continues with the best available chat model (preferring `openai/gpt-oss-120b`,
then `openai/gpt-oss-20b`, then the compound systems). It is logged loudly and
`/health` reports both `chat_model` (effective) and `chat_model_configured`.
`CHAT_MODEL_AUTO_FALLBACK=false` opts out. The stale default was also replaced in
all three `provider_config.py` copies.

**429 is two opposite problems.** `rate_limit_exceeded` is waitable;
`insufficient_quota` means the account has no credit and no amount of waiting
helps. They are now separated: quota exhaustion fails on the first attempt with
HTTP 402, genuine throttling honours `Retry-After` / `x-ratelimit-reset-*` (up to
90s for a bulk ingest) and halves the batch when the limit is on tokens rather
than requests. Partial progress is reported — `"N of M chunks were embedded"` —
instead of a bare `failed`.

`scripts/check_provider.py` reports all of this against the real `.env` in one
command, and prints the exact line to change.

---

## G. Remaining risks and follow-ups

1. **Nothing was verified against a live stack.** Docker, Neo4j, Postgres, Pinecone and Groq were not
   reachable here. Run `docker compose up --build`, then `python scripts/ingest_all.py`, then one
   authenticated `POST /api/v1/query` before trusting the numbers.
2. **Re-ingestion is required.** The index still holds vectors for the old 11-document corpus, and
   embedding-service no longer writes `content_preview`. Readers accept both shapes, so a mixed index
   works, but a clean re-ingest of all 250 documents is the intended next step.
3. **One intentional behaviour change.** The system used to answer HTTP 200 with stub text when a
   provider failed. It now retries transient failures with backoff and then returns **503**;
   permanent provider rejections (4xx — unknown model, bad key, no access) fail on the first attempt
   with a message naming the model and the environment variable to change. Set `LLM_STUB_FALLBACK=1`
   to restore the old behaviour — responses are then marked `degraded`.

   This is what surfaced the `llama-3.3-70b-versatile` 404 rather than hiding it behind a
   plausible-looking canned answer.
4. **Query and timeline caches are on by default** (1 h TTL, invalidated by `dna:corpus_version`).
   A repeat question inside the window returns the identical stored answer. Set `QUERY_CACHE_TTL=0`
   / `TIMELINE_CACHE_TTL=0` to disable.
5. **Files I could not delete** (the device bridge can write but not remove). Delete these manually:
   - `services/graph-service/app/main.py` — the stale, broken-Cypher module. Not imported; the
     Dockerfile runs `graph_services_main`. Tests will fail if the CMD is ever pointed back at it.
   - `scripts/data/synthetic/` — the stray corpus, now restored to `data/synthetic/`.
   - `shared/` — unreachable from every container, and `shared/utils/provider_config.py` is the
     pre-fix Groq routing bug waiting to be copied back.
6. **`--workers` was not enabled.** Do that only after reviewing the module-level singletons and the
   per-process health cache.
7. **The synthesis prompt asks for JSON without `response_format`.** Deliberate — the parameter is not
   uniformly supported across Groq and the stub client. Parsing is tolerant (fenced, bare, embedded)
   and falls back to the legacy text parse, but a model that ignores the instruction produces a
   plainer answer. Worth watching in the logs (`response was not usable JSON`).
8. **`DECISION_CONTEXT_CHUNKS=8` is a starting value**, not a profiled optimum, and
   `RETRIEVAL_MIN_SCORE` ships at `0.0` (disabled). Tune both against real retrieval once the full
   corpus is indexed.
9. **The tracing middleware is still duplicated six times.** Left alone on purpose — see §B.
