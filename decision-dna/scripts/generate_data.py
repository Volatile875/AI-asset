"""
scripts/generate_data.py
Generates realistic synthetic corporate data:
- 100 emails
- 50 meeting notes
- 100 Jira tickets
All around a cloud migration project scenario.
Run: python scripts/generate_data.py
"""

import json
import os
import random
import uuid
from datetime import datetime, timedelta
from pathlib import Path

# ── Config ─────────────────────────────────────────────────────

OUTPUT_DIR = Path("data/synthetic")
PEOPLE = [
    "Ravi Sharma", "Priya Patel", "Alex Johnson", "Neha Gupta",
    "John Smith", "Anjali Mehta", "David Chen", "Pooja Verma",
    "Michael Brown", "Sunita Rao",
]
PROJECTS = ["CloudMigration", "DataPlatform", "AuthRefactor", "MobileApp"]
START_DATE = datetime(2024, 1, 1)


def random_date(start: datetime, days: int = 365) -> str:
    return (start + timedelta(days=random.randint(0, days))).strftime("%Y-%m-%dT%H:%M:%S")


def random_people(n: int = 3) -> list:
    return random.sample(PEOPLE, min(n, len(PEOPLE)))


# ── Email Generator ────────────────────────────────────────────

EMAIL_SUBJECTS_TEMPLATES = [
    ("Cloud Migration Discussion", "CloudMigration", "migration"),
    ("Re: AWS vs Azure — Final Decision", "CloudMigration", "migration"),
    ("Vendor X Evaluation Results", "DataPlatform", "vendor"),
    ("Security Risk in Proposed Architecture", "CloudMigration", "risk"),
    ("Database Migration Concerns", "DataPlatform", "database"),
    ("API Timeout Issues — Urgent", "AuthRefactor", "issue"),
    ("Q3 Budget Review — Cloud Costs", "CloudMigration", "budget"),
    ("Rejected Proposal: Kubernetes Migration", "DataPlatform", "rejection"),
    ("Follow-up: Auth Service Refactor", "AuthRefactor", "auth"),
    ("Mobile App Backend Decision", "MobileApp", "backend"),
]

EMAIL_BODIES = [
    """Hi team,

After evaluating both AWS Lambda and Azure Functions for our migration, I want to share my concerns.

Azure Functions has better pricing for our workload, but Ravi raised a valid point about vendor lock-in. 
We should not ignore the security implications of moving our authentication layer to a new cloud provider 
without a proper audit.

My recommendation: proceed with Azure, but only after a security review.

Best,
{sender}""",

    """Team,

Just circling back on the Vendor X evaluation from last week's meeting.

The vendor was rejected primarily due to:
1. Pricing was 40% above our budget
2. SLA terms were unacceptable (99.5% uptime vs our requirement of 99.9%)
3. No GDPR compliance certification

{dissenter} had concerns about the evaluation process, but leadership agreed with the rejection.

Regards,
{sender}""",

    """Hi,

I want to flag a serious risk that came up during our architecture review.

The proposed database migration from PostgreSQL to MongoDB will break our existing reporting queries. 
I've already raised this in PROJ-045 but nobody has responded in 2 weeks.

If we proceed without addressing this, we will face data integrity issues in production.

{sender}""",

    """All,

Decision confirmed from yesterday's steering committee:

We are proceeding with Azure Functions for the cloud migration. 
Implementation starts March 20.

Key contacts:
- Architecture: {person1}
- Security review: {person2}
- Timeline owner: {person3}

Any objections should be raised by EOD Friday.

{sender}""",

    """Team,

I disagree with the current approach to the auth refactor.

Using JWT tokens without refresh token rotation is a security vulnerability. 
I've sent the OWASP reference 3 times now and it keeps getting deprioritized.

I'm formally documenting my objection here.

{sender}""",
]


def generate_emails(count: int = 100) -> list:
    emails = []
    for i in range(count):
        template_subject, project, tag = random.choice(EMAIL_SUBJECTS_TEMPLATES)
        body_template = random.choice(EMAIL_BODIES)
        people = random_people(4)
        sender = people[0]

        body = body_template.format(
            sender=sender,
            dissenter=people[1] if len(people) > 1 else "A colleague",
            person1=people[1] if len(people) > 1 else "TBD",
            person2=people[2] if len(people) > 2 else "TBD",
            person3=people[3] if len(people) > 3 else "TBD",
        )

        email = {
            "id": f"EMAIL-{str(i+1).zfill(3)}",
            "from": sender,
            "to": people[1:],
            "date": random_date(START_DATE),
            "subject": template_subject,
            "body": body,
            "project": project,
            "tags": [tag],
        }
        emails.append(email)
    return emails


# ── Meeting Generator ──────────────────────────────────────────

MEETING_TEMPLATES = [
    {
        "title": "Cloud Migration Planning — Sprint 1",
        "project": "CloudMigration",
        "agenda": "Discuss migration from AWS Lambda to Azure Functions. Evaluate timeline and risks.",
        "discussion": "The team debated the merits of both platforms. Ravi raised concerns about vendor lock-in with Azure. Priya presented cost analysis showing 30% savings with Azure. The security team flagged that OAuth integration needs review before migration.",
        "decisions": [
            "Proceed with Azure Functions as target platform",
            "Security audit to be completed by Feb 15",
            "Ravi's vendor lock-in concern formally noted but overruled by budget considerations",
        ],
        "action_items": [
            "Alex to complete security audit by Feb 15",
            "Neha to create migration runbook",
            "John to schedule stakeholder review",
        ],
        "tags": ["migration", "cloud", "azure"],
    },
    {
        "title": "Vendor X Evaluation Meeting",
        "project": "DataPlatform",
        "agenda": "Evaluate Vendor X for data pipeline tooling.",
        "discussion": "The team reviewed Vendor X's proposal. Key concerns were pricing (40% over budget), SLA terms (99.5% vs required 99.9%), and missing GDPR certification. Anjali argued for giving Vendor X another chance to revise their proposal, but was outvoted.",
        "decisions": [
            "Vendor X rejected",
            "Open tender to be issued for alternative vendors",
            "Decision logged in PROJ-089",
        ],
        "action_items": [
            "Pooja to draft new vendor requirements doc",
            "David to identify 3 alternative vendors",
        ],
        "tags": ["vendor", "data-platform"],
    },
    {
        "title": "Auth Refactor Architecture Review",
        "project": "AuthRefactor",
        "agenda": "Review proposed JWT implementation for auth service.",
        "discussion": "Michael raised a serious security concern: the proposed implementation does not include refresh token rotation, which violates OWASP guidelines. The engineering lead acknowledged but said the deadline was more important. Michael formally objected.",
        "decisions": [
            "Proceed with current JWT implementation for MVP",
            "Refresh token rotation to be added in v1.1",
            "Michael's security objection formally recorded",
        ],
        "action_items": [
            "Michael to document security risk in JIRA",
            "Sunita to update implementation timeline",
        ],
        "tags": ["auth", "security", "jwt"],
    },
]


def generate_meetings(count: int = 50) -> list:
    meetings = []
    for i in range(count):
        template = random.choice(MEETING_TEMPLATES).copy()
        template["id"] = f"MTG-{str(i+1).zfill(3)}"
        template["date"] = random_date(START_DATE)
        template["attendees"] = random_people(random.randint(3, 7))
        meetings.append(template)
    return meetings


# ── Jira Generator ─────────────────────────────────────────────

JIRA_TEMPLATES = [
    {
        "title": "API Timeout on High Load — Azure Functions",
        "description": "After migrating to Azure Functions, we are seeing 30% of requests timing out above 500 concurrent users. This was flagged as a risk in MTG-001 but migration proceeded anyway.",
        "status": "Open",
        "priority": "Critical",
        "labels": ["migration", "performance", "azure"],
        "project": "CloudMigration",
    },
    {
        "title": "PostgreSQL to MongoDB migration breaks reporting",
        "description": "The data migration has broken 14 existing SQL reporting queries. This was flagged by Ravi in EMAIL-023 but the risk was not addressed before go-live.",
        "status": "In Progress",
        "priority": "High",
        "labels": ["database", "migration", "reporting"],
        "project": "DataPlatform",
    },
    {
        "title": "JWT refresh token vulnerability — Auth Service",
        "description": "As formally objected to in MTG-012, the lack of refresh token rotation is a security vulnerability. A penetration test confirmed the issue. Escalating to CTO.",
        "status": "Open",
        "priority": "Critical",
        "labels": ["security", "auth", "vulnerability"],
        "project": "AuthRefactor",
    },
    {
        "title": "Vendor evaluation framework needs update",
        "description": "The Vendor X rejection highlighted gaps in our evaluation criteria. Need to add GDPR compliance and SLA requirements as mandatory fields.",
        "status": "Closed",
        "priority": "Medium",
        "labels": ["vendor", "process"],
        "project": "DataPlatform",
    },
    {
        "title": "Azure vendor lock-in risk documentation",
        "description": "Per Ravi's concern raised in MTG-001, documenting the vendor lock-in risks of Azure Functions migration for future reference.",
        "status": "Closed",
        "priority": "Low",
        "labels": ["migration", "risk", "documentation"],
        "project": "CloudMigration",
    },
]

COMMENT_TEMPLATES = [
    "Confirmed this is still an issue as of today.",
    "Spoke with the vendor — they cannot fix this in the current contract.",
    "This was raised before we migrated. See EMAIL-023.",
    "Escalated to VP Engineering.",
    "Temporary workaround applied, proper fix in next sprint.",
    "Root cause identified: insufficient load testing before migration.",
]


def generate_jira(count: int = 100) -> list:
    tickets = []
    for i in range(count):
        template = random.choice(JIRA_TEMPLATES).copy()
        people = random_people(3)
        comments = random.sample(COMMENT_TEMPLATES, random.randint(1, 3))

        ticket = {
            "id": f"PROJ-{str(i+1).zfill(3)}",
            "title": template["title"],
            "description": template["description"],
            "status": template["status"],
            "priority": template["priority"],
            "reporter": people[0],
            "assignee": people[1],
            "created": random_date(START_DATE),
            "labels": template["labels"],
            "project": template["project"],
            "comments": [
                {
                    "author": random.choice(PEOPLE),
                    "date": random_date(START_DATE),
                    "body": comment,
                }
                for comment in comments
            ],
        }
        tickets.append(ticket)
    return tickets


# ── Main ───────────────────────────────────────────────────────

def main():
    # Create output directories
    (OUTPUT_DIR / "emails").mkdir(parents=True, exist_ok=True)
    (OUTPUT_DIR / "meetings").mkdir(parents=True, exist_ok=True)
    (OUTPUT_DIR / "jira").mkdir(parents=True, exist_ok=True)

    # Generate emails
    emails = generate_emails(100)
    with open(OUTPUT_DIR / "emails" / "emails.json", "w") as f:
        json.dump(emails, f, indent=2)
    print(f"✅ Generated {len(emails)} emails")

    # Generate meetings
    meetings = generate_meetings(50)
    with open(OUTPUT_DIR / "meetings" / "meetings.json", "w") as f:
        json.dump(meetings, f, indent=2)
    print(f"✅ Generated {len(meetings)} meeting notes")

    # Generate Jira tickets
    tickets = generate_jira(100)
    with open(OUTPUT_DIR / "jira" / "tickets.json", "w") as f:
        json.dump(tickets, f, indent=2)
    print(f"✅ Generated {len(tickets)} Jira tickets")

    print(f"\n📁 Data saved to: {OUTPUT_DIR.absolute()}")
    print("Next: docker-compose up --build && python scripts/ingest_all.py")


if __name__ == "__main__":
    main()
