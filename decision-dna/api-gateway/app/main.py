"""
api-gateway/app/main.py
Central entry point — routes requests to microservices,
handles auth, rate limiting, and CORS.
"""

import os
import time
import uuid
import logging
from datetime import datetime, timedelta, timezone
import asyncio
import asyncpg
import bcrypt
import jwt
import httpx
from fastapi import FastAPI, Request, HTTPException, Depends, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
import redis.asyncio as redis

# ── Logging ────────────────────────────────────────────────────
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s [api-gateway] %(message)s",
)
log = logging.getLogger("api-gateway")
try:
    from scalar_fastapi import get_scalar_api_reference
except ImportError:  # scalar-fastapi 1.0.0 keeps it in a submodule
    try:
        from scalar_fastapi.scalar_fastapi import get_scalar_api_reference
    except ImportError:
        get_scalar_api_reference = None

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


# ── Request tracing ────────────────────────────────────────────
# Logs every request in/out with a correlation id (rid) so a single
# call can be followed across the gateway and every downstream service.
@app.middleware("http")
async def trace_requests(request: Request, call_next):
    rid = request.headers.get("x-request-id") or uuid.uuid4().hex[:8]
    client = request.client.host if request.client else "?"
    start = time.perf_counter()
    log.info("→ %s %s (rid=%s client=%s)", request.method, request.url.path, rid, client)
    try:
        response = await call_next(request)
    except Exception:
        dur = (time.perf_counter() - start) * 1000
        log.exception("✗ %s %s UNHANDLED after %.0fms (rid=%s)",
                      request.method, request.url.path, dur, rid)
        raise
    dur = (time.perf_counter() - start) * 1000
    log.info("← %s %s %s %.0fms (rid=%s)",
             request.method, request.url.path, response.status_code, dur, rid)
    response.headers["x-request-id"] = rid
    return response

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

# ── Postgres (user accounts) ──────────────────────────────────
# Not containerized — connects out to the native Postgres instance running
# on the host (see pgAdmin server "Auth-AI/asset"). host.docker.internal is
# Docker Desktop's DNS name for the host machine from inside a container.
POSTGRES_HOST = os.getenv("POSTGRES_HOST", "host.docker.internal")
POSTGRES_PORT = int(os.getenv("POSTGRES_PORT", "5432"))
POSTGRES_DB = os.getenv("POSTGRES_DB", "postgres")
POSTGRES_USER = os.getenv("POSTGRES_USER", "postgres")
POSTGRES_PASSWORD = os.getenv("POSTGRES_PASSWORD", "")

db_pool: asyncpg.Pool | None = None

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS users (
    username TEXT PRIMARY KEY,
    password_hash TEXT NOT NULL,
    team_name TEXT NOT NULL,
    reporting_manager TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

# ── JWT ────────────────────────────────────────────────────────
JWT_SECRET = os.getenv("JWT_SECRET", "")
JWT_ALGORITHM = "HS256"
JWT_EXPIRY_DAYS = 7


async def connect_postgres_with_retry(attempts: int = 5, delay_s: float = 2.0) -> asyncpg.Pool:
    """Postgres can still be finishing startup even after compose's healthcheck
    passes (e.g. on a cold volume). Retry briefly instead of crash-looping."""
    last_err = None
    for attempt in range(1, attempts + 1):
        try:
            pool = await asyncpg.create_pool(
                host=POSTGRES_HOST, port=POSTGRES_PORT, database=POSTGRES_DB,
                user=POSTGRES_USER, password=POSTGRES_PASSWORD,
                min_size=1, max_size=10,
            )
            async with pool.acquire() as conn:
                await conn.execute(SCHEMA_SQL)
            return pool
        except Exception as e:  # noqa: BLE001 - want to retry on any connect/DDL failure
            last_err = e
            log.warning("postgres connect attempt %d/%d failed: %r", attempt, attempts, e)
            await asyncio.sleep(delay_s)
    raise RuntimeError(f"Could not connect to Postgres after {attempts} attempts: {last_err}")


@app.on_event("startup")
async def startup():
    global redis_client, db_pool
    redis_client = redis.from_url(REDIS_URL, decode_responses=True)
    try:
        db_pool = await connect_postgres_with_retry()
        log.info("postgres: connected, schema ensured")
    except Exception:
        log.exception("postgres: failed to connect at startup; auth routes will 503 until it recovers")
        db_pool = None


@app.on_event("shutdown")
async def shutdown():
    if redis_client:
        await redis_client.close()
    if db_pool:
        await db_pool.close()


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


# ── Auth ───────────────────────────────────────────────────────

class SignupRequest(BaseModel):
    username: str
    password: str
    team_name: str
    reporting_manager: str


class LoginRequest(BaseModel):
    username: str
    password: str


def require_db():
    if db_pool is None:
        raise HTTPException(status_code=503, detail="Account storage is unavailable (Postgres not connected)")
    return db_pool


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except ValueError:
        return False


def create_access_token(username: str) -> str:
    if not JWT_SECRET:
        raise HTTPException(status_code=503, detail="Auth is misconfigured: JWT_SECRET is not set")
    now = datetime.now(timezone.utc)
    payload = {
        "sub": username,
        "iat": now,
        "exp": now + timedelta(days=JWT_EXPIRY_DAYS),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


async def require_auth(authorization: str = Header(default="")) -> str:
    """Verifies the Bearer JWT and returns the logged-in username. Raises 401
    on anything invalid/expired/missing so the frontend's existing "401 ->
    log out" handling actually means something. Stateless: no DB round-trip,
    which also means there's no server-side revocation short of rotating
    JWT_SECRET (acceptable trade-off for this app's scale)."""
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or malformed Authorization header")
    token = authorization.removeprefix("Bearer ").strip()
    if not token:
        raise HTTPException(status_code=401, detail="Missing token")
    if not JWT_SECRET:
        raise HTTPException(status_code=503, detail="Auth is misconfigured: JWT_SECRET is not set")

    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Session expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid session")

    username = payload.get("sub")
    if not username:
        raise HTTPException(status_code=401, detail="Invalid session")
    return username


# ── Proxy Helper ───────────────────────────────────────────────

async def proxy(service_name: str, path: str, method: str, body=None, params=None):
    base = SERVICES.get(service_name)
    if not base:
        log.error("proxy: unknown service '%s'", service_name)
        raise HTTPException(status_code=404, detail=f"Unknown service: {service_name}")
    url = f"{base}{path}"
    rid = uuid.uuid4().hex[:8]
    headers = {"x-request-id": rid}
    # Timeout must be >= the frontend's client timeout (90s), otherwise the gateway
    # aborts a slow-but-working pipeline first and masks where the real delay is.
    start = time.perf_counter()
    log.info("→ proxy[%s] %s %s (rid=%s)", service_name, method, url, rid)
    async with httpx.AsyncClient(timeout=120) as client:
        try:
            if method == "GET":
                resp = await client.get(url, params=params, headers=headers)
            elif method == "POST":
                resp = await client.post(url, json=body, headers=headers)
            else:
                raise HTTPException(status_code=405, detail="Method not allowed")
        except httpx.ConnectError as e:
            log.error("✗ proxy[%s] connection refused: %s (rid=%s)", service_name, e, rid)
            raise HTTPException(status_code=503, detail=f"{service_name} is unavailable (connection refused)")
        except httpx.TimeoutException as e:
            dur = (time.perf_counter() - start) * 1000
            log.error("✗ proxy[%s] timed out after %.0fms: %r (rid=%s)", service_name, dur, e, rid)
            raise HTTPException(status_code=504, detail=f"{service_name} timed out")
        except httpx.HTTPError as e:
            log.exception("✗ proxy[%s] transport error (rid=%s)", service_name, rid)
            raise HTTPException(status_code=502, detail=f"{service_name} transport error: {e}")

    dur = (time.perf_counter() - start) * 1000
    log.info("← proxy[%s] %s %.0fms (rid=%s)", service_name, resp.status_code, dur, rid)
    try:
        content = resp.json()
    except Exception:
        log.error("✗ proxy[%s] non-JSON response (status=%s): %s (rid=%s)",
                  service_name, resp.status_code, resp.text[:500], rid)
        raise HTTPException(status_code=502,
                            detail=f"{service_name} returned a non-JSON response (status {resp.status_code})")
    if resp.status_code >= 400:
        log.warning("proxy[%s] downstream returned %s: %s (rid=%s)",
                    service_name, resp.status_code, str(content)[:500], rid)
    return JSONResponse(content=content, status_code=resp.status_code)


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
                if r.status_code != 200:
                    log.warning("health: %s at %s returned %s", name, url, r.status_code)
            except Exception as e:
                statuses[name] = "unreachable"
                log.warning("health: %s at %s unreachable: %r", name, url, e)
    log.info("health check: %s", statuses)
    return {"gateway": "healthy", "services": statuses, "timestamp": time.time()}


# Auth routes
@app.post("/api/v1/auth/signup", dependencies=[Depends(rate_limit)])
async def signup(req: SignupRequest):
    username = req.username.strip()
    team_name = req.team_name.strip()
    reporting_manager = req.reporting_manager.strip()
    if not username or not req.password.strip() or not team_name or not reporting_manager:
        raise HTTPException(status_code=400, detail="All fields are required.")
    if len(req.password) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters.")

    pool = require_db()
    password_hash = hash_password(req.password)
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO users (username, password_hash, team_name, reporting_manager)
                VALUES ($1, $2, $3, $4)
                """,
                username, password_hash, team_name, reporting_manager,
            )
    except asyncpg.UniqueViolationError:
        raise HTTPException(status_code=409, detail="That username is already taken.")

    log.info("auth: user registered (username=%s)", username)
    return {"message": "User registered successfully.", "username": username}


@app.post("/api/v1/auth/login", dependencies=[Depends(rate_limit)])
async def login(req: LoginRequest):
    username = req.username.strip()
    pool = require_db()
    async with pool.acquire() as conn:
        user = await conn.fetchrow(
            "SELECT username, password_hash, team_name, reporting_manager FROM users WHERE username = $1",
            username,
        )
    if not user or not verify_password(req.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid username or password.")

    token = create_access_token(username)
    log.info("auth: login success (username=%s)", username)
    return {
        "token": token,
        "username": user["username"],
        "team_name": user["team_name"],
        "reporting_manager": user["reporting_manager"],
    }


# Ingestion routes
@app.post("/api/v1/ingest", dependencies=[Depends(rate_limit), Depends(require_auth)])
async def ingest_documents(request: Request):
    body = await request.json()
    return await proxy("ingestion", "/ingest", "POST", body)


@app.get("/api/v1/ingest/status/{job_id}", dependencies=[Depends(rate_limit), Depends(require_auth)])
async def ingestion_status(job_id: str):
    return await proxy("ingestion", f"/status/{job_id}", "GET")


# Query routes
@app.post("/api/v1/query", dependencies=[Depends(rate_limit), Depends(require_auth)])
async def query(request: Request):
    body = await request.json()
    return await proxy("query", "/query", "POST", body)


# Timeline routes
@app.get("/api/v1/timeline/{topic}", dependencies=[Depends(rate_limit), Depends(require_auth)])
async def get_timeline(topic: str):
    return await proxy("timeline", f"/timeline/{topic}", "GET")


# Graph routes
@app.get("/api/v1/graph/decisions", dependencies=[Depends(rate_limit), Depends(require_auth)])
async def get_decisions(project: str | None = None):
    return await proxy("graph", "/decisions", "GET", params={"project": project})


@app.get("/api/v1/graph/entities/{entity}", dependencies=[Depends(rate_limit), Depends(require_auth)])
async def get_entity(entity: str):
    return await proxy("graph", f"/entities/{entity}", "GET")


### Scalar API Documentation
@app.get("/scalar", include_in_schema=False)
def get_scalar_docs():
    if get_scalar_api_reference is None:
        raise HTTPException(status_code=503, detail="Scalar docs unavailable; use /docs instead")
    return get_scalar_api_reference(
        openapi_url=app.openapi_url,
        title="Scalar API",
    )