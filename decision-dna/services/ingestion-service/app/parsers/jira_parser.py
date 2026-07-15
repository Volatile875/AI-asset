"""
services/ingestion-service/app/parsers/jira_parser.py
Parses JSON Jira ticket files.
"""

import json
import uuid
from pathlib import Path
from typing import List, Dict, Any
from datetime import datetime


def parse_jira_tickets(jira_dir: str) -> List[Dict[str, Any]]:
    """
    Each ticket JSON:
    {
      "id": "PROJ-101", "title": "...", "description": "...",
      "status": "...", "reporter": "...", "assignee": "...",
      "created": "...", "comments": [...], "project": "...",
      "labels": [...], "priority": "..."
    }
    """
    documents = []
    path = Path(jira_dir)

    for file in path.glob("*.json"):
        with open(file) as f:
            tickets = json.load(f)

        for ticket in tickets:
            comments_text = "\n".join(
                f"[{c.get('author', '?')} on {c.get('date', '?')}]: {c.get('body', '')}"
                for c in ticket.get("comments", [])
            )

            participants: List[str] = list(set(filter(None, [
                ticket.get("reporter"),
                ticket.get("assignee"),
                *[c.get("author") for c in ticket.get("comments", [])],
            ])))

            content = (
                f"Ticket: {ticket.get('id', '')}\n"
                f"Title: {ticket.get('title', '')}\n"
                f"Status: {ticket.get('status', '')}\n"
                f"Priority: {ticket.get('priority', '')}\n"
                f"Reporter: {ticket.get('reporter', '')}\n"
                f"Assignee: {ticket.get('assignee', '')}\n"
                f"Created: {ticket.get('created', '')}\n\n"
                f"Description:\n{ticket.get('description', '')}\n\n"
                f"Comments:\n{comments_text}"
            )

            doc = {
                "doc_id": ticket.get("id", str(uuid.uuid4())),
                "doc_type": "jira_ticket",
                "title": ticket.get("title", "Untitled Ticket"),
                "content": content,
                "date": ticket.get("created", datetime.utcnow().isoformat()),
                "participants": participants,
                "project": ticket.get("project", None),
                "tags": ticket.get("labels", []),
                "status": ticket.get("status"),
                "priority": ticket.get("priority"),
                "source_path": str(file),
                "raw": ticket,
            }
            documents.append(doc)

    return documents