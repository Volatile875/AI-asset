import os
from typing import Dict, Optional


def resolve_provider_config() -> Dict[str, Optional[str]]:
    """Resolve provider configuration.
    
    NOTE: Groq only supports chat/completions, not embeddings.
    Embeddings always use OpenAI or fallback to local vectors.
    """
    # Chat: prefer Groq, fall back to OpenAI
    if os.getenv("GROQ_API_KEY"):
        chat_api_key = os.getenv("GROQ_API_KEY")
        chat_base_url = os.getenv("GROQ_BASE_URL", "https://api.groq.com/openai/v1")
        chat_model = os.getenv("GROQ_CHAT_MODEL", "llama-3.3-70b-versatile")
    else:
        chat_api_key = os.getenv("OPENAI_API_KEY", "")
        chat_base_url = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
        chat_model = os.getenv("OPENAI_CHAT_MODEL", "gpt-4o-mini")
    
    # Embeddings: always OpenAI (Groq doesn't support embeddings endpoint)
    embedding_api_key = os.getenv("OPENAI_API_KEY", "")
    embedding_base_url = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
    embedding_model = os.getenv("EMBEDDING_MODEL", "text-embedding-3-large")
    
    return {
        "chat_api_key": chat_api_key,
        "chat_base_url": chat_base_url,
        "chat_model": chat_model,
        "embedding_api_key": embedding_api_key,
        "embedding_base_url": embedding_base_url,
        "embedding_model": embedding_model,
        "provider": "groq" if os.getenv("GROQ_API_KEY") else "openai",
    }
