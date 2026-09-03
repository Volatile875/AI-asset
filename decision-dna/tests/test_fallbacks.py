"""Local fallback stubs and provider routing.

Previously these imported from `shared.utils.*`, which no service uses (each
Dockerfile does `COPY . .` from its own directory, so `shared/` never reaches a
container) — and `shared/utils/provider_config.py` is the pre-fix version of the
Groq routing bug. The assertions here now run against the code the containers
actually execute.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
QUERY_APP = REPO_ROOT / "services" / "query-service" / "app"


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def fallbacks():
    return _load(QUERY_APP / "fallbacks.py", "live_fallbacks")


def test_fallback_embeddings_and_index(fallbacks):
    embeddings = fallbacks.FallbackEmbeddings(dimensions=3)
    query_vector = embeddings.embed_query("hello")
    assert len(query_vector) == 3

    index = fallbacks.FallbackIndex()
    index.upsert([("doc-1", [0.1, 0.2, 0.3], {"content": "hello world"})])

    result = index.query([0.1, 0.2, 0.3], top_k=1)
    assert len(result.matches) == 1
    assert result.matches[0].id == "doc-1"


def test_fallback_openai_client_returns_content(fallbacks):
    client = fallbacks.FallbackOpenAIClient()
    response = client.chat.completions.create(
        model="test-model", messages=[{"role": "user", "content": "hi"}]
    )
    assert response.choices[0].message.content


def test_resolve_provider_config_prefers_groq_for_chat(monkeypatch):
    """Groq is chat-only. The routing keys are chat_* / embedding_*, not the
    single api_key/base_url pair the old (unused) shared/ helper returned —
    mixing a Groq key with the OpenAI endpoint is what produced the 401 recorded
    in GROQ_MIGRATION_CONTEXT.md."""
    monkeypatch.setenv("GROQ_API_KEY", "test-groq-key")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)

    module = _load(QUERY_APP / "provider_config.py", "query_provider_config")
    config = module.resolve_provider_config()

    assert config["provider"] == "groq"
    assert config["chat_api_key"] == "test-groq-key"
    assert config["chat_base_url"].startswith("https://api.groq.com")


def test_embeddings_never_route_to_groq(monkeypatch):
    """Groq has no embeddings endpoint, so embeddings must stay on OpenAI even
    when Groq is the chat provider."""
    monkeypatch.setenv("GROQ_API_KEY", "test-groq-key")
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)

    module = _load(QUERY_APP / "provider_config.py", "query_provider_config2")
    config = module.resolve_provider_config()

    assert config["embedding_api_key"] == "test-openai-key"
    assert config["embedding_base_url"].startswith("https://api.openai.com")
    assert config["chat_base_url"].startswith("https://api.groq.com")


def test_falls_back_to_openai_for_chat_when_groq_is_absent(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)

    module = _load(QUERY_APP / "provider_config.py", "query_provider_config3")
    config = module.resolve_provider_config()

    assert config["provider"] == "openai"
    assert config["chat_api_key"] == "test-openai-key"
    assert config["chat_base_url"].startswith("https://api.openai.com")


def test_all_three_services_share_one_provider_config():
    """These files are duplicated per service by necessity; they must not drift."""
    sources = {
        service: (REPO_ROOT / "services" / service / "app" / "provider_config.py")
        .read_text(encoding="utf-8")
        for service in ("query-service", "timeline-service", "embedding-service")
    }
    assert len(set(sources.values())) == 1, "provider_config.py copies have diverged"
