import pytest
from pydantic import ValidationError

from app.config import Settings
from tests.conftest import REQUIRED_ENV


def _clear_env(monkeypatch):
    for key in (*REQUIRED_ENV, "EMBEDDING_MODEL", "EMBEDDING_DIMENSIONS", "TRACING_PROVIDER"):
        monkeypatch.delenv(key, raising=False)


@pytest.mark.parametrize(
    "missing",
    ["DATABASE_URL", "REDIS_URL", "OPENAI_API_KEY", "RAG_SERVICE_TOKEN_SECRET"],
)
def test_missing_required_setting_fails_fast_naming_it(monkeypatch, missing):
    _clear_env(monkeypatch)
    env = {k: v for k, v in REQUIRED_ENV.items() if k != missing}
    with pytest.raises(ValidationError) as excinfo:
        Settings(_env_file=None, **env)
    assert missing in str(excinfo.value)


def test_missing_tracing_key_fails_fast_naming_it(monkeypatch):
    _clear_env(monkeypatch)
    env = {k: v for k, v in REQUIRED_ENV.items() if k != "LANGSMITH_API_KEY"}
    with pytest.raises(ValidationError) as excinfo:
        Settings(_env_file=None, **env)
    assert "LANGSMITH_API_KEY" in str(excinfo.value)


def test_langfuse_provider_requires_langfuse_keys(monkeypatch):
    _clear_env(monkeypatch)
    with pytest.raises(ValidationError) as excinfo:
        Settings(_env_file=None, TRACING_PROVIDER="langfuse", **REQUIRED_ENV)
    assert "LANGFUSE_PUBLIC_KEY" in str(excinfo.value)


def test_invalid_embedding_dimensions_rejected(monkeypatch):
    _clear_env(monkeypatch)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, EMBEDDING_DIMENSIONS=3072, **REQUIRED_ENV)


def test_pinned_embedding_defaults(settings):
    assert settings.embedding_model == "text-embedding-3-large"
    assert settings.embedding_dimensions == 1536
