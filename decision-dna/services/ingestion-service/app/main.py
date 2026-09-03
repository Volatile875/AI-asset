"""
services/ingestion-service/app/main.py
Parses raw documents (emails, Jira, meetings) into
normalized RawDocument objects, then triggers embedding.
"""

import asyncio
import json
import os
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import List, Optional

import httpx
import redis.asyncio as redis
from fastapi import FastAPI, BackgroundTasks, HTTPException
from pydantic import BaseModel

from app.parsers.email_parser import parse_emails
from app.parsers.jira_parser import parse_jira_tickets
from app.parsers.meeting_parser import parse_meeting_notes

import logging

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s [ingestion-service] %(message)s",
)
log = logging.getLogger("ingestion-service")

REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379")
EMBEDDING_URL = os.getenv("EMBEDDING_SERVICE_URL", "http://embedding-service:8002")
GRAPH_URL = os.getenv("GRAPH_SERVICE_URL", "http://graph-service:8003")

# Bumped after every successful ingest. Read-path caches in query-service and
# timeline-service namespace their keys by this value, so a re-ingest
# invalidates every cached answer at once without enumerating keys.
CORPUS_VERSION_KEY = "dna:corpus_version"

redis_client = None
http_client: Optional[httpx.AsyncClient] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global redis_client, http_client
    redis_client = redis.from_url(REDIS_URL, decode_responses=True)
    # One pool for the whole process instead of a new client per ingest job.
    http_client = httpx.AsyncClient(
        timeout=httpx.Timeout(300.0, connect=5.0),
        limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
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


app = FastAPI(title="Ingestion Service", version="1.0.0", lifespan=lifespan)


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

# ── Models ─────────────────────────────────────────────────────

class IngestRequest(BaseModel):
    data_dir: str = "/app/data/synthetic"
    trigger_embedding: bool = True
    trigger_graph: bool = True


class IngestResponse(BaseModel):
    job_id: str
    status: str
    message: str


# ── Background Job ─────────────────────────────────────────────

async def run_ingestion_job(job_id: str, data_dir: str, trigger_embedding: bool, trigger_graph: bool):
    """Full ingestion pipeline running as background task."""
    await redis_client.hset(f"job:{job_id}", mapping={"status": "running", "progress": "0"})

    try:
        documents = []

        # Parse emails
        email_path = Path(data_dir) / "emails"
        if email_path.exists():
            emails = parse_emails(str(email_path))
            documents.extend(emails)
            await redis_client.hset(f"job:{job_id}", "progress", f"emails:{len(emails)}")

        # Parse meeting notes
        meeting_path = Path(data_dir) / "meetings"
        if meeting_path.exists():
            meetings = parse_meeting_notes(str(meeting_path))
            documents.extend(meetings)
            await redis_client.hset(f"job:{job_id}", "progress", f"meetings:{len(meetings)}")

        # Parse Jira tickets
        jira_path = Path(data_dir) / "jira"
        if jira_path.exists():
            tickets = parse_jira_tickets(str(jira_path))
            documents.extend(tickets)
            await redis_client.hset(f"job:{job_id}", "progress", f"jira:{len(tickets)}")

        # Store normalized documents in Redis
        for doc in documents:
            await redis_client.set(f"doc:{doc['doc_id']}", json.dumps(doc), ex=86400)

        log.info("job %s: parsed %d documents", job_id, len(documents))

        # Trigger downstream services over the shared connection pool.
        client = http_client
        if client is None:
            raise RuntimeError("HTTP client is not initialised")

        embed_failed = None
        if trigger_embedding:
            log.info("job %s: POST %s/embed-batch (%d docs)", job_id, EMBEDDING_URL, len(documents))
            r = await client.post(f"{EMBEDDING_URL}/embed-batch", json={"documents": documents})
            log.info("job %s: embed-batch → %s %s", job_id, r.status_code, r.text[:200])
            if r.status_code >= 400:
                # e.g. the embedding account is out of credit (402), the provider
                # is still throttling (503), or fallback vectors were refused.
                # Surface it instead of reporting "completed".
                try:
                    reason = r.json().get("detail", r.text)
                except Exception:  # noqa: BLE001
                    reason = r.text
                kind = {
                    402: "embedding account is out of API credit",
                    503: "embedding provider unavailable",
                }.get(r.status_code, f"embed-batch returned {r.status_code}")
                embed_failed = f"{kind}: {str(reason)[:600]}"

        if trigger_graph:
            log.info("job %s: POST %s/build-graph (%d docs)", job_id, GRAPH_URL, len(documents))
            r = await client.post(f"{GRAPH_URL}/build-graph", json={"documents": documents})
            log.info("job %s: build-graph → %s %s", job_id, r.status_code, r.text[:200])

        if embed_failed:
            await redis_client.hset(f"job:{job_id}", mapping={
                "status": "failed",
                "error": embed_failed,
                "total_docs": str(len(documents)),
            })
            log.error("job %s: FAILED — %s", job_id, embed_failed)
            return

        # Corpus changed: invalidate every cached query/timeline answer.
        corpus_version = await redis_client.incr(CORPUS_VERSION_KEY)

        await redis_client.hset(f"job:{job_id}", mapping={
            "status": "completed",
            "total_docs": str(len(documents)),
            "corpus_version": str(corpus_version),
            "completed_at": datetime.utcnow().isoformat(),
        })
        log.info("job %s: completed (%d docs, corpus_version=%s)",
                 job_id, len(documents), corpus_version)

    except Exception as e:
        log.exception("job %s: FAILED", job_id)
        await redis_client.hset(f"job:{job_id}", mapping={"status": "failed", "error": str(e)})


# ── Routes ─────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"service": "ingestion-service", "status": "healthy"}


@app.post("/ingest", response_model=IngestResponse)
async def ingest(request: IngestRequest, background_tasks: BackgroundTasks):
    job_id = str(uuid.uuid4())
    background_tasks.add_task(
        run_ingestion_job,
        job_id,
        request.data_dir,
        request.trigger_embedding,
        request.trigger_graph,
    )
    return IngestResponse(
        job_id=job_id,
        status="accepted",
        message="Ingestion started. Poll /status/{job_id} for progress.",
    )


@app.get("/status/{job_id}")
async def get_status(job_id: str):
    data = await redis_client.hgetall(f"job:{job_id}")
    if not data:
        raise HTTPException(status_code=404, detail="Job not found")
    return data