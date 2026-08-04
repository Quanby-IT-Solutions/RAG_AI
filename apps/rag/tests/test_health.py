import httpx
import pytest

from app import health as health_checks
from app.main import create_app


async def _get_health(settings, monkeypatch, db_ok: bool, redis_ok: bool) -> httpx.Response:
    async def fake_db(_settings) -> None:
        if not db_ok:
            raise ConnectionError("db down")

    async def fake_redis(_settings) -> None:
        if not redis_ok:
            raise ConnectionError("redis down")

    monkeypatch.setattr(health_checks, "check_database", fake_db)
    monkeypatch.setattr(health_checks, "check_redis", fake_redis)

    app = create_app(settings)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://rag") as client:
        return await client.get("/health")


async def test_health_ok_when_db_and_redis_up(settings, monkeypatch):
    response = await _get_health(settings, monkeypatch, db_ok=True, redis_ok=True)
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["live"] is True
    assert body["ready"] is True
    assert body["checks"] == {"database": "up", "redis": "up"}


async def test_health_503_when_db_down(settings, monkeypatch):
    response = await _get_health(settings, monkeypatch, db_ok=False, redis_ok=True)
    assert response.status_code == 503
    body = response.json()
    assert body["ready"] is False
    assert body["checks"]["database"] == "down"
    assert body["checks"]["redis"] == "up"


async def test_health_503_when_redis_down(settings, monkeypatch):
    response = await _get_health(settings, monkeypatch, db_ok=True, redis_ok=False)
    assert response.status_code == 503
    assert response.json()["checks"]["redis"] == "down"


# ------------------------------------------------------------- OB-3 probes
@pytest.mark.parametrize(
    ("status_code", "expected"),
    [
        pytest.param(200, "up", id="success-200"),
        pytest.param(401, "unauthorized", id="unauthorized-401"),
        pytest.param(403, "unauthorized", id="unauthorized-403"),
        pytest.param(429, "up", id="rate-limited-429"),
        pytest.param(500, "down", id="server-error-500"),
    ],
)
async def test_probe_provider_classifies_http_status(
    monkeypatch, status_code: int, expected: str
):
    class SyntheticAsyncClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return None

        async def get(self, url, headers):
            return httpx.Response(status_code)

    monkeypatch.setattr(
        health_checks.httpx,
        "AsyncClient",
        lambda *, timeout: SyntheticAsyncClient(),
    )

    assert (
        await health_checks._probe_provider("provider", "https://provider", {}, 1.0)
        == expected
    )


async def test_probe_provider_reports_network_failure_as_down(monkeypatch):
    class FailingAsyncClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return None

        async def get(self, url, headers):
            raise httpx.ConnectError("synthetic network failure")

    monkeypatch.setattr(
        health_checks.httpx,
        "AsyncClient",
        lambda *, timeout: FailingAsyncClient(),
    )

    assert await health_checks._probe_provider("provider", "https://provider", {}, 1.0) == "down"


async def test_health_unauthorized_provider_degrades_without_failing_readiness(
    settings, monkeypatch
):
    async def fake_providers(_settings):
        return {"openai": "unauthorized"}

    monkeypatch.setattr(health_checks, "check_providers", fake_providers)

    response = await _get_health(settings, monkeypatch, db_ok=True, redis_ok=True)

    assert response.status_code == 200
    body = response.json()
    assert body["ready"] is True
    assert body["providers"] == {"openai": "unauthorized"}
    assert body["status"] == "degraded"


async def test_provider_probes_are_cached_per_interval(monkeypatch):
    import app.health as health
    from tests.conftest import REQUIRED_ENV

    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("PROVIDER_PROBE_ENABLED", "true")
    monkeypatch.setenv("PROVIDER_PROBE_INTERVAL_SECONDS", "60")
    from app.config import Settings

    settings = Settings(_env_file=None)
    health._provider_cache.clear()

    calls = {"count": 0}

    async def fake_probe(name, url, headers, probe_timeout):
        calls["count"] += 1
        return "up"

    monkeypatch.setattr(health, "_probe_provider", fake_probe)

    first = await health.check_providers(settings)
    second = await health.check_providers(settings)  # served from cache

    assert first == {"openai": "up"} == second
    assert calls["count"] == 1  # probed ONCE, never per request


async def test_health_body_reports_providers_and_degraded_status(monkeypatch):
    import httpx

    import app.health as health
    from app.main import create_app
    from tests.conftest import REQUIRED_ENV

    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("PROVIDER_PROBE_ENABLED", "true")
    from app.config import Settings

    settings = Settings(_env_file=None)
    health._provider_cache.clear()

    async def fake_probe(name, url, headers, probe_timeout):
        return "down"

    monkeypatch.setattr(health, "_probe_provider", fake_probe)
    app = create_app(settings)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://rag") as client:
        response = await client.get("/health")

    body = response.json()
    assert body["providers"] == {"openai": "down"}
    # Provider unreachability degrades status but never fails READINESS
    # (the LB must keep routing; the GN-5 breaker owns runtime failover).
    if body["ready"]:
        assert body["status"] == "degraded"
        assert response.status_code == 200


async def test_probes_disabled_returns_empty(monkeypatch):
    import app.health as health
    from tests.conftest import REQUIRED_ENV

    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("PROVIDER_PROBE_ENABLED", "false")
    from app.config import Settings

    assert await health.check_providers(Settings(_env_file=None)) == {}


async def test_watsonx_health_is_absent_when_disabled(monkeypatch):
    import app.health as health
    from app.config import Settings
    from tests.conftest import REQUIRED_ENV

    settings = Settings(
        _env_file=None,
        WATSONX_ENABLED="false",
        **{**REQUIRED_ENV, "PROVIDER_PROBE_ENABLED": "true"},
    )
    health._provider_cache.clear()

    async def fake_probe(name, url, headers, probe_timeout):
        assert name == "openai"
        return "up"

    monkeypatch.setattr(health, "_probe_provider", fake_probe)
    results = await health.check_providers(settings)

    assert results == {"openai": "up"}
    assert "watsonx" not in health._provider_cache


async def test_watsonx_health_is_cached_when_enabled(monkeypatch):
    import app.health as health
    from app.config import Settings
    from tests.conftest import REQUIRED_ENV

    settings = Settings(
        _env_file=None,
        WATSONX_ENABLED="true",
        IBM_CLOUD_API_KEY="ibm-test",
        WATSONX_PROJECT_ID="project",
        **{**REQUIRED_ENV, "PROVIDER_PROBE_ENABLED": "true"},
    )
    health._provider_cache.clear()
    health._provider_cache["openai"] = ("up", float("inf"))
    calls = {"tokens": 0, "probes": 0, "closes": 0}

    class FakeTokens:
        async def get_token(self):
            calls["tokens"] += 1
            return "iam-token"

        async def aclose(self):
            calls["closes"] += 1

    async def fake_probe(name, url, headers, probe_timeout):
        calls["probes"] += 1
        assert name == "watsonx"
        assert "/ml/v1/foundation_model_specs" in url
        assert headers == {"authorization": "Bearer iam-token"}
        return "up"

    monkeypatch.setattr(
        health, "WatsonxIamTokenProvider", lambda _settings: FakeTokens()
    )
    monkeypatch.setattr(health, "_probe_provider", fake_probe)
    first = await health.check_providers(settings)
    second = await health.check_providers(settings)

    assert first["watsonx"] == second["watsonx"] == "up"
    assert calls == {"tokens": 1, "probes": 1, "closes": 1}


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        pytest.param(
            "unauthorized-401", "unauthorized", id="iam-unauthorized-401"
        ),
        pytest.param(
            "unauthorized-403", "unauthorized", id="iam-unauthorized-403"
        ),
        pytest.param("server-error", "down", id="iam-server-error"),
        pytest.param("offline", "down", id="iam-offline"),
    ],
)
async def test_watsonx_health_maps_token_failures(
    monkeypatch, error, expected
):
    import app.health as health
    from app.config import Settings
    from app.watsonx import WatsonxApiError
    from tests.conftest import REQUIRED_ENV

    failures = {
        "unauthorized-401": WatsonxApiError("iam_token", 401, "invalid"),
        "unauthorized-403": WatsonxApiError("iam_token", 403, "forbidden"),
        "server-error": WatsonxApiError("iam_token", 500, "server_error"),
        "offline": ConnectionError("offline"),
    }
    settings = Settings(
        _env_file=None,
        WATSONX_ENABLED="true",
        IBM_CLOUD_API_KEY="ibm-test",
        WATSONX_PROJECT_ID="project",
        **{**REQUIRED_ENV, "PROVIDER_PROBE_ENABLED": "true"},
    )
    health._provider_cache.clear()
    health._provider_cache["openai"] = ("up", float("inf"))

    class FailingTokens:
        async def get_token(self):
            raise failures[error]

        async def aclose(self):
            return None

    monkeypatch.setattr(
        health, "WatsonxIamTokenProvider", lambda _settings: FailingTokens()
    )

    results = await health.check_providers(settings)

    assert results["watsonx"] == expected
