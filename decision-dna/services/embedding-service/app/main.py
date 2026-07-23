"""
services/embedding-service/app/main.py
Receives normalized documents, chunks them, generates
embeddings via OpenAI, and upserts into Pinecone.
"""

import os
import uuid
from typing import List, Dict, Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from langchain.text_splitter import RecursiveCharacterTextSplitter
from langchain_openai import OpenAIEmbeddings
from pinecone import Pinecone, ServerlessSpec

from app.fallbacks import FallbackEmbeddings, FallbackIndex
from app.provider_config import resolve_provider_config

import logging
import time
import uuid

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s [embedding-service] %(message)s",
)
log = logging.getLogger("embedding-service")

app = FastAPI(title="Embedding Service", version="1.0.0")


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

# ── Config ─────────────────────────────────────────────────────
PROVIDER_CONFIG = resolve_provider_config()
OPENAI_API_KEY = PROVIDER_CONFIG["embedding_api_key"]  # For legacy config references
PINECONE_API_KEY = os.getenv("PINECONE_API_KEY")
PINECONE_ENV = os.getenv("PINECONE_ENVIRONMENT", "us-east-1")
INDEX_NAME = os.getenv("PINECONE_INDEX_NAME", "ai-asset")
EMBEDDING_MODEL = PROVIDER_CONFIG["embedding_model"]
EMBEDDING_DIM = int(os.getenv("EMBEDDING_DIMENSIONS", "1024"))

pc = None
index = None
embeddings_model = None
text_splitter = None


def init_clients() -> bool:
    """Initialize external clients when possible, but fall back to local stubs if credentials are unavailable."""
    global pc, index, embeddings_model, text_splitter
    try:
        if text_splitter is None:
            text_splitter = RecursiveCharacterTextSplitter(
                chunk_size=800,
                chunk_overlap=100,
                separators=["\n\n", "\n", ". ", " ", ""],
            )
        if embeddings_model is None:
            try:
                embeddings_model = OpenAIEmbeddings(
                    model=EMBEDDING_MODEL,
                    dimensions=EMBEDDING_DIM,
                    openai_api_key=PROVIDER_CONFIG["embedding_api_key"],
                    base_url=PROVIDER_CONFIG["embedding_base_url"],
                )
            except Exception as exc:
                print(f"[embedding-service] OpenAI init failed, using fallback embeddings: {exc}", flush=True)
                embeddings_model = FallbackEmbeddings(dimensions=EMBEDDING_DIM)
        if index is None:
            try:
                pc = Pinecone(api_key=PINECONE_API_KEY)
                existing = [i.name for i in pc.list_indexes()]
                if INDEX_NAME not in existing:
                    pc.create_index(
                        name=INDEX_NAME,
                        dimension=EMBEDDING_DIM,
                        metric="cosine",
                        spec=ServerlessSpec(cloud="aws", region=PINECONE_ENV),
                    )
                else:
                    index_description = pc.describe_index(INDEX_NAME)
                    actual_dimension = getattr(index_description, "dimension", None)
                    if actual_dimension and actual_dimension != EMBEDDING_DIM:
                        print(
                            f"[embedding-service] WARNING: index '{INDEX_NAME}' has dimension "
                            f"{actual_dimension} but EMBEDDING_DIMENSIONS is {EMBEDDING_DIM}; "
                            f"upserts will fail until this is reconciled.",
                            flush=True,
                        )
                index = pc.Index(INDEX_NAME)
            except Exception as exc:
                print(f"[embedding-service] Pinecone init failed, using fallback index: {exc}", flush=True)
                index = FallbackIndex()
        return True
    except Exception as e:
        print(f"[embedding-service] client init failed (will retry on demand): {e}", flush=True)
        return False


@app.on_event("startup")
async def startup():
    # Non-fatal: never abort startup on a dependency hiccup. The service must stay
    # up and reachable so it can self-heal; clients are (re)initialized on demand.
    init_clients()


# ── Models ─────────────────────────────────────────────────────

class EmbedBatchRequest(BaseModel):
    documents: List[Dict[str, Any]]


class EmbedBatchResponse(BaseModel):
    embedded_count: int
    chunk_count: int
    status: str
    degraded: bool = False  # True = embedded with local fallback vectors (OpenAI unavailable)


# ── Core Logic ─────────────────────────────────────────────────

def chunk_document(doc: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Split a document into chunks with inherited metadata."""
    content = doc.get("content", "")
    chunks_text = text_splitter.split_text(content)

    chunks = []
    for i, chunk_text in enumerate(chunks_text):
        chunk = {
            "chunk_id": f"{doc['doc_id']}_chunk_{i}",
            "doc_id": doc["doc_id"],
            "doc_type": doc.get("doc_type", "unknown"),
            "content": chunk_text,
            "chunk_index": i,
            "total_chunks": len(chunks_text),
            # Metadata stored in Pinecone for filtering
            "metadata": {
                "doc_id": doc["doc_id"],
                "doc_type": doc.get("doc_type", "unknown"),
                "title": doc.get("title", ""),
                "date": doc.get("date", ""),
                "project": doc.get("project", "") or "",
                "participants": ",".join(doc.get("participants", [])),
                "tags": ",".join(doc.get("tags", [])),
                "chunk_index": i,
                "content": chunk_text,
                "content_preview": chunk_text[:200],
            },
        }
        chunks.append(chunk)
    return chunks


def _embed_texts(texts: List[str]) -> List[List[float]]:
    """Embed texts, degrading to local FallbackEmbeddings if the provider call fails.

    OpenAIEmbeddings' constructor never makes a network call, so a bad key or an
    exhausted quota (HTTP 429 insufficient_quota) only surfaces here, at call time.
    When that happens we permanently swap the process over to FallbackEmbeddings so
    the whole ingest run stays in one vector space rather than mixing real + local.
    """
    global embeddings_model
    try:
        return embeddings_model.embed_documents(texts)
    except Exception as exc:
        if isinstance(embeddings_model, FallbackEmbeddings):
            raise  # fallback itself failed — nothing left to try
        log.warning(
            "Embedding provider call failed (%s: %s); switching to local FallbackEmbeddings "
            "for the rest of this process. Retrieval quality will be degraded.",
            type(exc).__name__, exc,
        )
        embeddings_model = FallbackEmbeddings(dimensions=EMBEDDING_DIM)
        return embeddings_model.embed_documents(texts)


async def embed_and_upsert(chunks: List[Dict[str, Any]]):
    """Generate embeddings and upsert to Pinecone in batches of 100."""
    BATCH_SIZE = 100

    for i in range(0, len(chunks), BATCH_SIZE):
        batch = chunks[i : i + BATCH_SIZE]
        texts = [c["content"] for c in batch]

        # Generate embeddings (degrades to local vectors if the provider is down)
        vectors = _embed_texts(texts)

        # Prepare Pinecone upsert payload
        upsert_data = [
            (chunk["chunk_id"], vector, chunk["metadata"])
            for chunk, vector in zip(batch, vectors)
        ]

        index.upsert(vectors=upsert_data)


# ── Routes ─────────────────────────────────────────────────────

def _is_degraded() -> bool:
    """True when either the embedder or the index has fallen back to a local stub."""
    return isinstance(embeddings_model, FallbackEmbeddings) or isinstance(index, FallbackIndex)


@app.get("/health")
async def health():
    return {
        "service": "embedding-service",
        "status": "healthy",
        "ready": index is not None,  # False until Pinecone/OpenAI init succeeds
        "degraded": _is_degraded(),  # True = running on local fallback vectors, not real embeddings
        "pinecone_index": INDEX_NAME,
        "embedding_model": EMBEDDING_MODEL,
        "embedding_dimensions": EMBEDDING_DIM,
        "provider": PROVIDER_CONFIG["provider"],
    }


@app.post("/embed-batch", response_model=EmbedBatchResponse)
async def embed_batch(request: EmbedBatchRequest):
    if not init_clients():
        raise HTTPException(status_code=503, detail="Upstream (Pinecone/OpenAI) not ready - check API keys and that the index exists")
    if not request.documents:
        raise HTTPException(status_code=400, detail="No documents provided")

    all_chunks = []
    for doc in request.documents:
        chunks = chunk_document(doc)
        all_chunks.extend(chunks)

    await embed_and_upsert(all_chunks)

    return EmbedBatchResponse(
        embedded_count=len(request.documents),
        chunk_count=len(all_chunks),
        status="success",
        degraded=_is_degraded(),
    )


@app.post("/embed-single")
async def embed_single(doc: Dict[str, Any]):
    if not init_clients():
        raise HTTPException(status_code=503, detail="Upstream (Pinecone/OpenAI) not ready - check API keys and that the index exists")
    chunks = chunk_document(doc)
    await embed_and_upsert(chunks)
    return {"chunk_count": len(chunks), "status": "success"}


@app.get("/index-stats")
async def index_stats():
    if not init_clients():
        raise HTTPException(status_code=503, detail="Upstream (Pinecone/OpenAI) not ready - check API keys and that the index exists")
    stats = index.describe_index_stats()
    return {"total_vectors": stats.total_vector_count, "index": INDEX_NAME}


@app.get("/selftest")
async def selftest():
    """Actively verify OpenAI + Pinecone credentials with fresh clients.

    /health only reflects init-time state, and the OpenAI quota error surfaces
    only on a real embed call (the constructor never hits the network). This
    route does a genuine 1-string embed and a real Pinecone describe so a bad
    key is caught BEFORE a full ingest silently lands in the throwaway
    in-memory FallbackIndex that query-service can never read.
    """
    result: Dict[str, Any] = {
        "openai": {"ok": False, "error": None},
        "pinecone": {"ok": False, "error": None, "index": INDEX_NAME,
                     "dimension": None, "expected_dimension": EMBEDDING_DIM},
    }

    # OpenAI: real minimal embedding via a fresh client (not the possibly-swapped global)
    try:
        probe = OpenAIEmbeddings(
            model=EMBEDDING_MODEL,
            dimensions=EMBEDDING_DIM,
            openai_api_key=PROVIDER_CONFIG["embedding_api_key"],
            base_url=PROVIDER_CONFIG["embedding_base_url"],
        )
        vector = probe.embed_query("preflight")
        result["openai"]["ok"] = bool(vector)
    except Exception as exc:
        result["openai"]["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"

    # Pinecone: real describe via a fresh client; verify the index exists at the right dim
    try:
        probe_pc = Pinecone(api_key=PINECONE_API_KEY)
        names = [i.name for i in probe_pc.list_indexes()]
        if INDEX_NAME not in names:
            result["pinecone"]["error"] = f"index '{INDEX_NAME}' not found (available: {names})"
        else:
            desc = probe_pc.describe_index(INDEX_NAME)
            dim = getattr(desc, "dimension", None)
            result["pinecone"]["dimension"] = dim
            if dim is not None and dim != EMBEDDING_DIM:
                result["pinecone"]["error"] = (
                    f"index dimension {dim} != EMBEDDING_DIMENSIONS {EMBEDDING_DIM}; upserts will fail"
                )
            else:
                result["pinecone"]["ok"] = True
    except Exception as exc:
        result["pinecone"]["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"

    result["ok"] = result["openai"]["ok"] and result["pinecone"]["ok"]
    return result
