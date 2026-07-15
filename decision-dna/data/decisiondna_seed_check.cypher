// DecisionDNA Neo4j seed dump

CREATE CONSTRAINT person_name IF NOT EXISTS FOR (p:Person) REQUIRE p.name IS UNIQUE;

CREATE CONSTRAINT project_name IF NOT EXISTS FOR (p:Project) REQUIRE p.name IS UNIQUE;

CREATE CONSTRAINT decision_id IF NOT EXISTS FOR (d:Decision) REQUIRE d.id IS UNIQUE;

CREATE CONSTRAINT meeting_id IF NOT EXISTS FOR (m:Meeting) REQUIRE m.id IS UNIQUE;

CREATE CONSTRAINT ticket_id IF NOT EXISTS FOR (t:Ticket) REQUIRE t.id IS UNIQUE;

CREATE CONSTRAINT email_id IF NOT EXISTS FOR (e:Email) REQUIRE e.id IS UNIQUE;

// email EMAIL-001

:param doc => {"id": "EMAIL-001", "doc_type": "email", "title": "Cloud Migration Discussion", "date": "2024-01-15T10:30:00", "project": "CloudMigration", "participants": ["Ravi Sharma", "Alex Johnson", "Priya Patel"], "decisions": [], "status": "", "priority": "", "source_path": "C:\\project\\AI_asset\\decision-dna\\data\\synthetic\\emails\\emails.json"};

WITH $doc AS doc
MERGE (e:Email {id: doc.id})
SET e.subject = doc.title, e.date = doc.date, e.project = doc.project, e.source_path = doc.source_path
MERGE (proj:Project {name: doc.project})
MERGE (e)-[:PART_OF]->(proj)
WITH e, doc
UNWIND doc.participants AS person
MERGE (p:Person {name: person})
MERGE (p)-[:SENT_OR_RECEIVED]->(e);

// email EMAIL-002

:param doc => {"id": "EMAIL-002", "doc_type": "email", "title": "Re: Cloud Migration Discussion - Vendor X Evaluation", "date": "2024-01-16T14:20:00", "project": "DataPlatform", "participants": ["Ravi Sharma", "Neha Gupta", "Priya Patel"], "decisions": [], "status": "", "priority": "", "source_path": "C:\\project\\AI_asset\\decision-dna\\data\\synthetic\\emails\\emails.json"};

WITH $doc AS doc
MERGE (e:Email {id: doc.id})
SET e.subject = doc.title, e.date = doc.date, e.project = doc.project, e.source_path = doc.source_path
MERGE (proj:Project {name: doc.project})
MERGE (e)-[:PART_OF]->(proj)
WITH e, doc
UNWIND doc.participants AS person
MERGE (p:Person {name: person})
MERGE (p)-[:SENT_OR_RECEIVED]->(e);

// email EMAIL-003

:param doc => {"id": "EMAIL-003", "doc_type": "email", "title": "Security Risk in Auth Refactor", "date": "2024-02-05T09:15:00", "project": "AuthRefactor", "participants": ["Sunita Rao", "John Smith", "Michael Brown"], "decisions": [], "status": "", "priority": "", "source_path": "C:\\project\\AI_asset\\decision-dna\\data\\synthetic\\emails\\emails.json"};

WITH $doc AS doc
MERGE (e:Email {id: doc.id})
SET e.subject = doc.title, e.date = doc.date, e.project = doc.project, e.source_path = doc.source_path
MERGE (proj:Project {name: doc.project})
MERGE (e)-[:PART_OF]->(proj)
WITH e, doc
UNWIND doc.participants AS person
MERGE (p:Person {name: person})
MERGE (p)-[:SENT_OR_RECEIVED]->(e);

// meeting_notes MTG-001

:param doc => {"id": "MTG-001", "doc_type": "meeting_notes", "title": "Cloud Migration Planning \u00e2\u20ac\u201d Sprint 1", "date": "2024-01-20T10:00:00", "project": "CloudMigration", "participants": ["Ravi Sharma", "Priya Patel", "Alex Johnson", "Neha Gupta"], "decisions": ["Proceed with Azure Functions as target platform", "Security audit to be completed by Feb 15", "Ravi's vendor lock-in concern formally noted but overruled by budget considerations"], "status": "", "priority": "", "source_path": "C:\\project\\AI_asset\\decision-dna\\data\\synthetic\\meetings\\meetings.json"};

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

// meeting_notes MTG-002

:param doc => {"id": "MTG-002", "doc_type": "meeting_notes", "title": "Vendor X Evaluation Meeting", "date": "2024-01-25T14:00:00", "project": "DataPlatform", "participants": ["Priya Patel", "Anjali Mehta", "David Chen", "Pooja Verma"], "decisions": ["Vendor X rejected", "Open tender to be issued for alternative vendors", "Decision logged in PROJ-089"], "status": "", "priority": "", "source_path": "C:\\project\\AI_asset\\decision-dna\\data\\synthetic\\meetings\\meetings.json"};

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

// meeting_notes MTG-003

:param doc => {"id": "MTG-003", "doc_type": "meeting_notes", "title": "Auth Refactor Architecture Review", "date": "2024-02-10T11:00:00", "project": "AuthRefactor", "participants": ["Michael Brown", "Sunita Rao", "John Smith", "Pooja Verma"], "decisions": ["Proceed with current JWT implementation for MVP", "Refresh token rotation to be added in v1.1", "Michael's security objection formally recorded"], "status": "", "priority": "", "source_path": "C:\\project\\AI_asset\\decision-dna\\data\\synthetic\\meetings\\meetings.json"};

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

// jira_ticket PROJ-001

:param doc => {"id": "PROJ-001", "doc_type": "jira_ticket", "title": "API Timeout on High Load \u00e2\u20ac\u201d Azure Functions", "date": "2024-02-20T09:00:00", "project": "CloudMigration", "participants": ["Ravi Sharma", "Alex Johnson"], "decisions": [], "status": "Open", "priority": "Critical", "source_path": "C:\\project\\AI_asset\\decision-dna\\data\\synthetic\\jira\\tickets.json"};

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

// jira_ticket PROJ-002

:param doc => {"id": "PROJ-002", "doc_type": "jira_ticket", "title": "PostgreSQL to MongoDB migration breaks reporting", "date": "2024-02-15T11:00:00", "project": "DataPlatform", "participants": ["Neha Gupta", "Michael Brown"], "decisions": [], "status": "In Progress", "priority": "High", "source_path": "C:\\project\\AI_asset\\decision-dna\\data\\synthetic\\jira\\tickets.json"};

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

// jira_ticket PROJ-003

:param doc => {"id": "PROJ-003", "doc_type": "jira_ticket", "title": "JWT refresh token vulnerability \u00e2\u20ac\u201d Auth Service", "date": "2024-02-12T16:00:00", "project": "AuthRefactor", "participants": ["Sunita Rao", "Michael Brown"], "decisions": [], "status": "Open", "priority": "Critical", "source_path": "C:\\project\\AI_asset\\decision-dna\\data\\synthetic\\jira\\tickets.json"};

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

// jira_ticket PROJ-004

:param doc => {"id": "PROJ-004", "doc_type": "jira_ticket", "title": "Vendor evaluation framework needs update", "date": "2024-01-30T10:00:00", "project": "DataPlatform", "participants": ["Pooja Verma", "Priya Patel"], "decisions": [], "status": "Closed", "priority": "Medium", "source_path": "C:\\project\\AI_asset\\decision-dna\\data\\synthetic\\jira\\tickets.json"};

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

// jira_ticket PROJ-005

:param doc => {"id": "PROJ-005", "doc_type": "jira_ticket", "title": "Azure vendor lock-in risk documentation", "date": "2024-01-21T09:00:00", "project": "CloudMigration", "participants": ["Ravi Sharma", "Alex Johnson"], "decisions": [], "status": "Closed", "priority": "Low", "source_path": "C:\\project\\AI_asset\\decision-dna\\data\\synthetic\\jira\\tickets.json"};

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
