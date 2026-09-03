"""
services/timeline-service/app/main.py
Builds a chronological decision timeline from Pinecone
search results — the UNIQUE feature of DecisionDNA.

Two changes vs. the previous version:

1. One LLM call instead of two. `extract_events_with_openai` produced the event
   list and `assess_outcome` then summarised a summary of it — a second round
   trip over material the first call had just generated. They are now a single
   structured request returning {events, outcome, confidence}.

2. Genuinely non-blocking. The route was `async def` while calling synchronous
   embedding / chat / Pinecone helpers, so every one of them ran on the event
   loop thread and the service could only serve one timeline at a time.

The Timeline response model is unchanged.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import redis.asyncio as redis
from fastapi import FastAPI, HTTPException
from langchain_openai import OpenAIEmbeddings
from openai import AsyncOpenAI
from pinecone import Pinecone
from pydantic import BaseModel

from app.cache import ReadCache
from app.fallbacks import FallbackEmbeddings, FallbackIndex, FallbackOpenAIClient
from app.llm import (LLMUnavailable, achat, discover_chat_model, env_flag,
                     is_unknown_model)
from app.provider_config import resolve_provider_config

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s [timeline-service] %(message)s",
)
log = logging.getLogger("timeline-service")

PROVIDER_CONFIG = resolve_provider_config()
OPENAI_API_KEY = PROVIDER_CONFIG["chat_api_key"]  # For legacy config references
PINECONE_API_KEY = os.getenv("PINECONE_API_KEY")
INDEX_NAME = os.getenv("PINECONE_INDEX_NAME", "ai-asset")
CHAT_MODEL = PROVIDER_CONFIG["chat_model"]
EMBEDDING_MODEL = PROVIDER_CONFIG["embedding_model"]
EMBEDDING_DIM = int(os.getenv("EMBEDDING_DIMENSIONS", "1024"))
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


TIMELINE_TOP_K = _env_int("TIMELINE_TOP_K", 20)
TIMELINE_MAX_TOKENS = _env_int("TIMELINE_MAX_TOKENS", 2000)
TIMELINE_CACHE_TTL = _env_int("TIMELINE_CACHE_TTL", 3600)  # 0 disables
LLM_STUB_FALLBACK = env_flag("LLM_STUB_FALLBACK", False)
CHAT_MODEL_AUTO_FALLBACK = env_flag("CHAT_MODEL_AUTO_FALLBACK", True)

# The model actually in use. Starts as the configured one and only changes
# if the provider rejects it as unknown and discovery finds a replacement.
EFFECTIVE_CHAT_MODEL = CHAT_MODEL
_model_resolution_attempted = False

chat_client: Any = None
embeddings_model: Any = None
pc_index: Any = None
redis_client: Any = None
timeline_cache: Optional[ReadCache] = None

_chat_is_stub = False
_embeddings_is_stub = False
_index_is_stub = False


def init_clients() -> bool:
    """Construct clients once. Construction failure -> local stub (as before).
    A failing *call* no longer swaps the global out permanently."""
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
    global redis_client, timeline_cache
    init_clients()
    try:
        redis_client = redis.from_url(REDIS_URL, decode_responses=True)
        await redis_client.ping()
        log.info("redis: connected at %s", REDIS_URL)
    except Exception as exc:  # noqa: BLE001 - cache is optional
        log.warning("redis: unavailable (%s); timeline cache disabled", exc)
        redis_client = None
    timeline_cache = ReadCache(redis_client, "timeline", TIMELINE_CACHE_TTL)
    try:
        yield
    finally:
        if redis_client is not None:
            try:
                await redis_client.close()
            except Exception:  # noqa: BLE001
                pass


app = FastAPI(title="Timeline Service", version="1.0.0", lifespan=lifespan)


@app.middleware("http")
async def trace_requests(request, call_next):
    rid = request.headers.get("x-request-id") or uuid.uuid4().hex[:8]
    start = time.perf_counter()
    log.info("→ %s %s (rid=%s)", request.method, request.url.path, rid)
    try:
        response = await call_next(request)
    except Exception:
        log.exception("✗ %s %s UNHANDLED after %.0fms (rid=%s)",
                      request.method, request.url.path, (time.perf_counter() - start) * 1000, rid)
        raise
    log.info("← %s %s %s %.0fms (rid=%s)",
             request.method, request.url.path, response.status_code,
             (time.perf_counter() - start) * 1000, rid)
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


# ── Models ─────────────────────────────────────────────────────

class TimelineEvent(BaseModel):
    event_id: str
    date: str
    event_type: str  # discussion | decision | implementation | issue | risk_flag
    title: str
    description: str
    participants: List[str] = []
    doc_id: Optional[str] = None
    sentiment: str = "neutral"  # agreement | dissent | concern | neutral
    is_critical: bool = False
    icon: str = "📅"


class Timeline(BaseModel):
    topic: str
    events: List[TimelineEvent]
    outcome_assessment: Optional[str] = None
    confidence_score: float = 0.0
    total_documents: int = 0


# ── Icon mapping ───────────────────────────────────────────────

EVENT_ICONS = {
    "discussion":     "💬",
    "decision":       "✅",
    "implementation": "🔧",
    "issue":          "🐛",
    "risk_flag":      "⚠️",
    "approval":       "👍",
    "rejection":      "❌",
    "meeting":        "📋",
}

SENTIMENT_ICONS = {
    "dissent":   "🔴",
    "concern":   "🟡",
    "agreement": "🟢",
    "neutral":   "⚪",
}


# ── Parsing helpers (unit-tested) ──────────────────────────────

def extract_json(text: str) -> Optional[Any]:
    """Best-effort JSON extraction: bare, ```fenced, or embedded in prose."""
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


def chunk_preview_of(chunk: Dict[str, Any], limit: int = 200) -> str:
    """Preview text, tolerant of both metadata shapes.

    Vectors written before the metadata slimming carry an explicit
    `content_preview`; newer ones carry only `content` and the preview is derived.
    """
    metadata = chunk.get("metadata") or {}
    preview = metadata.get("content_preview")
    if preview:
        return preview
    return (metadata.get("content") or "")[:limit]


def normalise_timeline_payload(payload: Any) -> Tuple[List[Dict[str, Any]], Optional[str], Optional[float]]:
    """Accept either the merged object or a bare event array.

    Returns (events, outcome, confidence); outcome/confidence are None when the
    model returned only the legacy array shape, and the caller keeps its defaults.
    """
    if isinstance(payload, list):
        return [e for e in payload if isinstance(e, dict)], None, None
    if isinstance(payload, dict):
        events = payload.get("events")
        events = [e for e in events if isinstance(e, dict)] if isinstance(events, list) else []
        outcome = payload.get("outcome") or payload.get("outcome_assessment")
        confidence: Optional[float]
        try:
            confidence = max(0.0, min(1.0, float(payload.get("confidence"))))
        except (TypeError, ValueError):
            confidence = None
        return events, (str(outcome) if outcome else None), confidence
    return [], None, None


def parse_date_safe(value: Any) -> datetime:
    try:
        return datetime.fromisoformat(str(value))
    except Exception:  # noqa: BLE001
        return datetime.min


# ── Retrieval ──────────────────────────────────────────────────

def _pinecone_query(vector: List[float], filter_dict: Optional[Dict[str, Any]], top_k: int):
    """Blocking Pinecone call — pinecone-client 3.2.2 has no async API."""
    return pc_index.query(
        vector=vector,
        top_k=top_k,
        include_metadata=True,
        filter=filter_dict,
    )


async def search_pinecone(topic: str, project: str = "", top_k: int = None) -> List[Dict]:
    """Embed the topic and search, without blocking the event loop."""
    top_k = top_k or TIMELINE_TOP_K
    if embeddings_model is None or pc_index is None:
        init_clients()
    if embeddings_model is None or pc_index is None:
        raise HTTPException(
            status_code=503,
            detail="Timeline search unavailable: Pinecone/embedding clients not initialised",
        )

    if isinstance(embeddings_model, FallbackEmbeddings):
        query_vector = embeddings_model.embed_query(topic)
    else:
        try:
            query_vector = await embeddings_model.aembed_query(topic)
        except Exception as exc:  # noqa: BLE001
            if LLM_STUB_FALLBACK:
                log.warning("embedding failed (%s); LLM_STUB_FALLBACK is on, using local vectors",
                            type(exc).__name__)
                query_vector = FallbackEmbeddings(dimensions=EMBEDDING_DIM).embed_query(topic)
            else:
                log.error("embedding provider failed (%s: %s)", type(exc).__name__, str(exc)[:300])
                raise HTTPException(
                    status_code=503,
                    detail=f"Embedding provider unavailable: {type(exc).__name__}",
                ) from exc

    filter_dict = {"project": {"$eq": project}} if project else None
    results = await asyncio.to_thread(_pinecone_query, query_vector, filter_dict, top_k)
    return [{"chunk_id": m.id, "score": m.score, "metadata": m.metadata or {}} for m in results.matches]


# ── Timeline extraction (single LLM call) ──────────────────────

TIMELINE_PROMPT = """Extract a chronological timeline of events related to this topic from the documents, then assess the outcome.

Topic: {topic}

Documents:
{chunks_text}

Return ONLY a JSON object with exactly these keys:
{{
  "events": [
    {{
      "date": "<ISO date string, estimate from the doc date if unclear>",
      "event_type": "<one of: discussion, decision, implementation, issue, risk_flag, approval, rejection, meeting>",
      "title": "<short title, max 8 words>",
      "description": "<what happened, 1-2 sentences>",
      "participants": ["<names mentioned>"],
      "sentiment": "<one of: agreement, dissent, concern, neutral>",
      "is_critical": <true if this was a turning point>,
      "doc_id": "<the doc ID from the document header>"
    }}
  ],
  "outcome": "<a 2-sentence assessment of the overall outcome>",
  "confidence": <number 0.0-1.0 reflecting how well-documented this decision trail is>
}}

No markdown fences, no explanation outside the JSON.
Example event: {{"date":"2024-03-01","event_type":"discussion","title":"Initial migration proposal raised","description":"Ravi proposed migrating from AWS Lambda to Azure Functions citing cost.","participants":["Ravi","Priya"],"sentiment":"neutral","is_critical":false,"doc_id":"EMAIL-001"}}"""


async def extract_timeline(topic: str, chunks: List[Dict]) -> Tuple[List[Dict[str, Any]], str, float]:
    """One call returning events + outcome + confidence.

    Previously this was two sequential generations (2000 tokens of event
    extraction, then 300 tokens re-summarising those same events).
    """
    chunks_text = "\n---\n".join(
        f"[{(c.get('metadata') or {}).get('doc_type', '?')} | "
        f"{(c.get('metadata') or {}).get('date', '?')} | "
        f"doc:{(c.get('metadata') or {}).get('doc_id', '?')}]\n{chunk_preview_of(c)}"
        for c in chunks
    )

    text = await generate_text(
        TIMELINE_PROMPT.format(topic=topic, chunks_text=chunks_text),
        max_tokens=TIMELINE_MAX_TOKENS,
        label="timeline",
    )

    events, outcome, confidence = normalise_timeline_payload(extract_json(text))
    return (
        events,
        outcome if outcome is not None else "Outcome unclear from available documents.",
        confidence if confidence is not None else 0.5,
    )


# ── Routes ─────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {
        "service": "timeline-service",
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


@app.get("/timeline/{topic}")
async def build_timeline(topic: str, project: str = ""):
    if not init_clients():
        raise HTTPException(
            status_code=503,
            detail="Upstream (Pinecone/OpenAI) not ready - check API keys and that the index exists",
        )

    # (topic, project) fully determines the result, and the key is namespaced by
    # the corpus version that ingestion bumps, so a re-ingest invalidates it.
    if timeline_cache is not None:
        cached = await timeline_cache.get(topic, project)
        if cached is not None:
            log.info("timeline cache HIT: topic=%r project=%r", topic[:80], project)
            return cached

    chunks = await search_pinecone(topic, project)

    if not chunks:
        return Timeline(
            topic=topic,
            events=[],
            outcome_assessment="No documents found for this topic.",
            confidence_score=0.0,
            total_documents=0,
        )

    raw_events, outcome, confidence = await extract_timeline(topic, chunks)
    raw_events.sort(key=lambda e: parse_date_safe(e.get("date", "")))

    events = []
    for i, e in enumerate(raw_events):
        event_type = e.get("event_type", "discussion")
        sentiment = e.get("sentiment", "neutral")
        events.append(TimelineEvent(
            event_id=f"evt_{i}",
            date=str(e.get("date", "Unknown")),
            event_type=event_type,
            title=str(e.get("title", "Event")),
            description=str(e.get("description", "")),
            participants=[str(p) for p in (e.get("participants") or [])],
            doc_id=e.get("doc_id"),
            sentiment=sentiment,
            is_critical=bool(e.get("is_critical", False)),
            icon=SENTIMENT_ICONS.get(sentiment, "⚪") if e.get("is_critical")
                 else EVENT_ICONS.get(event_type, "📅"),
        ))

    timeline = Timeline(
        topic=topic,
        events=events,
        outcome_assessment=outcome,
        confidence_score=confidence,
        total_documents=len({(c.get("metadata") or {}).get("doc_id", "") for c in chunks}),
    )

    if timeline_cache is not None and not (_embeddings_is_stub or _chat_is_stub):
        await timeline_cache.set(timeline.model_dump(), topic, project)

    return timeline
