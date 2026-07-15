"""
DecisionDNA Jira MCP Server.

Fetches Jira ticket status from Atlassian Jira Cloud and persists status
snapshots plus status transition history in SQLite.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

SERVER_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SERVER_DIR.parent

load_dotenv(PROJECT_DIR / ".env")
load_dotenv(SERVER_DIR / ".env")

mcp = FastMCP("decision-dna-jira")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ValueError(f"Missing required environment variable: {name}")
    if value.startswith("your_") or "your-domain" in value:
        raise ValueError(f"Environment variable {name} still contains a placeholder value")
    return value


def db_path() -> Path:
    configured = os.getenv("JIRA_STATUS_DB_PATH", "").strip()
    if configured:
        path = Path(configured)
        if path.is_absolute():
            return path

        first_part = path.parts[0].lower() if path.parts else ""
        if first_part == "mcp-server":
            return PROJECT_DIR / path

        return SERVER_DIR / path

    return SERVER_DIR / "data" / "jira_status_history.db"


@contextmanager
def connect_db():
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        init_db(conn)
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS ticket_current_status (
            ticket_key TEXT PRIMARY KEY,
            current_status TEXT NOT NULL,
            last_status_changed_at TEXT,
            jira_updated_at TEXT,
            last_observed_at TEXT NOT NULL,
            summary TEXT,
            assignee TEXT
        );

        CREATE TABLE IF NOT EXISTS ticket_status_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticket_key TEXT NOT NULL,
            current_status TEXT NOT NULL,
            jira_updated_at TEXT,
            observed_at TEXT NOT NULL,
            summary TEXT,
            assignee TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_snapshots_ticket_observed
            ON ticket_status_snapshots(ticket_key, observed_at DESC);

        CREATE TABLE IF NOT EXISTS ticket_status_changes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticket_key TEXT NOT NULL,
            from_status TEXT,
            to_status TEXT NOT NULL,
            changed_at TEXT NOT NULL,
            author TEXT,
            jira_history_id TEXT,
            observed_at TEXT NOT NULL,
            UNIQUE(ticket_key, jira_history_id, from_status, to_status, changed_at)
        );

        CREATE INDEX IF NOT EXISTS idx_changes_ticket_changed
            ON ticket_status_changes(ticket_key, changed_at DESC);
        """
    )


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row else None


class JiraClient:
    def __init__(self) -> None:
        self.base_url = get_env("JIRA_BASE_URL").rstrip("/")
        self.email = get_env("JIRA_EMAIL")
        self.api_token = get_env("JIRA_API_TOKEN")

    async def get_issue(self, ticket_key: str) -> dict[str, Any]:
        url = f"{self.base_url}/rest/api/3/issue/{ticket_key}"
        params = {
            "fields": "summary,status,assignee,updated",
        }
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(
                url,
                params=params,
                auth=(self.email, self.api_token),
                headers={"Accept": "application/json"},
            )
            response.raise_for_status()
            return response.json()

    async def get_changelog(self, ticket_key: str) -> list[dict[str, Any]]:
        histories: list[dict[str, Any]] = []
        start_at = 0
        max_results = 100

        async with httpx.AsyncClient(timeout=30) as client:
            while True:
                url = f"{self.base_url}/rest/api/3/issue/{ticket_key}/changelog"
                response = await client.get(
                    url,
                    params={"startAt": start_at, "maxResults": max_results},
                    auth=(self.email, self.api_token),
                    headers={"Accept": "application/json"},
                )
                response.raise_for_status()
                payload = response.json()
                values = payload.get("values", [])
                histories.extend(values)

                total = int(payload.get("total", len(histories)))
                start_at += len(values)
                if not values or start_at >= total:
                    break

        return histories


def extract_issue_status(issue: dict[str, Any]) -> dict[str, Any]:
    fields = issue.get("fields", {})
    status = fields.get("status") or {}
    assignee = fields.get("assignee") or {}

    return {
        "ticket_key": issue.get("key"),
        "current_status": status.get("name"),
        "jira_updated_at": fields.get("updated"),
        "summary": fields.get("summary"),
        "assignee": assignee.get("displayName"),
    }


def extract_status_changes(
    ticket_key: str,
    histories: list[dict[str, Any]],
    observed_at: str,
) -> list[dict[str, Any]]:
    changes: list[dict[str, Any]] = []

    for history in histories:
        for item in history.get("items", []):
            if item.get("field") != "status":
                continue

            author = history.get("author") or {}
            changes.append(
                {
                    "ticket_key": ticket_key,
                    "from_status": item.get("fromString"),
                    "to_status": item.get("toString"),
                    "changed_at": history.get("created"),
                    "author": author.get("displayName"),
                    "jira_history_id": str(history.get("id", "")),
                    "observed_at": observed_at,
                }
            )

    return sorted(changes, key=lambda change: change["changed_at"] or "")


def store_ticket_status(
    issue_status: dict[str, Any],
    status_changes: list[dict[str, Any]],
    observed_at: str,
) -> dict[str, Any]:
    ticket_key = issue_status["ticket_key"]
    current_status = issue_status["current_status"]
    if not ticket_key or not current_status:
        raise ValueError("Jira issue response did not include ticket key or current status")

    last_status_changed_at = None
    for change in reversed(status_changes):
        if change["to_status"] == current_status:
            last_status_changed_at = change["changed_at"]
            break

    with connect_db() as conn:
        conn.execute(
            """
            INSERT INTO ticket_status_snapshots (
                ticket_key, current_status, jira_updated_at, observed_at, summary, assignee
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                ticket_key,
                current_status,
                issue_status.get("jira_updated_at"),
                observed_at,
                issue_status.get("summary"),
                issue_status.get("assignee"),
            ),
        )

        for change in status_changes:
            conn.execute(
                """
                INSERT OR IGNORE INTO ticket_status_changes (
                    ticket_key, from_status, to_status, changed_at, author,
                    jira_history_id, observed_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    change["ticket_key"],
                    change["from_status"],
                    change["to_status"],
                    change["changed_at"],
                    change["author"],
                    change["jira_history_id"],
                    change["observed_at"],
                ),
            )

        conn.execute(
            """
            INSERT INTO ticket_current_status (
                ticket_key, current_status, last_status_changed_at, jira_updated_at,
                last_observed_at, summary, assignee
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(ticket_key) DO UPDATE SET
                current_status = excluded.current_status,
                last_status_changed_at = excluded.last_status_changed_at,
                jira_updated_at = excluded.jira_updated_at,
                last_observed_at = excluded.last_observed_at,
                summary = excluded.summary,
                assignee = excluded.assignee
            """,
            (
                ticket_key,
                current_status,
                last_status_changed_at,
                issue_status.get("jira_updated_at"),
                observed_at,
                issue_status.get("summary"),
                issue_status.get("assignee"),
            ),
        )

    return {
        **issue_status,
        "last_status_changed_at": last_status_changed_at,
        "observed_at": observed_at,
        "stored_status_change_count": len(status_changes),
        "database_path": str(db_path()),
    }


@mcp.tool()
async def fetch_jira_ticket_status(ticket_key: str) -> dict[str, Any]:
    """
    Fetch a Jira ticket's current status from Atlassian and store status history.

    Args:
        ticket_key: Jira issue key, for example PROJ-123.
    """
    normalized_key = ticket_key.strip().upper()
    if not normalized_key:
        raise ValueError("ticket_key is required")

    observed_at = utc_now()
    client = JiraClient()
    issue = await client.get_issue(normalized_key)
    changelog = await client.get_changelog(normalized_key)

    issue_status = extract_issue_status(issue)
    changes = extract_status_changes(normalized_key, changelog, observed_at)
    return store_ticket_status(issue_status, changes, observed_at)


@mcp.tool()
async def fetch_many_jira_ticket_statuses(ticket_keys: list[str]) -> dict[str, Any]:
    """
    Fetch and store current status for multiple Jira tickets.

    Args:
        ticket_keys: List of Jira issue keys, for example ["PROJ-123", "PROJ-124"].
    """
    results = []
    errors = []

    for ticket_key in ticket_keys:
        try:
            results.append(await fetch_jira_ticket_status(ticket_key))
        except Exception as exc:
            errors.append({"ticket_key": ticket_key, "error": str(exc)})

    return {"results": results, "errors": errors}


@mcp.tool()
def get_stored_jira_ticket_status(ticket_key: str) -> dict[str, Any]:
    """
    Return the latest locally stored status for a Jira ticket.

    Args:
        ticket_key: Jira issue key, for example PROJ-123.
    """
    normalized_key = ticket_key.strip().upper()
    with connect_db() as conn:
        row = conn.execute(
            """
            SELECT *
            FROM ticket_current_status
            WHERE ticket_key = ?
            """,
            (normalized_key,),
        ).fetchone()

    return {
        "ticket_key": normalized_key,
        "current": row_to_dict(row),
        "database_path": str(db_path()),
    }


@mcp.tool()
def get_jira_ticket_status_history(ticket_key: str, limit: int = 50) -> dict[str, Any]:
    """
    Return locally stored status snapshots and transitions for a Jira ticket.

    Args:
        ticket_key: Jira issue key, for example PROJ-123.
        limit: Maximum number of snapshots and transitions to return.
    """
    normalized_key = ticket_key.strip().upper()
    bounded_limit = max(1, min(limit, 500))

    with connect_db() as conn:
        snapshots = conn.execute(
            """
            SELECT *
            FROM ticket_status_snapshots
            WHERE ticket_key = ?
            ORDER BY observed_at DESC
            LIMIT ?
            """,
            (normalized_key, bounded_limit),
        ).fetchall()

        changes = conn.execute(
            """
            SELECT *
            FROM ticket_status_changes
            WHERE ticket_key = ?
            ORDER BY changed_at DESC
            LIMIT ?
            """,
            (normalized_key, bounded_limit),
        ).fetchall()

    return {
        "ticket_key": normalized_key,
        "snapshots": [dict(row) for row in snapshots],
        "status_changes": [dict(row) for row in changes],
        "database_path": str(db_path()),
    }


if __name__ == "__main__":
    mcp.run()
