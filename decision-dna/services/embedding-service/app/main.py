"""
services/embedding-service/app/main.py
Receives normalized documents, chunks them, generates
embeddings via OpenAI, and upserts into Pinecone.

Two behavioural fixes vs. the previous version:

1. Fallback (hash-based) vectors can never reach the real Pinecone index.
   Previously `_embed_texts` swapped the module-global embedder for
   FallbackEmbeddings on any failure — permanently — and the very next line
   upserted those meaningless vectors into the production index, where they do
   not expire. Retrieval quality degraded for good, not for the outage.
   The rule is now explicit and enforced in one place: `assert_upsert_allowed`.

2. Embedding calls retry with bounded backoff and then fail loudly, instead of
   silently downgrading the process for its remaining lifetime.

Chunk metadata no longer stores `content_preview` alongside the full `content`
(readers derive the preview), removing a duplicate copy of every chunk from the
payload that comes back on every search.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from langchain.text_splitter import RecursiveCharacterTextSplitter
from langchain_openai import OpenAIEmbeddings
from pinecone import Pinecone, ServerlessSpec

from app.fallbacks import FallbackEmbeddings, FallbackIndex
from app.llm import (backoff_delay, describe, env_flag, is_rate_limited,
                     is_retryable, quota_exhausted, retry_after_seconds)
from app.provider_config import resolve_provider_config

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


# Ingestion is a bulk background job, not a user-facing request, so it can
# afford to wait out a rate-limit window that an interactive call cannot.
EMBED_MAX_ATTEMPTS = _env_int("EMBED_MAX_ATTEMPTS", 6)
EMBED_BATCH_SIZE = _env_int("EMBED_BATCH_SIZE", 64)
EMBED_MIN_BATCH_SIZE = _env_int("EMBED_MIN_BATCH_SIZE", 8)
# Cap on how long to honour a provider's Retry-After. OpenAI can ask for
# minutes when a per-minute token budget is exhausted; that is fine for a
# background ingest and is why this is far larger than the chat-side cap.
EMBED_RETRY_WAIT_MAX_S = _env_float("EMBED_RETRY_WAIT_MAX_S", 90.0)
# Optional pacing between successful batches, for accounts on a low TPM tier.
EMBED_REQUEST_INTERVAL_S = _env_float("EMBED_REQUEST_INTERVAL_S", 0.0)

pc = None
index = None
embeddings_model = None
text_splitter = None

_embeddings_is_stub = False
_index_is_stub = False


class FallbackVectorWriteBlocked(RuntimeError):
    """Raised when locally-generated stub vectors would land in a real index."""


class EmbeddingQuotaExhausted(RuntimeError):
    """The embedding account is out of credit. Retrying cannot help."""


class EmbeddingRateLimited(RuntimeError):
    """Still throttled after the full retry budget.

    Distinct from a flat 503 so the caller can respond by shrinking the batch:
    a per-minute TOKEN limit is not a per-request limit, and the same work often
    succeeds when split. The route turns an unrecovered one into 503.
    """


def index_is_real(target: Any) -> bool:
    """True when `target` is a live Pinecone index rather than the in-memory stub."""
    return target is not None and not isinstance(target, FallbackIndex)


def assert_upsert_allowed(target: Any, vectors_are_fallback: bool) -> None:
    """The one rule: stub vectors may only ever be written to the stub index.

    Hash-based fallback vectors carry no semantic meaning. Writing them into the
    production Pinecone index is not a temporary degradation — the vectors have
    no TTL, so every later search competes against permanent noise. There is
    deliberately no environment variable to override this.
    """
    if vectors_are_fallback and index_is_real(target):
        raise FallbackVectorWriteBlocked(
            "Refusing to upsert locally-generated fallback vectors into the real "
            f"Pinecone index '{INDEX_NAME}'. Fix the embedding provider credentials "
            "and re-run ingestion; the index has been left untouched."
        )


def init_clients() -> bool:
    """Construct clients once. Construction failure -> local stub (as before)."""
    global pc, index, embeddings_model, text_splitter
    global _embeddings_is_stub, _index_is_stub
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
                _embeddings_is_stub = False
            except Exception as exc:  # noqa: BLE001
                log.error("embeddings construction failed, using local stub: %r", exc)
                embeddings_model = FallbackEmbeddings(dimensions=EMBEDDING_DIM)
                _embeddings_is_stub = True
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
                        log.warning(
                            "index '%s' has dimension %s but EMBEDDING_DIMENSIONS is %s; "
                            "upserts will fail until this is reconciled.",
                            INDEX_NAME, actual_dimension, EMBEDDING_DIM,
                        )
                index = pc.Index(INDEX_NAME)
                _index_is_stub = False
            except Exception as exc:  # noqa: BLE001
                log.error("Pinecone construction failed, using local stub index: %r", exc)
                index = FallbackIndex()
                _index_is_stub = True
        return True
    except Exception as e:  # noqa: BLE001
        log.exception("client init failed (will retry on demand): %r", e)
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
    """Split a document into chunks with inherited metadata.

    `content_preview` is intentionally not stored: it was a second copy of the
    first 200 characters of `content`, and every search returns metadata, so the
    duplicate was paid for on every query. Readers derive the preview instead
    and still accept the old shape for vectors indexed before this change.
    """
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
            },
        }
        chunks.append(chunk)
    return chunks


async def _embed_texts(texts: List[str]) -> tuple[List[List[float]], bool]:
    """Embed one batch, distinguishing the three ways this fails.

    Returns (vectors, used_fallback). The module-global embedder is never
    replaced: a transient 429 must not change the behaviour of every later
    request for the rest of the process's life.

    Failure modes, which need opposite handling and all arrive as exceptions:

      * quota exhausted (429 insufficient_quota) - the account has no credit.
        Permanent. Raise immediately; retrying burns time and changes nothing.
      * rate limited (429 rate_limit_exceeded)   - too many requests or tokens
        in the window. Wait as long as the provider asks, then retry.
      * rejected (400/401/404 ...)               - misconfiguration. Raise.

    Local fallback vectors are produced only when the destination index is
    itself the in-memory stub — see assert_upsert_allowed.
    """
    if isinstance(embeddings_model, FallbackEmbeddings):
        return embeddings_model.embed_documents(texts), True

    last_error: Optional[BaseException] = None
    for attempt in range(1, max(1, EMBED_MAX_ATTEMPTS) + 1):
        try:
            return await embeddings_model.aembed_documents(texts), False
        except Exception as exc:  # noqa: BLE001
            last_error = exc

            if quota_exhausted(exc):
                log.error("embed: account is out of API credit, not retrying: %s", describe(exc))
                raise EmbeddingQuotaExhausted(
                    f"The embedding account has no remaining quota ({describe(exc)}). "
                    "This is a billing state, not an outage — retrying cannot fix it. "
                    "Top up the OpenAI balance for OPENAI_API_KEY, or point it at a funded "
                    "key. Nothing was written to the Pinecone index."
                ) from exc

            if not is_retryable(exc):
                log.error("embed: permanent provider error, not retrying (model=%r): %s",
                          EMBEDDING_MODEL, describe(exc))
                raise HTTPException(
                    status_code=503,
                    detail=(
                        f"Embedding provider rejected the request for model "
                        f"'{EMBEDDING_MODEL}': {describe(exc)}. This is a configuration "
                        "problem, not an outage — check EMBEDDING_MODEL and OPENAI_API_KEY "
                        "in .env. Nothing was written to the Pinecone index."
                    ),
                ) from exc

            if attempt >= EMBED_MAX_ATTEMPTS:
                break

            asked = retry_after_seconds(exc)
            if asked is not None:
                delay = min(asked, EMBED_RETRY_WAIT_MAX_S)
                source = f"provider asked for {asked:.0f}s"
            else:
                delay = min(backoff_delay(attempt) * 4, EMBED_RETRY_WAIT_MAX_S)
                source = "exponential backoff"
            log.warning("embed attempt %d/%d failed (%s); waiting %.1fs (%s)",
                        attempt, EMBED_MAX_ATTEMPTS, describe(exc), delay, source)
            await asyncio.sleep(delay)

    if not index_is_real(index):
        # Nothing real can be polluted, so degrade locally and keep going.
        log.warning(
            "embedding provider failed (%s); destination is the in-memory stub index, "
            "using local fallback vectors", type(last_error).__name__,
        )
        return FallbackEmbeddings(dimensions=EMBEDDING_DIM).embed_documents(texts), True

    if is_rate_limited(last_error):
        raise EmbeddingRateLimited(
            f"still rate limited after {EMBED_MAX_ATTEMPTS} attempts on a batch of "
            f"{len(texts)} texts ({describe(last_error)})"
        ) from last_error

    raise HTTPException(
        status_code=503,
        detail=(
            f"Embedding provider unavailable after {EMBED_MAX_ATTEMPTS} attempts "
            f"({describe(last_error)}). Nothing was written to the Pinecone index."
        ),
    )


async def embed_batch_adaptively(texts: List[str]) -> tuple[List[List[float]], bool]:
    """Embed `texts`, halving the sub-batch size while the provider throttles.

    A per-minute token budget is not a per-request limit: the same total work
    goes through when split into smaller requests. Halving finds a size that
    fits instead of failing the whole ingest at the first sustained 429.
    """
    size = max(1, min(len(texts), EMBED_BATCH_SIZE))
    while True:
        try:
            return await _split_embed(texts, size)
        except EmbeddingRateLimited:
            if size <= EMBED_MIN_BATCH_SIZE:
                raise
            size = max(EMBED_MIN_BATCH_SIZE, size // 2)
            log.warning("embed: still rate limited; halving sub-batch to %d texts", size)


async def _split_embed(texts: List[str], size: int) -> tuple[List[List[float]], bool]:
    """Embed `texts` in sub-batches of `size`, concatenating the result."""
    vectors: List[List[float]] = []
    degraded = False
    for start in range(0, len(texts), size):
        chunk_vectors, used_fallback = await _embed_texts(texts[start:start + size])
        vectors.extend(chunk_vectors)
        degraded = degraded or used_fallback
    return vectors, degraded


def _upsert(batch_vectors: List[tuple]) -> None:
    """Blocking Pinecone upsert — pinecone-client 3.2.2 has no async API."""
    index.upsert(vectors=batch_vectors)


async def embed_and_upsert(chunks: List[Dict[str, Any]]) -> tuple[bool, int]:
    """Generate embeddings and upsert in batches.

    Returns (degraded, chunks_written). On failure the exception carries how far
    it got, so a partial ingest reports a number instead of just "failed".
    """
    degraded = False
    written = 0
    total_batches = (len(chunks) + EMBED_BATCH_SIZE - 1) // max(1, EMBED_BATCH_SIZE)

    for index_of, i in enumerate(range(0, len(chunks), EMBED_BATCH_SIZE), start=1):
        batch = chunks[i: i + EMBED_BATCH_SIZE]
        texts = [c["content"] for c in batch]

        try:
            vectors, used_fallback = await embed_batch_adaptively(texts)
        except EmbeddingRateLimited as exc:
            raise HTTPException(
                status_code=503,
                detail=(
                    f"Embedding provider is still rate limiting at the minimum batch size "
                    f"({EMBED_MIN_BATCH_SIZE}): {exc}. {written} of {len(chunks)} chunks were "
                    "embedded before this failure. Raise EMBED_REQUEST_INTERVAL_S to pace the "
                    "run, or wait for the rate-limit window to reset and ingest again."
                ),
            ) from exc
        except (HTTPException, EmbeddingQuotaExhausted) as exc:
            # Say how much landed before giving up; the run is resumable.
            note = f" {written} of {len(chunks)} chunks were embedded before this failure."
            if isinstance(exc, HTTPException):
                exc.detail = f"{exc.detail}{note}"
            else:
                raise EmbeddingQuotaExhausted(f"{exc}{note}") from exc
            raise

        degraded = degraded or used_fallback
        # Enforced before every single write, not once at startup.
        assert_upsert_allowed(index, used_fallback)

        upsert_data = [
            (chunk["chunk_id"], vector, chunk["metadata"])
            for chunk, vector in zip(batch, vectors)
        ]
        await asyncio.to_thread(_upsert, upsert_data)
        written += len(batch)
        log.info("embed: batch %d/%d upserted (%d/%d chunks)",
                 index_of, total_batches, written, len(chunks))

        if EMBED_REQUEST_INTERVAL_S > 0 and index_of < total_batches:
            await asyncio.sleep(EMBED_REQUEST_INTERVAL_S)

    return degraded, written


# ── Routes ─────────────────────────────────────────────────────

def _is_degraded() -> bool:
    """True when either the embedder or the index is a local stub."""
    return _embeddings_is_stub or _index_is_stub


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
        all_chunks.extend(chunk_document(doc))

    try:
        degraded, _written = await embed_and_upsert(all_chunks)
    except FallbackVectorWriteBlocked as exc:
        log.error("embed-batch blocked: %s", exc)
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except EmbeddingQuotaExhausted as exc:
        log.error("embed-batch stopped: %s", exc)
        raise HTTPException(status_code=402, detail=str(exc)) from exc

    return EmbedBatchResponse(
        embedded_count=len(request.documents),
        chunk_count=len(all_chunks),
        status="success",
        degraded=degraded or _is_degraded(),
    )


@app.post("/embed-single")
async def embed_single(doc: Dict[str, Any]):
    if not init_clients():
        raise HTTPException(status_code=503, detail="Upstream (Pinecone/OpenAI) not ready - check API keys and that the index exists")
    chunks = chunk_document(doc)
    try:
        degraded, _written = await embed_and_upsert(chunks)
    except FallbackVectorWriteBlocked as exc:
        log.error("embed-single blocked: %s", exc)
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except EmbeddingQuotaExhausted as exc:
        log.error("embed-single stopped: %s", exc)
        raise HTTPException(status_code=402, detail=str(exc)) from exc
    return {"chunk_count": len(chunks), "status": "success", "degraded": degraded or _is_degraded()}


@app.get("/index-stats")
async def index_stats():
    if not init_clients():
        raise HTTPException(status_code=503, detail="Upstream (Pinecone/OpenAI) not ready - check API keys and that the index exists")
    stats = await asyncio.to_thread(index.describe_index_stats)
    return {"total_vectors": stats.total_vector_count, "index": INDEX_NAME}


@app.get("/selftest")
async def selftest():
    """Actively verify OpenAI + Pinecone credentials with fresh clients.

    /health only reflects init-time state, and the OpenAI quota error surfaces
    only on a real embed call (the constructor never hits the network). This
    route does a genuine 1-string embed and a real Pinecone describe so a bad
    key is caught BEFORE ingestion is attempted.
    """
    result: Dict[str, Any] = {
        "openai": {"ok": False, "error": None},
        "pinecone": {"ok": False, "error": None, "index": INDEX_NAME,
                     "dimension": None, "expected_dimension": EMBEDDING_DIM},
    }

    try:
        probe = OpenAIEmbeddings(
            model=EMBEDDING_MODEL,
            dimensions=EMBEDDING_DIM,
            openai_api_key=PROVIDER_CONFIG["embedding_api_key"],
            base_url=PROVIDER_CONFIG["embedding_base_url"],
        )
        vector = await probe.aembed_query("preflight")
        result["openai"]["ok"] = bool(vector)
    except Exception as exc:  # noqa: BLE001
        result["openai"]["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"

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
    except Exception as exc:  # noqa: BLE001
        result["pinecone"]["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"

    result["ok"] = result["openai"]["ok"] and result["pinecone"]["ok"]
    return result
