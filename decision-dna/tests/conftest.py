"""Shared fixtures.

Each service is its own container with `COPY . .` from its own directory, so
modules are imported as `app.*` from that service's root. These helpers put the
right root on sys.path per test module and give every test a clean import.
"""

import importlib
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SERVICE_ROOTS = {
    "query": REPO_ROOT / "services" / "query-service",
    "timeline": REPO_ROOT / "services" / "timeline-service",
    "embedding": REPO_ROOT / "services" / "embedding-service",
    "gateway": REPO_ROOT / "api-gateway",
    "ingestion": REPO_ROOT / "services" / "ingestion-service",
}


def load_service_main(service: str):
    """Import `app.main` for one service in isolation from the others."""
    root = str(SERVICE_ROOTS[service])
    for name in [m for m in list(sys.modules) if m == "app" or m.startswith("app.")]:
        del sys.modules[name]
    sys.path = [p for p in sys.path if p not in SERVICE_ROOTS.values() and Path(p) not in SERVICE_ROOTS.values()]
    for other in SERVICE_ROOTS.values():
        while str(other) in sys.path:
            sys.path.remove(str(other))
    sys.path.insert(0, root)
    return importlib.import_module("app.main")


@pytest.fixture(autouse=True)
def _neutral_env(monkeypatch):
    """No real credentials in tests; keep behaviour deterministic."""
    for var in ("OPENAI_API_KEY", "GROQ_API_KEY", "PINECONE_API_KEY", "LLM_STUB_FALLBACK"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("LLM_BACKOFF_BASE_S", "0.001")
    monkeypatch.setenv("LLM_BACKOFF_MAX_S", "0.002")


# ── Test doubles ───────────────────────────────────────────────

class FakeMatch(SimpleNamespace):
    pass


class RecordingIndex:
    """Stands in for a real Pinecone index and records everything written to it."""

    def __init__(self, matches: List[Dict[str, Any]] = None):
        self.upserts: List[tuple] = []
        self._matches = matches or []
        self.query_calls = 0

    def upsert(self, vectors):
        self.upserts.extend(vectors)

    def query(self, vector, top_k=5, include_metadata=True, filter=None):
        self.query_calls += 1
        matches = [
            FakeMatch(id=m["id"], score=m["score"], metadata=m.get("metadata", {}))
            for m in self._matches
        ]
        return SimpleNamespace(matches=matches[:top_k])

    def describe_index_stats(self):
        return SimpleNamespace(total_vector_count=len(self.upserts))


class ScriptedChatClient:
    """Async chat client that replays scripted responses and counts calls.

    Entries may be a string (returned) or an Exception instance (raised), which
    is how the retry / no-permanent-downgrade tests drive failures.
    """

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls: List[Dict[str, Any]] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, model, messages, max_tokens=None, temperature=None, **kwargs):
        prompt = messages[-1]["content"] if messages else ""
        self.calls.append({"model": model, "prompt": prompt, "max_tokens": max_tokens})
        item = self._responses.pop(0) if self._responses else ""
        if isinstance(item, BaseException):
            raise item
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=item))]
        )

    @property
    def call_count(self) -> int:
        return len(self.calls)


class FakeEmbeddings:
    """Records how many embedding *requests* are made (batching is the point)."""

    def __init__(self, dimensions: int = 8, fail_with: BaseException = None):
        self.dimensions = dimensions
        self.document_batches: List[List[str]] = []
        self.query_calls: List[str] = []
        self._fail_with = fail_with

    async def aembed_documents(self, texts):
        if self._fail_with:
            raise self._fail_with
        self.document_batches.append(list(texts))
        return [[0.1] * self.dimensions for _ in texts]

    async def aembed_query(self, text):
        if self._fail_with:
            raise self._fail_with
        self.query_calls.append(text)
        return [0.1] * self.dimensions

    def embed_documents(self, texts):
        self.document_batches.append(list(texts))
        return [[0.1] * self.dimensions for _ in texts]

    def embed_query(self, text):
        self.query_calls.append(text)
        return [0.1] * self.dimensions

    @property
    def request_count(self) -> int:
        return len(self.document_batches) + len(self.query_calls)
