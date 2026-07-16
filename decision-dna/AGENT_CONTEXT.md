# DecisionDNA — Complete Agent Context Document

> This document provides full context for any AI agent (GPT-4, Gemini, etc.) working on the DecisionDNA codebase. It includes the original architecture, all changes made during integration, and the current state of the project.

---

## Project Summary

DecisionDNA is an **AI Organizational Memory Engine** built as a microservices system. It answers questions like "Why did we reject Vendor X?" by reading emails, meeting notes, and Jira tickets, then returning a structured answer with a decision timeline, dissent capture, and confidence score.

### Key Capabilities
- **Semantic Search**: Pinecone vector database with OpenAI embeddings
- **Knowledge Graph**: Neo4j for entity relationships (people, decisions, projects)
- **Decision Timeline**: Chronological reconstruction of decision trails
- **RAG Pipeline**: 5-agent LangGraph pipeline for Q&A
- **MCP Server**: Jira ticket status integration

---

## Tech Stack

| Component | Technology |
|-----------|------------|
| LLM | OpenAI chat model (`gpt-4o-mini` by default) |
| Embeddings | OpenAI text-embedding-3-large (1024 dimensions) |
| Vector DB | Pinecone (Serverless, AWS us-east-1) |
| Graph DB | Neo4j 5.15 |
| Agent Framework | LangGraph 0.1.5 |
| RAG | LangChain 0.2.1 |
| API | FastAPI (one per microservice) |
| Cache | Redis 7 |
| UI | Streamlit |
| Infra | Docker Compose |
| MCP Server | FastMCP for Jira integration |

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────┐
│                         DECISIONDNA SYSTEM                          │
├─────────────────────────────────────────────────────────────────────┤
│                                                                     │
│  ┌──────────────┐                                                   │
│  │   Frontend   │  Streamlit UI (Port 3000)                        │
│  │  (Streamlit) │  - Ask DecisionDNA                               │
│  └──────┬───────┘  - Decision Timeline                             │
│         │          - Knowledge Graph                               │
│         │          - Ingest Data                                   │
│         │          - Health Check                                  │
│         ▼                                                           │
│  ┌──────────────┐                                                   │
│  │  API Gateway │  Port 8000                                       │
│  │  (FastAPI)   │  - Rate limiting (100 req/min per IP)            │
│  └──────┬───────┘  - CORS enabled                                  │
│         │          - Scalar API documentation                      │
│         │                                                           │
│    ┌────┴──────────────────────────────────────────────────────┐   │
│    │                                                          │   │
│    ▼                           ▼                              ▼   │
│  ┌────────────┐         ┌────────────┐                 ┌──────────┐│
│  │ Ingestion  │         │   Query    │                 │ Timeline ││
│  │  Service   │         │  Service   │                 │ Service  ││
│  │  :8001     │         │  :8004     │                 │  :8005   ││
│  └─────┬──────┘         └─────┬──────┘                 └────┬─────┘│
│        │                      │                             │      │
│        │    ┌─────────────────┼─────────────────┐           │      │
│        ▼    ▼                 ▼                 ▼           ▼      │
│  ┌────────────┐         ┌────────────┐         ┌────────────┐     │
│  │  Embedding │         │   Graph    │         │  Pinecone  │     │
│  │  Service   │────────▶│  Service   │────────▶│  (Vector)  │     │
│  │  :8002     │         │  :8003     │         │            │     │
│  └────────────┘         └────────────┘         └────────────┘     │
│         │                      │                                  │
│         ▼                      ▼                                  │
│  ┌────────────┐         ┌────────────┐                           │
│  │  Pinecone  │         │   Neo4j    │                           │
│  │  (Vector)  │         │  (Graph)   │                           │
│  └────────────┘         └────────────┘                           │
│                                                                     │
│  ┌────────────┐         ┌────────────┐                           │
│  │   Redis    │         │ MCP Server │  Jira Integration         │
│  │   (Cache)  │         │  (stdio)   │  - Status fetching        │
│  └────────────┘         └────────────┘  - History tracking       │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
```

---

## Microservices Detail

### 1. API Gateway (Port 8000)
**File**: `api-gateway/app/main.py`

**Routes**:
- `GET /health` - Health check for all services
- `POST /api/v1/ingest` - Trigger data ingestion
- `GET /api/v1/ingest/status/{job_id}` - Check ingestion job status
- `POST /api/v1/query` - Query the system
- `GET /api/v1/timeline/{topic}` - Build decision timeline
- `GET /api/v1/graph/decisions` - Get all decisions from Neo4j
- `GET /api/v1/graph/entities/{entity}` - Get entity connections
- `GET /scalar` - Scalar API documentation UI

**Features**:
- IP-based rate limiting (100 requests/minute)
- Redis-backed rate limiter
- CORS enabled for all origins
- Scalar OpenAPI documentation

---

### 2. Ingestion Service (Port 8001)
**File**: `services/ingestion-service/app/main.py`

**Parsers**:
- `email_parser.py` - Parses JSON email files
- `meeting_parser.py` - Parses JSON meeting notes
- `jira_parser.py` - Parses JSON Jira tickets

**Flow**:
1. Read JSON files from `data/synthetic/{emails,meetings,jira}/`
2. Parse into normalized document format
3. Store in Redis temporarily
4. Trigger embedding-service for Pinecone upsert
5. Trigger graph-service for Neo4j population

**Output Document Format**:
```json
{
  "doc_id": "EMAIL-001",
  "doc_type": "email",
  "title": "Subject line",
  "content": "Combined text for embedding",
  "date": "2024-01-15T10:30:00",
  "participants": ["sender", "recipients"],
  "project": "CloudMigration",
  "tags": ["migration", "azure"],
  "source_path": "/app/data/...",
  "raw": { ... original data ... }
}
```

---

### 3. Embedding Service (Port 8002)
**File**: `services/embedding-service/app/main.py`

**Process**:
1. Receive documents from ingestion-service
2. Chunk documents using `RecursiveCharacterTextSplitter`
   - Chunk size: 800 characters
   - Overlap: 100 characters
3. Generate embeddings with `text-embedding-3-large` (3072 dims)
4. Upsert to Pinecone with metadata

**Pinecone Metadata Schema**:
```json
{
  "doc_id": "EMAIL-001",
  "doc_type": "email",
  "title": "Subject",
  "date": "2024-01-15",
  "project": "CloudMigration",
  "participants": "Ravi,Priya",
  "tags": "migration,azure",
  "chunk_index": 0,
  "content_preview": "First 200 chars..."
}
```

---

### 4. Graph Service (Port 8003)
**File**: `services/graph-service/app/main.py`

**Neo4j Schema**:
```cypher
// Node Types
(:Person {name: STRING})
(:Project {name: STRING})
(:Decision {id: STRING, description: STRING, date: STRING, project: STRING})
(:Meeting {id: STRING, title: STRING, date: STRING, project: STRING})
(:Ticket {id: STRING, title: STRING, status: STRING, date: STRING})
(:Email {id: STRING, subject: STRING, date: STRING, project: STRING})

// Relationships
(Person)-[:ATTENDED]->(Meeting)
(Person)-[:INVOLVED_IN]->(Ticket)
(Person)-[:SENT_OR_RECEIVED]->(Email)
(Meeting)-[:PART_OF]->(Project)
(Meeting)-[:PRODUCED]->(Decision)
(Ticket)-[:PART_OF]->(Project)
```

**Endpoints**:
- `POST /build-graph` - Ingest documents into Neo4j
- `GET /decisions` - List all decisions
- `GET /entities/{name}` - Get entity connections
- `GET /project-timeline/{project}` - Get project timeline
- `POST /query` - Run raw Cypher queries

---

### 5. Query Service (Port 8004)
**File**: `services/query-service/app/main.py`

**5-Agent LangGraph Pipeline**:

```
┌─────────────┐     ┌─────────────┐     ┌─────────────┐
│   Planner   │────▶│   Search    │────▶│  Timeline   │
│   Agent     │     │   Agent     │     │   Agent     │
└─────────────┘     └─────────────┘     └─────────────┘
                                               │
                                               ▼
                                        ┌─────────────┐
                                        │  Decision   │
                                        │   Agent     │
                                        └──────┬──────┘
                                               │
                                               ▼
                                        ┌─────────────┐
                                        │   Answer    │
                                        │   Agent     │
                                        └─────────────┘
```

**Agent Responsibilities**:

1. **Planner Agent** - Breaks question into 2-4 search sub-tasks
2. **Search Agent** - Queries Pinecone for relevant chunks
3. **Timeline Agent** - Calls timeline-service for chronological events
4. **Decision Agent** - Analyzes decisions, dissent, risks, outcomes
5. **Answer Agent** - Synthesizes final response with sources

---

### 6. Timeline Service (Port 8005)
**File**: `services/timeline-service/app/main.py`

**Process**:
1. Search Pinecone for relevant documents
2. Use OpenAI to extract structured timeline events
3. Sort events by date
4. Assess outcome and confidence score

**Event Types**:
- `discussion` 💬
- `decision` ✅
- `implementation` 🔧
- `issue` 🐛
- `risk_flag` ⚠️
- `approval` 👍
- `rejection` ❌

**Sentiment Types**:
- `agreement` 🟢
- `dissent` 🔴
- `concern` 🟡
- `neutral` ⚪

---

### 7. Frontend (Port 3000)
**File**: `frontend/app.py`

**Pages**:
1. **Ask DecisionDNA** - Main Q&A interface
2. **Decision Timeline** - Visualize decision trails
3. **Knowledge Graph** - Explore Neo4j relationships
4. **Ingest Data** - Trigger data ingestion
5. **Health** - System health dashboard

---

### 8. MCP Server (stdio)
**File**: `mcp-server/server.py`

**Purpose**: Jira ticket status integration for AI assistants

**Tools Provided**:
1. `fetch_jira_ticket_status(ticket_key)` - Fetch current status from Atlassian Jira
2. `fetch_many_jira_ticket_statuses(ticket_keys)` - Batch fetch
3. `get_stored_jira_ticket_status(ticket_key)` - Get cached status from SQLite
4. `get_jira_ticket_status_history(ticket_key, limit)` - Get status change history

**SQLite Schema**:
```sql
-- Current status table
CREATE TABLE ticket_current_status (
    ticket_key TEXT PRIMARY KEY,
    current_status TEXT NOT NULL,
    last_status_changed_at TEXT,
    jira_updated_at TEXT,
    last_observed_at TEXT NOT NULL,
    summary TEXT,
    assignee TEXT
);

-- Status snapshots
CREATE TABLE ticket_status_snapshots (...);

-- Status transitions
CREATE TABLE ticket_status_changes (
    ticket_key TEXT,
    from_status TEXT,
    to_status TEXT,
    changed_at TEXT,
    author TEXT,
    jira_history_id TEXT,
    observed_at TEXT
);
```

---

## Shared Modules

### `shared/config/settings.py`
Centralized Pydantic settings:
```python
class Settings(BaseSettings):
    # LLM
    openai_api_key: str
    openai_chat_model: str = "gpt-4o-mini"
    
    # Vector DB
    pinecone_api_key: str
    pinecone_environment: str = "us-east-1"
    pinecone_index_name: str = "decision-dna"
    
    # Graph DB
    neo4j_uri: str = "bolt://neo4j:7687"
    neo4j_username: str = "neo4j"
    neo4j_password: str = "password123"
    
    # Cache
    redis_url: str = "redis://redis:6379"
    
    # Service URLs
    ingestion_service_url: str
    embedding_service_url: str
    graph_service_url: str
    query_service_url: str
    timeline_service_url: str
```

### `shared/models/schemas.py`
Pydantic models shared across all services:
- `DocumentType` enum
- `DecisionStatus` enum
- `SentimentType` enum
- `RawDocument`, `ChunkedDocument`, `EmbeddedChunk`
- `Decision`, `DecisionParticipant`
- `TimelineEvent`, `DecisionTimeline`
- `QueryRequest`, `QueryResponse`, `Source`
- `IngestionRequest`, `IngestionResponse`
- `HealthResponse`

### `shared/utils/helpers.py`
Common utilities:
- `get_logger(service_name)` - Structured logging
- `generate_id(prefix)` - UUID generation
- `call_service(url, method, payload)` - Async HTTP client
- `parse_date(date_str)` - Multi-format date parsing
- `truncate(text, max_len)` - Text truncation
- `clean_text(text)` - Whitespace normalization

---

## Sample Data Structure

### emails.json
```json
[
  {
    "id": "EMAIL-001",
    "from": "Ravi Sharma",
    "to": ["Priya Patel", "Alex Johnson"],
    "date": "2024-01-15T10:30:00",
    "subject": "Cloud Migration Discussion",
    "body": "After evaluating AWS Lambda vs Azure Functions...",
    "project": "CloudMigration",
    "tags": ["migration", "azure"]
  }
]
```

### meetings.json
```json
[
  {
    "id": "MTG-001",
    "title": "Cloud Migration Planning — Sprint 1",
    "date": "2024-01-20T10:00:00",
    "attendees": ["Ravi Sharma", "Priya Patel"],
    "agenda": "Discuss migration options",
    "discussion": "The team debated...",
    "decisions": ["Proceed with Azure Functions"],
    "action_items": ["Alex to complete security audit"],
    "project": "CloudMigration",
    "tags": ["migration", "cloud"]
  }
]
```

### tickets.json
```json
[
  {
    "id": "PROJ-001",
    "title": "API Timeout on High Load",
    "description": "After migrating to Azure Functions...",
    "status": "Open",
    "priority": "Critical",
    "reporter": "Alex Johnson",
    "assignee": "Ravi Sharma",
    "created": "2024-02-20T09:00:00",
    "labels": ["migration", "performance"],
    "project": "CloudMigration",
    "comments": [
      {"author": "Ravi", "date": "...", "body": "Confirmed issue"}
    ]
  }
]
```

---

## Docker Compose Services

```yaml
services:
  redis:          # Port 6379
  neo4j:          # Ports 7474 (HTTP), 7687 (Bolt)
  ingestion-service:    # Port 8001
  embedding-service:    # Port 8002
  graph-service:        # Port 8003
  query-service:        # Port 8004
  timeline-service:     # Port 8005
  api-gateway:          # Port 8000
  frontend:             # Port 3000
```

---

## Running the Project

```bash
# Navigate to project directory
cd c:/project/AI_asset/decision-dna

# Build and start all services
docker-compose up --build

# Access points:
# - Frontend:      http://localhost:3000
# - API Gateway:   http://localhost:8000
# - Scalar Docs:   http://localhost:8000/scalar
# - Neo4j Browser: http://localhost:7474
```

---

## Changes Made During Integration

### Files Created:

1. **API Gateway**
   - `api-gateway/app/main.py` - Complete routing implementation with Scalar docs
   - Updated `api-gateway/requirements.txt` with `scalar-fastapi==1.0.0`

2. **Ingestion Service**
   - `services/ingestion-service/app/main.py` - Background ingestion pipeline
   - `services/ingestion-service/app/parsers/__init__.py`
   - `services/ingestion-service/app/parsers/email_parser.py`
   - `services/ingestion-service/app/parsers/meeting_parser.py`
   - `services/ingestion-service/app/parsers/jira_parser.py` - Fixed type annotation

3. **Timeline Service**
   - `services/timeline-service/app/main.py` - Timeline builder with OpenAI integration

4. **Shared Modules**
   - `shared/__init__.py`
   - `shared/config/__init__.py`
   - `shared/config/settings.py` - API keys configured
   - `shared/models/__init__.py`
   - `shared/models/schemas.py` - All Pydantic models
   - `shared/utils/__init__.py`
   - `shared/utils/helpers.py`

5. **Sample Data**
   - `data/synthetic/emails/emails.json` - 3 sample emails
   - `data/synthetic/meetings/meetings.json` - 3 sample meetings
   - `data/synthetic/jira/tickets.json` - 5 sample Jira tickets

6. **Configuration**
   - `.env` - Environment variables with API keys

### Bug Fixes:
- Fixed type annotation in `jira_parser.py` line 36: Added explicit `List[str]` type for `participants` variable

---

## Environment Variables Required

```env
# LLM
OPENAI_API_KEY=<your_key>
OPENAI_CHAT_MODEL=gpt-4o-mini

# Pinecone
PINECONE_API_KEY=<your_key>
PINECONE_ENVIRONMENT=us-east-1
PINECONE_INDEX_NAME=decision-dna

# Neo4j
NEO4J_URI=bolt://neo4j:7687
NEO4J_USERNAME=neo4j
NEO4J_PASSWORD=password123

# Redis
REDIS_URL=redis://redis:6379

# Service URLs
INGESTION_SERVICE_URL=http://ingestion-service:8001
EMBEDDING_SERVICE_URL=http://embedding-service:8002
GRAPH_SERVICE_URL=http://graph-service:8003
QUERY_SERVICE_URL=http://query-service:8004
TIMELINE_SERVICE_URL=http://timeline-service:8005

# Jira MCP Server (optional)
JIRA_BASE_URL=https://your-domain.atlassian.net
JIRA_EMAIL=your.email@company.com
JIRA_API_TOKEN=your_atlassian_api_token
```

---

## Key Integration Points

### Pinecone Integration
- **Location**: `embedding-service/app/main.py`, `query-service/app/main.py`, `timeline-service/app/main.py`
- **Model**: `text-embedding-3-large` (3072 dimensions)
- **Index**: Serverless, AWS us-east-1, cosine similarity
- **Operations**: Upsert chunks, query with metadata filters

### Neo4j Integration
- **Location**: `graph-service/app/main.py`
- **Driver**: `neo4j` Python async driver
- **Operations**: Create nodes/relationships, query entity connections
- **Constraints**: Unique constraints on Person, Project, Decision, Meeting, Ticket IDs

### MCP Server Integration
- **Location**: `mcp-server/server.py`
- **Framework**: FastMCP for stdio communication
- **Capabilities**: Jira status fetching, SQLite persistence, status history tracking
- **Usage**: Can be invoked by MCP-compatible AI assistants

---

## Query Flow Example

```
User Question: "Why did we reject Vendor X?"
           │
           ▼
┌─────────────────────────────────────────────────────┐
│ Query Service - 5-Agent Pipeline                    │
├─────────────────────────────────────────────────────┤
│ 1. Planner: ["Find emails about Vendor X",          │
│              "Find meetings discussing Vendor X",    │
│              "Find Jira tickets related to Vendor"]  │
│                                                     │
│ 2. Search: Query Pinecone → 15 relevant chunks      │
│                                                     │
│ 3. Timeline: Build chronological events             │
│    - 2024-01-20: Meeting about evaluation           │
│    - 2024-01-25: Formal rejection decision          │
│                                                     │
│ 4. Decision Analyzer:                               │
│    - DECISION: Vendor X rejected                    │
│    - PARTICIPANTS: Priya (agreement),               │
│                    Anjali (concern)                 │
│    - RISKS: Pricing, SLA, GDPR                      │
│    - CONFIDENCE: 0.85                               │
│                                                     │
│ 5. Answer Generator: Structured response            │
└─────────────────────────────────────────────────────┘
           │
           ▼
┌─────────────────────────────────────────────────────┐
│ Response to User                                    │
├─────────────────────────────────────────────────────┤
│ Answer: Vendor X was rejected due to:               │
│ - Pricing 40% above budget                          │
│ - SLA 99.5% vs required 99.9%                       │
│ - No GDPR certification                             │
│                                                     │
│ Timeline: [3 events]                                │
│ Sources: [EMAIL-002, MTG-002, PROJ-004]             │
│ Confidence: 85%                                     │
└─────────────────────────────────────────────────────┘
```

---

## Future Extensions

1. **Slack Integration** - Add Slack message ingestion
2. **Confluence Integration** - Add Confluence page parsing
3. **Real-time Updates** - WebSocket for live query progress
4. **Authentication** - Add JWT-based auth to API Gateway
5. **Multi-tenant** - Support multiple organizations
6. **Export** - PDF/Word export of decision timelines

---

## Troubleshooting

### Common Issues:

1. **Pinecone Connection Failed**
   - Check `PINECONE_API_KEY` is valid
   - Verify index exists in Pinecone console
   - Ensure region matches `PINECONE_ENVIRONMENT`

2. **Neo4j Connection Failed**
   - Wait for Neo4j to fully start (can take 30+ seconds)
   - Check Neo4j browser at http://localhost:7474
   - Verify `NEO4J_PASSWORD` matches docker-compose

3. **Rate Limiting**
   - Default is 100 requests/minute per IP
   - Check Redis is running: `docker-compose ps redis`

4. **MCP Server Not Responding**
   - Verify Jira credentials in `.env`
   - Check Atlassian API token is valid
   - Test with: `python mcp-server/server.py`

---

## Contact & Support

- **Project**: DecisionDNA
- **Type**: AI Organizational Memory Engine
- **Repository**: Local development
- **Documentation**: This file + README.md + PROJECT_STRUCTURE.md
