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
    # CP-1: unit tests never touch a real Redis; cache tests inject fakes.
    "CACHE_ENABLED": "false",
    # CP-4: unit tests never draw from a real provider budget.
    "PROVIDER_BUCKET_ENABLED": "false",
    # OB-3: unit tests never probe real provider endpoints.
    "PROVIDER_PROBE_ENABLED": "false",
    # CE-0: unit tests run the baseline profile (all Phase 7 flags off);
    # profile-specific tests construct their own Settings.
    "RAG_PROFILE": "baseline",
}


@pytest.fixture
def settings() -> Settings:
    return Settings(_env_file=None, **REQUIRED_ENV)
