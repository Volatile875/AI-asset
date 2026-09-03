"""Drive the ORIGINAL (pre-optimisation) pipeline against the simulated provider.

Runs in its own process so the `app` package resolves to the baseline copy.
"""

import asyncio
import copy
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

BENCH = Path(__file__).resolve().parent
WORK = BENCH.parent
REPO = WORK                     # bench/ lives inside the repo
sys.path.insert(0, str(BENCH))

BASELINE = WORK.parent / "baseline"
if not (BASELINE / "query-service" / "app" / "main.py").exists():
    sys.stderr.write(
        "bench: no baseline checkout found at %s\n"
        "Recreate it from git to reproduce the 'before' column, e.g.\n"
        "  git worktree add ../baseline-repo <commit-before-optimisation>\n"
        "  mkdir -p ../baseline && cp -r ../baseline-repo/decision-dna/services/query-service \\\n"
        "        ../baseline-repo/decision-dna/services/timeline-service ../baseline/\n" % BASELINE)
    raise SystemExit(3)

from model import (Meter, chat_latency, EMBED_LATENCY_S, PINECONE_LATENCY_S,
                   reply_for, load_chunks)

METER = Meter()
CHUNKS = load_chunks(REPO, limit=15)

# ── Baseline timeline-service (in-process) ─────────────────────
sys.path.insert(0, str(WORK.parent / "baseline" / "timeline-service"))
import app.main as ts_old            # noqa: E402
sys.path.pop(0)
TS_MODULES = {k: v for k, v in sys.modules.items() if k == "app" or k.startswith("app.")}
for k in list(TS_MODULES):
    del sys.modules[k]

# ── Baseline query-service (in-process) ────────────────────────
sys.path.insert(0, str(WORK.parent / "baseline" / "query-service"))
import app.main as qs_old            # noqa: E402
sys.path.pop(0)


# ── Sync stand-ins matching the ORIGINAL blocking call style ───

class SyncChat:
    def __init__(self):
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, model, messages, max_tokens=None, temperature=None, **kw):
        prompt = messages[-1]["content"]
        METER.record_chat(max_tokens, prompt)
        time.sleep(chat_latency(max_tokens))   # blocking, exactly as before
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=reply_for(prompt)))])


class SyncEmbeddings:
    def embed_query(self, text):
        METER.record_embed(1)                  # one HTTP request per sub-task
        time.sleep(EMBED_LATENCY_S)
        return [0.1] * 8

    def embed_documents(self, texts):
        METER.record_embed(len(texts))
        time.sleep(EMBED_LATENCY_S)
        return [[0.1] * 8 for _ in texts]


class SyncIndex:
    """Each sub-task retrieves a different slice, as distinct queries would."""

    def __init__(self):
        self._n = 0

    def query(self, vector, top_k=5, include_metadata=True, filter=None):
        METER.record_pinecone()
        time.sleep(PINECONE_LATENCY_S)         # blocking, serial in the old loop
        offset = (self._n * 5) % max(1, len(CHUNKS))
        self._n += 1
        window = (CHUNKS[offset:] + CHUNKS[:offset])[:top_k]
        matches = [SimpleNamespace(id=c["id"], score=c["score"], metadata=c["metadata"])
                   for c in window]
        return SimpleNamespace(matches=matches)


def wire():
    for mod in (qs_old, ts_old):
        mod.openai_client = SyncChat()
        mod.embeddings_model = SyncEmbeddings()
        mod.pc_index = SyncIndex()
        mod.init_clients = lambda: True

    # timeline_agent normally does an HTTP GET to timeline-service; call the
    # baseline timeline-service in-process instead so both ends are measured.
    async def timeline_in_process(state):
        result = await ts_old.build_timeline(state["question"], state.get("project_filter") or "")
        state["timeline"] = json.loads(result.model_dump_json())
        state["processing_steps"].append("Timeline: Built chronological sequence")
        return state

    qs_old.timeline_agent_async = timeline_in_process

    # Instrument the decision agent to record how much context reaches the prompt.
    original_decision = qs_old.decision_agent

    def counting_decision(state):
        METER.retrieved_chunks = len(state.get("retrieved_chunks", []))
        METER.context_chunks = len(state.get("retrieved_chunks", []))  # old code sent them all
        return original_decision(state)

    qs_old.decision_agent = counting_decision
    qs_old.agent_graph = qs_old.build_graph()


INITIAL = {
    "question": "Why did we decide to migrate from AWS Lambda to Azure Functions?",
    "project_filter": None, "sub_tasks": [], "retrieved_chunks": [], "timeline": None,
    "graph_data": None, "decision_analysis": None, "final_answer": "", "sources": [],
    "confidence_score": 0.0, "processing_steps": [], "embeddings_degraded": False,
}


async def single():
    start = time.perf_counter()
    result = qs_old.agent_graph.invoke(copy.deepcopy(INITIAL))     # SYNCHRONOUS invoke
    elapsed = time.perf_counter() - start
    return result, elapsed


async def concurrent(n):
    """The old route was `async def` calling the blocking `.invoke()`, so the
    provider sleeps run on the event-loop thread and requests serialise."""
    async def one():
        return qs_old.agent_graph.invoke(copy.deepcopy(INITIAL))
    start = time.perf_counter()
    await asyncio.gather(*(one() for _ in range(n)))
    return time.perf_counter() - start


def main():
    wire()
    result, elapsed = asyncio.run(single())
    summary = METER.summary(elapsed)
    summary["answer_chars"] = len(result["final_answer"])
    summary["sources"] = len(result["sources"])
    summary["confidence"] = result["confidence_score"]
    summary["response_keys"] = sorted(["question", "answer", "timeline", "sources",
                                       "confidence_score", "processing_steps", "degraded"])
    summary["processing_steps"] = result["processing_steps"]

    n = 4
    summary["concurrent_n"] = n
    summary["concurrent_wall_clock_s"] = round(asyncio.run(concurrent(n)), 3)
    print(json.dumps(summary))


if __name__ == "__main__":
    main()
