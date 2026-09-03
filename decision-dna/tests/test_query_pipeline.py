"""query-service: graph topology, async execution, concurrency, contract, parsing."""

import asyncio
import json
import time

import pytest
from langgraph.graph import END, START

from conftest import RecordingIndex, ScriptedChatClient, FakeEmbeddings, load_service_main


@pytest.fixture()
def qs(monkeypatch):
    module = load_service_main("query")
    monkeypatch.setattr(module, "chat_client", None, raising=False)
    monkeypatch.setattr(module, "embeddings_model", None, raising=False)
    monkeypatch.setattr(module, "pc_index", None, raising=False)
    monkeypatch.setattr(module, "query_cache", None, raising=False)
    return module


SYNTH_JSON = json.dumps({
    "decision": "Migrate to Azure Functions",
    "participants": "Ravi (for), Priya (concern)",
    "risks_flagged": "Cold starts",
    "outcome": "Shipped in Q3",
    "confidence": 0.82,
    "analysis": "Long form analysis.",
    "answer": "1. Direct answer.\n2. Key findings.\n3. Who.\n4. Risks.\n5. Outcome.",
})

CHUNKS = [
    {"id": f"DOC-{i}_chunk_0", "score": 0.9 - i * 0.05,
     "metadata": {"doc_id": f"DOC-{i}", "doc_type": "email", "title": f"T{i}",
                  "date": "2024-03-01", "content": f"body of document {i} " * 12}}
    for i in range(12)
]


def _wire(module, monkeypatch, *, chat, embeddings=None, index=None):
    monkeypatch.setattr(module, "chat_client", chat, raising=False)
    monkeypatch.setattr(module, "embeddings_model", embeddings or FakeEmbeddings(), raising=False)
    monkeypatch.setattr(module, "pc_index", index or RecordingIndex(CHUNKS), raising=False)
    monkeypatch.setattr(module, "init_clients", lambda: True, raising=False)


# ── Topology ───────────────────────────────────────────────────

def test_timeline_branch_does_not_depend_on_retrieval(qs):
    """The whole point of the parallelisation: timeline hangs off START."""
    topology = qs.graph_topology()
    assert set(topology[START]) == {"retrieve", "timeline_step"}
    assert "timeline_step" not in topology["retrieve"]


@pytest.mark.asyncio
async def test_search_still_depends_on_planner_output(qs, monkeypatch):
    """search consumes sub_tasks: a real dependency, kept inside `retrieve`."""
    chat = ScriptedChatClient(['["alpha", "beta"]'])
    embeddings = FakeEmbeddings()
    _wire(qs, monkeypatch, chat=chat, embeddings=embeddings)
    update = await qs.retrieve_agent({"question": "q", "project_filter": None, "sub_tasks": []})
    assert update["sub_tasks"] == ["alpha", "beta"]
    assert embeddings.document_batches == [["alpha", "beta"]]


def test_synthesis_joins_both_branches(qs):
    topology = qs.graph_topology()
    assert topology["retrieve"] == ["synthesize"]
    assert topology["timeline_step"] == ["synthesize"]
    assert topology["synthesize"] == [END]


def test_compiled_graph_matches_declared_topology(qs):
    """graph_topology() is documentation; assert it against the compiled graph."""
    graph = qs.build_graph().get_graph()
    actual = {}
    for edge in graph.edges:
        actual.setdefault(edge.source, set()).add(edge.target)
    declared = {k: set(v) for k, v in qs.graph_topology().items()}
    assert actual == declared


def test_decision_and_answer_agents_are_gone(qs):
    """decision_agent + answer_agent merged into a single synthesis node."""
    nodes = set(qs.build_graph().get_graph().nodes) - {START, END}
    assert nodes == {"retrieve", "timeline_step", "synthesize"}
    assert not hasattr(qs, "decision_agent")
    assert not hasattr(qs, "answer_agent")


@pytest.mark.asyncio
async def test_synthesis_runs_exactly_once(qs, monkeypatch):
    """Regression guard for a langgraph 0.1.5 trap.

    A node does not wait for every inbound edge; it runs once per superstep in
    which any inbound edge fires. With branches of unequal depth
    (START->planner->search vs START->timeline_step) the join node executed
    TWICE — an extra LLM generation and duplicated processing_steps. Both
    branches must stay one superstep deep.
    """
    runs = []

    async def counting_synth(state):
        runs.append(1)
        return {"final_answer": "x", "sources": [], "processing_steps": ["synth"]}

    async def fast_retrieve(state):
        return {"sub_tasks": ["a"], "retrieved_chunks": [], "processing_steps": ["retrieve"]}

    async def fast_timeline(state):
        return {"timeline": None, "processing_steps": ["timeline"]}

    monkeypatch.setattr(qs, "retrieve_agent", fast_retrieve)
    monkeypatch.setattr(qs, "timeline_agent", fast_timeline)
    monkeypatch.setattr(qs, "synthesis_agent", counting_synth)

    result = await qs.build_graph().ainvoke({
        "question": "q", "project_filter": None, "sub_tasks": [], "retrieved_chunks": [],
        "timeline": None, "graph_data": None, "decision_analysis": None, "final_answer": "",
        "sources": [], "confidence_score": 0.0, "processing_steps": [], "embeddings_degraded": False,
    })
    assert len(runs) == 1, f"synthesis ran {len(runs)} times"
    assert result["processing_steps"].count("synth") == 1


# ── Async execution ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_ainvoke_runs_the_graph(qs, monkeypatch):
    chat = ScriptedChatClient(['["sub task a", "sub task b"]', SYNTH_JSON])
    _wire(qs, monkeypatch, chat=chat)

    async def fake_timeline(state):
        return {"timeline": {"events": []}, "processing_steps": ["Timeline: Built chronological sequence"]}

    monkeypatch.setattr(qs, "timeline_agent", fake_timeline)
    graph = qs.build_graph()

    result = await graph.ainvoke({
        "question": "why did we pick azure?", "project_filter": None, "sub_tasks": [],
        "retrieved_chunks": [], "timeline": None, "graph_data": None,
        "decision_analysis": None, "final_answer": "", "sources": [],
        "confidence_score": 0.0, "processing_steps": [], "embeddings_degraded": False,
    })
    assert result["final_answer"].startswith("1. Direct answer.")
    assert result["confidence_score"] == pytest.approx(0.82)


@pytest.mark.asyncio
async def test_exactly_two_llm_generations_on_the_critical_path(qs, monkeypatch):
    """planner + synthesis inside query-service. The old chain made four here
    (planner, decision, answer) plus two more inside timeline-service."""
    chat = ScriptedChatClient(['["a", "b", "c"]', SYNTH_JSON])
    _wire(qs, monkeypatch, chat=chat)

    async def fake_timeline(state):
        return {"timeline": None, "processing_steps": ["Timeline: Unavailable (test)"]}

    monkeypatch.setattr(qs, "timeline_agent", fake_timeline)
    await qs.build_graph().ainvoke({
        "question": "q", "project_filter": None, "sub_tasks": [], "retrieved_chunks": [],
        "timeline": None, "graph_data": None, "decision_analysis": None, "final_answer": "",
        "sources": [], "confidence_score": 0.0, "processing_steps": [], "embeddings_degraded": False,
    })
    assert chat.call_count == 2


@pytest.mark.asyncio
async def test_branches_actually_overlap_in_time(qs, monkeypatch):
    """Concurrency proof: two ~0.3s branches must finish in well under 0.6s."""
    async def slow_planner(state):
        await asyncio.sleep(0.3)
        return {"sub_tasks": [state["question"]], "retrieved_chunks": [],
                "processing_steps": ["Planner: Generated 1 sub-tasks"]}

    async def slow_timeline(state):
        await asyncio.sleep(0.3)
        return {"timeline": {"events": []}, "processing_steps": ["Timeline: Built chronological sequence"]}

    async def instant_synth(state):
        return {"final_answer": "done", "sources": [], "processing_steps": ["ok"]}

    monkeypatch.setattr(qs, "retrieve_agent", slow_planner)
    monkeypatch.setattr(qs, "timeline_agent", slow_timeline)
    monkeypatch.setattr(qs, "synthesis_agent", instant_synth)

    start = time.perf_counter()
    await qs.build_graph().ainvoke({
        "question": "q", "project_filter": None, "sub_tasks": [], "retrieved_chunks": [],
        "timeline": None, "graph_data": None, "decision_analysis": None, "final_answer": "",
        "sources": [], "confidence_score": 0.0, "processing_steps": [], "embeddings_degraded": False,
    })
    elapsed = time.perf_counter() - start
    assert elapsed < 0.5, f"branches ran sequentially ({elapsed:.3f}s)"


# ── Retrieval ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_sub_tasks_are_embedded_in_one_batched_request(qs, monkeypatch):
    embeddings = FakeEmbeddings()
    index = RecordingIndex(CHUNKS)
    _wire(qs, monkeypatch, chat=ScriptedChatClient([]), embeddings=embeddings, index=index)

    await qs.search_agent({"question": "q", "project_filter": None,
                           "sub_tasks": ["one", "two", "three"]})

    assert embeddings.request_count == 1, "one HTTP call per sub-task is the old behaviour"
    assert embeddings.document_batches == [["one", "two", "three"]]
    assert index.query_calls == 3, "one vector search per sub-task, run concurrently"


@pytest.mark.asyncio
async def test_pinecone_searches_run_concurrently(qs, monkeypatch):
    calls = []

    def slow_query(vector, filter_dict):
        calls.append(time.perf_counter())
        time.sleep(0.15)
        from conftest import FakeMatch
        from types import SimpleNamespace
        return SimpleNamespace(matches=[FakeMatch(id="x", score=0.5, metadata={"doc_id": "x"})])

    _wire(qs, monkeypatch, chat=ScriptedChatClient([]))
    monkeypatch.setattr(qs, "_pinecone_query", slow_query)

    start = time.perf_counter()
    await qs.search_agent({"question": "q", "project_filter": None,
                           "sub_tasks": ["a", "b", "c", "d"]})
    elapsed = time.perf_counter() - start
    assert len(calls) == 4
    assert elapsed < 0.4, f"four 0.15s searches took {elapsed:.3f}s — they ran serially"


def test_context_selection_caps_chunks_without_touching_sources(qs, monkeypatch):
    chunks = [{"score": 1.0 - i / 100, "metadata": {"doc_id": f"D{i}"}, "content": "x"}
              for i in range(15)]
    monkeypatch.setattr(qs, "DECISION_CONTEXT_CHUNKS", 8)
    monkeypatch.setattr(qs, "RETRIEVAL_MIN_SCORE", 0.0)
    assert len(qs.select_context_chunks(chunks)) == 8
    # sources are still derived from the full retrieved list
    assert len(qs.build_sources(chunks)) == 5


def test_score_threshold_never_empties_the_prompt(qs, monkeypatch):
    monkeypatch.setattr(qs, "DECISION_CONTEXT_CHUNKS", 8)
    monkeypatch.setattr(qs, "RETRIEVAL_MIN_SCORE", 0.99)
    chunks = [{"score": 0.2, "metadata": {"doc_id": "D0"}, "content": "x"}]
    assert len(qs.select_context_chunks(chunks)) == 1


def test_chunk_text_reads_both_metadata_shapes(qs):
    """Vectors indexed before the metadata slimming still carry content_preview."""
    legacy = {"metadata": {"content": "full text", "content_preview": "full te"}}
    new = {"metadata": {"content": "full text"}}
    preview_only = {"metadata": {"content_preview": "just the preview"}}
    assert qs.chunk_text_of(legacy) == "full text"
    assert qs.chunk_text_of(new) == "full text"
    assert qs.chunk_text_of(preview_only) == "just the preview"


# ── Response contract ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_response_contract_is_unchanged(qs, monkeypatch):
    chat = ScriptedChatClient(['["a"]', SYNTH_JSON])
    _wire(qs, monkeypatch, chat=chat)

    async def fake_timeline(state):
        return {"timeline": {"events": [], "outcome_assessment": "ok"},
                "processing_steps": ["Timeline: Built chronological sequence"]}

    monkeypatch.setattr(qs, "timeline_agent", fake_timeline)
    monkeypatch.setattr(qs, "agent_graph", qs.build_graph(), raising=False)

    response = await qs.query(qs.QueryRequest(question="why azure?", project_filter=None))

    assert set(response) == {"question", "answer", "timeline", "sources",
                             "confidence_score", "processing_steps", "degraded"}
    assert response["question"] == "why azure?"
    assert isinstance(response["sources"], list)
    assert isinstance(response["processing_steps"], list)
    assert response["degraded"] is False


@pytest.mark.asyncio
async def test_processing_steps_keep_the_strings_the_ui_renders(qs, monkeypatch):
    chat = ScriptedChatClient(['["a"]', SYNTH_JSON])
    _wire(qs, monkeypatch, chat=chat)

    async def fake_timeline(state):
        return {"timeline": None, "processing_steps": ["Timeline: Built chronological sequence"]}

    monkeypatch.setattr(qs, "timeline_agent", fake_timeline)
    monkeypatch.setattr(qs, "agent_graph", qs.build_graph(), raising=False)
    response = await qs.query(qs.QueryRequest(question="q", project_filter=None))

    steps = "\n".join(response["processing_steps"])
    for expected in ("Planner:", "Search:", "Timeline:",
                     "Decision Agent: Analyzed decisions and dissent",
                     "Answer Agent: Generated final response"):
        assert expected in steps


@pytest.mark.asyncio
async def test_sources_shape_matches_the_previous_answer_agent(qs):
    sources = qs.build_sources(CHUNKS[:3])
    assert set(sources[0]) == {"doc_id", "doc_type", "title", "date",
                               "relevance_score", "excerpt"}
    assert len(sources[0]["excerpt"]) <= 200


@pytest.mark.asyncio
async def test_non_json_synthesis_still_produces_an_answer(qs, monkeypatch):
    """Model ignoring the JSON instruction must not break the contract."""
    chat = ScriptedChatClient(["Plain prose answer.\nCONFIDENCE: 0.4"])
    _wire(qs, monkeypatch, chat=chat)
    update = await qs.synthesis_agent({"question": "q", "retrieved_chunks": CHUNKS[:2],
                                       "timeline": None})
    assert update["final_answer"].startswith("Plain prose answer.")
    assert update["confidence_score"] == pytest.approx(0.4)


# ── Confidence parsing ─────────────────────────────────────────

@pytest.mark.parametrize("text,expected", [
    ("CONFIDENCE: 0.85", 0.85),
    ("CONFIDENCE: 0.5", 0.5),
    ("CONFIDENCE: invalid", 0.5),          # line present but unusable -> old default
    ("CONFIDENCE: text: 0.7", 0.7),        # extra colon used to crash split(":")[1]
    ("confidence: 0.33", 0.33),            # case-insensitive
    ("CONFIDENCE: 1.9", 1.0),              # clamped
    ("CONFIDENCE: -2", 0.0),               # clamped
    ("no confidence line here", None),     # absent -> caller keeps its value
    ("", None),
])
def test_parse_confidence(qs, text, expected):
    assert qs.parse_confidence(text) == expected


def test_confidence_absent_leaves_the_existing_score_untouched(qs):
    """Preserves the old behaviour: a missing CONFIDENCE line changed nothing."""
    assert qs.parse_confidence("DECISION: something") is qs.CONFIDENCE_ABSENT


@pytest.mark.parametrize("raw,expected", [
    ('{"a": 1}', {"a": 1}),
    ('```json\n{"a": 1}\n```', {"a": 1}),
    ('Here you go:\n{"a": 1}\nhope that helps', {"a": 1}),
    ('[1, 2]', [1, 2]),
    ('not json at all', None),
])
def test_extract_json(qs, raw, expected):
    assert qs.extract_json(raw) == expected
