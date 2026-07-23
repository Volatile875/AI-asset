import os
from typing import Dict, Optional


def resolve_provider_config() -> Dict[str, Optional[str]]:
    """Return provider configuration for OpenAI-compatible services, preferring Groq when available."""
    api_key = os.getenv("GROQ_API_KEY") or os.getenv("OPENAI_API_KEY") or ""
    base_url = os.getenv("OPENAI_BASE_URL") or os.getenv("GROQ_BASE_URL") or "https://api.groq.com/openai/v1"
    chat_model = os.getenv("GROQ_CHAT_MODEL") or os.getenv("OPENAI_CHAT_MODEL") or "llama-3.3-70b-versatile"
    embedding_model = os.getenv("GROQ_EMBEDDING_MODEL") or os.getenv("EMBEDDING_MODEL") or "text-embedding-3-small"
    return {
        "api_key": api_key,
        "base_url": base_url,
        "chat_model": chat_model,
        "embedding_model": embedding_model,
        "provider": "groq" if os.getenv("GROQ_API_KEY") else "openai",
    }
