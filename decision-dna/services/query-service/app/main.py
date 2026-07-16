"""
services/query-service/app/main.py
The heart of DecisionDNA.
LangGraph 5-agent pipeline:
  Planner → Search → Timeline → Decision → Answer
"""

import os
from urllib.parse import quote
from typing import Any, Dict, List, Optional, TypedDict


import httpx
from fastapi import FastAPI, HTTPException
from langchain_openai import OpenAIEmbeddings
from langgraph.graph import END, StateGraph
from openai import OpenAI
from pinecone import Pinecone
from pydantic import BaseModel
from openai import OpenAIError

app = FastAPI(title="Query Service", version="1.0.0")

# ── Config ─────────────────────────────────────────────────────
OPENAI_API_KEY    = os.getenv("OPENAI_API_KEY")
PINECONE_API_KEY  = os.getenv("PINECONE_API_KEY")
INDEX_NAME        = os.getenv("PINECONE_INDEX_NAME", "ai-asset")
CHAT_MODEL        = os.getenv("OPENAI_CHAT_MODEL", "gpt-4o-mini")
EMBEDDING_MODEL   = os.getenv("EMBEDDING_MODEL", "text-embedding-3-large")
EMBEDDING_DIM     = int(os.getenv("EMBEDDING_DIMENSIONS", "1024"))
TIMELINE_URL      = os.getenv("TIMELINE_SERVICE_URL", "http://timeline-service:8005")
GRAPH_URL         = os.getenv("GRAPH_SERVICE_URL",    "http://graph-service:8003")

openai_client = None
embeddings_model = None
pc_index = None


def init_clients() -> bool:
    """Lazily (re)initialize external clients. Idempotent and never raises, so a
    transient Pinecone/OpenAI failure can't crash startup or leave the service
    permanently unreachable — it retries on the next call and self-heals.
    Returns True once all clients are ready."""
    global openai_client, embeddings_model, pc_index
    try:
        if openai_client is None:
            openai_client = OpenAI(api_key=OPENAI_API_KEY)
        if embeddings_model is None:
            embeddings_model = OpenAIEmbeddings(
                model=EMBEDDING_MODEL,
                dimensions=EMBEDDING_DIM,
                openai_api_key=OPENAI_API_KEY,
            )
        if pc_index is None:
            pc = Pinecone(api_key=PINECONE_API_KEY)
            pc_index = pc.Index(INDEX_NAME)
        return True
    except Exception as e:
        print(f"[query-service] client init failed (will retry on demand): {e}", flush=True)
        return False


def generate_text(prompt: str, max_tokens: int) -> str:
    if openai_client is None:
        raise RuntimeError("OpenAI client is not initialized")

    try:
        response = openai_client.chat.completions.create(
            model=CHAT_MODEL,
            messages=[
                {
                    "role": "user",
                    "content": prompt
                }
            ],
            max_tokens=max_tokens,
            temperature=0.3
        )

        if not response.choices:
            raise RuntimeError("OpenAI returned no choices.")

        return response.choices[0].message.content or ""

    except OpenAIError as e:
        print(f"[OpenAI ERROR] {e}", flush=True)
        raise HTTPException(
            status_code=502,
            detail=f"OpenAI API Error: {str(e)}"
        )

    except Exception as e:
        print(f"[generate_text ERROR] {e}", flush=True)
        raise HTTPException(
            status_code=500,
            detail=str(e)
        )


@app.on_event("startup")
async def startup():
    # Non-fatal: never abort startup on a dependency hiccup. The service must stay
    # up and reachable so it can self-heal; clients are (re)initialized on demand.
    init_clients()


# ── LangGraph State ────────────────────────────────────────────

class AgentState(TypedDict):
    question: str
    project_filter: Optional[str]
    sub_tasks: List[str]
    retrieved_chunks: List[Dict[str, Any]]
    timeline: Optional[Dict[str, Any]]
    graph_data: Optional[Dict[str, Any]]
    decision_analysis: Optional[str]
    final_answer: str
    sources: List[Dict[str, Any]]
    confidence_score: float
    processing_steps: List[str]


# ── Agent 1: Planner ───────────────────────────────────────────

def planner_agent(state: AgentState) -> AgentState:
    """Breaks the user question into sub-tasks."""
    question = state["question"]

    text = generate_text(
        f"""You are a query planner for an organizational memory system.
Break this question into 2-4 specific search sub-tasks.
Return ONLY a JSON array of strings.

Question: {question}

Example output:
["Find meetings about topic X", "Find emails discussing Y", "Find Jira tickets related to Z"]

Output:""",
        max_tokens=500,
    )

    import json
    try:
        sub_tasks = json.loads(text.strip())
    except Exception:
        sub_tasks = [question]

    state["sub_tasks"] = sub_tasks
    state["processing_steps"].append(f"Planner: Generated {len(sub_tasks)} sub-tasks")
    return state


# ── Agent 2: Search ────────────────────────────────────────────

def search_agent(state: AgentState) -> AgentState:
    """Searches Pinecone vector DB for relevant chunks."""
    if not init_clients():
        raise RuntimeError("Search agent unavailable: Pinecone/OpenAI clients could not be initialized")

    embeddings_model_local = embeddings_model
    pc_index_local = pc_index
    if embeddings_model_local is None or pc_index_local is None:
        raise RuntimeError("Search agent unavailable: Pinecone/OpenAI clients are not initialized")

    all_chunks = []
    seen_ids = set()

    for task in state["sub_tasks"]:
        # Embed the sub-task query
        query_vector = embeddings_model_local.embed_query(task)

        # Build filter
        filter_dict = {}
        if state.get("project_filter"):
            filter_dict["project"] = {"$eq": state["project_filter"]}

        # Query Pinecone
        results = pc_index_local.query(
            vector=query_vector,
            top_k=5,
            include_metadata=True,
            filter=filter_dict if filter_dict else None,
        )

        for match in results.matches:
            if match.id not in seen_ids:
                seen_ids.add(match.id)
                all_chunks.append({
                    "chunk_id": match.id,
                    "score": match.score,
                    "metadata": match.metadata,
                    "content": match.metadata.get("content", match.metadata.get("content_preview", "")),
                })

    # Sort by relevance score
    all_chunks.sort(key=lambda x: x["score"], reverse=True)
    state["retrieved_chunks"] = all_chunks[:15]
    state["processing_steps"].append(f"Search: Retrieved {len(all_chunks)} chunks")
    return state


# ── Agent 3: Timeline ──────────────────────────────────────────

async def timeline_agent_async(state: AgentState) -> AgentState:
    """Calls Timeline Service to build chronological event sequence."""
    topic = state["question"]
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                f"{TIMELINE_URL}/timeline/{quote(topic, safe='')}",
                params={"project": state.get("project_filter", "")},
            )
            if resp.status_code == 200:
                state["timeline"] = resp.json()
                state["processing_steps"].append("Timeline: Built chronological sequence")
            else:
                state["timeline"] = None
    except Exception as e:
        state["timeline"] = None
        state["processing_steps"].append(f"Timeline: Unavailable ({str(e)[:50]})")
    return state


def timeline_agent(state: AgentState) -> AgentState:
    import asyncio
    return asyncio.get_event_loop().run_until_complete(timeline_agent_async(state))


# ── Agent 4: Decision Analyzer ─────────────────────────────────

def decision_agent(state: AgentState) -> AgentState:
    """Analyzes retrieved context to identify decisions, dissent, and outcomes."""

    chunks_text = "\n---\n".join([
        f"[{c['metadata'].get('doc_type', '?')} | "
        f"{c['metadata'].get('date', '?')} | "
        f"Score: {c['score']:.2f}]\n{c['content']}"
        for c in state["retrieved_chunks"]
    ])

    # Safe handling for Optional timeline
    timeline = state.get("timeline") or {}
    events = timeline.get("events", [])

    timeline_text = ""
    if events:
        timeline_text = "\n".join([
            f"• {e.get('date', '?')}: {e.get('title', '')} — {e.get('description', '')}"
            for e in events
        ])

    analysis_text = generate_text(
        f"""You are analyzing organizational documents to understand decisions made.

Question:
{state['question']}

Retrieved Documents:
{chunks_text}

Timeline of Events:
{timeline_text if timeline_text else 'Not available'}

Analyze and identify:
1. What decision was made (if any)
2. Who was involved and their stance (agreement/dissent/concern)
3. What risks were flagged
4. What the outcome was
5. Confidence score (0.0-1.0) based on evidence quality

Respond in this format:
DECISION: <what was decided>
PARTICIPANTS: <who was involved and their stance>
RISKS_FLAGGED: <risks that were mentioned>
OUTCOME: <what happened after>
CONFIDENCE: <0.0-1.0>
ANALYSIS: <your detailed analysis>""",
        max_tokens=1000,
    )

    state["decision_analysis"] = analysis_text
    state["processing_steps"].append("Decision Agent: Analyzed decisions and dissent")

    # Extract confidence score
    try:
        for line in analysis_text.split("\n"):
            if line.startswith("CONFIDENCE:"):
                score = float(line.split(":")[1].strip())
                state["confidence_score"] = min(max(score, 0.0), 1.0)
    except Exception:
        state["confidence_score"] = 0.5

    return state


# ── Agent 5: Answer Generator ──────────────────────────────────

def answer_agent(state: AgentState) -> AgentState:
    """Synthesizes a final answer with sources and timeline."""
    answer_text = generate_text(
        f"""You are DecisionDNA, an AI organizational memory engine.
Generate a clear, structured answer to the user's question.

Question: {state['question']}

Decision Analysis:
{state.get('decision_analysis', 'No analysis available')}

Format your answer as:
1. A direct answer paragraph
2. Key findings (bullet points)
3. Who was involved
4. What risks were flagged (if any)
5. Outcome (if known)

Be factual, cite document types when mentioning sources.""",
        max_tokens=1500,
    )

    state["final_answer"] = answer_text

    # Build sources list from retrieved chunks
    seen_docs = set()
    sources = []
    for chunk in state["retrieved_chunks"]:
        doc_id = chunk["metadata"].get("doc_id", "")
        if doc_id and doc_id not in seen_docs:
            seen_docs.add(doc_id)
            sources.append({
                "doc_id": doc_id,
                "doc_type": chunk["metadata"].get("doc_type", ""),
                "title": chunk["metadata"].get("title", ""),
                "date": chunk["metadata"].get("date", ""),
                "relevance_score": chunk["score"],
                "excerpt": chunk["content"][:200],
            })

    state["sources"] = sources[:5]
    state["processing_steps"].append("Answer Agent: Generated final response")
    return state


# ── Build LangGraph ────────────────────────────────────────────

def build_graph():
    workflow = StateGraph(AgentState)

    # NOTE: node ids must not collide with AgentState keys (newer LangGraph rejects
    # that), so the timeline node is "timeline_step" while the state key stays "timeline".
    workflow.add_node("planner", planner_agent)
    workflow.add_node("search", search_agent)
    workflow.add_node("timeline_step", timeline_agent)
    workflow.add_node("decision", decision_agent)
    workflow.add_node("answer", answer_agent)

    workflow.set_entry_point("planner")
    workflow.add_edge("planner", "search")
    workflow.add_edge("search", "timeline_step")
    workflow.add_edge("timeline_step", "decision")
    workflow.add_edge("decision", "answer")
    workflow.add_edge("answer", END)

    return workflow.compile()


agent_graph = None


@app.on_event("startup")
async def build_agent():
    global agent_graph
    agent_graph = build_graph()


# ── Models ─────────────────────────────────────────────────────

class QueryRequest(BaseModel):
    question: str
    project_filter: Optional[str] = None


# ── Routes ─────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {
        "service": "query-service",
        "status": "healthy",
        "ready": pc_index is not None,  # False until Pinecone/OpenAI init succeeds
        "pinecone_index": INDEX_NAME,
        "chat_model": CHAT_MODEL,
        "embedding_model": EMBEDDING_MODEL,
        "embedding_dimensions": EMBEDDING_DIM,
    }


@app.post("/query")
async def query(request: QueryRequest):
    if not agent_graph:
        raise HTTPException(status_code=503, detail="Agent not ready")
    if not init_clients():
        raise HTTPException(status_code=503, detail="Upstream (Pinecone/OpenAI) not ready - check API keys and that the index exists")

    initial_state: AgentState = {
        "question": request.question,
        "project_filter": request.project_filter,
        "sub_tasks": [],
        "retrieved_chunks": [],
        "timeline": None,
        "graph_data": None,
        "decision_analysis": None,
        "final_answer": "",
        "sources": [],
        "confidence_score": 0.0,
        "processing_steps": [],
    }

    result = agent_graph.invoke(initial_state)

    return {
        "question": result["question"],
        "answer": result["final_answer"],
        "timeline": result.get("timeline"),
        "sources": result["sources"],
        "confidence_score": result["confidence_score"],
        "processing_steps": result["processing_steps"],
    }
