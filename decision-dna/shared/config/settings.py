"""
shared/config/settings.py
Centralized config using pydantic-settings.
Each microservice imports this.
"""

from functools import lru_cache
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # LLM
    anthropic_api_key: str = ""
    openai_api_key: str = ""

    # Vector DB
    pinecone_api_key: str = ""
    pinecone_environment: str = "us-east-1"
    pinecone_index_name: str = "ai-asset"
    embedding_model: str = "text-embedding-3-large"
    embedding_dimensions: int = 1024

    # Graph DB
    neo4j_uri: str = "neo4j+s://e6a3d1fa.databases.neo4j.io"
    neo4j_username: str = "neo4j"
    neo4j_password: str = ""

    # Cache
    redis_url: str = "redis://localhost:6379"

    # Service URLs
    ingestion_service_url: str = "http://localhost:8001"
    embedding_service_url: str = "http://localhost:8002"
    graph_service_url: str = "http://localhost:8003"
    query_service_url: str = "http://localhost:8004"
    timeline_service_url: str = "http://localhost:8005"

    # App
    environment: str = "development"
    log_level: str = "INFO"
    secret_key: str = "change-me-in-production"

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        case_sensitive = False
        extra = "ignore"


@lru_cache()
def get_settings() -> Settings:
    return Settings()
