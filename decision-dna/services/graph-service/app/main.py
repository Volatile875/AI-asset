"""
services/graph-service/app/main.py
Builds a Neo4j knowledge graph from documents.
Nodes: Person, Project, Decision, Meeting, Ticket, Risk
Edges: ATTENDED, MADE_DECISION, FLAGGED_RISK, LINKED_TO, OVERRULED
"""

import os
import re
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException
from neo4j import AsyncGraphDatabase
from pydantic import BaseModel

app = FastAPI(title="Graph Service", version="1.0.0")

NEO4J_URI  = os.getenv("NEO4J_URI", "bolt://neo4j:7687")
NEO4J_USER = os.getenv("NEO4J_USERNAME", "neo4j")
NEO4J_PASS = os.getenv("NEO4J_PASSWORD", "password123")

driver = None


@app.on_event("startup")
async def startup():
    global driver
    driver = AsyncGraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASS))
    await driver.verify_connectivity()
    await create_constraints()


@app.on_event("shutdown")
async def shutdown():
    if driver:
        await driver.close()


# ── Schema Setup ───────────────────────────────────────────────

async def create_constraints():
    async with driver.session() as session:
        constraints = [
            "CREATE CONSTRAINT IF NOT EXISTS FOR (p:Person) REQUIRE p.name IS UNIQUE",
            "CREATE CONSTRAINT IF NOT EXISTS FOR (proj:Project) REQUIRE proj.name IS UNIQUE",
            "CREATE CONSTRAINT IF NOT EXISTS FOR (d:Decision) REQUIRE d.id IS UNIQUE",
            "CREATE CONSTRAINT IF NOT EXISTS FOR (m:Meeting) REQUIRE m.id IS UNIQUE",
            "CREATE CONSTRAINT IF NOT EXISTS FOR (t:Ticket) REQUIRE t.id IS UNIQUE",
        ]
        for constraint in constraints:
            await session.run(constraint)


# ── Models ─────────────────────────────────────────────────────

class BuildGraphRequest(BaseModel):
    documents: List[Dict[str, Any]]


class GraphQueryRequest(BaseModel):
    cypher: str
    params: Dict[str, Any] = {}


# ── Graph Building ─────────────────────────────────────────────

async def process_meeting(session, doc: Dict[str, Any]):
    meeting_id = doc["doc_id"]
    project = doc.get("project") or "General"
    decisions = doc.get("decisions", [])

    # Create Meeting node
    await session.run(
        """
        MERGE (m:Meeting {id: $id})
        SET m.title = $title, m.date = $date, m.project = $project
        """,
        id=meeting_id, title=doc.get("title", ""), date=doc.get("date", ""), project=project,
    )

    # Create Project node and link
    await session.run(
        """
        MERGE (proj:Project {name: $project})
        WITH proj
        MATCH (m:Meeting {id: $meeting_id})
        MERGE (m)-[:PART_OF]->(proj)
        """,
        project=project, meeting_id=meeting_id,
    )

    # Create Person nodes and ATTENDED relationships
    for person in doc.get("participants", []):
        name = person.strip()
        if not name:
            continue
        await session.run(
            """
            MERGE (p:Person {name: $name})
            WITH p
            MATCH (m:Meeting {id: $meeting_id})
            MERGE (p)-[:ATTENDED]->(m)
            """,
            name=name, meeting_id=meeting_id,
        )

    # Create Decision nodes
    for i, decision_text in enumerate(decisions):
        decision_id = f"{meeting_id}_decision_{i}"
        await session.run(
            """
            MERGE (d:Decision {id: $id})
            SET d.description = $desc, d.date = $date, d.project = $project
            WITH d
            MATCH (m:Meeting {id: $meeting_id})
            MERGE (m)-[:PRODUCED]->(d)
            """,
            id=decision_id, desc=decision_text,
            date=doc.get("date", ""), project=project,
            meeting_id=meeting_id,
        )


async def process_jira(session, doc: Dict[str, Any]):
    ticket_id = doc["doc_id"]
    project = doc.get("project") or "General"

    await session.run(
        """
        MERGE (t:Ticket {id: $id})
        SET t.title = $title, t.status = $status,
            t.date = $date, t.project = $project
        """,
        id=ticket_id, title=doc.get("title", ""),
        status=doc.get("status", ""), date=doc.get("date", ""), project=project,
    )

    await session.run(
        """
        MERGE (proj:Project {name: $project})
        WITH proj
        MATCH (t:Ticket {id: $ticket_id})
        MERGE (t)-[:PART_OF]->(proj)
        """,
        project=project, ticket_id=ticket_id,
    )

    for person in doc.get("participants", []):
        name = person.strip()
        if not name:
            continue
        await session.run(
            """
            MERGE (p:Person {name: $name})
            WITH p
            MATCH (t:Ticket {id: $ticket_id})
            MERGE (p)-[:INVOLVED_IN]->(t)
            """,
            name=name, ticket_id=ticket_id,
        )


async def process_email(session, doc: Dict[str, Any]):
    email_id = doc["doc_id"]
    project = doc.get("project") or "General"

    await session.run(
        """
        MERGE (e:Email {id: $id})
        SET e.subject = $subject, e.date = $date, e.project = $project
        """,
        id=email_id, subject=doc.get("title", ""),
        date=doc.get("date", ""), project=project,
    )

    for person in doc.get("participants", []):
        name = person.strip()
        if not name:
            continue
        await session.run(
            """
            MERGE (p:Person {name: $name})
            WITH p
            MATCH (em:Email {id: $email_id})
            MERGE (p)-[:SENT_OR_RECEIVED]->(em)
            """,
            name=name, email_id=email_id,
        )


# ── Routes ─────────────────────────────────────────────────────

@app.get("/health")
async def health():
    try:
        await driver.verify_connectivity()
        return {
            "service": "graph-service",
            "status": "healthy",
            "neo4j_uri": NEO4J_URI,
        }
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Neo4j unavailable: {e}")


@app.post("/build-graph")
async def build_graph(request: BuildGraphRequest):
    processed = 0
    async with driver.session() as session:
        for doc in request.documents:
            doc_type = doc.get("doc_type", "")
            try:
                if doc_type == "meeting_notes":
                    await process_meeting(session, doc)
                elif doc_type == "jira_ticket":
                    await process_jira(session, doc)
                elif doc_type == "email":
                    await process_email(session, doc)
                processed += 1
            except Exception as e:
                print(f"Error processing {doc.get('doc_id')}: {e}")

    return {"processed": processed, "status": "success"}


@app.get("/decisions")
async def get_decisions(project: Optional[str] = None):
    async with driver.session() as session:
        if project:
            result = await session.run(
                "MATCH (d:Decision) WHERE d.project = $project RETURN d",
                project=project,
            )
        else:
            result = await session.run("MATCH (d:Decision) RETURN d LIMIT 100")
        records = await result.data()
    return {"decisions": [r["d"] for r in records]}


@app.get("/entities/{entity_name}")
async def get_entity_graph(entity_name: str):
    """Get all connections for a person or project."""
    async with driver.session() as session:
        result = await session.run(
            """
            MATCH (n {name: $name})-[r]-(m)
            RETURN type(r) as rel, labels(m) as labels, m
            LIMIT 50
            """,
            name=entity_name,
        )
        records = await result.data()
    return {"entity": entity_name, "connections": records}


@app.get("/project-timeline/{project}")
async def project_timeline(project: str):
    """All meetings + decisions for a project ordered by date."""
    async with driver.session() as session:
        result = await session.run(
            """
            MATCH (m:Meeting)-[:PART_OF]->(proj:Project {name: $project})
            OPTIONAL MATCH (m)-[:PRODUCED]->(d:Decision)
            RETURN m, collect(d) as decisions
            ORDER BY m.date ASC
            """,
            project=project,
        )
        records = await result.data()
    return {"project": project, "timeline": records}


@app.post("/query")
async def run_cypher(request: GraphQueryRequest):
    """Run a raw Cypher query (for advanced use)."""
    async with driver.session() as session:
        result = await session.run(request.cypher, **request.params)
        records = await result.data()
    return {"results": records}
