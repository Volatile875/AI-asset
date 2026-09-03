"""Corpus discovery, cache correctness, and the graph-service entry point."""

import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = REPO_ROOT / "data" / "synthetic"


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# ══════════════════════════════════════════════════════════════
#  Corpus path — the root cause, not just the symptom
# ══════════════════════════════════════════════════════════════

def test_generate_data_output_is_anchored_to_the_repo_not_the_cwd():
    """The bug: OUTPUT_DIR was the relative Path("data/synthetic"), so running
    the script from scripts/ wrote the corpus to scripts/data/synthetic/ — a
    tree nothing reads. It must resolve identically from any directory."""
    module = _load(REPO_ROOT / "scripts" / "generate_data.py", "generate_data_probe")
    assert module.DEFAULT_OUTPUT_DIR.is_absolute()
    assert module.DEFAULT_OUTPUT_DIR == DATA_DIR
    assert not str(module.DEFAULT_OUTPUT_DIR).endswith(str(Path("scripts") / "data" / "synthetic"))


def test_generate_data_default_matches_the_compose_mount():
    """docker-compose mounts ./data -> /app/data and ingestion reads
    /app/data/synthetic, so the generator must target ./data/synthetic."""
    compose = (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "./data:/app/data" in compose
    module = _load(REPO_ROOT / "scripts" / "generate_data.py", "generate_data_probe2")
    assert module.DEFAULT_OUTPUT_DIR == REPO_ROOT / "data" / "synthetic"


def test_ingestion_discovers_the_full_corpus():
    sys.path.insert(0, str(REPO_ROOT / "services" / "ingestion-service"))
    try:
        for name in [m for m in list(sys.modules) if m == "app" or m.startswith("app.")]:
            del sys.modules[name]
        from app.parsers import parse_emails, parse_jira_tickets, parse_meeting_notes

        emails = parse_emails(str(DATA_DIR / "emails"))
        meetings = parse_meeting_notes(str(DATA_DIR / "meetings"))
        tickets = parse_jira_tickets(str(DATA_DIR / "jira"))
    finally:
        sys.path.remove(str(REPO_ROOT / "services" / "ingestion-service"))

    assert len(emails) == 100, f"expected the generated 100 emails, found {len(emails)}"
    assert len(meetings) == 50
    assert len(tickets) == 100

    doc_ids = [d["doc_id"] for d in emails + meetings + tickets]
    assert len(doc_ids) == 250
    assert len(set(doc_ids)) == 250, "duplicate doc_ids would collide in Pinecone and Neo4j"


def test_curated_seed_is_preserved_but_not_ingested():
    """The hand-written demo docs were moved to <kind>/seed/, which the parsers'
    non-recursive glob does not pick up. Nothing was deleted."""
    for kind, filename in (("emails", "emails.json"), ("meetings", "meetings.json"),
                           ("jira", "tickets.json")):
        seed = DATA_DIR / kind / "seed" / filename
        assert seed.exists(), f"curated seed missing: {seed}"
        assert json.loads(seed.read_text(encoding="utf-8")), "seed file is empty"
        # the parsers glob "*.json" non-recursively
        assert list((DATA_DIR / kind).glob("*.json")) == [DATA_DIR / kind / filename]


def test_every_document_has_content_to_embed():
    for kind, filename in (("emails", "emails.json"), ("meetings", "meetings.json"),
                           ("jira", "tickets.json")):
        records = json.loads((DATA_DIR / kind / filename).read_text(encoding="utf-8"))
        assert all(r.get("id") for r in records)


# ══════════════════════════════════════════════════════════════
#  Cache keys
# ══════════════════════════════════════════════════════════════

@pytest.fixture()
def cache_mod():
    return _load(REPO_ROOT / "services" / "query-service" / "app" / "cache.py", "qcache")


def test_project_filter_is_part_of_the_key(cache_mod):
    """A filtered and an unfiltered question must never share a cache entry."""
    unfiltered = cache_mod.make_key("query", "3", "why azure?", None)
    filtered = cache_mod.make_key("query", "3", "why azure?", "CloudMigration")
    other_project = cache_mod.make_key("query", "3", "why azure?", "MobileApp")
    assert len({unfiltered, filtered, other_project}) == 3


def test_corpus_version_invalidates_every_key(cache_mod):
    before = cache_mod.make_key("query", "3", "q", "P")
    after = cache_mod.make_key("query", "4", "q", "P")
    assert before != after
    assert ":v3:" in before and ":v4:" in after


def test_keys_are_stable_and_normalised(cache_mod):
    assert cache_mod.make_key("query", "1", "Why Azure?", "P") == \
           cache_mod.make_key("query", "1", "  why azure?  ", "p")


def test_namespaces_do_not_collide(cache_mod):
    assert cache_mod.make_key("query", "1", "x") != cache_mod.make_key("timeline", "1", "x")


class _FlakyRedis:
    async def get(self, key):
        raise ConnectionError("redis down")

    async def set(self, key, value, ex=None):
        raise ConnectionError("redis down")


@pytest.mark.asyncio
async def test_cache_failure_never_breaks_a_request(cache_mod):
    cache = cache_mod.ReadCache(_FlakyRedis(), "query", 60)
    assert await cache.get("q") is None
    await cache.set({"answer": "x"}, "q")  # must not raise


@pytest.mark.asyncio
async def test_cache_disabled_when_ttl_is_zero(cache_mod):
    class Boom:
        async def get(self, key):
            raise AssertionError("should not be consulted")

    cache = cache_mod.ReadCache(Boom(), "query", 0)
    assert cache.enabled is False
    assert await cache.get("q") is None


def test_ingestion_bumps_the_corpus_version_key():
    """The invalidation signal has to actually be written by ingestion."""
    source = (REPO_ROOT / "services" / "ingestion-service" / "app" / "main.py").read_text(encoding="utf-8")
    assert "CORPUS_VERSION_KEY" in source
    assert "incr(CORPUS_VERSION_KEY)" in source
    assert source.count('CORPUS_VERSION_KEY = "dna:corpus_version"') == 1
    # and both readers must use the same key name
    for service in ("query-service", "timeline-service"):
        cache_src = (REPO_ROOT / "services" / service / "app" / "cache.py").read_text(encoding="utf-8")
        assert 'CORPUS_VERSION_KEY = "dna:corpus_version"' in cache_src


# ══════════════════════════════════════════════════════════════
#  graph-service entry point
# ══════════════════════════════════════════════════════════════

GRAPH_DIR = REPO_ROOT / "services" / "graph-service"


def test_graph_service_entrypoint_is_the_repaired_module():
    """The Dockerfile already runs graph_services_main, not main. Pin that down
    so nobody 'fixes' the CMD back to the broken file."""
    dockerfile = (GRAPH_DIR / "Dockerfile").read_text(encoding="utf-8")
    assert "app.graph_services_main:app" in dockerfile
    assert '"app.main:app"' not in dockerfile


def test_live_graph_cypher_has_its_parameter_markers():
    """The unused app/main.py has Cypher like `MERGE (d:Document {id: })` with the
    $params stripped. The module the container actually runs must not."""
    live = (GRAPH_DIR / "app" / "graph_services_main.py").read_text(encoding="utf-8")
    broken = re.findall(r"\{id:\s*\}|WHERE\s+\w+\.\w+\s*=\s*$", live, re.MULTILINE)
    assert broken == [], f"stripped Cypher parameters found: {broken}"
    assert "$id" in live and "$project" in live


def test_live_graph_service_startup_is_non_fatal():
    """Every other service self-heals; graph-service must not crash-loop just
    because Neo4j is slow to accept connections."""
    live = (GRAPH_DIR / "app" / "graph_services_main.py").read_text(encoding="utf-8")
    startup = live.split("async def startup()", 1)[1].split("async def shutdown", 1)[0]
    assert "verify_connectivity" in startup
    assert "raise RuntimeError" not in startup


def test_live_graph_service_matches_the_frontend_node_model():
    """App.tsx reads conn.rel / conn.labels / conn.m and Person/Project/Decision/
    Meeting/Ticket/Email labels — the model graph_services_main builds."""
    live = (GRAPH_DIR / "app" / "graph_services_main.py").read_text(encoding="utf-8")
    for label in ("Person", "Project", "Decision", "Meeting", "Ticket", "Email"):
        assert f":{label} " in live or f":{label} {{" in live or f"({label.lower()[0]}:{label}" in live
    assert "RETURN type(r) AS rel, labels(m) AS labels, m" in live


# ══════════════════════════════════════════════════════════════
#  scripts/check_provider.py
# ══════════════════════════════════════════════════════════════

@pytest.fixture()
def checker():
    return _load(REPO_ROOT / "scripts" / "check_provider.py", "check_provider_probe")


def test_checker_env_path_is_anchored_to_the_repo(checker):
    assert checker.ENV_PATH == REPO_ROOT / ".env"
    assert checker.ENV_PATH.is_absolute()


def test_checker_resolution_matches_provider_config(checker):
    """If these two drift, the diagnostic lies about what the services do."""
    groq = checker.resolve({"GROQ_API_KEY": "gsk_x", "OPENAI_API_KEY": "sk_y"})
    assert groq["provider"] == "groq"
    assert groq["chat_base_url"].startswith("https://api.groq.com")
    assert groq["chat_model"] == "openai/gpt-oss-120b"
    # embeddings never follow chat to Groq
    assert groq["embedding_api_key"] == "sk_y"
    assert groq["embedding_base_url"].startswith("https://api.openai.com")

    openai = checker.resolve({"OPENAI_API_KEY": "sk_y"})
    assert openai["provider"] == "openai"
    assert openai["chat_model"] == "gpt-4o-mini"
    assert openai["chat_base_url"].startswith("https://api.openai.com")


def test_checker_dotenv_last_assignment_wins(checker, tmp_path):
    """docker-compose loads the last declaration; the reader must agree."""
    env_file = tmp_path / ".env"
    env_file.write_text("A=1\n# comment\nA=2\nB = spaced \nC=\"quoted\"\n", encoding="utf-8")
    values = checker.load_env(env_file)
    assert values == {"A": "2", "B": "spaced", "C": "quoted"}


def test_checker_never_prints_a_whole_key(checker):
    secret = "sk-proj-" + "z" * 150
    masked = checker.mask(secret)
    assert secret not in masked
    assert masked.startswith("sk-proj")
    assert "150" not in masked or str(len(secret)) in masked


def test_checker_suggests_same_family_chat_models_only(checker):
    catalogue = ["llama-3.1-8b-instant", "llama-4-scout-17b", "whisper-large-v3",
                 "text-embedding-3-large", "meta-llama/llama-guard-4-12b"]
    suggestions = checker.suggest(catalogue, "llama-3.3-70b-versatile")
    assert "llama-3.1-8b-instant" in suggestions
    assert "whisper-large-v3" not in suggestions
    assert "text-embedding-3-large" not in suggestions
    assert not any("guard" in s for s in suggestions)


def test_checker_suggest_handles_an_empty_catalogue(checker):
    assert checker.suggest(None, "x") == []
    assert checker.suggest([], "x") == []


def test_env_has_no_duplicate_or_space_padded_assignments():
    """Guards the two .env defects: a duplicated key (the last one wins, so the
    first is dead) and `KEY= value`, whose leading space can reach the client."""
    env_path = REPO_ROOT / ".env"
    if not env_path.exists():
        pytest.skip(".env is not committed")
    seen, duplicates, padded = set(), [], []
    for raw in env_path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key in seen:
            duplicates.append(key)
        seen.add(key)
        if value != value.strip():
            padded.append(key)
    assert duplicates == [], (
        f"{duplicates} declared more than once in .env. docker-compose loads the LAST "
        "assignment, so the earlier line is dead and silently ignored — delete the duplicate."
    )
    assert padded == [], (
        f"{padded} written as `KEY= value` in .env. The space after '=' can travel into "
        "the value and reach the provider inside the Authorization header — remove it."
    )
