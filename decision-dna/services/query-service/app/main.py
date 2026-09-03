"""
services/query-service/app/main.py
The heart of DecisionDNA.

LangGraph pipeline (async, with a genuine parallel branch):

    START ─┬─► planner ──► search ──┐
           │                        ├─► synthesize ─► END
           └─► timeline_step ───────┘

`timeline_step` reads only `question` and `project_filter`, so it has no real
dependency on planner/search and now runs concurrently with them. `synthesize`
replaces the old decision+answer pair: the old answer agent's prompt contained
nothing but `decision_analysis`, so it was a second round trip re-narrating the
first one's output.

Critical path: 2 LLM generations (the parallel timeline branch, then the
synthesis) instead of 5. The planner still runs — it is simply hidden behind
the timeline branch and no longer costs wall-clock.

Response contract is unchanged:
    question, answer, timeline, sources, confidence_score, processing_steps, degraded
"""

from __future__ import annotations

import asyncio
import json
import logging
import operator
import os
import re
import time
import uuid
from contextlib import asynccontextmanager
from urllib.parse import quote
from typing import Annotated, Any, Dict, List, Optional, TypedDict

import httpx
import redis.asyncio as redis
from fastapi import FastAPI, Request, HTTPException
from langchain_openai import OpenAIEmbeddings
from langgraph.graph import END, START, StateGraph
from openai import AsyncOpenAI
from pinecone import Pinecone
from pydantic import BaseModel

from app.cache import CORPUS_VERSION_KEY, ReadCache
from app.fallbacks import FallbackEmbeddings, FallbackIndex, FallbackOpenAIClient
from app.llm import (LLMUnavailable, achat, discover_chat_model, env_flag,
                     is_unknown_model)
from app.provider_config import resolve_provider_config

# ── Logging ────────────────────────────────────────────────────
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s [query-service] %(message)s",
)
log = logging.getLogger("query-service")


# ── Config ─────────────────────────────────────────────────────
PROVIDER_CONFIG = resolve_provider_config()
OPENAI_API_KEY = PROVIDER_CONFIG["chat_api_key"]  # For legacy config references
PINECONE_API_KEY = os.getenv("PINECONE_API_KEY")
INDEX_NAME = os.getenv("PINECONE_INDEX_NAME", "ai-asset")
CHAT_MODEL = PROVIDER_CONFIG["chat_model"]
EMBEDDING_MODEL = PROVIDER_CONFIG["embedding_model"]
EMBEDDING_DIM = int(os.getenv("EMBEDDING_DIMENSIONS", "1024"))
TIMELINE_URL = os.getenv("TIMELINE_SERVICE_URL", "http://timeline-service:8005")
GRAPH_URL = os.getenv("GRAPH_SERVICE_URL", "http://graph-service:8003")
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


# Retrieval knobs. Defaults reproduce the previous retrieval behaviour except
# for DECISION_CONTEXT_CHUNKS, which trims the synthesis prompt only (see below).
SEARCH_TOP_K = _env_int("SEARCH_TOP_K", 5)              # per sub-task, as before
MAX_RETRIEVED_CHUNKS = _env_int("MAX_RETRIEVED_CHUNKS", 15)  # kept: `sources` is derived from this
DECISION_CONTEXT_CHUNKS = _env_int("DECISION_CONTEXT_CHUNKS", 8)  # only what reaches the prompt
RETRIEVAL_MIN_SCORE = _env_float("RETRIEVAL_MIN_SCORE", 0.0)      # 0.0 = threshold disabled
SYNTHESIS_MAX_TOKENS = _env_int("SYNTHESIS_MAX_TOKENS", 2000)
PLANNER_MAX_TOKENS = _env_int("PLANNER_MAX_TOKENS", 500)
PLANNER_ENABLED = env_flag("PLANNER_ENABLED", True)
TIMELINE_TIMEOUT_S = _env_float("TIMELINE_TIMEOUT_S", 60.0)
QUERY_CACHE_TTL = _env_int("QUERY_CACHE_TTL", 3600)     # 0 disables
LLM_STUB_FALLBACK = env_flag("LLM_STUB_FALLBACK", False)
CHAT_MODEL_AUTO_FALLBACK = env_flag("CHAT_MODEL_AUTO_FALLBACK", True)

# The model actually in use. Starts as the configured one and only changes
# if the provider rejects it as unknown and discovery finds a replacement.
EFFECTIVE_CHAT_MODEL = CHAT_MODEL
_model_resolution_attempted = False

# ── Module state (initialised in lifespan, never swapped on call failure) ──
chat_client: Any = None
embeddings_model: Any = None
pc_index: Any = None
http_client: Optional[httpx.AsyncClient] = None
redis_client: Any = None
query_cache: Optional[ReadCache] = None
agent_graph = None

_chat_is_stub = False
_embeddings_is_stub = False
_index_is_stub = False


def init_clients() -> bool:
    """Create the external clients once.

    Construction failures (missing/invalid credentials) fall back to the local
    stubs, exactly as before. What is NOT done any more: swapping a working
    client out for a stub because a single *call* failed. Call failures are
    handled by bounded retries in app.llm and then surfaced.
    """
    global chat_client, embeddings_model, pc_index
    global _chat_is_stub, _embeddings_is_stub, _index_is_stub
    try:
        if chat_client is None:
            try:
                chat_client = AsyncOpenAI(
                    api_key=PROVIDER_CONFIG["chat_api_key"],
                    base_url=PROVIDER_CONFIG["chat_base_url"],
                )
                _chat_is_stub = False
            except Exception as exc:  # noqa: BLE001
                log.error("chat client construction failed, using local stub: %r", exc)
                chat_client = FallbackOpenAIClient()
                _chat_is_stub = True
        if embeddings_model is None:
            try:
                embeddings_model = OpenAIEmbeddings(
                    model=EMBEDDING_MODEL,
                    dimensions=EMBEDDING_DIM,
                    openai_api_key=PROVIDER_CONFIG["embedding_api_key"],
                    base_url=PROVIDER_CONFIG["embedding_base_url"],
                )
                _embeddings_is_stub = False
            except Exception as exc:  # noqa: BLE001
                log.error("embeddings construction failed, using local stub: %r", exc)
                embeddings_model = FallbackEmbeddings(dimensions=EMBEDDING_DIM)
                _embeddings_is_stub = True
        if pc_index is None:
            try:
                pc = Pinecone(api_key=PINECONE_API_KEY)
                pc_index = pc.Index(INDEX_NAME)
                _index_is_stub = False
            except Exception as exc:  # noqa: BLE001
                log.error("Pinecone construction failed, using local stub index: %r", exc)
                pc_index = FallbackIndex()
                _index_is_stub = True
        return True
    except Exception as e:  # noqa: BLE001
        log.exception("client init failed (will retry on demand): %r", e)
        return False


@asynccontextmanager
async def lifespan(app: FastAPI):
    global http_client, redis_client, query_cache, agent_graph
    init_clients()
    # One connection pool for the life of the process instead of one per request.
    http_client = httpx.AsyncClient(
        timeout=httpx.Timeout(TIMELINE_TIMEOUT_S, connect=5.0),
        limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
    )
    try:
        redis_client = redis.from_url(REDIS_URL, decode_responses=True)
        await redis_client.ping()
        log.info("redis: connected at %s", REDIS_URL)
    except Exception as exc:  # noqa: BLE001 - cache is optional
        log.warning("redis: unavailable (%s); read cache disabled", exc)
        redis_client = None
    query_cache = ReadCache(redis_client, "query", QUERY_CACHE_TTL)
    agent_graph = build_graph()
    log.info(
        "startup: provider=%s chat_model=%s planner=%s cache_ttl=%ds",
        PROVIDER_CONFIG["provider"], CHAT_MODEL, PLANNER_ENABLED, QUERY_CACHE_TTL,
    )
    try:
        yield
    finally:
        if http_client is not None:
            await http_client.aclose()
        if redis_client is not None:
            try:
                await redis_client.close()
            except Exception:  # noqa: BLE001
                pass


app = FastAPI(title="Query Service", version="1.0.0", lifespan=lifespan)


# ── Request tracing ────────────────────────────────────────────
@app.middleware("http")
async def trace_requests(request: Request, call_next):
    rid = request.headers.get("x-request-id") or uuid.uuid4().hex[:8]
    start = time.perf_counter()
    log.info("→ %s %s (rid=%s)", request.method, request.url.path, rid)
    try:
        response = await call_next(request)
    except Exception:
        dur = (time.perf_counter() - start) * 1000
        log.exception("✗ %s %s UNHANDLED after %.0fms (rid=%s)",
                      request.method, request.url.path, dur, rid)
        raise
    dur = (time.perf_counter() - start) * 1000
    log.info("← %s %s %s %.0fms (rid=%s)",
             request.method, request.url.path, response.status_code, dur, rid)
    response.headers["x-request-id"] = rid
    return response


# ── Chat helper ────────────────────────────────────────────────

async def _resolve_chat_model() -> bool:
    """Once per process: ask the provider what it serves and adopt a usable model.

    A pinned model id rots when a provider retires it — every request then 404s
    with no code change on our side. Groq dropping the Llama families is exactly
    that. Set CHAT_MODEL_AUTO_FALLBACK=false to fail loudly instead.
    """
    global EFFECTIVE_CHAT_MODEL, _model_resolution_attempted
    if _model_resolution_attempted or not CHAT_MODEL_AUTO_FALLBACK:
        return False
    _model_resolution_attempted = True
    chosen = await discover_chat_model(chat_client, EFFECTIVE_CHAT_MODEL)
    if not chosen:
        return False
    EFFECTIVE_CHAT_MODEL = chosen
    return True


async def generate_text(prompt: str, max_tokens: int, *, label: str = "chat") -> str:
    """Bounded-retry chat call; raises LLMUnavailable instead of degrading forever."""
    if chat_client is None:
        raise LLMUnavailable("chat client is not initialised", attempts=0)

    async def attempt() -> str:
        return await achat(
            chat_client,
            model=EFFECTIVE_CHAT_MODEL,
            prompt=prompt,
            max_tokens=max_tokens,
            temperature=0.3,
            label=label,
        )

    try:
        return await attempt()
    except LLMUnavailable as first:
        # An unknown model is not an outage: the provider's catalogue moved.
        # Look it up once and retry with something it actually serves.
        if is_unknown_model(first.last_error) and await _resolve_chat_model():
            log.warning("llm[%s] retrying with discovered model %r", label, EFFECTIVE_CHAT_MODEL)
            try:
                return await attempt()
            except LLMUnavailable:
                pass
        if LLM_STUB_FALLBACK and not _chat_is_stub:
            log.warning("llm[%s] exhausted retries; LLM_STUB_FALLBACK is on, using local stub", label)
            stub = FallbackOpenAIClient()
            return await achat(stub, model=EFFECTIVE_CHAT_MODEL, prompt=prompt,
                               max_tokens=max_tokens, label=f"{label}:stub")
        raise


# ── Parsing helpers (unit-tested) ──────────────────────────────

_CONFIDENCE_LINE = re.compile(r"^\s*CONFIDENCE\s*:\s*(.*)$", re.IGNORECASE)
_FIRST_FLOAT = re.compile(r"[-+]?\d*\.?\d+")

# Sentinel meaning "no CONFIDENCE line was present" — the previous code left the
# score at its initial value in that case, and that behaviour is preserved.
CONFIDENCE_ABSENT = None
CONFIDENCE_MALFORMED_DEFAULT = 0.5


def parse_confidence(text: str) -> Optional[float]:
    """Extract a 0.0–1.0 confidence from a `CONFIDENCE: ...` line.

    Returns None when no CONFIDENCE line exists (caller keeps its current value),
    0.5 when a line exists but holds no usable number, otherwise the clamped value.
    Tolerates extra colons, e.g. `CONFIDENCE: score: 0.7`.
    """
    if not text:
        return CONFIDENCE_ABSENT
    found = False
    for line in text.splitlines():
        match = _CONFIDENCE_LINE.match(line)
        if not match:
            continue
        found = True
        number = _FIRST_FLOAT.search(match.group(1))
        if number:
            try:
                return max(0.0, min(1.0, float(number.group(0))))
            except ValueError:
                pass
    return CONFIDENCE_MALFORMED_DEFAULT if found else CONFIDENCE_ABSENT


def coerce_confidence(value: Any) -> Optional[float]:
    """Clamp a JSON-supplied confidence, or None if it isn't a number."""
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return None


def extract_json(text: str) -> Optional[Any]:
    """Best-effort JSON extraction from a model response.

    Handles bare JSON, ```json fenced blocks, and JSON with surrounding prose.
    """
    if not text:
        return None
    candidate = text.strip()
    if candidate.startswith("```"):
        parts = candidate.split("```")
        if len(parts) >= 2:
            candidate = parts[1]
            if candidate.lstrip().lower().startswith("json"):
                candidate = candidate.lstrip()[4:]
            candidate = candidate.strip()
    try:
        return json.loads(candidate)
    except Exception:  # noqa: BLE001
        pass
    for opener, closer in (("{", "}"), ("[", "]")):
        start = candidate.find(opener)
        end = candidate.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(candidate[start:end + 1])
            except Exception:  # noqa: BLE001
                continue
    return None


def select_context_chunks(chunks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Top-k cap plus optional score floor for the synthesis prompt.

    Applied to the prompt only. `state["retrieved_chunks"]` keeps the full list
    so the `sources` array in the response is byte-for-byte what it always was.
    """
    selected = chunks
    if RETRIEVAL_MIN_SCORE > 0.0:
        above = [c for c in selected if (c.get("score") or 0.0) >= RETRIEVAL_MIN_SCORE]
        # Never let a strict threshold empty the prompt entirely.
        selected = above or selected[:1]
    return selected[:DECISION_CONTEXT_CHUNKS]


def chunk_text_of(chunk: Dict[str, Any]) -> str:
    """Chunk body, tolerant of both the old and new Pinecone metadata shapes.

    Vectors written before the metadata slimming carry both `content` and
    `content_preview`; new ones carry only `content`.
    """
    metadata = chunk.get("metadata") or {}
    return (
        chunk.get("content")
        or metadata.get("content")
        or metadata.get("content_preview")
        or ""
    )


# ── LangGraph State ────────────────────────────────────────────

class AgentState(TypedDict):
    question: str
    project_filter: Optional[str]
    sub_tasks: List[str]
    retrieved_chunks: List[Dict[str, Any]]
    timeline: Optional[Dict[str, Any]]
    graph_data: Optional[Dict[str, Any]]
    decision_analysis: Optional[str]
    final_answer: str
    sources: List[Dict[str, Any]]
    confidence_score: float
    # Annotated with a reducer for two reasons: parallel branches both append to
    # it, and langgraph 0.1.5 refuses multiple edges out of a node unless the
    # state has at least one annotated channel.
    processing_steps: Annotated[List[str], operator.add]
    embeddings_degraded: bool


# NOTE: every node returns a PARTIAL dict. Returning the whole state (the old
# style) makes langgraph raise InvalidUpdateError as soon as two branches run in
# the same superstep, because both would be writing every channel.


# ── Branch A, step 1: Planner ──────────────────────────────────

async def planner_agent(state: AgentState) -> Dict[str, Any]:
    """Break the user question into sub-tasks.

    Still an LLM call, but it now sits on the parallel branch: its latency is
    hidden behind the timeline branch instead of adding to the critical path.
    Set PLANNER_ENABLED=false to skip it and search the raw question.
    """
    question = state["question"]

    if not PLANNER_ENABLED:
        return {
            "sub_tasks": [question],
            "processing_steps": ["Planner: Skipped (disabled), searching the question directly"],
        }

    log.info("agent[planner] start: q=%r", question[:120])
    text = await generate_text(
        f"""You are a query planner for an organizational memory system.
Break this question into 2-4 specific search sub-tasks.
Return ONLY a JSON array of strings.

Question: {question}

Example output:
["Find meetings about topic X", "Find emails discussing Y", "Find Jira tickets related to Z"]

Output:""",
        max_tokens=PLANNER_MAX_TOKENS,
        label="planner",
    )

    parsed = extract_json(text)
    if isinstance(parsed, list) and parsed:
        sub_tasks = [str(t) for t in parsed if str(t).strip()]
    else:
        sub_tasks = [question]
    if not sub_tasks:
        sub_tasks = [question]

    log.info("agent[planner] done: %d sub-tasks", len(sub_tasks))
    return {
        "sub_tasks": sub_tasks,
        "processing_steps": [f"Planner: Generated {len(sub_tasks)} sub-tasks"],
    }


# ── Branch A, step 2: Search ───────────────────────────────────

async def _embed_sub_tasks(sub_tasks: List[str]) -> tuple[List[List[float]], bool]:
    """One batched embedding request instead of one per sub-task.

    Returns (vectors, degraded). `degraded` is True only when the query had to
    be embedded with local hash vectors, which the response surfaces.
    """
    model = embeddings_model
    if model is None:
        raise HTTPException(status_code=503, detail="Embedding client is not initialised")

    if isinstance(model, FallbackEmbeddings):
        return model.embed_documents(sub_tasks), True

    try:
        vectors = await model.aembed_documents(sub_tasks)
        return vectors, False
    except Exception as exc:  # noqa: BLE001
        if LLM_STUB_FALLBACK:
            # Opt-in only. Hash vectors search a real index as noise, so this is
            # a demo affordance, not a default.
            log.warning(
                "embedding provider failed (%s: %s); LLM_STUB_FALLBACK is on, using local vectors",
                type(exc).__name__, str(exc)[:200],
            )
            local = FallbackEmbeddings(dimensions=EMBEDDING_DIM)
            return local.embed_documents(sub_tasks), True
        log.error("embedding provider failed (%s: %s)", type(exc).__name__, str(exc)[:300])
        raise HTTPException(
            status_code=503,
            detail=f"Embedding provider unavailable: {type(exc).__name__}",
        ) from exc


def _pinecone_query(vector: List[float], filter_dict: Optional[Dict[str, Any]]):
    """Blocking Pinecone call. pinecone-client 3.2.2 has no async API, so the
    caller offloads this to a worker thread rather than running it on the loop."""
    return pc_index.query(
        vector=vector,
        top_k=SEARCH_TOP_K,
        include_metadata=True,
        filter=filter_dict,
    )


async def search_agent(state: AgentState) -> Dict[str, Any]:
    """Embed every sub-task in one request, then search Pinecone concurrently."""
    sub_tasks = state.get("sub_tasks") or [state["question"]]
    log.info("agent[search] start: %d sub-tasks", len(sub_tasks))

    if not init_clients() or embeddings_model is None or pc_index is None:
        raise HTTPException(
            status_code=503,
            detail="Upstream (Pinecone/OpenAI) not ready - check API keys and that the index exists",
        )

    vectors, degraded = await _embed_sub_tasks(sub_tasks)

    filter_dict: Optional[Dict[str, Any]] = None
    if state.get("project_filter"):
        filter_dict = {"project": {"$eq": state["project_filter"]}}

    # Independent searches -> run them together instead of one after another.
    results = await asyncio.gather(
        *(asyncio.to_thread(_pinecone_query, vector, filter_dict) for vector in vectors),
        return_exceptions=True,
    )

    all_chunks: List[Dict[str, Any]] = []
    seen_ids = set()
    failures = 0
    for result in results:
        if isinstance(result, BaseException):
            failures += 1
            log.warning("agent[search] one sub-task query failed: %r", result)
            continue
        for match in result.matches:
            if match.id in seen_ids:
                continue
            seen_ids.add(match.id)
            metadata = match.metadata or {}
            all_chunks.append({
                "chunk_id": match.id,
                "score": match.score,
                "metadata": metadata,
                "content": metadata.get("content", metadata.get("content_preview", "")),
            })

    if failures and not all_chunks:
        raise HTTPException(status_code=503, detail="Vector search failed for every sub-task")

    all_chunks.sort(key=lambda c: c["score"], reverse=True)
    kept = all_chunks[:MAX_RETRIEVED_CHUNKS]
    log.info("agent[search] done: retrieved %d unique chunks (keeping top %d)",
             len(all_chunks), len(kept))

    update: Dict[str, Any] = {
        "retrieved_chunks": kept,
        "processing_steps": [f"Search: Retrieved {len(all_chunks)} chunks"],
    }
    if degraded:
        update["embeddings_degraded"] = True
    return update


# ── Branch B: Timeline (parallel with planner+search) ──────────

async def timeline_agent(state: AgentState) -> Dict[str, Any]:
    """Call timeline-service.

    Reads only `question` and `project_filter`, which is why it can start at the
    same instant as the planner. Native async node — the previous version built a
    ThreadPoolExecutor and a fresh event loop per request to work around being a
    sync node inside a running loop.
    """
    topic = state["question"]
    url = f"{TIMELINE_URL}/timeline/{quote(topic, safe='')}"
    log.info("agent[timeline] start: GET %s", url)

    client = http_client
    try:
        if client is None:  # defensive: only if lifespan did not run (tests)
            async with httpx.AsyncClient(timeout=TIMELINE_TIMEOUT_S) as tmp:
                resp = await tmp.get(url, params={"project": state.get("project_filter") or ""})
        else:
            resp = await client.get(url, params={"project": state.get("project_filter") or ""})

        if resp.status_code == 200:
            timeline = resp.json()
            log.info("agent[timeline] done: %d events", len((timeline or {}).get("events", [])))
            return {
                "timeline": timeline,
                "processing_steps": ["Timeline: Built chronological sequence"],
            }

        log.warning("agent[timeline] service returned %s: %s", resp.status_code, resp.text[:300])
        return {
            "timeline": None,
            "processing_steps": [f"Timeline: Unavailable (HTTP {resp.status_code})"],
        }
    except Exception as e:  # noqa: BLE001 - timeline is non-fatal by design
        log.warning("agent[timeline] failed (non-fatal): %r", e)
        return {
            "timeline": None,
            "processing_steps": [f"Timeline: Unavailable ({str(e)[:50]})"],
        }


# ── Join: Synthesis (replaces decision_agent + answer_agent) ───

SYNTHESIS_PROMPT = """You are DecisionDNA, an AI organizational memory engine.
Analyse the organizational documents below and answer the user's question.

Question:
{question}

Retrieved Documents:
{chunks_text}

Timeline of Events:
{timeline_text}

Identify what decision was made, who was involved and their stance
(agreement/dissent/concern), what risks were flagged, and what the outcome was.
Then write the final answer for the user.

Return ONLY a JSON object with exactly these keys:
{{
  "decision": "<what was decided>",
  "participants": "<who was involved and their stance>",
  "risks_flagged": "<risks that were mentioned>",
  "outcome": "<what happened after>",
  "confidence": <number between 0.0 and 1.0 based on evidence quality>,
  "analysis": "<your detailed analysis>",
  "answer": "<the final answer for the user, formatted as: 1. a direct answer paragraph, 2. key findings as bullet points, 3. who was involved, 4. what risks were flagged (if any), 5. outcome (if known). Be factual and cite document types when mentioning sources.>"
}}

No markdown fences, no commentary outside the JSON."""


def _render_analysis_block(payload: Dict[str, Any]) -> str:
    """Rebuild the DECISION/PARTICIPANTS/... block the old decision agent produced.

    Kept because it is the internal representation the pipeline reasons about and
    logs; it is not part of the HTTP response.
    """
    return (
        f"DECISION: {payload.get('decision', '')}\n"
        f"PARTICIPANTS: {payload.get('participants', '')}\n"
        f"RISKS_FLAGGED: {payload.get('risks_flagged', '')}\n"
        f"OUTCOME: {payload.get('outcome', '')}\n"
        f"CONFIDENCE: {payload.get('confidence', '')}\n"
        f"ANALYSIS: {payload.get('analysis', '')}"
    )


def build_sources(chunks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Unchanged from the previous answer agent: first 5 unique doc_ids."""
    seen_docs = set()
    sources: List[Dict[str, Any]] = []
    for chunk in chunks:
        metadata = chunk.get("metadata") or {}
        doc_id = metadata.get("doc_id", "")
        if doc_id and doc_id not in seen_docs:
            seen_docs.add(doc_id)
            sources.append({
                "doc_id": doc_id,
                "doc_type": metadata.get("doc_type", ""),
                "title": metadata.get("title", ""),
                "date": metadata.get("date", ""),
                "relevance_score": chunk.get("score"),
                "excerpt": chunk_text_of(chunk)[:200],
            })
    return sources[:5]


async def synthesis_agent(state: AgentState) -> Dict[str, Any]:
    """One generation producing both the structured analysis and the final answer.

    The old answer agent's prompt contained only `decision_analysis`, so it was a
    second round trip that re-narrated the first. Merging them removes an entire
    generation from the critical path without losing any response field.
    """
    retrieved = state.get("retrieved_chunks") or []
    context_chunks = select_context_chunks(retrieved)
    log.info("agent[synthesis] start: %d retrieved, %d in prompt",
             len(retrieved), len(context_chunks))

    chunks_text = "\n---\n".join(
        f"[{(c.get('metadata') or {}).get('doc_type', '?')} | "
        f"{(c.get('metadata') or {}).get('date', '?')} | "
        f"Score: {(c.get('score') or 0.0):.2f}]\n{chunk_text_of(c)}"
        for c in context_chunks
    )

    events = (state.get("timeline") or {}).get("events", [])
    timeline_text = "\n".join(
        f"• {e.get('date', '?')}: {e.get('title', '')} — {e.get('description', '')}"
        for e in events
    ) or "Not available"

    prompt = SYNTHESIS_PROMPT.format(
        question=state["question"],
        chunks_text=chunks_text,
        timeline_text=timeline_text,
    )
    log.info("agent[synthesis] prompt chars=%d", len(prompt))

    text = await generate_text(prompt, max_tokens=SYNTHESIS_MAX_TOKENS, label="synthesis")

    payload = extract_json(text)
    update: Dict[str, Any] = {}

    if isinstance(payload, dict) and payload.get("answer"):
        update["decision_analysis"] = _render_analysis_block(payload)
        update["final_answer"] = str(payload["answer"])
        confidence = coerce_confidence(payload.get("confidence"))
    else:
        # Model ignored the JSON instruction: treat the whole response as the
        # answer and fall back to the legacy text parse for confidence.
        log.warning("agent[synthesis] response was not usable JSON; using raw text")
        update["decision_analysis"] = text
        update["final_answer"] = text
        confidence = parse_confidence(text)

    if confidence is not None:
        update["confidence_score"] = confidence

    update["sources"] = build_sources(retrieved)

    # Same two strings the UI has always rendered, so the visible pipeline
    # narration does not change even though the nodes merged.
    update["processing_steps"] = [
        "Decision Agent: Analyzed decisions and dissent",
        "Answer Agent: Generated final response",
    ]

    log.info("agent[synthesis] done: %d chars, %d sources, confidence=%s",
             len(update["final_answer"] or ""), len(update["sources"]),
             update.get("confidence_score", state.get("confidence_score")))
    return update


# ── Branch A: planner + search as one node ─────────────────────

async def retrieve_agent(state: AgentState) -> Dict[str, Any]:
    """The retrieval branch: plan the sub-tasks, then search for them.

    planner -> search is a genuine data dependency (search consumes `sub_tasks`),
    so these two never had any parallelism to exploit between them. They are one
    node for a second, more important reason — see build_graph().
    """
    planned = await planner_agent(state)
    searched = await search_agent({**state, **planned})
    merged = {**planned, **searched}
    merged["processing_steps"] = (
        planned.get("processing_steps", []) + searched.get("processing_steps", [])
    )
    return merged


# ── Build LangGraph ────────────────────────────────────────────

def build_graph():
    """Async graph with one genuine parallel branch.

        START ─┬─► retrieve (planner → search) ──┐
               │                                 ├─► synthesize ─► END
               └─► timeline_step ────────────────┘

    Why the retrieval branch is a single node
    -----------------------------------------
    langgraph 0.1.5 does not defer a node until every inbound edge has fired.
    It advances in supersteps, and a node runs once per superstep in which any
    inbound edge triggers it. With branches of unequal length:

        superstep 1: planner, timeline_step
        superstep 2: search            <- from planner
                     synthesize        <- from timeline_step (already done!)
        superstep 3: synthesize again  <- from search

    ...synthesize executes TWICE, costing an extra generation and appending its
    processing_steps twice. Collapsing planner+search into one node makes both
    branches one superstep deep, so they finish together and synthesize fires
    exactly once. There is a regression test for this
    (test_synthesis_runs_exactly_once).

    Node ids must not collide with AgentState keys (langgraph rejects that),
    which is why the timeline node is "timeline_step" while the state key stays
    "timeline".
    """
    workflow = StateGraph(AgentState)

    workflow.add_node("retrieve", retrieve_agent)
    workflow.add_node("timeline_step", timeline_agent)
    workflow.add_node("synthesize", synthesis_agent)

    # Two edges out of START. Legal in langgraph 0.1.5 only because AgentState
    # has an annotated (reducer) channel — processing_steps.
    workflow.add_edge(START, "retrieve")
    workflow.add_edge(START, "timeline_step")

    workflow.add_edge("retrieve", "synthesize")
    workflow.add_edge("timeline_step", "synthesize")

    workflow.add_edge("synthesize", END)

    return workflow.compile()


def graph_topology() -> Dict[str, List[str]]:
    """Adjacency of the compiled graph, for tests and documentation."""
    return {
        START: ["retrieve", "timeline_step"],
        "retrieve": ["synthesize"],
        "timeline_step": ["synthesize"],
        "synthesize": [END],
    }


# ── Models ─────────────────────────────────────────────────────

class QueryRequest(BaseModel):
    question: str
    project_filter: Optional[str] = None


# ── Routes ─────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {
        "service": "query-service",
        "status": "healthy",
        "ready": pc_index is not None,  # False until Pinecone/OpenAI init succeeds
        "degraded": _embeddings_is_stub or _index_is_stub or _chat_is_stub,
        "pinecone_index": INDEX_NAME,
        "chat_model": EFFECTIVE_CHAT_MODEL,
        "chat_model_configured": CHAT_MODEL,
        "embedding_model": EMBEDDING_MODEL,
        "embedding_dimensions": EMBEDDING_DIM,
        "provider": PROVIDER_CONFIG["provider"],
    }


@app.post("/query")
async def query(request: QueryRequest):
    if not agent_graph:
        raise HTTPException(status_code=503, detail="Agent not ready")
    if not init_clients():
        raise HTTPException(
            status_code=503,
            detail="Upstream (Pinecone/OpenAI) not ready - check API keys and that the index exists",
        )

    cache_parts = (request.question, request.project_filter)
    if query_cache is not None:
        cached = await query_cache.get(*cache_parts)
        if cached is not None:
            log.info("pipeline cache HIT: q=%r", request.question[:120])
            return cached

    initial_state: AgentState = {
        "question": request.question,
        "project_filter": request.project_filter,
        "sub_tasks": [],
        "retrieved_chunks": [],
        "timeline": None,
        "graph_data": None,
        "decision_analysis": None,
        "final_answer": "",
        "sources": [],
        "confidence_score": 0.0,
        "processing_steps": [],
        "embeddings_degraded": False,
    }

    log.info("pipeline start: q=%r project_filter=%r",
             request.question[:120], request.project_filter)
    pipeline_start = time.perf_counter()
    try:
        # ainvoke, not invoke: the nodes are coroutines, and langgraph's sync
        # entry point cannot drive them. This also keeps every provider call off
        # the event-loop thread, so concurrent queries actually make progress.
        result = await agent_graph.ainvoke(initial_state)
    except HTTPException:
        raise
    except LLMUnavailable as e:
        log.error("pipeline FAILED after %.0fms: %s",
                  (time.perf_counter() - pipeline_start) * 1000, e)
        raise HTTPException(status_code=503, detail=f"Chat provider unavailable: {e}") from e
    except Exception as e:  # noqa: BLE001
        log.exception("pipeline FAILED after %.0fms",
                      (time.perf_counter() - pipeline_start) * 1000)
        raise HTTPException(status_code=500, detail=f"Query pipeline failed: {e}") from e

    log.info("pipeline done in %.0fms (steps: %s)",
             (time.perf_counter() - pipeline_start) * 1000, result.get("processing_steps"))

    response = {
        "question": result["question"],
        "answer": result["final_answer"],
        "timeline": result.get("timeline"),
        "sources": result["sources"],
        "confidence_score": result["confidence_score"],
        "processing_steps": result["processing_steps"],
        "degraded": result.get("embeddings_degraded", False),
    }

    if query_cache is not None and not response["degraded"]:
        # Never cache a degraded answer as if it were a real one.
        await query_cache.set(response, *cache_parts)

    return response
