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

app = FastAPI(title="Embedding Service", version="1.0.0")

# ── Config ─────────────────────────────────────────────────────
OPENAI_API_KEY    = os.getenv("OPENAI_API_KEY")
PINECONE_API_KEY  = os.getenv("PINECONE_API_KEY")
PINECONE_ENV      = os.getenv("PINECONE_ENVIRONMENT", "us-east-1")
INDEX_NAME        = os.getenv("PINECONE_INDEX_NAME", "ai-asset")
EMBEDDING_MODEL   = os.getenv("EMBEDDING_MODEL", "text-embedding-3-large")
EMBEDDING_DIM     = int(os.getenv("EMBEDDING_DIMENSIONS", "1024"))

pc = None
index = None
embeddings_model = None
text_splitter = None


def init_clients() -> bool:
    """Lazily (re)initialize external clients. Idempotent and never raises, so a
    transient Pinecone/OpenAI failure can't crash startup or leave the service
    permanently unreachable — it retries on the next call and self-heals.
    Returns True once all clients are ready."""
    global pc, index, embeddings_model, text_splitter
    try:
        # Chunker + embedding model don't need the network to construct.
        if text_splitter is None:
            text_splitter = RecursiveCharacterTextSplitter(
                chunk_size=800,
                chunk_overlap=100,
                separators=["\n\n", "\n", ". ", " ", ""],
            )
        if embeddings_model is None:
            embeddings_model = OpenAIEmbeddings(
                model=EMBEDDING_MODEL,
                dimensions=EMBEDDING_DIM,
                openai_api_key=OPENAI_API_KEY,
            )
        # Pinecone: create the index if missing; warn (don't crash) on dim mismatch.
        if index is None:
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


async def embed_and_upsert(chunks: List[Dict[str, Any]]):
    """Generate embeddings and upsert to Pinecone in batches of 100."""
    BATCH_SIZE = 100

    for i in range(0, len(chunks), BATCH_SIZE):
        batch = chunks[i : i + BATCH_SIZE]
        texts = [c["content"] for c in batch]

        # Generate embeddings
        vectors = embeddings_model.embed_documents(texts)

        # Prepare Pinecone upsert payload
        upsert_data = [
            (chunk["chunk_id"], vector, chunk["metadata"])
            for chunk, vector in zip(batch, vectors)
        ]

        index.upsert(vectors=upsert_data)


# ── Routes ─────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {
        "service": "embedding-service",
        "status": "healthy",
        "ready": index is not None,  # False until Pinecone/OpenAI init succeeds
        "pinecone_index": INDEX_NAME,
        "embedding_model": EMBEDDING_MODEL,
        "embedding_dimensions": EMBEDDING_DIM,
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
