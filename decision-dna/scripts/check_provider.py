"""
scripts/check_provider.py

Answer one question: which chat and embedding models can THIS .env actually reach?

Run it from anywhere:

    python scripts/check_provider.py

It reads decision-dna/.env, resolves the provider exactly the way
services/*/app/provider_config.py does, then makes three real calls:

  1. GET  {chat_base_url}/models        - what the chat key can see
  2. POST {chat_base_url}/chat/completions with the configured model, 1 token
  3. POST {embedding_base_url}/embeddings with the configured model

and prints the exact .env line to change when something is wrong.

Written because a 404 model_not_found is indistinguishable, from the UI, from
an outage — and because /health cannot see it: the OpenAI/Groq client
constructors never touch the network, so a bad model id or an unauthorised key
only surfaces on a real call.

API keys are never printed; only a masked prefix.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

try:
    import httpx
except ImportError:  # pragma: no cover - the message is the point
    sys.exit("This script needs httpx:  pip install httpx")

# Anchored to this file, not the working directory — the same bug that sent the
# synthetic corpus into scripts/data/synthetic/.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = PROJECT_ROOT / ".env"

OK = "  ✅"
BAD = "  ❌"
WARN = "  ⚠️"


# ── .env ───────────────────────────────────────────────────────

def load_env(path: Path) -> Dict[str, str]:
    """Minimal dotenv reader: last assignment wins, matching docker-compose."""
    values: Dict[str, str] = {}
    if not path.exists():
        sys.exit(f"No .env at {path}")
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key in values and values[key] != value:
            print(f"{WARN} {key} is declared more than once with different values; "
                  f"the last one wins")
        values[key] = value
    return values


def resolve(env: Dict[str, str]) -> Dict[str, Optional[str]]:
    """Mirror services/*/app/provider_config.py. Keep the two in step."""
    if env.get("GROQ_API_KEY"):
        chat_api_key = env["GROQ_API_KEY"]
        chat_base_url = env.get("GROQ_BASE_URL", "https://api.groq.com/openai/v1")
        chat_model = env.get("GROQ_CHAT_MODEL", "openai/gpt-oss-120b")
        provider = "groq"
        chat_model_var = "GROQ_CHAT_MODEL"
    else:
        chat_api_key = env.get("OPENAI_API_KEY", "")
        chat_base_url = env.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
        chat_model = env.get("OPENAI_CHAT_MODEL", "gpt-4o-mini")
        provider = "openai"
        chat_model_var = "OPENAI_CHAT_MODEL"

    return {
        "provider": provider,
        "chat_api_key": chat_api_key,
        "chat_base_url": chat_base_url.rstrip("/"),
        "chat_model": chat_model,
        "chat_model_var": chat_model_var,
        # Groq has no embeddings endpoint: embeddings always go to OpenAI.
        "embedding_api_key": env.get("OPENAI_API_KEY", ""),
        "embedding_base_url": env.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/"),
        "embedding_model": env.get("EMBEDDING_MODEL", "text-embedding-3-large"),
        "embedding_dimensions": env.get("EMBEDDING_DIMENSIONS", "1024"),
    }


def mask(secret: str) -> str:
    if not secret:
        return "(empty)"
    return f"{secret[:7]}…{secret[-4:]} ({len(secret)} chars)"


def detail_of(response: httpx.Response) -> str:
    try:
        body = response.json()
        error = body.get("error") or {}
        return error.get("message") or str(body)[:200]
    except Exception:  # noqa: BLE001
        return response.text[:200]


# ── Probes ─────────────────────────────────────────────────────

def list_models(base_url: str, api_key: str) -> Tuple[Optional[List[str]], str]:
    try:
        r = httpx.get(f"{base_url}/models",
                      headers={"Authorization": f"Bearer {api_key}"}, timeout=20)
    except Exception as exc:  # noqa: BLE001
        return None, f"{type(exc).__name__}: {exc}"
    if r.status_code != 200:
        return None, f"HTTP {r.status_code}: {detail_of(r)}"
    try:
        return sorted(m["id"] for m in r.json().get("data", [])), ""
    except Exception as exc:  # noqa: BLE001
        return None, f"unparseable /models response: {exc}"


def probe_chat(base_url: str, api_key: str, model: str) -> Tuple[bool, str]:
    try:
        r = httpx.post(
            f"{base_url}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={"model": model, "messages": [{"role": "user", "content": "ping"}],
                  "max_tokens": 1},
            timeout=30,
        )
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"
    return (True, "") if r.status_code == 200 else (False, f"HTTP {r.status_code}: {detail_of(r)}")


def probe_embeddings(base_url: str, api_key: str, model: str, dimensions: str) -> Tuple[bool, str]:
    payload: Dict[str, object] = {"model": model, "input": "preflight"}
    if dimensions.isdigit():
        payload["dimensions"] = int(dimensions)
    try:
        r = httpx.post(f"{base_url}/embeddings",
                       headers={"Authorization": f"Bearer {api_key}"},
                       json=payload, timeout=30)
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"
    if r.status_code != 200:
        return False, f"HTTP {r.status_code}: {detail_of(r)}"
    try:
        got = len(r.json()["data"][0]["embedding"])
    except Exception:  # noqa: BLE001
        return True, "returned 200 but the response shape was unexpected"
    if dimensions.isdigit() and got != int(dimensions):
        return False, (f"returned {got} dimensions but EMBEDDING_DIMENSIONS is {dimensions}; "
                       "Pinecone upserts will fail")
    return True, f"{got} dimensions"


# Same preference order the services use for automatic fallback.
PREFERRED = ("openai/gpt-oss-120b", "openai/gpt-oss-20b", "groq/compound",
             "groq/compound-mini", "gpt-4o-mini", "gpt-4o")
SKIP = ("embedding", "whisper", "tts", "guard", "safeguard", "moderation",
        "dall-e", "vision-preview", "audio", "realtime", "rerank")


def suggest(models: Optional[List[str]], wanted: str) -> List[str]:
    """Usable chat models, best first."""
    if not models:
        return []
    usable = [m for m in models if not any(s in m.lower() for s in SKIP)]
    ranked = [m for m in PREFERRED if m in usable]
    return ranked + [m for m in usable if m not in ranked][:12 - len(ranked)]


# ── Main ───────────────────────────────────────────────────────

def main() -> int:
    print(f"Reading {ENV_PATH}\n")
    env = load_env(ENV_PATH)
    cfg = resolve(env)

    print(f"Provider resolved to : {cfg['provider']}")
    print(f"Chat endpoint        : {cfg['chat_base_url']}")
    print(f"Chat model           : {cfg['chat_model']}   (from {cfg['chat_model_var']})")
    print(f"Chat key             : {mask(cfg['chat_api_key'])}")
    print(f"Embedding endpoint   : {cfg['embedding_base_url']}")
    print(f"Embedding model      : {cfg['embedding_model']} @ {cfg['embedding_dimensions']}d")
    print(f"Embedding key        : {mask(cfg['embedding_api_key'])}")
    print()

    failures: List[str] = []

    print("1. Chat models this key can reach")
    models, error = list_models(cfg["chat_base_url"], cfg["chat_api_key"])
    if models is None:
        print(f"{BAD} {error}")
        if "401" in error or "403" in error:
            print("     The chat API key is rejected. Regenerate it and update .env.")
        failures.append("model listing")
    else:
        print(f"{OK} {len(models)} model(s) visible")
        if cfg["chat_model"] in models:
            print(f"{OK} '{cfg['chat_model']}' is in the catalogue")
        else:
            print(f"{BAD} '{cfg['chat_model']}' is NOT in the catalogue")
            options = suggest(models, cfg["chat_model"])
            for candidate in options:
                print(f"        {candidate}")
            if options:
                print()
                print(f"     Put this in decision-dna/.env:")
                print(f"       {cfg['chat_model_var']}={options[0]}")
            failures.append("chat model not available")
    print()

    print("2. Chat completion with the configured model")
    ok, error = probe_chat(cfg["chat_base_url"], cfg["chat_api_key"], cfg["chat_model"])
    if ok:
        print(f"{OK} '{cfg['chat_model']}' answered")
    else:
        print(f"{BAD} {error}")
        failures.append("chat call")
    print()

    print("3. Embeddings (always OpenAI - Groq has no embeddings endpoint)")
    if not cfg["embedding_api_key"]:
        print(f"{BAD} OPENAI_API_KEY is empty; embeddings cannot work")
        failures.append("embeddings")
    else:
        ok, note = probe_embeddings(cfg["embedding_base_url"], cfg["embedding_api_key"],
                                    cfg["embedding_model"], cfg["embedding_dimensions"])
        print(f"{OK} {cfg['embedding_model']}: {note}" if ok else f"{BAD} {note}")
        if not ok:
            failures.append("embeddings")
    print()

    print("-" * 70)
    if not failures:
        print("All good. Chat and embeddings both reachable with this .env.")
        return 0

    print(f"Problems: {', '.join(failures)}\n")
    if "chat model not available" in failures or "chat call" in failures:
        print("Fix one of these in decision-dna/.env, then rebuild:")
        print(f"  a) point {cfg['chat_model_var']} at a model listed under step 1")
        if cfg["provider"] == "groq":
            print("  b) comment out GROQ_API_KEY to route chat to OpenAI instead")
            print("     (OPENAI_CHAT_MODEL defaults to gpt-4o-mini)")
        print("\n  docker compose up --build query-service timeline-service")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
