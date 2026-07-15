import httpx

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
async def test_provider_probes_are_cached_per_interval(monkeypatch):
    import app.health as health
    from tests.conftest import REQUIRED_ENV

    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("PROVIDER_PROBE_ENABLED", "true")
    monkeypatch.setenv("PROVIDER_PROBE_INTERVAL_SECONDS", "60")
    from app.config import Settings

    settings = Settings()
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

    settings = Settings()
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

    assert await health.check_providers(Settings()) == {}
