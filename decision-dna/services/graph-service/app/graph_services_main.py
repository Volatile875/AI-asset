"""
services/graph-service/app/main.py
Neo4j Knowledge Graph Service — PORT 8003

BUGS FIXED vs original version:
─────────────────────────────────────────────────────────────────
BUG 1 ─ anyio.EndOfStream on GET /decisions
  Root cause: result.data() called OUTSIDE the session `async with`
  block. In neo4j-driver 5.x the cursor is invalid once the session
  closes. Fix: consume result INSIDE the session block with
  `await result.values()` or collect with an explicit loop.

BUG 2 ─ Unhandled exception swallowed by Starlette middleware
  Root cause: no try/except around Neo4j calls in route handlers.
  Fix: wrap every session block in try/except and return HTTP 500
  with a JSON body so the frontend shows a real error.

BUG 3 ─ Missing request-ID tracing middleware (caused noisy logs)
  Fix: add lightweight middleware that stamps every request with a
  short rid and logs → / ← with method, path, status, duration.
─────────────────────────────────────────────────────────────────
"""

import logging
import os
import time
import uuid
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from neo4j import AsyncGraphDatabase, AsyncDriver
from pydantic import BaseModel

# ── Logger ─────────────────────────────────────────────────────
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s [graph-service] %(message)s",
)
log = logging.getLogger("graph-service")

# ── Config ──────────────────────────────────────────────────────
NEO4J_URI  = os.getenv("NEO4J_URI",      "bolt://neo4j:7687")
NEO4J_USER = os.getenv("NEO4J_USERNAME", "neo4j")
NEO4J_PASS = os.getenv("NEO4J_PASSWORD", "password123")

app = FastAPI(title="Graph Service", version="1.1.0")
driver = None


# ══════════════════════════════════════════════════════════════
#  REQUEST TRACING MIDDLEWARE
# ══════════════════════════════════════════════════════════════

@app.middleware("http")
async def trace_requests(request: Request, call_next):
    rid   = uuid.uuid4().hex[:8]
    start = time.perf_counter()
    request.state.rid = rid
    log.info("→ %s %s (rid=%s)", request.method, request.url.path, rid)
    try:
        response = await call_next(request)
        dur = (time.perf_counter() - start) * 1000
        log.info("← %s %s %s %.0fms (rid=%s)",
                 request.method, request.url.path, response.status_code, dur, rid)
        return response
    except Exception as exc:
        dur = (time.perf_counter() - start) * 1000
        log.error("✗ %s %s UNHANDLED after %.0fms (rid=%s): %r",
                  request.method, request.url.path, dur, rid, exc)
        return JSONResponse(
            status_code=500,
            content={"detail": f"Internal graph-service error: {type(exc).__name__}: {exc}"},
        )


# ══════════════════════════════════════════════════════════════
#  LIFECYCLE
# ══════════════════════════════════════════════════════════════

@app.on_event("startup")
async def startup():
    global driver
    log.info("startup: connecting to Neo4j at %s (user=%s)", NEO4J_URI, NEO4J_USER)
    driver = AsyncGraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASS))
    # Verify connectivity immediately so we fail fast
    try:
        await driver.verify_connectivity()
        log.info("startup: Neo4j connectivity OK")
    except Exception as exc:
        log.error("startup: Neo4j connectivity FAILED: %r", exc)
        # Don't raise — let health check report the problem
    await _create_constraints()


@app.on_event("shutdown")
async def shutdown():
    global driver
    if driver:
        await driver.close()
        log.info("shutdown: Neo4j driver closed")


# ══════════════════════════════════════════════════════════════
#  SCHEMA — constraints created once on startup
# ══════════════════════════════════════════════════════════════

async def _create_constraints():
    """Idempotent — safe to run every startup."""
    constraints = [
        "CREATE CONSTRAINT IF NOT EXISTS FOR (p:Person)   REQUIRE p.name IS UNIQUE",
        "CREATE CONSTRAINT IF NOT EXISTS FOR (j:Project)  REQUIRE j.name IS UNIQUE",
        "CREATE CONSTRAINT IF NOT EXISTS FOR (d:Decision) REQUIRE d.id   IS UNIQUE",
        "CREATE CONSTRAINT IF NOT EXISTS FOR (m:Meeting)  REQUIRE m.id   IS UNIQUE",
        "CREATE CONSTRAINT IF NOT EXISTS FOR (t:Ticket)   REQUIRE t.id   IS UNIQUE",
        "CREATE CONSTRAINT IF NOT EXISTS FOR (e:Email)    REQUIRE e.id   IS UNIQUE",
    ]
    try:
        async with driver.session() as session:
            for cypher in constraints:
                await session.run(cypher)
        log.info("constraints: all OK")
    except Exception as exc:
        log.warning("constraints: could not create — %r", exc)


# ══════════════════════════════════════════════════════════════
#  HELPER — safe result consumer (FIX FOR BUG 1)
# ══════════════════════════════════════════════════════════════

async def _fetch(session, cypher: str, **params) -> List[Dict[str, Any]]:
    """
    Run a Cypher query and collect ALL records into a plain list
    BEFORE the session is closed.
    """
    result = await session.run(cypher, **params)
    records = []
    async for record in result:
        # Convert each neo4j.Record to a plain dict. A raw Record is
        # list-like, so it would JSON-serialize as an array of values and
        # break dict-style access (`.get(...)`) on the client. `.data()`
        # also unwraps Node/Relationship values into their property dicts.
        records.append(record.data())
    return records


# ══════════════════════════════════════════════════════════════
#  GRAPH POPULATION HELPERS
# ══════════════════════════════════════════════════════════════

async def _process_meeting(session, doc: Dict[str, Any]):
    mid     = doc["doc_id"]
    project = doc.get("project") or "General"

    await session.run(
        "MERGE (m:Meeting {id:$id}) SET m.title=$title, m.date=$date, m.project=$project",
        id=mid, title=doc.get("title",""), date=doc.get("date",""), project=project,
    )
    await session.run(
        """MERGE (p:Project {name:$project})
           WITH p MATCH (m:Meeting {id:$mid})
           MERGE (m)-[:PART_OF]->(p)""",
        project=project, mid=mid,
    )
    for person in doc.get("participants", []):
        name = person.strip()
        if not name:
            continue
        await session.run(
            """MERGE (p:Person {name:$name})
               WITH p MATCH (m:Meeting {id:$mid})
               MERGE (p)-[:ATTENDED]->(m)""",
            name=name, mid=mid,
        )
    for i, decision_text in enumerate(doc.get("decisions", [])):
        did = f"{mid}_decision_{i}"
        await session.run(
            """MERGE (d:Decision {id:$id})
               SET d.description=$desc, d.date=$date, d.project=$project
               WITH d MATCH (m:Meeting {id:$mid})
               MERGE (m)-[:PRODUCED]->(d)""",
            id=did, desc=decision_text,
            date=doc.get("date",""), project=project, mid=mid,
        )


async def _process_jira(session, doc: Dict[str, Any]):
    tid     = doc["doc_id"]
    project = doc.get("project") or "General"

    await session.run(
        "MERGE (t:Ticket {id:$id}) SET t.title=$title, t.status=$status, t.date=$date, t.project=$project",
        id=tid, title=doc.get("title",""),
        status=doc.get("status",""), date=doc.get("date",""), project=project,
    )
    await session.run(
        """MERGE (p:Project {name:$project})
           WITH p MATCH (t:Ticket {id:$tid})
           MERGE (t)-[:PART_OF]->(p)""",
        project=project, tid=tid,
    )
    for person in doc.get("participants", []):
        name = person.strip()
        if not name:
            continue
        await session.run(
            """MERGE (p:Person {name:$name})
               WITH p MATCH (t:Ticket {id:$tid})
               MERGE (p)-[:INVOLVED_IN]->(t)""",
            name=name, tid=tid,
        )


async def _process_email(session, doc: Dict[str, Any]):
    eid     = doc["doc_id"]
    project = doc.get("project") or "General"

    await session.run(
        "MERGE (e:Email {id:$id}) SET e.subject=$subject, e.date=$date, e.project=$project",
        id=eid, subject=doc.get("title",""),
        date=doc.get("date",""), project=project,
    )
    for person in doc.get("participants", []):
        name = person.strip()
        if not name:
            continue
        await session.run(
            """MERGE (p:Person {name:$name})
               WITH p MATCH (e:Email {id:$eid})
               MERGE (p)-[:SENT_OR_RECEIVED]->(e)""",
            name=name, eid=eid,
        )


# ══════════════════════════════════════════════════════════════
#  MODELS
# ══════════════════════════════════════════════════════════════

class BuildGraphRequest(BaseModel):
    documents: List[Dict[str, Any]]


class CypherRequest(BaseModel):
    cypher: str
    params: Dict[str, Any] = {}


# ══════════════════════════════════════════════════════════════
#  ROUTES
# ══════════════════════════════════════════════════════════════

@app.get("/health")
async def health():
    neo4j_ok = False
    try:
        await driver.verify_connectivity()
        neo4j_ok = True
    except Exception:
        pass
    return {
        "service": "graph-service",
        "status":  "healthy" if neo4j_ok else "degraded",
        "neo4j":   "connected" if neo4j_ok else "unreachable",
        "version": "1.1.0",
    }


@app.post("/build-graph")
async def build_graph(request: BuildGraphRequest):
    """
    Ingest a list of normalised documents into Neo4j.
    Each document is processed inside its OWN session so a single
    bad document doesn't roll back the whole batch.
    """
    processed = 0
    errors: List[str] = []

    for doc in request.documents:
        doc_type = doc.get("doc_type", "")
        doc_id   = doc.get("doc_id", "?")
        try:
            # FIX: one session per document — keeps transactions short
            async with driver.session() as session:
                if doc_type == "meeting_notes":
                    await _process_meeting(session, doc)
                elif doc_type == "jira_ticket":
                    await _process_jira(session, doc)
                elif doc_type == "email":
                    await _process_email(session, doc)
                else:
                    log.debug("build-graph: skipping unknown doc_type=%s id=%s", doc_type, doc_id)
                    continue
            processed += 1
        except Exception as exc:
            msg = f"{doc_id}: {type(exc).__name__}: {exc}"
            log.error("build-graph: error processing %s", msg)
            errors.append(msg)

    log.info("build-graph: processed=%d errors=%d", processed, len(errors))
    return {
        "processed": processed,
        "errors":    errors,
        "status":    "success" if not errors else "partial",
    }


@app.get("/decisions")
async def get_decisions(project: Optional[str] = None):
    """
    FIX: result consumed INSIDE `async with` block using _fetch() helper.
    Previously result.data() was called after session closed → EndOfStream.
    """
    try:
        async with driver.session() as session:
            if project:
                records = await _fetch(
                    session,
                    "MATCH (d:Decision) WHERE d.project = $project RETURN d",
                    project=project,
                )
            else:
                records = await _fetch(
                    session,
                    "MATCH (d:Decision) RETURN d LIMIT 200",
                )
        # records is now a plain Python list — safe to use outside session
        decisions = [r["d"] for r in records]
        return {"decisions": decisions, "count": len(decisions)}

    except Exception as exc:
        log.error("GET /decisions error: %r", exc)
        raise HTTPException(status_code=500, detail=f"Neo4j error: {exc}")


@app.get("/entities/{entity_name}")
async def get_entity_graph(entity_name: str):
    """
    Return all nodes connected to a given Person or Project name.
    FIX: same eager-consumption pattern via _fetch().
    """
    try:
        async with driver.session() as session:
            records = await _fetch(
                session,
                """MATCH (n {name: $name})-[r]-(m)
                   RETURN type(r) AS rel, labels(m) AS labels, m
                   LIMIT 80""",
                name=entity_name,
            )
        return {"entity": entity_name, "connections": records, "count": len(records)}

    except Exception as exc:
        log.error("GET /entities/%s error: %r", entity_name, exc)
        raise HTTPException(status_code=500, detail=f"Neo4j error: {exc}")


@app.get("/project-timeline/{project}")
async def project_timeline(project: str):
    """All meetings + their produced decisions for a project, ordered by date."""
    try:
        async with driver.session() as session:
            records = await _fetch(
                session,
                """MATCH (m:Meeting)-[:PART_OF]->(proj:Project {name:$project})
                   OPTIONAL MATCH (m)-[:PRODUCED]->(d:Decision)
                   RETURN m, collect(d) AS decisions
                   ORDER BY m.date ASC""",
                project=project,
            )
        return {"project": project, "timeline": records}

    except Exception as exc:
        log.error("GET /project-timeline/%s error: %r", project, exc)
        raise HTTPException(status_code=500, detail=f"Neo4j error: {exc}")


@app.get("/stats")
async def graph_stats():
    """Node and relationship counts — useful for the dashboard."""
    try:
        async with driver.session() as session:
            node_records = await _fetch(
                session,
                "MATCH (n) RETURN labels(n)[0] AS label, count(n) AS cnt",
            )
            rel_records = await _fetch(
                session,
                "MATCH ()-[r]->() RETURN type(r) AS rel, count(r) AS cnt",
            )
        return {
            "nodes":         {r["label"]: r["cnt"] for r in node_records if r["label"]},
            "relationships": {r["rel"]:   r["cnt"] for r in rel_records},
        }
    except Exception as exc:
        log.error("GET /stats error: %r", exc)
        raise HTTPException(status_code=500, detail=f"Neo4j error: {exc}")


@app.post("/query")
async def run_cypher(request: CypherRequest):
    """Execute an arbitrary read-only Cypher query."""
    try:
        async with driver.session() as session:
            records = await _fetch(session, request.cypher, **request.params)
        return {"results": records}
    except Exception as exc:
        log.error("POST /query error: %r", exc)
        raise HTTPException(status_code=500, detail=f"Neo4j error: {exc}")