"""
api-gateway/app/main.py
Central entry point — routes requests to microservices,
handles auth, rate limiting, and CORS.
"""

import os
import time
import uuid
import logging
import httpx
from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
import redis.asyncio as redis
import psycopg2
import bcrypt
import jwt
from datetime import datetime, timedelta
from pydantic import BaseModel

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

# ── PostgreSQL Authentication Config ──────────────────────────
POSTGRES_HOST = os.getenv("POSTGRES_HOST", "host.docker.internal")
POSTGRES_PORT = int(os.getenv("POSTGRES_PORT", "5432"))
POSTGRES_USER = os.getenv("POSTGRES_USER", "postgres")
POSTGRES_PASSWORD = os.getenv("POSTGRES_PASSWORD", "postgres")
POSTGRES_DB = os.getenv("POSTGRES_DB", "asset")
SECRET_KEY = os.getenv("SECRET_KEY", "your_secret_key_change_in_production")

def get_db_conn():
    try:
        return psycopg2.connect(
            host=POSTGRES_HOST,
            port=POSTGRES_PORT,
            user=POSTGRES_USER,
            password=POSTGRES_PASSWORD,
            database=POSTGRES_DB
        )
    except psycopg2.OperationalError as e:
        if "database" in str(e) and "does not exist" in str(e):
            log.warning("Database '%s' not found. Falling back to default 'postgres' database.", POSTGRES_DB)
            return psycopg2.connect(
                host=POSTGRES_HOST,
                port=POSTGRES_PORT,
                user=POSTGRES_USER,
                password=POSTGRES_PASSWORD,
                database="postgres"
            )
        raise

def init_postgres():
    log.info("Initializing PostgreSQL schema at host=%s db=%s...", POSTGRES_HOST, POSTGRES_DB)
    conn = None
    try:
        conn = get_db_conn()
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    username VARCHAR(255) PRIMARY KEY,
                    team_name VARCHAR(255) NOT NULL,
                    reporting_manager VARCHAR(255) NOT NULL,
                    password_hash VARCHAR(255) NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS jwt_tokens (
                    token TEXT PRIMARY KEY,
                    username VARCHAR(255) NOT NULL,
                    expires_at TIMESTAMP NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (username) REFERENCES users(username) ON DELETE CASCADE
                );
            """)
            conn.commit()
        log.info("PostgreSQL schema successfully initialized.")
    except Exception as e:
        log.error("Failed to initialize PostgreSQL: %s. Running with local signature verification.", e)
    finally:
        if conn:
            conn.close()

async def verify_jwt(request: Request):
    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid authentication token")
    token = auth_header.split(" ")[1]
    
    try:
        # Check in PostgreSQL if token is active
        conn = get_db_conn()
        with conn.cursor() as cur:
            cur.execute("SELECT username, expires_at FROM jwt_tokens WHERE token = %s", (token,))
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=401, detail="Token revoked or invalid")
            username, expires_at = row
            if expires_at < datetime.utcnow():
                raise HTTPException(status_code=401, detail="Token has expired")
        conn.close()
        
        # Verify JWT payload
        payload = jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
        return payload
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token signature expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")
    except Exception as e:
        log.warning("PostgreSQL verification query failed (falling back to stateless verify): %s", e)
        try:
            payload = jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
            return payload
        except Exception:
            raise HTTPException(status_code=401, detail="Authentication failed")

class SignupPayload(BaseModel):
    username: str
    team_name: str
    reporting_manager: str
    password: str

class LoginPayload(BaseModel):
    username: str
    password: str


@app.on_event("startup")
async def startup():
    global redis_client
    redis_client = redis.from_url(REDIS_URL, decode_responses=True)
    init_postgres()


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


# Authentication endpoints
@app.post("/api/v1/auth/signup")
async def signup(payload: SignupPayload):
    username = payload.username.strip()
    team_name = payload.team_name.strip()
    reporting_manager = payload.reporting_manager.strip()
    password = payload.password
    
    if not username or not team_name or not reporting_manager or not password:
        raise HTTPException(status_code=400, detail="All fields are required")
        
    password_hash = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
    
    try:
        conn = get_db_conn()
        with conn.cursor() as cur:
            # Check if user exists
            cur.execute("SELECT username FROM users WHERE username = %s", (username,))
            if cur.fetchone():
                raise HTTPException(status_code=400, detail="Username already exists")
            
            # Insert user
            cur.execute(
                "INSERT INTO users (username, team_name, reporting_manager, password_hash) VALUES (%s, %s, %s, %s)",
                (username, team_name, reporting_manager, password_hash)
            )
            conn.commit()
        conn.close()
        return {"status": "success", "message": "User registered successfully"}
    except HTTPException:
        raise
    except Exception as e:
        log.error("Signup DB error: %s", e)
        raise HTTPException(status_code=500, detail=f"Database error during signup: {e}")

@app.post("/api/v1/auth/login")
async def login(payload: LoginPayload):
    username = payload.username.strip()
    password = payload.password
    
    try:
        conn = get_db_conn()
        with conn.cursor() as cur:
            cur.execute("SELECT password_hash, team_name, reporting_manager FROM users WHERE username = %s", (username,))
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=401, detail="Invalid username or password")
            password_hash, team_name, reporting_manager = row
            
            if not bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8")):
                raise HTTPException(status_code=401, detail="Invalid username or password")
                
            # Create JWT token
            expires_at = datetime.utcnow() + timedelta(hours=24)
            token_payload = {
                "sub": username,
                "team_name": team_name,
                "reporting_manager": reporting_manager,
                "exp": expires_at
            }
            token = jwt.encode(token_payload, SECRET_KEY, algorithm="HS256")
            
            # Store token in DB
            cur.execute(
                "INSERT INTO jwt_tokens (token, username, expires_at) VALUES (%s, %s, %s)",
                (token, username, expires_at)
            )
            conn.commit()
        conn.close()
        return {
            "status": "success",
            "token": token,
            "username": username,
            "team_name": team_name,
            "reporting_manager": reporting_manager
        }
    except HTTPException:
        raise
    except Exception as e:
        log.error("Login DB error: %s", e)
        raise HTTPException(status_code=500, detail=f"Database error during login: {e}")


# Ingestion routes
@app.post("/api/v1/ingest", dependencies=[Depends(rate_limit), Depends(verify_jwt)])
async def ingest_documents(request: Request):
    body = await request.json()
    return await proxy("ingestion", "/ingest", "POST", body)


@app.get("/api/v1/ingest/status/{job_id}", dependencies=[Depends(rate_limit), Depends(verify_jwt)])
async def ingestion_status(job_id: str):
    return await proxy("ingestion", f"/status/{job_id}", "GET")


# Query routes
@app.post("/api/v1/query", dependencies=[Depends(rate_limit), Depends(verify_jwt)])
async def query(request: Request):
    body = await request.json()
    return await proxy("query", "/query", "POST", body)


# Timeline routes
@app.get("/api/v1/timeline/{topic}", dependencies=[Depends(rate_limit), Depends(verify_jwt)])
async def get_timeline(topic: str):
    return await proxy("timeline", f"/timeline/{topic}", "GET")


# Graph routes
@app.get("/api/v1/graph/decisions", dependencies=[Depends(rate_limit), Depends(verify_jwt)])
async def get_decisions(project: str = None):
    return await proxy("graph", "/decisions", "GET", params={"project": project})


@app.get("/api/v1/graph/entities/{entity}", dependencies=[Depends(rate_limit), Depends(verify_jwt)])
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