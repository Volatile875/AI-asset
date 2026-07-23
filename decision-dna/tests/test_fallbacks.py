import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shared.utils.fallbacks import FallbackEmbeddings, FallbackIndex, FallbackOpenAIClient


def test_fallback_embeddings_and_index():
    embeddings = FallbackEmbeddings(dimensions=3)
    query_vector = embeddings.embed_query("hello")
    assert len(query_vector) == 3

    index = FallbackIndex()
    index.upsert([("doc-1", [0.1, 0.2, 0.3], {"content": "hello world"})])

    result = index.query([0.1, 0.2, 0.3], top_k=1)
    assert len(result.matches) == 1
    assert result.matches[0].id == "doc-1"


def test_fallback_openai_client_returns_content():
    client = FallbackOpenAIClient()
    response = client.chat.completions.create(model="test-model", messages=[{"role": "user", "content": "hi"}])
    assert response.choices[0].message.content


def test_resolve_provider_config_prefers_groq_when_available(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-groq-key")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    import importlib.util
    import sys
    from pathlib import Path

    module_path = Path(__file__).resolve().parents[1] / "services" / "query-service" / "app" / "provider_config.py"
    spec = importlib.util.spec_from_file_location("query_provider_config", module_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    config = module.resolve_provider_config()
    assert config["provider"] == "groq"
    assert config["api_key"] == "test-groq-key"
    assert config["base_url"].startswith("https://api.groq.com")
