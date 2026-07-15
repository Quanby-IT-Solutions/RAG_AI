import pytest

from app.config import Settings

REQUIRED_ENV = {
    "DATABASE_URL": "postgres://postgres:password@localhost:5432/turbo-template",
    "REDIS_URL": "redis://localhost:6379/0",
    "OPENAI_API_KEY": "test-openai-key",
    "RAG_SERVICE_TOKEN_SECRET": "test-service-secret",
    "LANGSMITH_API_KEY": "test-langsmith-key",
    # SC-2: unit tests must never touch a real Redis for limit counters.
    "RATE_LIMIT_STORAGE": "memory://",
}


@pytest.fixture
def settings() -> Settings:
    return Settings(_env_file=None, **REQUIRED_ENV)
