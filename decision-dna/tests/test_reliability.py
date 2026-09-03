"""Reliability: bounded retries, no permanent downgrade, no index poisoning."""

import asyncio
from types import SimpleNamespace

import pytest

from conftest import RecordingIndex, ScriptedChatClient, FakeEmbeddings, load_service_main


# ── Bounded retry, no permanent client swap (query-service) ────

@pytest.fixture()
def qs():
    return load_service_main("query")


@pytest.mark.asyncio
async def test_transient_failure_is_retried_then_succeeds(qs, monkeypatch):
    chat = ScriptedChatClient([TimeoutError("boom"), "recovered"])
    monkeypatch.setattr(qs, "chat_client", chat, raising=False)
    assert await qs.generate_text("hello", 10) == "recovered"
    assert chat.call_count == 2


@pytest.mark.asyncio
async def test_transient_failure_does_not_permanently_replace_the_client(qs, monkeypatch):
    """The old code assigned FallbackOpenAIClient to the module global on ANY
    exception, for the life of the process. One 429 and every later answer was
    canned text served with HTTP 200."""
    from app.fallbacks import FallbackOpenAIClient

    chat = ScriptedChatClient([TimeoutError("t1"), TimeoutError("t2"), TimeoutError("t3")])
    monkeypatch.setattr(qs, "chat_client", chat, raising=False)

    with pytest.raises(qs.LLMUnavailable):
        await qs.generate_text("hello", 10)

    assert qs.chat_client is chat
    assert not isinstance(qs.chat_client, FallbackOpenAIClient)

    # The very next request uses the real client again and works.
    chat._responses.append("back to normal")
    assert await qs.generate_text("hello again", 10) == "back to normal"


@pytest.mark.asyncio
async def test_retry_budget_is_bounded(qs, monkeypatch):
    """A provider outage must fail fast, not retry forever."""
    import app.llm as llm

    chat = ScriptedChatClient([TimeoutError("x")] * 20)
    monkeypatch.setattr(qs, "chat_client", chat, raising=False)
    monkeypatch.setattr(llm, "LLM_MAX_ATTEMPTS", 3, raising=False)
    with pytest.raises(qs.LLMUnavailable) as excinfo:
        await qs.generate_text("hello", 10)
    assert chat.call_count == 3
    assert excinfo.value.attempts == 3


@pytest.mark.asyncio
async def test_exhausted_provider_surfaces_as_503_not_a_fake_answer(qs, monkeypatch):
    from fastapi import HTTPException

    chat = ScriptedChatClient([TimeoutError("down")] * 5)
    monkeypatch.setattr(qs, "chat_client", chat, raising=False)
    monkeypatch.setattr(qs, "embeddings_model", FakeEmbeddings(), raising=False)
    monkeypatch.setattr(qs, "pc_index", RecordingIndex([]), raising=False)
    monkeypatch.setattr(qs, "init_clients", lambda: True, raising=False)
    monkeypatch.setattr(qs, "query_cache", None, raising=False)
    monkeypatch.setattr(qs, "agent_graph", qs.build_graph(), raising=False)

    with pytest.raises(HTTPException) as excinfo:
        await qs.query(qs.QueryRequest(question="q", project_filter=None))
    assert excinfo.value.status_code == 503


@pytest.mark.asyncio
async def test_stub_fallback_is_opt_in_only(qs, monkeypatch):
    """LLM_STUB_FALLBACK off (the default) => raise. On => stub text."""
    chat = ScriptedChatClient([TimeoutError("x")] * 5)
    monkeypatch.setattr(qs, "chat_client", chat, raising=False)

    monkeypatch.setattr(qs, "LLM_STUB_FALLBACK", False, raising=False)
    with pytest.raises(qs.LLMUnavailable):
        await qs.generate_text("p", 10)

    chat._responses.extend([TimeoutError("x")] * 5)
    monkeypatch.setattr(qs, "LLM_STUB_FALLBACK", True, raising=False)
    assert "fallback" in (await qs.generate_text("p", 10)).lower()


def test_backoff_is_exponential_and_capped(qs, monkeypatch):
    import app.llm as llm

    # conftest shrinks these for speed; assert the shape against real values.
    monkeypatch.setattr(llm, "LLM_BACKOFF_BASE_S", 0.5, raising=False)
    monkeypatch.setattr(llm, "LLM_BACKOFF_MAX_S", 4.0, raising=False)

    delays = [llm.backoff_delay(i, jitter=False) for i in range(1, 8)]
    assert delays == sorted(delays), "backoff must be non-decreasing"
    assert delays[:3] == [0.5, 1.0, 2.0], f"not exponential: {delays[:3]}"
    assert max(delays) == 4.0, "backoff must be capped"


def test_backoff_jitter_stays_within_the_cap(qs, monkeypatch):
    import app.llm as llm
    monkeypatch.setattr(llm, "LLM_BACKOFF_BASE_S", 0.5, raising=False)
    monkeypatch.setattr(llm, "LLM_BACKOFF_MAX_S", 4.0, raising=False)
    samples = [llm.backoff_delay(6) for _ in range(50)]
    assert all(2.0 <= s <= 4.0 for s in samples)


# ── Fallback vectors must never reach a real index (embedding) ─

@pytest.fixture()
def es():
    return load_service_main("embedding")


@pytest.mark.asyncio
async def test_fallback_vectors_are_never_upserted_into_the_real_index(es, monkeypatch):
    """The single most damaging behaviour in the old code: hash vectors written
    into production Pinecone, where they never expire."""
    real_index = RecordingIndex()
    monkeypatch.setattr(es, "index", real_index, raising=False)
    monkeypatch.setattr(es, "embeddings_model",
                        FakeEmbeddings(fail_with=RuntimeError("429 insufficient_quota")),
                        raising=False)
    monkeypatch.setattr(es, "EMBED_MAX_ATTEMPTS", 2, raising=False)
    monkeypatch.setattr(es, "text_splitter", _splitter(), raising=False)

    from fastapi import HTTPException
    chunks = es.chunk_document({"doc_id": "D1", "content": "hello world", "doc_type": "email"})
    with pytest.raises(HTTPException) as excinfo:
        await es.embed_and_upsert(chunks)

    assert excinfo.value.status_code == 503
    assert real_index.upserts == [], "the real index must be left untouched"


@pytest.mark.asyncio
async def test_guard_blocks_fallback_writes_even_if_a_caller_slips_through(es, monkeypatch):
    """Belt and braces: the rule is enforced at the write, not only at embed time."""
    real_index = RecordingIndex()
    with pytest.raises(es.FallbackVectorWriteBlocked):
        es.assert_upsert_allowed(real_index, vectors_are_fallback=True)
    # real vectors are fine
    es.assert_upsert_allowed(real_index, vectors_are_fallback=False)


@pytest.mark.asyncio
async def test_fallback_vectors_are_allowed_into_the_stub_index(es, monkeypatch):
    """When Pinecone itself is unavailable there is nothing real to pollute, so
    the local stub path still works end to end."""
    from app.fallbacks import FallbackIndex

    stub_index = FallbackIndex()
    monkeypatch.setattr(es, "index", stub_index, raising=False)
    monkeypatch.setattr(es, "embeddings_model",
                        FakeEmbeddings(fail_with=RuntimeError("429")), raising=False)
    monkeypatch.setattr(es, "EMBED_MAX_ATTEMPTS", 1, raising=False)
    monkeypatch.setattr(es, "text_splitter", _splitter(), raising=False)

    chunks = es.chunk_document({"doc_id": "D1", "content": "hello world", "doc_type": "email"})
    degraded, written = await es.embed_and_upsert(chunks)
    assert degraded is True
    assert written == len(chunks)
    assert stub_index.describe_index_stats().total_vector_count == len(chunks)


@pytest.mark.asyncio
async def test_embedding_retries_before_giving_up(es, monkeypatch):
    calls = {"n": 0}

    class FlakyEmbeddings:
        async def aembed_documents(self, texts):
            calls["n"] += 1
            if calls["n"] < 3:
                raise RuntimeError("transient")
            return [[0.1] * 4 for _ in texts]

    monkeypatch.setattr(es, "index", RecordingIndex(), raising=False)
    monkeypatch.setattr(es, "embeddings_model", FlakyEmbeddings(), raising=False)
    monkeypatch.setattr(es, "EMBED_MAX_ATTEMPTS", 3, raising=False)
    vectors, used_fallback = await es._embed_texts(["a", "b"])
    assert calls["n"] == 3
    assert used_fallback is False
    assert len(vectors) == 2


def test_chunk_metadata_no_longer_duplicates_the_chunk_text(es, monkeypatch):
    monkeypatch.setattr(es, "text_splitter", _splitter(), raising=False)
    chunks = es.chunk_document({"doc_id": "D1", "content": "a" * 500, "doc_type": "email"})
    metadata = chunks[0]["metadata"]
    assert "content" in metadata
    assert "content_preview" not in metadata, "preview was a second copy of content"
    # everything readers rely on is still present
    for key in ("doc_id", "doc_type", "title", "date", "project", "participants", "tags"):
        assert key in metadata


def _splitter():
    from langchain.text_splitter import RecursiveCharacterTextSplitter
    return RecursiveCharacterTextSplitter(chunk_size=800, chunk_overlap=100,
                                          separators=["\n\n", "\n", ". ", " ", ""])


# ── Permanent vs transient provider errors ─────────────────────
#
# A Groq 404 "The model `llama-3.3-70b-versatile` does not exist or you do not
# have access to it" is a configuration error. Retrying it three times with
# backoff turns an instant failure into a slow one and triples the log noise.

class _ProviderError(Exception):
    """Mimics an openai APIStatusError: carries an HTTP status."""

    def __init__(self, status_code, message):
        super().__init__(message)
        self.status_code = status_code


class NotFoundError(_ProviderError):
    """Same class name openai uses for a 404."""


def _groq_model_not_found():
    return NotFoundError(404, "Error code: 404 - {'error': {'message': 'The model "
                              "`llama-3.3-70b-versatile` does not exist or you do not have "
                              "access to it.', 'code': 'model_not_found'}}")


@pytest.mark.parametrize("status,retryable", [
    (400, False), (401, False), (403, False), (404, False), (405, False), (422, False),
    (408, True), (409, True), (429, True), (500, True), (502, True), (503, True), (504, True),
])
def test_error_classification_by_status(qs, status, retryable):
    from app.llm import is_retryable
    assert is_retryable(_ProviderError(status, "x")) is retryable


def test_transport_errors_are_retryable(qs):
    from app.llm import is_retryable
    assert is_retryable(TimeoutError("read timed out")) is True
    assert is_retryable(ConnectionError("connection reset")) is True
    assert is_retryable(RuntimeError("chat provider returned no choices")) is True


def test_classification_falls_back_to_exception_class_name(qs):
    """Some clients raise typed errors without a status_code attribute."""
    from app.llm import is_retryable

    class AuthenticationError(Exception):
        pass

    class RateLimitError(Exception):
        pass

    assert is_retryable(AuthenticationError("bad key")) is False
    assert is_retryable(RateLimitError("slow down")) is True


@pytest.mark.asyncio
async def test_unknown_model_fails_on_the_first_attempt(qs, monkeypatch):
    """The reported bug: 3 attempts against a verdict that cannot change."""
    chat = ScriptedChatClient([_groq_model_not_found()] * 5)
    monkeypatch.setattr(qs, "chat_client", chat, raising=False)

    with pytest.raises(qs.LLMUnavailable) as excinfo:
        await qs.generate_text("hello", 10)

    assert chat.call_count == 1, "a 404 model_not_found must not be retried"
    assert excinfo.value.permanent is True


@pytest.mark.asyncio
async def test_permanent_error_message_names_the_model_and_the_fix(qs, monkeypatch):
    chat = ScriptedChatClient([_groq_model_not_found()])
    monkeypatch.setattr(qs, "chat_client", chat, raising=False)
    monkeypatch.setattr(qs, "CHAT_MODEL", "llama-3.3-70b-versatile", raising=False)

    with pytest.raises(qs.LLMUnavailable) as excinfo:
        await qs.generate_text("hello", 10)

    detail = str(excinfo.value)
    assert "llama-3.3-70b-versatile" in detail
    assert "GROQ_CHAT_MODEL" in detail
    assert "configuration problem" in detail


@pytest.mark.asyncio
async def test_rate_limits_are_still_retried(qs, monkeypatch):
    """429 is transient — the permanent-error path must not swallow it."""
    chat = ScriptedChatClient([_ProviderError(429, "rate limited"), "recovered"])
    monkeypatch.setattr(qs, "chat_client", chat, raising=False)
    assert await qs.generate_text("hello", 10) == "recovered"
    assert chat.call_count == 2


@pytest.mark.asyncio
async def test_embedding_permanent_error_is_not_retried_and_writes_nothing(es, monkeypatch):
    from fastapi import HTTPException

    real_index = RecordingIndex()
    calls = {"n": 0}

    class RejectingEmbeddings:
        async def aembed_documents(self, texts):
            calls["n"] += 1
            raise NotFoundError(404, "The model `text-embedding-3-large` does not exist")

    monkeypatch.setattr(es, "index", real_index, raising=False)
    monkeypatch.setattr(es, "embeddings_model", RejectingEmbeddings(), raising=False)
    monkeypatch.setattr(es, "EMBED_MAX_ATTEMPTS", 3, raising=False)

    with pytest.raises(HTTPException) as excinfo:
        await es._embed_texts(["a"])

    assert calls["n"] == 1, "permanent embedding error must not be retried"
    assert excinfo.value.status_code == 503
    assert "configuration problem" in excinfo.value.detail
    assert real_index.upserts == []


# ── 429 is two different problems ──────────────────────────────
#
# rate_limit_exceeded : too many tokens/requests this minute. Wait and retry.
# insufficient_quota  : the account has no credit. Waiting is pointless.
# Both arrive as HTTP 429 and the old code retried them identically.

class _Resp:
    def __init__(self, status_code, headers=None, body=None):
        self.status_code = status_code
        self.headers = headers or {}
        self._body = body or {}

    def json(self):
        return self._body


class RateLimitError(Exception):
    def __init__(self, message, headers=None, body=None):
        super().__init__(message)
        self.status_code = 429
        self.response = _Resp(429, headers, body)


QUOTA_MESSAGE = ("Error code: 429 - {'error': {'message': 'You exceeded your current quota, "
                 "please check your plan and billing details.', 'type': 'insufficient_quota'}}")
THROTTLE_MESSAGE = ("Error code: 429 - {'error': {'message': 'Rate limit reached for "
                    "text-embedding-3-large', 'type': 'requests'}}")


def test_quota_exhaustion_is_not_retryable(qs):
    from app.llm import is_retryable, quota_exhausted, is_rate_limited
    exc = RateLimitError(QUOTA_MESSAGE, body={"error": {"type": "insufficient_quota"}})
    assert quota_exhausted(exc) is True
    assert is_rate_limited(exc) is False
    assert is_retryable(exc) is False


def test_plain_rate_limit_is_still_retryable(qs):
    from app.llm import is_retryable, quota_exhausted, is_rate_limited
    exc = RateLimitError(THROTTLE_MESSAGE, body={"error": {"type": "requests"}})
    assert quota_exhausted(exc) is False
    assert is_rate_limited(exc) is True
    assert is_retryable(exc) is True


@pytest.mark.parametrize("header,expected", [
    ({"retry-after": "30"}, 30.0),
    ({"x-ratelimit-reset-tokens": "6m0s"}, 360.0),
    ({"x-ratelimit-reset-requests": "20ms"}, 0.02),
    ({}, None),
    ({"retry-after": "nonsense"}, None),
])
def test_retry_after_is_read_from_the_response(qs, header, expected):
    from app.llm import retry_after_seconds
    assert retry_after_seconds(RateLimitError("x", headers=header)) == expected


@pytest.mark.asyncio
async def test_chat_waits_exactly_as_long_as_the_provider_asks(qs, monkeypatch):
    import app.llm as llm
    slept = []

    async def record(seconds):
        slept.append(seconds)

    monkeypatch.setattr(llm.asyncio, "sleep", record)
    monkeypatch.setattr(llm, "LLM_BACKOFF_MAX_S", 60.0, raising=False)
    chat = ScriptedChatClient([RateLimitError("slow down", headers={"retry-after": "7"}), "ok"])
    monkeypatch.setattr(qs, "chat_client", chat, raising=False)

    assert await qs.generate_text("hi", 10) == "ok"
    assert slept == [7.0], "should honour Retry-After, not its own backoff curve"


@pytest.mark.asyncio
async def test_ingest_stops_immediately_when_the_account_is_out_of_credit(es, monkeypatch):
    """The reported failure. 6 attempts against an empty balance is 6 wasted calls."""
    real_index = RecordingIndex()
    calls = {"n": 0}

    class OutOfCredit:
        async def aembed_documents(self, texts):
            calls["n"] += 1
            raise RateLimitError(QUOTA_MESSAGE, body={"error": {"type": "insufficient_quota"}})

    monkeypatch.setattr(es, "index", real_index, raising=False)
    monkeypatch.setattr(es, "embeddings_model", OutOfCredit(), raising=False)
    monkeypatch.setattr(es, "EMBED_MAX_ATTEMPTS", 6, raising=False)

    with pytest.raises(es.EmbeddingQuotaExhausted) as excinfo:
        await es._embed_texts(["a"])

    assert calls["n"] == 1, "an exhausted quota must not be retried"
    assert "billing" in str(excinfo.value).lower()
    assert real_index.upserts == []


@pytest.mark.asyncio
async def test_rate_limited_ingest_waits_and_recovers(es, monkeypatch):
    attempts = {"n": 0}
    slept = []

    class Throttled:
        async def aembed_documents(self, texts):
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise RateLimitError(THROTTLE_MESSAGE, headers={"retry-after": "12"})
            return [[0.1] * 4 for _ in texts]

    async def record(seconds):
        slept.append(seconds)

    monkeypatch.setattr(es.asyncio, "sleep", record)
    monkeypatch.setattr(es, "index", RecordingIndex(), raising=False)
    monkeypatch.setattr(es, "embeddings_model", Throttled(), raising=False)
    monkeypatch.setattr(es, "EMBED_MAX_ATTEMPTS", 6, raising=False)
    monkeypatch.setattr(es, "EMBED_RETRY_WAIT_MAX_S", 90.0, raising=False)

    vectors, used_fallback = await es._embed_texts(["a", "b"])
    assert len(vectors) == 2 and used_fallback is False
    assert slept == [12.0, 12.0], "a bulk ingest should wait the full window the provider names"


@pytest.mark.asyncio
async def test_batch_halves_when_the_provider_keeps_throttling(es, monkeypatch):
    """A per-minute TOKEN limit is not a per-request limit: the same work fits
    when split smaller. Failing the whole ingest instead is a waste."""
    seen_sizes = []

    class TooBig:
        async def aembed_documents(self, texts):
            seen_sizes.append(len(texts))
            if len(texts) > 16:
                raise RateLimitError(THROTTLE_MESSAGE)
            return [[0.1] * 4 for _ in texts]

    async def nosleep(_):
        return None

    monkeypatch.setattr(es.asyncio, "sleep", nosleep)
    monkeypatch.setattr(es, "index", RecordingIndex(), raising=False)
    monkeypatch.setattr(es, "embeddings_model", TooBig(), raising=False)
    monkeypatch.setattr(es, "EMBED_MAX_ATTEMPTS", 1, raising=False)
    monkeypatch.setattr(es, "EMBED_MIN_BATCH_SIZE", 8, raising=False)

    vectors, _ = await es.embed_batch_adaptively(["t"] * 64)
    assert len(vectors) == 64, "every text must still be embedded"
    assert min(seen_sizes) <= 16, f"batch never shrank: {seen_sizes}"


@pytest.mark.asyncio
async def test_partial_progress_is_reported_on_failure(es, monkeypatch):
    """`failed` with no number is unactionable; say how far it got."""
    calls = {"n": 0}

    class DiesAfterFirstBatch:
        async def aembed_documents(self, texts):
            calls["n"] += 1
            if calls["n"] > 1:
                raise RateLimitError(QUOTA_MESSAGE, body={"error": {"type": "insufficient_quota"}})
            return [[0.1] * 4 for _ in texts]

    monkeypatch.setattr(es, "index", RecordingIndex(), raising=False)
    monkeypatch.setattr(es, "embeddings_model", DiesAfterFirstBatch(), raising=False)
    monkeypatch.setattr(es, "EMBED_BATCH_SIZE", 4, raising=False)
    monkeypatch.setattr(es, "text_splitter", _splitter(), raising=False)

    chunks = [{"chunk_id": f"c{i}", "content": f"text {i}", "metadata": {"doc_id": "D"}}
              for i in range(12)]
    with pytest.raises(es.EmbeddingQuotaExhausted) as excinfo:
        await es.embed_and_upsert(chunks)
    assert "4 of 12 chunks were embedded" in str(excinfo.value)


# ── A retired model id is not an outage ────────────────────────
#
# Groq removed the Llama families from its catalogue, so the pinned
# llama-3.3-70b-versatile started returning 404 model_not_found on every
# request. Nothing in the code was wrong; the provider's menu changed under it.

class NotFound404(Exception):
    def __init__(self, model="llama-3.3-70b-versatile"):
        super().__init__(
            f"Error code: 404 - {{'error': {{'message': 'The model `{model}` does not exist "
            "or you do not have access to it.', 'type': 'invalid_request_error', "
            "'code': 'model_not_found'}}"
        )
        self.status_code = 404


GROQ_CATALOGUE_2026_08 = [
    "openai/gpt-oss-120b", "openai/gpt-oss-20b",
    "groq/compound", "groq/compound-mini",
    "openai/gpt-oss-safeguard-20b", "whisper-large-v3",
]


class CatalogueClient(ScriptedChatClient):
    """Chat client that also answers models.list(), like AsyncOpenAI."""

    def __init__(self, responses, catalogue=None):
        super().__init__(responses)
        self._catalogue = catalogue if catalogue is not None else GROQ_CATALOGUE_2026_08
        self.list_calls = 0
        outer = self

        class _Models:
            async def list(self):
                outer.list_calls += 1
                return SimpleNamespace(
                    data=[SimpleNamespace(id=m) for m in outer._catalogue])

        self.models = _Models()


def test_unknown_model_is_recognised(qs):
    from app.llm import is_unknown_model, is_retryable
    exc = NotFound404()
    assert is_unknown_model(exc) is True
    assert is_retryable(exc) is False
    assert is_unknown_model(TimeoutError("nope")) is False


def test_preference_order_picks_the_current_groq_flagship(qs):
    from app.llm import pick_chat_model
    assert pick_chat_model(GROQ_CATALOGUE_2026_08, "llama-3.3-70b-versatile") == "openai/gpt-oss-120b"


def test_non_chat_models_are_never_auto_selected(qs):
    from app.llm import pick_chat_model
    assert pick_chat_model(["whisper-large-v3", "text-embedding-3-large",
                            "openai/gpt-oss-safeguard-20b"], "x") is None


def test_configured_model_is_never_reselected(qs):
    from app.llm import pick_chat_model
    assert pick_chat_model(["gpt-4o-mini"], "gpt-4o-mini") is None


@pytest.mark.asyncio
async def test_retired_model_is_replaced_from_the_live_catalogue(qs, monkeypatch):
    """The actual production failure: the pinned model 404s, discovery reads the
    catalogue, and the same request succeeds on a model the provider serves."""
    chat = CatalogueClient([NotFound404(), "the real answer"])
    monkeypatch.setattr(qs, "chat_client", chat, raising=False)
    monkeypatch.setattr(qs, "CHAT_MODEL_AUTO_FALLBACK", True, raising=False)
    monkeypatch.setattr(qs, "EFFECTIVE_CHAT_MODEL", "llama-3.3-70b-versatile", raising=False)
    monkeypatch.setattr(qs, "_model_resolution_attempted", False, raising=False)

    assert await qs.generate_text("why azure?", 100) == "the real answer"
    assert qs.EFFECTIVE_CHAT_MODEL == "openai/gpt-oss-120b"
    assert chat.calls[0]["model"] == "llama-3.3-70b-versatile"
    assert chat.calls[1]["model"] == "openai/gpt-oss-120b"


@pytest.mark.asyncio
async def test_the_catalogue_is_read_once_per_process(qs, monkeypatch):
    """Discovery must not run on every request."""
    chat = CatalogueClient([NotFound404(), "first", "second"])
    monkeypatch.setattr(qs, "chat_client", chat, raising=False)
    monkeypatch.setattr(qs, "CHAT_MODEL_AUTO_FALLBACK", True, raising=False)
    monkeypatch.setattr(qs, "EFFECTIVE_CHAT_MODEL", "llama-3.3-70b-versatile", raising=False)
    monkeypatch.setattr(qs, "_model_resolution_attempted", False, raising=False)

    await qs.generate_text("q1", 10)
    await qs.generate_text("q2", 10)
    assert chat.list_calls == 1


@pytest.mark.asyncio
async def test_auto_fallback_can_be_switched_off(qs, monkeypatch):
    """Opt out and a retired model fails loudly instead of silently substituting."""
    chat = CatalogueClient([NotFound404()] * 3)
    monkeypatch.setattr(qs, "chat_client", chat, raising=False)
    monkeypatch.setattr(qs, "CHAT_MODEL_AUTO_FALLBACK", False, raising=False)
    monkeypatch.setattr(qs, "EFFECTIVE_CHAT_MODEL", "llama-3.3-70b-versatile", raising=False)
    monkeypatch.setattr(qs, "_model_resolution_attempted", False, raising=False)

    with pytest.raises(qs.LLMUnavailable) as excinfo:
        await qs.generate_text("q", 10)
    assert excinfo.value.permanent is True
    assert chat.list_calls == 0
    assert qs.EFFECTIVE_CHAT_MODEL == "llama-3.3-70b-versatile"


@pytest.mark.asyncio
async def test_empty_catalogue_surfaces_the_original_error(qs, monkeypatch):
    """Nothing usable to switch to: report the real problem, don't loop."""
    chat = CatalogueClient([NotFound404()] * 3, catalogue=["whisper-large-v3"])
    monkeypatch.setattr(qs, "chat_client", chat, raising=False)
    monkeypatch.setattr(qs, "CHAT_MODEL_AUTO_FALLBACK", True, raising=False)
    monkeypatch.setattr(qs, "EFFECTIVE_CHAT_MODEL", "llama-3.3-70b-versatile", raising=False)
    monkeypatch.setattr(qs, "_model_resolution_attempted", False, raising=False)

    with pytest.raises(qs.LLMUnavailable):
        await qs.generate_text("q", 10)


def test_default_model_is_no_longer_the_retired_llama(qs):
    """A fresh checkout with no GROQ_CHAT_MODEL must not ship a dead default."""
    from pathlib import Path
    for service in ("query-service", "timeline-service", "embedding-service"):
        source = (Path(__file__).resolve().parents[1] / "services" / service /
                  "app" / "provider_config.py").read_text(encoding="utf-8")
        assert '"llama-3.3-70b-versatile"' not in source
        assert '"openai/gpt-oss-120b"' in source


def test_health_reports_configured_and_effective_model(qs, monkeypatch):
    import asyncio as _asyncio
    monkeypatch.setattr(qs, "EFFECTIVE_CHAT_MODEL", "openai/gpt-oss-120b", raising=False)
    monkeypatch.setattr(qs, "CHAT_MODEL", "llama-3.3-70b-versatile", raising=False)
    payload = _asyncio.get_event_loop_policy().new_event_loop().run_until_complete(qs.health())
    assert payload["chat_model"] == "openai/gpt-oss-120b"
    assert payload["chat_model_configured"] == "llama-3.3-70b-versatile"
