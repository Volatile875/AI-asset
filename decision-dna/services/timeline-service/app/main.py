"""
services/timeline-service/app/main.py
Builds a chronological decision timeline from Pinecone
search results — the UNIQUE feature of DecisionDNA.
"""

import os
from datetime import datetime
from typing import Any, Dict, List, Optional

from anthropic import Anthropic
from fastapi import FastAPI
from langchain_openai import OpenAIEmbeddings
from pinecone import Pinecone
from pydantic import BaseModel

app = FastAPI(title="Timeline Service", version="1.0.0")

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
OPENAI_API_KEY    = os.getenv("OPENAI_API_KEY")
PINECONE_API_KEY  = os.getenv("PINECONE_API_KEY")
INDEX_NAME        = os.getenv("PINECONE_INDEX_NAME", "ai-asset")
EMBEDDING_MODEL   = os.getenv("EMBEDDING_MODEL", "text-embedding-3-large")
EMBEDDING_DIM     = int(os.getenv("EMBEDDING_DIMENSIONS", "1024"))

claude_client = None
embeddings_model = None
pc_index = None


@app.on_event("startup")
async def startup():
    global claude_client, embeddings_model, pc_index
    claude_client = Anthropic(api_key=ANTHROPIC_API_KEY)
    embeddings_model = OpenAIEmbeddings(
        model=EMBEDDING_MODEL,
        dimensions=EMBEDDING_DIM,
        openai_api_key=OPENAI_API_KEY,
    )
    pc = Pinecone(api_key=PINECONE_API_KEY)
    pc_index = pc.Index(INDEX_NAME)


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


def extract_events_with_claude(topic: str, chunks: List[Dict]) -> List[Dict]:
    """Ask Claude to extract structured timeline events from chunks."""
    chunks_text = "\n---\n".join([
        f"[{c['metadata'].get('doc_type', '?')} | {c['metadata'].get('date', '?')} | doc:{c['metadata'].get('doc_id', '?')}]\n{c['metadata'].get('content_preview', '')}"
        for c in chunks
    ])

    response = claude_client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2000,
        messages=[{
            "role": "user",
            "content": f"""Extract a chronological timeline of events related to this topic from the documents.

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
[{{"date":"2024-03-01","event_type":"discussion","title":"Initial migration proposal raised","description":"Ravi proposed migrating from AWS Lambda to Azure Functions citing cost.","participants":["Ravi","Priya"],"sentiment":"neutral","is_critical":false,"doc_id":"EMAIL-001"}}]"""
        }]
    )

    import json
    text = response.content[0].text.strip()
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
    """Ask Claude to assess the overall outcome and confidence."""
    events_summary = "\n".join([
        f"• {e.get('date','?')}: [{e.get('event_type','?')}] {e.get('title','')} — {e.get('sentiment','neutral')}"
        for e in events
    ])

    response = claude_client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=300,
        messages=[{
            "role": "user",
            "content": f"""Based on this event sequence about "{topic}", provide:
1. A 2-sentence outcome assessment
2. A confidence score (0.0-1.0) reflecting how well-documented this decision trail is

Events:
{events_summary}

Format:
OUTCOME: <2 sentence assessment>
CONFIDENCE: <0.0-1.0>"""
        }]
    )

    text = response.content[0].text
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
        "pinecone_index": INDEX_NAME,
        "embedding_model": EMBEDDING_MODEL,
        "embedding_dimensions": EMBEDDING_DIM,
    }


@app.get("/timeline/{topic}")
async def build_timeline(topic: str, project: str = ""):
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

    # Step 2: Extract events using Claude
    raw_events = extract_events_with_claude(topic, chunks)

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
