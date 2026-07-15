"""
shared/models/schemas.py
Pydantic models shared across ALL microservices.
Import from here to maintain consistency.
"""

from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


# ── Enums ─────────────────────────────────────────────────────


class DocumentType(str, Enum):
    EMAIL = "email"
    MEETING_NOTES = "meeting_notes"
    JIRA_TICKET = "jira_ticket"
    SLACK_MESSAGE = "slack_message"
    CONFLUENCE_PAGE = "confluence_page"
    DECISION_DOCUMENT = "decision_document"


class DecisionStatus(str, Enum):
    PROPOSED = "proposed"
    APPROVED = "approved"
    REJECTED = "rejected"
    DEFERRED = "deferred"
    IMPLEMENTED = "implemented"
    ROLLED_BACK = "rolled_back"


class SentimentType(str, Enum):
    AGREEMENT = "agreement"
    DISSENT = "dissent"
    CONCERN = "concern"
    NEUTRAL = "neutral"


# ── Core Document Models ───────────────────────────────────────


class DocumentMetadata(BaseModel):
    doc_id: str
    doc_type: DocumentType
    date: datetime
    project: Optional[str] = None
    participants: List[str] = Field(default_factory=list)
    tags: List[str] = Field(default_factory=list)
    source_path: Optional[str] = None


class RawDocument(BaseModel):
    metadata: DocumentMetadata
    content: str
    title: Optional[str] = None


class ChunkedDocument(BaseModel):
    chunk_id: str
    doc_id: str
    doc_type: DocumentType
    content: str
    metadata: Dict[str, Any]
    chunk_index: int
    total_chunks: int


class EmbeddedChunk(BaseModel):
    chunk_id: str
    doc_id: str
    doc_type: DocumentType
    content: str
    embedding: List[float]
    metadata: Dict[str, Any]


# ── Decision Models ────────────────────────────────────────────


class DecisionParticipant(BaseModel):
    name: str
    role: Optional[str] = None
    sentiment: SentimentType = SentimentType.NEUTRAL
    concern_raised: Optional[str] = None


class Decision(BaseModel):
    decision_id: str
    title: str
    description: str
    status: DecisionStatus
    date: datetime
    project: Optional[str] = None
    participants: List[DecisionParticipant] = Field(default_factory=list)
    risks_flagged: List[str] = Field(default_factory=list)
    outcome: Optional[str] = None
    related_doc_ids: List[str] = Field(default_factory=list)
    confidence_score: Optional[float] = None


# ── Timeline Models ────────────────────────────────────────────


class TimelineEvent(BaseModel):
    event_id: str
    date: datetime
    event_type: str  # "discussion", "decision", "implementation", "issue"
    title: str
    description: str
    participants: List[str] = Field(default_factory=list)
    doc_id: Optional[str] = None
    sentiment: SentimentType = SentimentType.NEUTRAL
    is_critical: bool = False


class DecisionTimeline(BaseModel):
    topic: str
    events: List[TimelineEvent]
    final_decision: Optional[Decision] = None
    outcome_assessment: Optional[str] = None
    confidence_score: float = 0.0


# ── Query Models ───────────────────────────────────────────────


class QueryRequest(BaseModel):
    question: str
    project_filter: Optional[str] = None
    date_from: Optional[datetime] = None
    date_to: Optional[datetime] = None
    top_k: int = 10


class Source(BaseModel):
    doc_id: str
    doc_type: DocumentType
    title: str
    date: datetime
    relevance_score: float
    excerpt: str


class QueryResponse(BaseModel):
    question: str
    answer: str
    timeline: Optional[DecisionTimeline] = None
    sources: List[Source] = Field(default_factory=list)
    confidence_score: float = 0.0
    processing_steps: List[str] = Field(default_factory=list)


# ── Ingestion Models ───────────────────────────────────────────


class IngestionRequest(BaseModel):
    documents: List[RawDocument]
    trigger_embedding: bool = True
    trigger_graph: bool = True


class IngestionResponse(BaseModel):
    ingested_count: int
    doc_ids: List[str]
    status: str
    errors: List[str] = Field(default_factory=list)


# ── Health Check ───────────────────────────────────────────────


class HealthResponse(BaseModel):
    service: str
    status: str
    version: str = "1.0.0"
    dependencies: Dict[str, str] = Field(default_factory=dict)