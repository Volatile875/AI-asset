"""
services/ingestion-service/app/parsers/meeting_parser.py
Parses JSON meeting note files.
"""

import json
import uuid
from pathlib import Path
from typing import List, Dict, Any
from datetime import datetime


def parse_meeting_notes(meeting_dir: str) -> List[Dict[str, Any]]:
    """
    Each meeting JSON:
    {
      "title": "...", "date": "...", "attendees": [...],
      "agenda": "...", "discussion": "...", "decisions": [...],
      "action_items": [...], "project": "..."
    }
    """
    documents = []
    path = Path(meeting_dir)

    for file in path.glob("*.json"):
        with open(file) as f:
            meetings = json.load(f)

        for meeting in meetings:
            decisions_text = "\n".join(
                f"- {d}" for d in meeting.get("decisions", [])
            )
            action_text = "\n".join(
                f"- {a}" for a in meeting.get("action_items", [])
            )

            content = (
                f"Meeting: {meeting.get('title', '')}\n"
                f"Date: {meeting.get('date', '')}\n"
                f"Attendees: {', '.join(meeting.get('attendees', []))}\n\n"
                f"Agenda:\n{meeting.get('agenda', '')}\n\n"
                f"Discussion:\n{meeting.get('discussion', '')}\n\n"
                f"Decisions Made:\n{decisions_text}\n\n"
                f"Action Items:\n{action_text}"
            )

            doc = {
                "doc_id": meeting.get("id", str(uuid.uuid4())),
                "doc_type": "meeting_notes",
                "title": meeting.get("title", "Untitled Meeting"),
                "content": content,
                "date": meeting.get("date", datetime.utcnow().isoformat()),
                "participants": meeting.get("attendees", []),
                "project": meeting.get("project", None),
                "tags": meeting.get("tags", []),
                "decisions": meeting.get("decisions", []),
                "action_items": meeting.get("action_items", []),
                "source_path": str(file),
                "raw": meeting,
            }
            documents.append(doc)

    return documents