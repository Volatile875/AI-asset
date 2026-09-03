"""timeline-service merge + async, gateway health concurrency, rate limiter."""

import asyncio
import json
import time
from types import SimpleNamespace

import pytest

from conftest import RecordingIndex, ScriptedChatClient, FakeEmbeddings, load_service_main


# ══════════════════════════════════════════════════════════════
#  timeline-service
# ══════════════════════════════════════════════════════════════

@pytest.fixture()
def ts(monkeypatch):
    module = load_service_main("timeline")
    monkeypatch.setattr(module, "timeline_cache", None, raising=False)
    return module


TIMELINE_JSON = json.dumps({
    "events": [
        {"date": "2024-03-05", "event_type": "decision", "title": "Chose Azure",
         "description": "Team agreed.", "participants": ["Ravi"], "sentiment": "agreement",
         "is_critical": True, "doc_id": "EMAIL-002"},
        {"date": "2024-01-10", "event_type": "discussion", "title": "Cost review",
         "description": "Compared bills.", "participants": ["Priya"], "sentiment": "concern",
         "is_critical": False, "doc_id": "MTG-001"},
    ],
    "outcome": "Migration completed on schedule. Costs fell 18%.",
    "confidence": 0.77,
})

MATCHES = [{"id": f"D{i}_chunk_0", "score": 0.9 - i / 50,
            "metadata": {"doc_id": f"D{i}", "doc_type": "email", "date": "2024-02-01",
                         "content": f"document body {i} " * 10}}
           for i in range(6)]


@pytest.mark.asyncio
async def test_timeline_uses_one_llm_call_not_two(ts, monkeypatch):
    """extract_events + assess_outcome were two sequential generations, the second
    summarising what the first had just produced."""
    chat = ScriptedChatClient([TIMELINE_JSON])
    monkeypatch.setattr(ts, "chat_client", chat, raising=False)
    monkeypatch.setattr(ts, "embeddings_model", FakeEmbeddings(), raising=False)
    monkeypatch.setattr(ts, "pc_index", RecordingIndex(MATCHES), raising=False)
    monkeypatch.setattr(ts, "init_clients", lambda: True, raising=False)

    result = await ts.build_timeline("azure migration", "")
    assert chat.call_count == 1
    assert len(result.events) == 2
    assert result.outcome_assessment.startswith("Migration completed")
    assert result.confidence_score == pytest.approx(0.77)


@pytest.mark.asyncio
async def test_timeline_response_contract_is_unchanged(ts, monkeypatch):
    chat = ScriptedChatClient([TIMELINE_JSON])
    monkeypatch.setattr(ts, "chat_client", chat, raising=False)
    monkeypatch.setattr(ts, "embeddings_model", FakeEmbeddings(), raising=False)
    monkeypatch.setattr(ts, "pc_index", RecordingIndex(MATCHES), raising=False)
    monkeypatch.setattr(ts, "init_clients", lambda: True, raising=False)

    payload = (await ts.build_timeline("topic", "")).model_dump()
    assert set(payload) == {"topic", "events", "outcome_assessment",
                            "confidence_score", "total_documents"}
    event = payload["events"][0]
    assert set(event) == {"event_id", "date", "event_type", "title", "description",
                          "participants", "doc_id", "sentiment", "is_critical", "icon"}


@pytest.mark.asyncio
async def test_timeline_events_are_sorted_by_date(ts, monkeypatch):
    chat = ScriptedChatClient([TIMELINE_JSON])
    monkeypatch.setattr(ts, "chat_client", chat, raising=False)
    monkeypatch.setattr(ts, "embeddings_model", FakeEmbeddings(), raising=False)
    monkeypatch.setattr(ts, "pc_index", RecordingIndex(MATCHES), raising=False)
    monkeypatch.setattr(ts, "init_clients", lambda: True, raising=False)
    result = await ts.build_timeline("topic", "")
    assert [e.date for e in result.events] == ["2024-01-10", "2024-03-05"]


@pytest.mark.asyncio
async def test_empty_corpus_short_circuits_without_an_llm_call(ts, monkeypatch):
    chat = ScriptedChatClient([])
    monkeypatch.setattr(ts, "chat_client", chat, raising=False)
    monkeypatch.setattr(ts, "embeddings_model", FakeEmbeddings(), raising=False)
    monkeypatch.setattr(ts, "pc_index", RecordingIndex([]), raising=False)
    monkeypatch.setattr(ts, "init_clients", lambda: True, raising=False)
    result = await ts.build_timeline("nothing here", "")
    assert result.events == []
    assert result.total_documents == 0
    assert chat.call_count == 0


def test_legacy_event_array_shape_is_still_accepted(ts):
    """If the model returns the old bare array, keep the previous defaults."""
    events, outcome, confidence = ts.normalise_timeline_payload([{"title": "x"}])
    assert events == [{"title": "x"}]
    assert outcome is None and confidence is None


def test_preview_derives_from_content_when_absent(ts):
    assert ts.chunk_preview_of({"metadata": {"content_preview": "old shape"}}) == "old shape"
    assert ts.chunk_preview_of({"metadata": {"content": "x" * 500}}) == "x" * 200


@pytest.mark.asyncio
async def test_pinecone_search_does_not_block_the_event_loop(ts, monkeypatch):
    """The route was async while calling sync helpers, so one timeline blocked
    every other request. Two searches must now overlap."""
    def slow_query(vector, filter_dict, top_k):
        time.sleep(0.2)
        return SimpleNamespace(matches=[])

    monkeypatch.setattr(ts, "embeddings_model", FakeEmbeddings(), raising=False)
    monkeypatch.setattr(ts, "pc_index", object(), raising=False)
    monkeypatch.setattr(ts, "_pinecone_query", slow_query)

    start = time.perf_counter()
    await asyncio.gather(ts.search_pinecone("a"), ts.search_pinecone("b"))
    elapsed = time.perf_counter() - start
    assert elapsed < 0.35, f"searches serialised on the event loop ({elapsed:.3f}s)"


# ══════════════════════════════════════════════════════════════
#  api-gateway
# ══════════════════════════════════════════════════════════════

@pytest.fixture()
def gw(monkeypatch):
    module = load_service_main("gateway")
    monkeypatch.setattr(module, "_health_cache", None, raising=False)
    return module


class SlowHealthClient:
    """Fake httpx client whose /health probes each take `delay` seconds."""

    def __init__(self, delay=0.2, statuses=None):
        self.delay = delay
        self.statuses = statuses or {}
        self.concurrent = 0
        self.max_concurrent = 0

    async def get(self, url, timeout=None, **kwargs):
        self.concurrent += 1
        self.max_concurrent = max(self.max_concurrent, self.concurrent)
        try:
            await asyncio.sleep(self.delay)
            status = next((v for k, v in self.statuses.items() if k in url), 200)
            return SimpleNamespace(status_code=status)
        finally:
            self.concurrent -= 1


@pytest.mark.asyncio
async def test_health_probes_run_concurrently(gw, monkeypatch):
    """Was a for-loop with a 5s timeout each: 25s worst case for five services."""
    client = SlowHealthClient(delay=0.2)
    monkeypatch.setattr(gw, "http_client", client, raising=False)
    monkeypatch.setattr(gw, "HEALTH_CACHE_TTL_S", 0.0, raising=False)

    start = time.perf_counter()
    payload = await gw.gateway_health()
    elapsed = time.perf_counter() - start

    assert len(payload["services"]) == 5
    assert client.max_concurrent == 5, "probes did not overlap"
    assert elapsed < 0.5, f"five 0.2s probes took {elapsed:.3f}s — sequential"


@pytest.mark.asyncio
async def test_health_reports_each_service_state(gw, monkeypatch):
    client = SlowHealthClient(delay=0.0, statuses={"8003": 500})
    monkeypatch.setattr(gw, "http_client", client, raising=False)
    monkeypatch.setattr(gw, "HEALTH_CACHE_TTL_S", 0.0, raising=False)
    payload = await gw.gateway_health()
    assert payload["services"]["graph"] == "degraded"
    assert payload["services"]["query"] == "healthy"


@pytest.mark.asyncio
async def test_unreachable_service_is_reported_not_raised(gw, monkeypatch):
    class Boom:
        async def get(self, url, timeout=None, **kwargs):
            raise ConnectionError("refused")

    monkeypatch.setattr(gw, "http_client", Boom(), raising=False)
    monkeypatch.setattr(gw, "HEALTH_CACHE_TTL_S", 0.0, raising=False)
    payload = await gw.gateway_health()
    assert set(payload["services"].values()) == {"unreachable"}
    assert payload["gateway"] == "healthy"


@pytest.mark.asyncio
async def test_health_result_is_cached_for_its_ttl(gw, monkeypatch):
    client = SlowHealthClient(delay=0.0)
    calls = {"n": 0}
    original = client.get

    async def counting_get(url, timeout=None, **kwargs):
        calls["n"] += 1
        return await original(url, timeout=timeout, **kwargs)

    client.get = counting_get
    monkeypatch.setattr(gw, "http_client", client, raising=False)
    monkeypatch.setattr(gw, "HEALTH_CACHE_TTL_S", 30.0, raising=False)

    await gw.gateway_health()
    await gw.gateway_health()
    await gw.gateway_health()
    assert calls["n"] == 5, "cache should have served the 2nd and 3rd calls"


@pytest.mark.asyncio
async def test_health_timeout_is_short(gw):
    assert gw.HEALTH_TIMEOUT_S <= 2.0
    assert gw.PROXY_TIMEOUT_S >= 90.0, "must stay above the frontend's query deadline"


# ── Rate limiter ───────────────────────────────────────────────

class FakeRedisPipeline:
    def __init__(self, store, round_trips):
        self.store = store
        self.round_trips = round_trips
        self.ops = []

    def incr(self, key):
        self.ops.append(("incr", key))

    def expire(self, key, seconds, nx=False):
        self.ops.append(("expire", key, seconds, nx))

    async def execute(self):
        self.round_trips.append(len(self.ops))
        results = []
        for op in self.ops:
            if op[0] == "incr":
                self.store[op[1]] = self.store.get(op[1], 0) + 1
                results.append(self.store[op[1]])
            else:
                results.append(True)
        return results


class FakeRedis:
    def __init__(self):
        self.store = {}
        self.round_trips = []

    def pipeline(self, transaction=False):
        return FakeRedisPipeline(self.store, self.round_trips)


@pytest.mark.asyncio
async def test_rate_limiter_uses_one_round_trip(gw, monkeypatch):
    fake = FakeRedis()
    monkeypatch.setattr(gw, "redis_client", fake, raising=False)
    request = SimpleNamespace(client=SimpleNamespace(host="10.0.0.1"))
    await gw.rate_limit(request)
    assert len(fake.round_trips) == 1, "INCR and EXPIRE should travel together"
    assert fake.round_trips[0] == 2


@pytest.mark.asyncio
async def test_rate_limit_semantics_are_unchanged(gw, monkeypatch):
    """Still 100 requests per IP per window; the 101st is rejected."""
    from fastapi import HTTPException

    fake = FakeRedis()
    monkeypatch.setattr(gw, "redis_client", fake, raising=False)
    request = SimpleNamespace(client=SimpleNamespace(host="10.0.0.2"))
    for _ in range(gw.RATE_LIMIT_MAX):
        await gw.rate_limit(request)
    with pytest.raises(HTTPException) as excinfo:
        await gw.rate_limit(request)
    assert excinfo.value.status_code == 429


@pytest.mark.asyncio
async def test_rate_limiter_failure_does_not_reject_traffic(gw, monkeypatch):
    class BrokenRedis:
        def pipeline(self, transaction=False):
            raise ConnectionError("redis down")

    monkeypatch.setattr(gw, "redis_client", BrokenRedis(), raising=False)
    request = SimpleNamespace(client=SimpleNamespace(host="10.0.0.3"))
    await gw.rate_limit(request)  # must not raise
