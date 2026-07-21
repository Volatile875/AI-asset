"""
services/graph-service/app/main.py
Graph Service for DecisionDNA.
"""

import os
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException
from neo4j import AsyncGraphDatabase, AsyncDriver
from neo4j.exceptions import Neo4jError
from pydantic import BaseModel

import logging
import time
import uuid

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s [graph-service] %(message)s",
)
log = logging.getLogger("graph-service")

app = FastAPI(title="Graph Service", version="1.0.0")


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

NEO4J_URI = os.getenv("NEO4J_URI", "bolt://neo4j:7687")
NEO4J_USERNAME = os.getenv("NEO4J_USERNAME", "neo4j")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "password123")
NEO4J_DATABASE = os.getenv("NEO4J_DATABASE", "neo4j")

driver: Optional[AsyncDriver] = None


class BuildGraphRequest(BaseModel):
    documents: List[Dict[str, Any]]


class CypherQuery(BaseModel):
    query: str
    params: Optional[Dict[str, Any]] = None


@app.on_event("startup")
async def startup():
    global driver
    log.info("startup: connecting to Neo4j at %s (user=%s)", NEO4J_URI, NEO4J_USERNAME)
    driver = AsyncGraphDatabase.driver(
        NEO4J_URI,
        auth=(NEO4J_USERNAME, NEO4J_PASSWORD),
    )
    try:
        await driver.verify_connectivity()
        log.info("startup: Neo4j connectivity OK")
    except Exception as exc:
        # NOTE: this raises and CRASHES the container if Neo4j is unreachable — unlike
        # the other services which self-heal. If graph-service keeps exiting, this is why.
        log.error("startup: cannot connect to Neo4j at %s: %r", NEO4J_URI, exc)
        raise RuntimeError(f"Unable to connect to Neo4j at {NEO4J_URI}: {exc}") from exc


@app.on_event("shutdown")
async def shutdown():
    global driver
    if driver:
        await driver.close()


def normalize_participants(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [p.strip() for p in value.split(",") if p.strip()]
    if isinstance(value, list):
        return [str(p).strip() for p in value if str(p).strip()]
    return [str(value).strip()]


async def run_cypher(query: str, params: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    if driver is None:
        raise RuntimeError("Neo4j driver is not initialized")

    async with driver.session(database=NEO4J_DATABASE) as session:
        result = await session.run(query, params or {})
        records = []
        async for record in result:
            records.append(record)

    rows = []
    for record in records:
        row = {}
        for key, value in record.items():
            if hasattr(value, "items"):
                try:
                    row[key] = dict(value)
                except Exception:
                    row[key] = str(value)
            else:
                row[key] = value
        rows.append(row)
    return rows


@app.get("/health")
async def health():
    return {
        "service": "graph-service",
        "status": "healthy",
        "ready": driver is not None,
        "neo4j_uri": NEO4J_URI,
    }


@app.post("/build-graph")
async def build_graph(request: BuildGraphRequest):
    query = """
        MERGE (d:Document {id: })
        SET d += 
        WITH d,  AS project,  AS participants
        FOREACH (_ IN CASE WHEN project IS NULL THEN [] ELSE [1] END |
            MERGE (p:Project {name: project})
            MERGE (d)-[:PART_OF]->(p)
        )
        FOREACH (person IN participants |
            MERGE (person_node:Person {name: person})
            MERGE (person_node)-[:INVOLVED_IN]->(d)
        )
    """

    async with driver.session(database=NEO4J_DATABASE) as session:
        for document in request.documents:
            props = {
                k: document.get(k)
                for k in ["title", "date", "project", "content", "doc_type", "tags"]
                if document.get(k) is not None
            }
            participants = normalize_participants(document.get("participants") or document.get("attendees") or document.get("to"))
            await session.execute_write(
                lambda tx, doc_id=document.get("doc_id"), props=props, project=document.get("project"), participants=participants: tx.run(
                    query,
                    doc_id=doc_id,
                    props=props,
                    project=project,
                    participants=participants,
                )
            )

    return {"status": "ok", "documents_indexed": len(request.documents)}


@app.get("/decisions")
async def get_decisions(project: Optional[str] = None):
    query = """
        MATCH (d:Document)
        WHERE toLower(coalesce(d.doc_type, '')) = 'decision'
          AND ( IS NULL OR d.project = )
        RETURN d
        ORDER BY d.date
    """
    result = await run_cypher(query, {"project": project})
    return {"decisions": [row.get("d") for row in result]}


@app.get("/entities/{name}")
async def get_entity_connections(name: str):
    query = """
        MATCH (entity)-[r]-(other)
        WHERE toLower(coalesce(entity.name, '')) = toLower()
        RETURN labels(entity) AS entity_labels,
               entity.name AS entity_name,
               type(r) AS relationship,
               labels(other) AS other_labels,
               other AS other
    """
    result = await run_cypher(query, {"name": name})
    connections = []
    for row in result:
        connections.append({
            "entity": {"labels": row.get("entity_labels"), "name": row.get("entity_name")},
            "relationship": row.get("relationship"),
            "other": row.get("other"),
        })
    return {"connections": connections}


@app.get("/project-timeline/{project}")
async def project_timeline(project: str):
    query = """
        MATCH (d:Document)
        WHERE d.project = 
        RETURN d.doc_type AS type,
               d.title AS title,
               d.date AS date,
               d.content AS content,
               d.project AS project
        ORDER BY d.date
    """
    result = await run_cypher(query, {"project": project})
    return {"events": result}


@app.post("/query")
async def raw_query(request: CypherQuery):
    try:
        result = await run_cypher(request.query, request.params or {})
        return {"results": result}
    except Neo4jError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
