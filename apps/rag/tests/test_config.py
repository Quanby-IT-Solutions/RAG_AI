import pytest
from pydantic import ValidationError

from app.config import PROFILE_EXPANSION, Settings
from tests.conftest import REQUIRED_ENV


def _clear_env(monkeypatch):
    for key in (
        *REQUIRED_ENV,
        "EMBEDDING_MODEL",
        "EMBEDDING_DIMENSIONS",
        "TRACING_PROVIDER",
        "WATSONX_ENABLED",
        "IBM_CLOUD_API_KEY",
        "WATSONX_PROJECT_ID",
        "WATSONX_URL",
        "WATSONX_MODEL",
        "WATSONX_API_VERSION",
        "PROVIDER_RPM_WATSONX",
    ):
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


def test_watsonx_disabled_requires_no_credentials(monkeypatch):
    _clear_env(monkeypatch)
    settings = Settings(_env_file=None, WATSONX_ENABLED="false", **REQUIRED_ENV)
    assert settings.watsonx_enabled is False
    assert settings.ibm_cloud_api_key is None
    assert settings.watsonx_project_id is None


@pytest.mark.parametrize(
    ("override", "missing"),
    [
        ({"WATSONX_PROJECT_ID": "project"}, "IBM_CLOUD_API_KEY"),
        ({"IBM_CLOUD_API_KEY": "secret"}, "WATSONX_PROJECT_ID"),
    ],
)
def test_watsonx_enabled_names_missing_required_setting(
    monkeypatch, override, missing
):
    _clear_env(monkeypatch)
    with pytest.raises(ValidationError) as excinfo:
        Settings(
            _env_file=None,
            WATSONX_ENABLED="true",
            **override,
            **REQUIRED_ENV,
        )
    assert missing in str(excinfo.value)


def test_watsonx_enabled_uses_quality_first_tokyo_defaults(monkeypatch):
    _clear_env(monkeypatch)
    settings = Settings(
        _env_file=None,
        WATSONX_ENABLED="true",
        IBM_CLOUD_API_KEY="secret",
        WATSONX_PROJECT_ID="project",
        **REQUIRED_ENV,
    )
    assert settings.watsonx_url == "https://jp-tok.ml.cloud.ibm.com"
    assert settings.watsonx_api_version == "2024-03-14"
    assert settings.watsonx_model == (
        "meta-llama/llama-4-maverick-17b-128e-instruct-fp8"
    )
    assert settings.provider_rpm_watsonx == 60


@pytest.mark.parametrize("setting", ["IBM_CLOUD_API_KEY", "WATSONX_PROJECT_ID"])
def test_watsonx_enabled_rejects_blank_credentials(monkeypatch, setting):
    _clear_env(monkeypatch)
    values = {
        "IBM_CLOUD_API_KEY": "secret",
        "WATSONX_PROJECT_ID": "project",
        setting: "   ",
    }
    with pytest.raises(ValidationError) as excinfo:
        Settings(
            _env_file=None,
            WATSONX_ENABLED="true",
            **values,
            **REQUIRED_ENV,
        )
    assert setting in str(excinfo.value)


def test_watsonx_enabled_rejects_non_https_url(monkeypatch):
    _clear_env(monkeypatch)
    with pytest.raises(ValidationError) as excinfo:
        Settings(
            _env_file=None,
            WATSONX_ENABLED="true",
            IBM_CLOUD_API_KEY="secret",
            WATSONX_PROJECT_ID="project",
            WATSONX_URL="http://jp-tok.ml.cloud.ibm.com",
            **REQUIRED_ENV,
        )
    assert "WATSONX_URL" in str(excinfo.value)


# --- CE-0: RAG_PROFILE presets + feature-flag spine ---

# REQUIRED_ENV pins RAG_PROFILE=baseline for the suite; profile tests build
# their own Settings without that pin.
_ENV_SANS_PROFILE = {k: v for k, v in REQUIRED_ENV.items() if k != "RAG_PROFILE"}


def test_unset_profile_defaults_to_baseline_all_flags_off(monkeypatch):
    _clear_env(monkeypatch)
    s = Settings(_env_file=None, **_ENV_SANS_PROFILE)
    assert s.rag_profile == "baseline"
    assert s.contextual_chunks_enabled is False
    assert s.rag_mode == "linear"
    assert s.graph_rag_enabled is False
    assert s.multimodal_enabled is False


@pytest.mark.parametrize("profile", ["baseline", "contextual", "agentic", "full"])
def test_profile_expands_to_its_flag_set(monkeypatch, profile):
    _clear_env(monkeypatch)
    # CE-5: graph-enabled profiles fail fast without store credentials.
    extra = {"NEO4J_PASSWORD": "test-pw"} if profile == "full" else {}
    s = Settings(_env_file=None, RAG_PROFILE=profile, **extra, **_ENV_SANS_PROFILE)
    for field_name, expected in PROFILE_EXPANSION[profile].items():
        assert getattr(s, field_name) == expected, f"{profile}.{field_name}"


def test_explicit_flag_overrides_profile_preset(monkeypatch):
    _clear_env(monkeypatch)
    s = Settings(
        _env_file=None,
        RAG_PROFILE="full",
        CONTEXTUAL_CHUNKS_ENABLED="false",
        NEO4J_PASSWORD="test-pw",
        **_ENV_SANS_PROFILE,
    )
    # The explicit flag wins; the rest of the preset still applies.
    assert s.contextual_chunks_enabled is False
    assert s.rag_mode == "agentic"
    assert s.graph_rag_enabled is True
    assert s.multimodal_enabled is True


def test_invalid_profile_fails_fast_naming_it(monkeypatch):
    _clear_env(monkeypatch)
    with pytest.raises(ValidationError) as excinfo:
        Settings(_env_file=None, RAG_PROFILE="turbo", **_ENV_SANS_PROFILE)
    assert "RAG_PROFILE" in str(excinfo.value)


def test_invalid_rag_mode_fails_fast_naming_it(monkeypatch):
    _clear_env(monkeypatch)
    with pytest.raises(ValidationError) as excinfo:
        Settings(_env_file=None, RAG_MODE="autonomous", **_ENV_SANS_PROFILE)
    assert "RAG_MODE" in str(excinfo.value)


def test_effective_config_exposes_profile_group(settings):
    from app.config_view import effective_config

    view = effective_config(settings)
    profile = {entry["env"]: entry["value"] for entry in view["groups"]["profile"]}
    assert profile == {
        "RAG_PROFILE": "baseline",
        "CONTEXTUAL_CHUNKS_ENABLED": False,
        "RAG_MODE": "linear",
        "GRAPH_RAG_ENABLED": False,
        "MULTIMODAL_ENABLED": False,
    }
