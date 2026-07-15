"""
Seed Neo4j from the local synthetic DecisionDNA data.

Usage:
  python scripts/seed_neo4j.py
  python scripts/seed_neo4j.py --clear
  python scripts/seed_neo4j.py --dump-cypher data/decisiondna_seed.cypher
"""

import argparse
import json
import os
import sys
from urllib.parse import urlparse
from pathlib import Path
from typing import Any, Dict, Iterable, List


ROOT = Path(__file__).resolve().parents[1]
PARSERS_DIR = ROOT / "services" / "ingestion-service"
sys.path.insert(0, str(PARSERS_DIR))

from app.parsers.email_parser import parse_emails  # noqa: E402
from app.parsers.jira_parser import parse_jira_tickets  # noqa: E402
from app.parsers.meeting_parser import parse_meeting_notes  # noqa: E402


CONSTRAINTS = [
    "CREATE CONSTRAINT person_name IF NOT EXISTS FOR (p:Person) REQUIRE p.name IS UNIQUE",
    "CREATE CONSTRAINT project_name IF NOT EXISTS FOR (p:Project) REQUIRE p.name IS UNIQUE",
    "CREATE CONSTRAINT decision_id IF NOT EXISTS FOR (d:Decision) REQUIRE d.id IS UNIQUE",
    "CREATE CONSTRAINT meeting_id IF NOT EXISTS FOR (m:Meeting) REQUIRE m.id IS UNIQUE",
    "CREATE CONSTRAINT ticket_id IF NOT EXISTS FOR (t:Ticket) REQUIRE t.id IS UNIQUE",
    "CREATE CONSTRAINT email_id IF NOT EXISTS FOR (e:Email) REQUIRE e.id IS UNIQUE",
]


def load_env(env_path: Path, override: bool = True) -> None:
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip().strip('"').strip("'")
        key = key.strip()
        if override or key not in os.environ:
            os.environ[key] = value


def load_documents(data_dir: Path) -> List[Dict[str, Any]]:
    documents: List[Dict[str, Any]] = []
    documents.extend(parse_emails(str(data_dir / "emails")))
    documents.extend(parse_meeting_notes(str(data_dir / "meetings")))
    documents.extend(parse_jira_tickets(str(data_dir / "jira")))
    return documents


def run_query(tx, query: str, **params: Any) -> None:
    tx.run(query, **params).consume()


def create_constraints(session) -> None:
    for constraint in CONSTRAINTS:
        session.execute_write(run_query, constraint)


def clear_graph(session) -> None:
    session.execute_write(
        run_query,
        """
        MATCH (n)
        WHERE n:Person OR n:Project OR n:Decision OR n:Meeting OR n:Ticket OR n:Email
        DETACH DELETE n
        """,
    )


def seed_email(session, doc: Dict[str, Any]) -> None:
    session.execute_write(
        run_query,
        """
        MERGE (e:Email {id: $id})
        SET e.subject = $title,
            e.date = $date,
            e.project = $project,
            e.source_path = $source_path
        MERGE (proj:Project {name: $project})
        MERGE (e)-[:PART_OF]->(proj)
        """,
        id=doc["doc_id"],
        title=doc.get("title", ""),
        date=doc.get("date", ""),
        project=doc.get("project") or "General",
        source_path=doc.get("source_path", ""),
    )
    for person in doc.get("participants", []):
        if person:
            session.execute_write(
                run_query,
                """
                MERGE (p:Person {name: $name})
                WITH p
                MATCH (e:Email {id: $email_id})
                MERGE (p)-[:SENT_OR_RECEIVED]->(e)
                """,
                name=person.strip(),
                email_id=doc["doc_id"],
            )


def seed_meeting(session, doc: Dict[str, Any]) -> None:
    project = doc.get("project") or "General"
    session.execute_write(
        run_query,
        """
        MERGE (m:Meeting {id: $id})
        SET m.title = $title,
            m.date = $date,
            m.project = $project,
            m.source_path = $source_path
        MERGE (proj:Project {name: $project})
        MERGE (m)-[:PART_OF]->(proj)
        """,
        id=doc["doc_id"],
        title=doc.get("title", ""),
        date=doc.get("date", ""),
        project=project,
        source_path=doc.get("source_path", ""),
    )
    for person in doc.get("participants", []):
        if person:
            session.execute_write(
                run_query,
                """
                MERGE (p:Person {name: $name})
                WITH p
                MATCH (m:Meeting {id: $meeting_id})
                MERGE (p)-[:ATTENDED]->(m)
                """,
                name=person.strip(),
                meeting_id=doc["doc_id"],
            )

    for index, decision in enumerate(doc.get("decisions", [])):
        decision_id = f"{doc['doc_id']}_decision_{index}"
        session.execute_write(
            run_query,
            """
            MERGE (d:Decision {id: $id})
            SET d.description = $description,
                d.date = $date,
                d.project = $project
            WITH d
            MATCH (m:Meeting {id: $meeting_id})
            MERGE (m)-[:PRODUCED]->(d)
            """,
            id=decision_id,
            description=decision,
            date=doc.get("date", ""),
            project=project,
            meeting_id=doc["doc_id"],
        )


def seed_ticket(session, doc: Dict[str, Any]) -> None:
    project = doc.get("project") or "General"
    session.execute_write(
        run_query,
        """
        MERGE (t:Ticket {id: $id})
        SET t.title = $title,
            t.status = $status,
            t.priority = $priority,
            t.date = $date,
            t.project = $project,
            t.source_path = $source_path
        MERGE (proj:Project {name: $project})
        MERGE (t)-[:PART_OF]->(proj)
        """,
        id=doc["doc_id"],
        title=doc.get("title", ""),
        status=doc.get("status", ""),
        priority=doc.get("priority", ""),
        date=doc.get("date", ""),
        project=project,
        source_path=doc.get("source_path", ""),
    )
    for person in doc.get("participants", []):
        if person:
            session.execute_write(
                run_query,
                """
                MERGE (p:Person {name: $name})
                WITH p
                MATCH (t:Ticket {id: $ticket_id})
                MERGE (p)-[:INVOLVED_IN]->(t)
                """,
                name=person.strip(),
                ticket_id=doc["doc_id"],
            )


def seed_documents(session, documents: Iterable[Dict[str, Any]]) -> Dict[str, int]:
    counts = {"email": 0, "meeting_notes": 0, "jira_ticket": 0}
    for doc in documents:
        doc_type = doc.get("doc_type", "")
        if doc_type == "email":
            seed_email(session, doc)
        elif doc_type == "meeting_notes":
            seed_meeting(session, doc)
        elif doc_type == "jira_ticket":
            seed_ticket(session, doc)
        counts[doc_type] = counts.get(doc_type, 0) + 1
    return counts


def cypher_value(value: Any) -> str:
    return json.dumps(value)


def get_neo4j_config() -> Dict[str, str]:
    uri = os.getenv("NEO4J_URI", "").strip()
    username = os.getenv("NEO4J_USERNAME", "").strip()
    password = os.getenv("NEO4J_PASSWORD", "").strip()
    database = os.getenv("NEO4J_DATABASE", "").strip()

    if not uri:
        host = os.getenv("NEO4J_HOST", "").strip()
        if host:
            uri = f"neo4j+s://{host}"

    missing = [
        name
        for name, value in {
            "NEO4J_URI": uri,
            "NEO4J_USERNAME": username,
            "NEO4J_PASSWORD": password,
        }.items()
        if not value
    ]
    if missing:
        raise SystemExit(f"Missing required Neo4j config in .env: {', '.join(missing)}")

    parsed = urlparse(uri)
    if parsed.scheme not in {"neo4j", "neo4j+s", "bolt", "bolt+s"}:
        raise SystemExit(
            "NEO4J_URI must start with one of: neo4j://, neo4j+s://, bolt://, bolt+s://"
        )
    if not parsed.hostname:
        raise SystemExit("NEO4J_URI must include a hostname, for example neo4j+s://example.databases.neo4j.io")

    return {
        "uri": uri,
        "username": username,
        "password": password,
        "database": database,
    }


def format_connection_error(error: Exception, uri: str) -> str:
    parsed = urlparse(uri)
    host = parsed.hostname or uri
    message = str(error)

    if "Cannot resolve address" in message or "getaddrinfo failed" in message:
        return (
            f"Could not resolve Neo4j host '{host}'. Copy the exact connection URI "
            "from Neo4j Aura > Connect > Drivers and put it in NEO4J_URI."
        )
    if "authentication" in message.lower() or "unauthorized" in message.lower():
        return "Neo4j rejected the username/password. Update NEO4J_USERNAME and NEO4J_PASSWORD in .env."
    return f"Could not connect to Neo4j at {uri}: {error}"


def write_cypher_dump(path: Path, documents: List[Dict[str, Any]]) -> None:
    lines = [
        "// DecisionDNA Neo4j seed dump",
        *[f"{constraint};" for constraint in CONSTRAINTS],
    ]
    for doc in documents:
        doc_type = doc.get("doc_type", "")
        payload = {
            "id": doc["doc_id"],
            "doc_type": doc_type,
            "title": doc.get("title", ""),
            "date": doc.get("date", ""),
            "project": doc.get("project") or "General",
            "participants": doc.get("participants", []),
            "decisions": doc.get("decisions", []),
            "status": doc.get("status", ""),
            "priority": doc.get("priority", ""),
            "source_path": doc.get("source_path", ""),
        }
        lines.append(f"// {payload['doc_type']} {payload['id']}")
        lines.append(f":param doc => {cypher_value(payload)};")
        if doc_type == "email":
            lines.append(
                """
WITH $doc AS doc
MERGE (e:Email {id: doc.id})
SET e.subject = doc.title, e.date = doc.date, e.project = doc.project, e.source_path = doc.source_path
MERGE (proj:Project {name: doc.project})
MERGE (e)-[:PART_OF]->(proj)
WITH e, doc
UNWIND doc.participants AS person
MERGE (p:Person {name: person})
MERGE (p)-[:SENT_OR_RECEIVED]->(e);
""".strip()
            )
        elif doc_type == "meeting_notes":
            lines.append(
                """
WITH $doc AS doc
MERGE (m:Meeting {id: doc.id})
SET m.title = doc.title, m.date = doc.date, m.project = doc.project, m.source_path = doc.source_path
MERGE (proj:Project {name: doc.project})
MERGE (m)-[:PART_OF]->(proj)
WITH m, doc
UNWIND doc.participants AS person
MERGE (p:Person {name: person})
MERGE (p)-[:ATTENDED]->(m)
WITH m, doc
UNWIND range(0, size(doc.decisions) - 1) AS i
WITH m, doc, i WHERE i >= 0
MERGE (d:Decision {id: doc.id + '_decision_' + toString(i)})
SET d.description = doc.decisions[i], d.date = doc.date, d.project = doc.project
MERGE (m)-[:PRODUCED]->(d);
""".strip()
            )
        elif doc_type == "jira_ticket":
            lines.append(
                """
WITH $doc AS doc
MERGE (t:Ticket {id: doc.id})
SET t.title = doc.title, t.status = doc.status, t.priority = doc.priority,
    t.date = doc.date, t.project = doc.project, t.source_path = doc.source_path
MERGE (proj:Project {name: doc.project})
MERGE (t)-[:PART_OF]->(proj)
WITH t, doc
UNWIND doc.participants AS person
MERGE (p:Person {name: person})
MERGE (p)-[:INVOLVED_IN]->(t);
""".strip()
            )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", default=str(ROOT / "data" / "synthetic"))
    parser.add_argument("--env-file", default=str(ROOT / ".env"))
    parser.add_argument("--clear", action="store_true")
    parser.add_argument("--dump-cypher", help="Write a Cypher seed file instead of only loading Neo4j.")
    parser.add_argument("--skip-load", action="store_true", help="Only write --dump-cypher; do not connect to Neo4j.")
    args = parser.parse_args()

    load_env(Path(args.env_file))
    documents = load_documents(Path(args.data_dir))

    if args.dump_cypher:
        write_cypher_dump(Path(args.dump_cypher), documents)
        print(f"Wrote Cypher dump: {args.dump_cypher}")

    if args.skip_load:
        return

    config = get_neo4j_config()

    try:
        from neo4j import GraphDatabase
    except ImportError as error:
        raise SystemExit("Install the Neo4j Python driver first: pip install neo4j") from error

    driver = GraphDatabase.driver(config["uri"], auth=(config["username"], config["password"]))
    try:
        try:
            driver.verify_connectivity()
        except Exception as error:
            raise SystemExit(format_connection_error(error, config["uri"])) from error

        session_kwargs = {"database": config["database"]} if config["database"] else {}
        with driver.session(**session_kwargs) as session:
            create_constraints(session)
            if args.clear:
                clear_graph(session)
            counts = seed_documents(session, documents)
        print(f"Seeded Neo4j: {sum(counts.values())} documents ({counts})")
    finally:
        driver.close()


if __name__ == "__main__":
    main()
