"""Shared simulated-provider latency model and instrumentation.

The real Groq/OpenAI/Pinecone endpoints are NOT called: this measures the two
pipelines against identical, deterministic stand-ins so the comparison isolates
the code change rather than provider jitter or quota state.

Counts (LLM calls, embedding requests, Pinecone queries, prompt characters) are
EXACT — they come from the real code paths. Wall-clock numbers are SIMULATED
under the model below and should be read as ratios, not absolutes.
"""

import json
import re
import threading

# Simulated provider latency.
#   chat:      fixed round-trip + per-token generation
#   embedding: one HTTPS round-trip
#   pinecone:  one query round-trip
CHAT_OVERHEAD_S = 0.25
CHAT_PER_TOKEN_S = 0.0012      # ~830 tok/s, in the range Groq llama-3.3-70b serves
EMBED_LATENCY_S = 0.08
PINECONE_LATENCY_S = 0.06


def chat_latency(max_tokens: int) -> float:
    return CHAT_OVERHEAD_S + (max_tokens or 0) * CHAT_PER_TOKEN_S


class Meter:
    """Thread-safe counters shared across sync and async call paths."""

    def __init__(self):
        self._lock = threading.Lock()
        self.chat_calls = []          # [{"max_tokens": int, "prompt_chars": int}]
        self.embed_requests = 0       # HTTP requests, not texts
        self.embed_texts = 0
        self.pinecone_queries = 0
        self.retrieved_chunks = 0
        self.context_chunks = 0

    def record_chat(self, max_tokens, prompt):
        with self._lock:
            self.chat_calls.append({"max_tokens": max_tokens or 0,
                                    "prompt_chars": len(prompt or "")})

    def record_embed(self, n_texts):
        with self._lock:
            self.embed_requests += 1
            self.embed_texts += n_texts

    def record_pinecone(self):
        with self._lock:
            self.pinecone_queries += 1

    def summary(self, wall_clock_s):
        # ~4 characters per token is the usual rough English/code ratio; this is
        # an estimate and is labelled as one in the report.
        prompt_chars = sum(c["prompt_chars"] for c in self.chat_calls)
        return {
            "llm_generations": len(self.chat_calls),
            "max_tokens_budget": sum(c["max_tokens"] for c in self.chat_calls),
            "prompt_chars": prompt_chars,
            "prompt_tokens_est": round(prompt_chars / 4),
            "embedding_requests": self.embed_requests,
            "embedding_texts": self.embed_texts,
            "pinecone_queries": self.pinecone_queries,
            "retrieved_chunks": self.retrieved_chunks,
            "context_chunks": self.context_chunks,
            "wall_clock_s": round(wall_clock_s, 3),
            "per_call": list(self.chat_calls),
        }


# ── Canned provider responses, keyed off prompt content ────────

PLANNER_REPLY = json.dumps([
    "Find meetings about the Azure migration decision",
    "Find emails discussing AWS versus Azure cost",
    "Find Jira tickets related to the migration rollout",
])

TIMELINE_EVENTS = [
    {"date": "2024-01-10", "event_type": "discussion", "title": "Cost comparison raised",
     "description": "Team compared AWS and Azure bills.", "participants": ["Priya"],
     "sentiment": "concern", "is_critical": False, "doc_id": "EMAIL-004"},
    {"date": "2024-03-05", "event_type": "decision", "title": "Azure Functions chosen",
     "description": "Decision ratified in the architecture review.", "participants": ["Ravi"],
     "sentiment": "agreement", "is_critical": True, "doc_id": "MTG-011"},
]

LEGACY_TIMELINE_REPLY = json.dumps(TIMELINE_EVENTS)
MERGED_TIMELINE_REPLY = json.dumps({
    "events": TIMELINE_EVENTS,
    "outcome": "The migration completed in Q3 with an 18% cost reduction.",
    "confidence": 0.78,
})
OUTCOME_REPLY = ("OUTCOME: The migration completed in Q3 with an 18% cost reduction.\n"
                 "CONFIDENCE: 0.78")
DECISION_REPLY = ("DECISION: Migrate from AWS Lambda to Azure Functions\n"
                  "PARTICIPANTS: Ravi (proposer), Priya (raised cold-start concerns)\n"
                  "RISKS_FLAGGED: Cold starts, reporting query breakage\n"
                  "OUTCOME: Completed in Q3\n"
                  "CONFIDENCE: 0.78\n"
                  "ANALYSIS: The trail is well documented across email and meeting notes.")
ANSWER_REPLY = ("1. The team moved to Azure Functions for cost reasons.\n"
                "2. Key findings: ...\n3. Who: Ravi, Priya\n4. Risks: cold starts\n"
                "5. Outcome: completed Q3")
SYNTHESIS_REPLY = json.dumps({
    "decision": "Migrate from AWS Lambda to Azure Functions",
    "participants": "Ravi (proposer), Priya (raised cold-start concerns)",
    "risks_flagged": "Cold starts, reporting query breakage",
    "outcome": "Completed in Q3",
    "confidence": 0.78,
    "analysis": "The trail is well documented across email and meeting notes.",
    "answer": ANSWER_REPLY,
})


def reply_for(prompt: str) -> str:
    """Pick the canned response the prompt is asking for."""
    if "query planner" in prompt:
        return PLANNER_REPLY
    if "Return ONLY a JSON object with exactly these keys" in prompt and '"events"' in prompt:
        return MERGED_TIMELINE_REPLY
    if "Extract a chronological timeline" in prompt:
        return LEGACY_TIMELINE_REPLY
    if prompt.lstrip().startswith("Based on this event sequence"):
        return OUTCOME_REPLY
    if "You are DecisionDNA" in prompt and '"answer"' in prompt:
        return SYNTHESIS_REPLY
    if "Respond in this format:" in prompt:
        return DECISION_REPLY
    if "You are DecisionDNA" in prompt:
        return ANSWER_REPLY
    return "unrecognised prompt"


# ── Corpus: real chunks from the restored synthetic dataset ────

def load_chunks(repo_root, limit=15):
    """Build realistic Pinecone matches from the actual ingested corpus."""
    import sys
    from pathlib import Path

    ingestion = str(Path(repo_root) / "services" / "ingestion-service")
    sys.path.insert(0, ingestion)
    for name in [m for m in list(sys.modules) if m == "app" or m.startswith("app.")]:
        del sys.modules[name]
    from app.parsers import parse_emails
    docs = parse_emails(str(Path(repo_root) / "data" / "synthetic" / "emails"))
    sys.path.remove(ingestion)
    for name in [m for m in list(sys.modules) if m == "app" or m.startswith("app.")]:
        del sys.modules[name]

    from langchain.text_splitter import RecursiveCharacterTextSplitter
    splitter = RecursiveCharacterTextSplitter(chunk_size=800, chunk_overlap=100,
                                              separators=["\n\n", "\n", ". ", " ", ""])
    matches = []
    for doc in docs:
        for i, text in enumerate(splitter.split_text(doc["content"])):
            matches.append({
                "id": f"{doc['doc_id']}_chunk_{i}",
                "score": max(0.05, 0.92 - len(matches) * 0.012),
                "metadata": {
                    "doc_id": doc["doc_id"], "doc_type": doc["doc_type"],
                    "title": doc["title"], "date": doc["date"],
                    "project": doc.get("project") or "",
                    "chunk_index": i,
                    "content": text,
                    "content_preview": text[:200],
                },
            })
            if len(matches) >= limit:
                return matches
    return matches
