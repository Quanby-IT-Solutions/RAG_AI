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
