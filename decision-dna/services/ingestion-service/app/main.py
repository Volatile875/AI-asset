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
from datetime import datetime
from pathlib import Path
from typing import List

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

app = FastAPI(title="Ingestion Service", version="1.0.0")


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

REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379")
EMBEDDING_URL = os.getenv("EMBEDDING_SERVICE_URL", "http://embedding-service:8002")
GRAPH_URL = os.getenv("GRAPH_SERVICE_URL", "http://graph-service:8003")

redis_client = None


@app.on_event("startup")
async def startup():
    global redis_client
    redis_client = redis.from_url(REDIS_URL, decode_responses=True)


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

        # Trigger downstream services
        async with httpx.AsyncClient(timeout=120) as client:
            if trigger_embedding:
                log.info("job %s: POST %s/embed-batch (%d docs)", job_id, EMBEDDING_URL, len(documents))
                r = await client.post(f"{EMBEDDING_URL}/embed-batch", json={"documents": documents})
                log.info("job %s: embed-batch → %s %s", job_id, r.status_code, r.text[:200])

            if trigger_graph:
                log.info("job %s: POST %s/build-graph (%d docs)", job_id, GRAPH_URL, len(documents))
                r = await client.post(f"{GRAPH_URL}/build-graph", json={"documents": documents})
                log.info("job %s: build-graph → %s %s", job_id, r.status_code, r.text[:200])

        await redis_client.hset(f"job:{job_id}", mapping={
            "status": "completed",
            "total_docs": str(len(documents)),
            "completed_at": datetime.utcnow().isoformat(),
        })
        log.info("job %s: completed (%d docs)", job_id, len(documents))

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