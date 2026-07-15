"""
services/ingestion-service/app/parsers/email_parser.py
Parses JSON email files into normalized document dicts.
"""

import json
import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any


def parse_emails(email_dir: str) -> List[Dict[str, Any]]:
    """
    Reads all .json files in email_dir.
    Each file should be a list of email objects:
    {
      "from": "...", "to": [...], "date": "...",
      "subject": "...", "body": "..."
    }
    """
    documents = []
    path = Path(email_dir)

    for file in path.glob("*.json"):
        with open(file) as f:
            emails = json.load(f)

        for email in emails:
            # Build combined text for embedding
            content = (
                f"Subject: {email.get('subject', '')}\n"
                f"From: {email.get('from', '')}\n"
                f"To: {', '.join(email.get('to', []))}\n"
                f"Date: {email.get('date', '')}\n\n"
                f"{email.get('body', '')}"
            )

            participants = [email.get("from", "")] + email.get("to", [])

            doc = {
                "doc_id": email.get("id", str(uuid.uuid4())),
                "doc_type": "email",
                "title": email.get("subject", "Untitled Email"),
                "content": content,
                "date": email.get("date", datetime.utcnow().isoformat()),
                "participants": list(set(participants)),
                "project": email.get("project", None),
                "tags": email.get("tags", []),
                "source_path": str(file),
                "raw": email,
            }
            documents.append(doc)

    return documents