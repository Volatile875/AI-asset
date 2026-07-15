"""
api-gateway/app/main.py
Central entry point — routes requests to microservices,
handles auth, rate limiting, and CORS.
"""

import os
import time
import httpx
from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
import redis.asyncio as redis
from scalar_fastapi import get_scalar_api_reference

app = FastAPI(
    title="DecisionDNA API Gateway",
    description="Routes all client requests to appropriate microservices",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Service URLs ───────────────────────────────────────────────
SERVICES = {
    "ingestion": os.getenv("INGESTION_SERVICE_URL", "http://ingestion-service:8001"),
    "embedding": os.getenv("EMBEDDING_SERVICE_URL", "http://embedding-service:8002"),
    "graph":     os.getenv("GRAPH_SERVICE_URL",     "http://graph-service:8003"),
    "query":     os.getenv("QUERY_SERVICE_URL",     "http://query-service:8004"),
    "timeline":  os.getenv("TIMELINE_SERVICE_URL",  "http://timeline-service:8005"),
}

REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379")
redis_client = None


@app.on_event("startup")
async def startup():
    global redis_client
    redis_client = redis.from_url(REDIS_URL, decode_responses=True)


@app.on_event("shutdown")
async def shutdown():
    if redis_client:
        await redis_client.close()


# ── Rate Limiting ──────────────────────────────────────────────

async def rate_limit(request: Request):
    """Simple IP-based rate limiter using Redis."""
    if not redis_client:
        return
    ip = request.client.host
    key = f"ratelimit:{ip}"
    current = await redis_client.incr(key)
    if current == 1:
        await redis_client.expire(key, 60)  # 60 second window
    if current > 100:  # 100 requests per minute
        raise HTTPException(status_code=429, detail="Rate limit exceeded")


# ── Proxy Helper ───────────────────────────────────────────────

async def proxy(service_name: str, path: str, method: str, body=None, params=None):
    base = SERVICES.get(service_name)
    if not base:
        raise HTTPException(status_code=404, detail=f"Unknown service: {service_name}")
    url = f"{base}{path}"
    async with httpx.AsyncClient(timeout=60) as client:
        try:
            if method == "GET":
                resp = await client.get(url, params=params)
            elif method == "POST":
                resp = await client.post(url, json=body)
            else:
                raise HTTPException(status_code=405, detail="Method not allowed")
            return JSONResponse(content=resp.json(), status_code=resp.status_code)
        except httpx.ConnectError:
            raise HTTPException(status_code=503, detail=f"{service_name} is unavailable")


# ── Routes ─────────────────────────────────────────────────────

@app.get("/health")
async def gateway_health():
    """Check health of all downstream services."""
    statuses = {}
    async with httpx.AsyncClient(timeout=5) as client:
        for name, url in SERVICES.items():
            try:
                r = await client.get(f"{url}/health")
                statuses[name] = "healthy" if r.status_code == 200 else "degraded"
            except Exception:
                statuses[name] = "unreachable"
    return {"gateway": "healthy", "services": statuses, "timestamp": time.time()}


# Ingestion routes
@app.post("/api/v1/ingest", dependencies=[Depends(rate_limit)])
async def ingest_documents(request: Request):
    body = await request.json()
    return await proxy("ingestion", "/ingest", "POST", body)


@app.get("/api/v1/ingest/status/{job_id}", dependencies=[Depends(rate_limit)])
async def ingestion_status(job_id: str):
    return await proxy("ingestion", f"/status/{job_id}", "GET")


# Query routes
@app.post("/api/v1/query", dependencies=[Depends(rate_limit)])
async def query(request: Request):
    body = await request.json()
    return await proxy("query", "/query", "POST", body)


# Timeline routes
@app.get("/api/v1/timeline/{topic}", dependencies=[Depends(rate_limit)])
async def get_timeline(topic: str):
    return await proxy("timeline", f"/timeline/{topic}", "GET")


# Graph routes
@app.get("/api/v1/graph/decisions", dependencies=[Depends(rate_limit)])
async def get_decisions(project: str = None):
    return await proxy("graph", "/decisions", "GET", params={"project": project})


@app.get("/api/v1/graph/entities/{entity}", dependencies=[Depends(rate_limit)])
async def get_entity(entity: str):
    return await proxy("graph", f"/entities/{entity}", "GET")


### Scalar API Documentation
@app.get("/scalar", include_in_schema=False)
def get_scalar_docs():
    return get_scalar_api_reference(
        openapi_url=app.openapi_url,
        title="Scalar API",
    )