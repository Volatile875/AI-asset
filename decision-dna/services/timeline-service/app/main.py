"""
services/timeline-service/app/main.py
Builds a chronological decision timeline from Pinecone
search results — the UNIQUE feature of DecisionDNA.
"""

import os
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException
from langchain_openai import OpenAIEmbeddings
from openai import OpenAI
from pinecone import Pinecone
from pydantic import BaseModel

from app.fallbacks import FallbackEmbeddings, FallbackIndex, FallbackOpenAIClient
from app.provider_config import resolve_provider_config

import logging
import time
import uuid

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s [timeline-service] %(message)s",
)
log = logging.getLogger("timeline-service")

app = FastAPI(title="Timeline Service", version="1.0.0")


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

PROVIDER_CONFIG = resolve_provider_config()
OPENAI_API_KEY = PROVIDER_CONFIG["chat_api_key"]  # For legacy config references
PINECONE_API_KEY = os.getenv("PINECONE_API_KEY")
INDEX_NAME = os.getenv("PINECONE_INDEX_NAME", "ai-asset")
CHAT_MODEL = PROVIDER_CONFIG["chat_model"]
EMBEDDING_MODEL = PROVIDER_CONFIG["embedding_model"]
EMBEDDING_DIM = int(os.getenv("EMBEDDING_DIMENSIONS", "1024"))

openai_client = None
embeddings_model = None
pc_index = None


def init_clients() -> bool:
    """Initialize external clients when possible, but fall back to local stubs if credentials are unavailable."""
    global openai_client, embeddings_model, pc_index
    try:
        if openai_client is None:
            try:
                openai_client = OpenAI(api_key=PROVIDER_CONFIG["chat_api_key"], base_url=PROVIDER_CONFIG["chat_base_url"])
            except Exception as exc:
                print(f"[timeline-service] OpenAI init failed, using fallback client: {exc}", flush=True)
                openai_client = FallbackOpenAIClient()
        if embeddings_model is None:
            try:
                embeddings_model = OpenAIEmbeddings(
                    model=EMBEDDING_MODEL,
                    dimensions=EMBEDDING_DIM,
                    openai_api_key=PROVIDER_CONFIG["embedding_api_key"],
                    base_url=PROVIDER_CONFIG["embedding_base_url"],
                )
            except Exception as exc:
                print(f"[timeline-service] OpenAI embeddings init failed, using fallback embeddings: {exc}", flush=True)
                embeddings_model = FallbackEmbeddings(dimensions=EMBEDDING_DIM)
        if pc_index is None:
            try:
                pc = Pinecone(api_key=PINECONE_API_KEY)
                pc_index = pc.Index(INDEX_NAME)
            except Exception as exc:
                print(f"[timeline-service] Pinecone init failed, using fallback index: {exc}", flush=True)
                pc_index = FallbackIndex()
        return True
    except Exception as e:
        print(f"[timeline-service] client init failed (will retry on demand): {e}", flush=True)
        return False


def generate_text(prompt: str, max_tokens: int) -> str:
    """Generate text using OpenAI's chat completions API, falling back to local client on failure."""
    global openai_client
    if openai_client is None:
        raise RuntimeError("OpenAI client is not initialized")

    try:
        response = openai_client.chat.completions.create(
            model=CHAT_MODEL,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.choices[0].message.content or ""
    except Exception as e:
        if isinstance(openai_client, FallbackOpenAIClient):
            print(f"[timeline-service] Fallback OpenAI client failed: {e}", flush=True)
            raise HTTPException(
                status_code=500,
                detail=f"Fallback client error: {str(e)}"
            )

        print(f"[timeline-service] Chat provider call failed ({type(e).__name__}: {e}); switching to local FallbackOpenAIClient", flush=True)
        openai_client = FallbackOpenAIClient()
        try:
            response = openai_client.chat.completions.create(
                model=CHAT_MODEL,
                max_tokens=max_tokens,
                messages=[{"role": "user", "content": prompt}],
            )
            return response.choices[0].message.content or ""
        except Exception as fallback_err:
            print(f"[timeline-service] Fallback client failed on retry: {fallback_err}", flush=True)
            raise HTTPException(
                status_code=500,
                detail=f"Local fallback client failed: {str(fallback_err)}"
            )


@app.on_event("startup")
async def startup():
    # Non-fatal: never abort startup on a dependency hiccup. The service must stay
    # up and reachable so it can self-heal; clients are (re)initialized on demand.
    init_clients()


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


# ── Timeline Building ──────────────────────────────────────────

def search_pinecone(topic: str, project: str = "", top_k: int = 20) -> List[Dict]:
    global embeddings_model
    # Defensive: callers (build_timeline) init first, but don't trust globals blindly.
    # This also narrows the Optional types for the type checker.
    if embeddings_model is None or pc_index is None:
        init_clients()
    if embeddings_model is None or pc_index is None:
        raise RuntimeError("Timeline search unavailable: Pinecone/embedding clients not initialized")
    try:
        query_vector = embeddings_model.embed_query(topic)
    except Exception as embed_err:
        # If embedding fails (rate limit, auth error, etc.), fall back to stub embeddings
        print(f"[timeline-service] Embedding failed ({type(embed_err).__name__}), using fallback embeddings", flush=True)
        embeddings_model = FallbackEmbeddings(dimensions=EMBEDDING_DIM)
        query_vector = embeddings_model.embed_query(topic)
    
    filter_dict = {"project": {"$eq": project}} if project else None

    results = pc_index.query(
        vector=query_vector,
        top_k=top_k,
        include_metadata=True,
        filter=filter_dict,
    )
    return [
        {
            "chunk_id": m.id,
            "score": m.score,
            "metadata": m.metadata,
        }
        for m in results.matches
    ]


def extract_events_with_openai(topic: str, chunks: List[Dict]) -> List[Dict]:
    """Ask OpenAI to extract structured timeline events from chunks."""
    chunks_text = "\n---\n".join([
        f"[{c['metadata'].get('doc_type', '?')} | {c['metadata'].get('date', '?')} | doc:{c['metadata'].get('doc_id', '?')}]\n{c['metadata'].get('content_preview', '')}"
        for c in chunks
    ])

    text = generate_text(
        f"""Extract a chronological timeline of events related to this topic from the documents.

Topic: {topic}

Documents:
{chunks_text}

Return a JSON array of events. Each event must have:
- date: ISO date string (estimate if unclear, use doc date)
- event_type: one of [discussion, decision, implementation, issue, risk_flag, approval, rejection, meeting]
- title: short title (max 8 words)
- description: what happened (1-2 sentences)
- participants: list of names mentioned
- sentiment: one of [agreement, dissent, concern, neutral]
- is_critical: true if this was a turning point
- doc_id: the doc ID from the document header

Return ONLY valid JSON array. No explanation, no markdown.
Example:
[{{"date":"2024-03-01","event_type":"discussion","title":"Initial migration proposal raised","description":"Ravi proposed migrating from AWS Lambda to Azure Functions citing cost.","participants":["Ravi","Priya"],"sentiment":"neutral","is_critical":false,"doc_id":"EMAIL-001"}}]""",
        max_tokens=2000,
    )

    import json
    text = text.strip()
    # Remove markdown fences if present
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    try:
        return json.loads(text)
    except Exception:
        return []


def assess_outcome(topic: str, events: List[Dict]) -> tuple:
    """Ask OpenAI to assess the overall outcome and confidence."""
    events_summary = "\n".join([
        f"• {e.get('date','?')}: [{e.get('event_type','?')}] {e.get('title','')} — {e.get('sentiment','neutral')}"
        for e in events
    ])

    text = generate_text(
        f"""Based on this event sequence about "{topic}", provide:
1. A 2-sentence outcome assessment
2. A confidence score (0.0-1.0) reflecting how well-documented this decision trail is

Events:
{events_summary}

Format:
OUTCOME: <2 sentence assessment>
CONFIDENCE: <0.0-1.0>""",
        max_tokens=300,
    )

    outcome = "Outcome unclear from available documents."
    confidence = 0.5

    for line in text.split("\n"):
        if line.startswith("OUTCOME:"):
            outcome = line.replace("OUTCOME:", "").strip()
        elif line.startswith("CONFIDENCE:"):
            try:
                confidence = float(line.replace("CONFIDENCE:", "").strip())
            except Exception:
                pass

    return outcome, confidence


# ── Routes ─────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {
        "service": "timeline-service",
        "status": "healthy",
        "ready": pc_index is not None,  # False until Pinecone/OpenAI init succeeds
        "pinecone_index": INDEX_NAME,
        "chat_model": CHAT_MODEL,
        "embedding_model": EMBEDDING_MODEL,
        "embedding_dimensions": EMBEDDING_DIM,
        "provider": PROVIDER_CONFIG["provider"],
    }


@app.get("/timeline/{topic}")
async def build_timeline(topic: str, project: str = ""):
    if not init_clients():
        raise HTTPException(status_code=503, detail="Upstream (Pinecone/OpenAI) not ready - check API keys and that the index exists")

    # Step 1: Search for relevant documents
    chunks = search_pinecone(topic, project)

    if not chunks:
        return Timeline(
            topic=topic,
            events=[],
            outcome_assessment="No documents found for this topic.",
            confidence_score=0.0,
            total_documents=0,
        )

    # Step 2: Extract events using OpenAI
    raw_events = extract_events_with_openai(topic, chunks)

    # Step 3: Sort by date
    def parse_date_safe(d):
        try:
            return datetime.fromisoformat(d)
        except Exception:
            return datetime.min

    raw_events.sort(key=lambda e: parse_date_safe(e.get("date", "")))

    # Step 4: Build TimelineEvent objects
    events = []
    for i, e in enumerate(raw_events):
        event_type = e.get("event_type", "discussion")
        sentiment = e.get("sentiment", "neutral")
        events.append(TimelineEvent(
            event_id=f"evt_{i}",
            date=e.get("date", "Unknown"),
            event_type=event_type,
            title=e.get("title", "Event"),
            description=e.get("description", ""),
            participants=e.get("participants", []),
            doc_id=e.get("doc_id"),
            sentiment=sentiment,
            is_critical=e.get("is_critical", False),
            icon=SENTIMENT_ICONS.get(sentiment, "⚪") if e.get("is_critical") else EVENT_ICONS.get(event_type, "📅"),
        ))

    # Step 5: Assess outcome
    outcome, confidence = assess_outcome(topic, raw_events)

    return Timeline(
        topic=topic,
        events=events,
        outcome_assessment=outcome,
        confidence_score=confidence,
        total_documents=len(set(c["metadata"].get("doc_id", "") for c in chunks)),
    )
