"""SC-2 per-client rate-limit tests."""

from typing import Any

import httpx
import pytest

from tests.conftest import REQUIRED_ENV
from tests.test_ingest import make_token


@pytest.fixture
def settings(monkeypatch):
    def build(**env: str):
        for key, value in REQUIRED_ENV.items():
            monkeypatch.setenv(key, value)
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        from app.config import Settings

        return Settings()

    return build


def make_app(config):
    from app.main import create_app
    from app.retrieval import get_retrieval_service

    application = create_app(config)

    class FakeRetrieval:
        async def retrieve(self, query, *, k=None, filters=None):
            return []

    application.dependency_overrides[get_retrieval_service] = lambda: FakeRetrieval()
    return application


async def post_retrieve(app, *, token: str | None, headers: dict[str, str] | None = None) -> Any:
    request_headers = dict(headers or {})
    if token:
        request_headers["authorization"] = f"Bearer {token}"
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://rag") as client:
        return await client.post(
            "/internal/retrieve", headers=request_headers, json={"query": "q"}
        )


async def test_exceeding_the_limit_returns_429_with_retry_after(settings):
    config = settings(RATE_LIMIT_DEFAULT="2/minute")
    app = make_app(config)
    token = make_token()

    assert (await post_retrieve(app, token=token)).status_code == 200
    assert (await post_retrieve(app, token=token)).status_code == 200
    third = await post_retrieve(app, token=token)

    assert third.status_code == 429
    assert "Retry-After" in third.headers
    assert int(third.headers["Retry-After"]) >= 1
    assert "Rate limit exceeded" in third.json()["detail"]


async def test_limits_are_per_principal(settings):
    config = settings(RATE_LIMIT_DEFAULT="1/minute")
    app = make_app(config)

    assert (await post_retrieve(app, token=make_token(sub="user-a"))).status_code == 200
    assert (await post_retrieve(app, token=make_token(sub="user-a"))).status_code == 429
    # A different principal has an untouched budget.
    assert (await post_retrieve(app, token=make_token(sub="user-b"))).status_code == 200


async def test_health_is_exempt(settings):
    config = settings(RATE_LIMIT_DEFAULT="1/minute")
    app = make_app(config)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://rag") as client:
        for _ in range(5):
            response = await client.get("/health")
            assert response.status_code in (200, 503)  # never 429


async def test_disabled_toggle_bypasses_limits(settings):
    config = settings(RATE_LIMIT_ENABLED="false", RATE_LIMIT_DEFAULT="1/minute")
    app = make_app(config)
    token = make_token()

    for _ in range(3):
        assert (await post_retrieve(app, token=token)).status_code == 200


async def test_garbage_tokens_never_pollute_a_principals_budget(settings):
    # Router-level JWT verification runs BEFORE the limiter, so unverifiable
    # tokens 401 without consuming anyone's budget.
    config = settings(RATE_LIMIT_DEFAULT="1/minute")
    app = make_app(config)

    for _ in range(3):
        assert (await post_retrieve(app, token="not-a-jwt")).status_code == 401
    assert (await post_retrieve(app, token=make_token(sub="user-c"))).status_code == 200
