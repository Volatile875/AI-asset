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
from anthropic import Anthropic
from fastapi import FastAPI, HTTPException
from langchain_openai import OpenAIEmbeddings
from langgraph.graph import END, StateGraph
from pinecone import Pinecone
from pydantic import BaseModel

app = FastAPI(title="Query Service", version="1.0.0")

# ── Config ─────────────────────────────────────────────────────
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
OPENAI_API_KEY    = os.getenv("OPENAI_API_KEY")
PINECONE_API_KEY  = os.getenv("PINECONE_API_KEY")
INDEX_NAME        = os.getenv("PINECONE_INDEX_NAME", "ai-asset")
EMBEDDING_MODEL   = os.getenv("EMBEDDING_MODEL", "text-embedding-3-large")
EMBEDDING_DIM     = int(os.getenv("EMBEDDING_DIMENSIONS", "1024"))
TIMELINE_URL      = os.getenv("TIMELINE_SERVICE_URL", "http://timeline-service:8005")
GRAPH_URL         = os.getenv("GRAPH_SERVICE_URL",    "http://graph-service:8003")

claude_client = None
embeddings_model = None
pc_index = None


@app.on_event("startup")
async def startup():
    global claude_client, embeddings_model, pc_index
    claude_client = Anthropic(api_key=ANTHROPIC_API_KEY)
    embeddings_model = OpenAIEmbeddings(
        model=EMBEDDING_MODEL,
        dimensions=EMBEDDING_DIM,
        openai_api_key=OPENAI_API_KEY,
    )
    pc = Pinecone(api_key=PINECONE_API_KEY)
    pc_index = pc.Index(INDEX_NAME)


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

    response = claude_client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=500,
        messages=[{
            "role": "user",
            "content": f"""You are a query planner for an organizational memory system.
Break this question into 2-4 specific search sub-tasks.
Return ONLY a JSON array of strings.

Question: {question}

Example output:
["Find meetings about topic X", "Find emails discussing Y", "Find Jira tickets related to Z"]

Output:"""
        }]
    )

    import json
    try:
        text = response.content[0].text.strip()
        sub_tasks = json.loads(text)
    except Exception:
        sub_tasks = [question]

    state["sub_tasks"] = sub_tasks
    state["processing_steps"].append(f"Planner: Generated {len(sub_tasks)} sub-tasks")
    return state


# ── Agent 2: Search ────────────────────────────────────────────

def search_agent(state: AgentState) -> AgentState:
    """Searches Pinecone vector DB for relevant chunks."""
    all_chunks = []
    seen_ids = set()

    for task in state["sub_tasks"]:
        # Embed the sub-task query
        query_vector = embeddings_model.embed_query(task)

        # Build filter
        filter_dict = {}
        if state.get("project_filter"):
            filter_dict["project"] = {"$eq": state["project_filter"]}

        # Query Pinecone
        results = pc_index.query(
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
        f"[{c['metadata'].get('doc_type', '?')} | {c['metadata'].get('date', '?')} | Score: {c['score']:.2f}]\n{c['content']}"
        for c in state["retrieved_chunks"]
    ])

    timeline_text = ""
    if state.get("timeline"):
        events = state["timeline"].get("events", [])
        timeline_text = "\n".join([
            f"• {e.get('date', '?')}: {e.get('title', '')} — {e.get('description', '')}"
            for e in events
        ])

    response = claude_client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1000,
        messages=[{
            "role": "user",
            "content": f"""You are analyzing organizational documents to understand decisions made.

Question: {state['question']}

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
ANALYSIS: <your detailed analysis>"""
        }]
    )

    state["decision_analysis"] = response.content[0].text
    state["processing_steps"].append("Decision Agent: Analyzed decisions and dissent")

    # Extract confidence score
    try:
        for line in response.content[0].text.split("\n"):
            if line.startswith("CONFIDENCE:"):
                score = float(line.split(":")[1].strip())
                state["confidence_score"] = min(max(score, 0.0), 1.0)
    except Exception:
        state["confidence_score"] = 0.5

    return state


# ── Agent 5: Answer Generator ──────────────────────────────────

def answer_agent(state: AgentState) -> AgentState:
    """Synthesizes a final answer with sources and timeline."""
    response = claude_client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1500,
        messages=[{
            "role": "user",
            "content": f"""You are DecisionDNA, an AI organizational memory engine.
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

Be factual, cite document types when mentioning sources."""
        }]
    )

    state["final_answer"] = response.content[0].text

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

    workflow.add_node("planner", planner_agent)
    workflow.add_node("search", search_agent)
    workflow.add_node("timeline", timeline_agent)
    workflow.add_node("decision", decision_agent)
    workflow.add_node("answer", answer_agent)

    workflow.set_entry_point("planner")
    workflow.add_edge("planner", "search")
    workflow.add_edge("search", "timeline")
    workflow.add_edge("timeline", "decision")
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
        "pinecone_index": INDEX_NAME,
        "embedding_model": EMBEDDING_MODEL,
        "embedding_dimensions": EMBEDDING_DIM,
    }


@app.post("/query")
async def query(request: QueryRequest):
    if not agent_graph:
        raise HTTPException(status_code=503, detail="Agent not ready")

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
