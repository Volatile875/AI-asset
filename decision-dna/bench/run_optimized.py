"""Drive the OPTIMISED pipeline against the identical simulated provider.

Same latency model, same corpus, same canned responses as run_baseline.py.
Runs in its own process so `app` resolves to the optimised copy.
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

from model import (Meter, chat_latency, EMBED_LATENCY_S, PINECONE_LATENCY_S,
                   reply_for, load_chunks)

METER = Meter()
CHUNKS = load_chunks(REPO, limit=15)

sys.path.insert(0, str(REPO / "services" / "timeline-service"))
import app.main as ts_new            # noqa: E402
sys.path.pop(0)
for k in [m for m in list(sys.modules) if m == "app" or m.startswith("app.")]:
    del sys.modules[k]

sys.path.insert(0, str(REPO / "services" / "query-service"))
import app.main as qs_new            # noqa: E402
sys.path.pop(0)


# ── Async stand-ins matching the OPTIMISED call style ──────────

class AsyncChat:
    def __init__(self):
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, model, messages, max_tokens=None, temperature=None, **kw):
        prompt = messages[-1]["content"]
        METER.record_chat(max_tokens, prompt)
        await asyncio.sleep(chat_latency(max_tokens))   # awaitable: frees the loop
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=reply_for(prompt)))])


class AsyncEmbeddings:
    async def aembed_documents(self, texts):
        METER.record_embed(len(texts))                  # ONE request for the batch
        await asyncio.sleep(EMBED_LATENCY_S)
        return [[0.1] * 8 for _ in texts]

    async def aembed_query(self, text):
        METER.record_embed(1)
        await asyncio.sleep(EMBED_LATENCY_S)
        return [0.1] * 8


class ThreadedIndex:
    """Called via asyncio.to_thread, so blocking here does not stall the loop."""

    def __init__(self):
        self._n = 0

    def query(self, vector, top_k=5, include_metadata=True, filter=None):
        METER.record_pinecone()
        time.sleep(PINECONE_LATENCY_S)
        offset = (self._n * 5) % max(1, len(CHUNKS))
        self._n += 1
        window = (CHUNKS[offset:] + CHUNKS[:offset])[:top_k]
        matches = [SimpleNamespace(id=c["id"], score=c["score"], metadata=c["metadata"])
                   for c in window]
        return SimpleNamespace(matches=matches)


def wire():
    for mod in (qs_new, ts_new):
        mod.chat_client = AsyncChat()
        mod.embeddings_model = AsyncEmbeddings()
        mod.pc_index = ThreadedIndex()
        mod.init_clients = lambda: True
    qs_new.query_cache = None          # measure the cold path, not a cache hit
    ts_new.timeline_cache = None

    async def timeline_in_process(state):
        result = await ts_new.build_timeline(state["question"], state.get("project_filter") or "")
        return {"timeline": json.loads(result.model_dump_json()),
                "processing_steps": ["Timeline: Built chronological sequence"]}

    qs_new.timeline_agent = timeline_in_process

    original_synth = qs_new.synthesis_agent

    async def counting_synth(state):
        METER.retrieved_chunks = len(state.get("retrieved_chunks", []))
        METER.context_chunks = len(qs_new.select_context_chunks(state.get("retrieved_chunks", [])))
        return await original_synth(state)

    qs_new.synthesis_agent = counting_synth
    qs_new.agent_graph = qs_new.build_graph()


INITIAL = {
    "question": "Why did we decide to migrate from AWS Lambda to Azure Functions?",
    "project_filter": None, "sub_tasks": [], "retrieved_chunks": [], "timeline": None,
    "graph_data": None, "decision_analysis": None, "final_answer": "", "sources": [],
    "confidence_score": 0.0, "processing_steps": [], "embeddings_degraded": False,
}


async def single():
    start = time.perf_counter()
    result = await qs_new.agent_graph.ainvoke(copy.deepcopy(INITIAL))   # ASYNC invoke
    return result, time.perf_counter() - start


async def concurrent(n):
    async def one():
        return await qs_new.agent_graph.ainvoke(copy.deepcopy(INITIAL))
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
