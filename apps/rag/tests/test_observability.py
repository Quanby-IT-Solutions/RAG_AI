"""OB-1 trace/cost tests."""

import pytest

from app.costs import compute_cost_usd, pricing_table
from app.tracing import stage
from tests.conftest import REQUIRED_ENV


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


# --------------------------------------------------------------------- cost
def test_cost_computed_from_pricing_table(settings):
    config = settings()
    cost = compute_cost_usd(
        config,
        model="gpt-4o-mini",
        token_usage={"promptTokens": 1_000_000, "completionTokens": 1_000_000},
    )
    assert cost == pytest.approx(0.75)  # 0.15 in + 0.60 out


def test_unknown_model_or_missing_usage_is_none_not_zero(settings):
    config = settings()
    assert compute_cost_usd(config, model="mystery-model", token_usage={"promptTokens": 5}) is None
    assert compute_cost_usd(config, model="gpt-4o-mini", token_usage=None) is None


def test_pricing_overrides_merge(settings):
    config = settings(MODEL_PRICING_JSON='{"gpt-4o-mini": [1.0, 2.0], "custom": [3, 4]}')
    table = pricing_table(config)
    assert table["gpt-4o-mini"] == (1.0, 2.0)
    assert table["custom"] == (3.0, 4.0)
    assert "claude-opus-4-8" in table  # defaults preserved


def test_invalid_pricing_json_falls_back_to_defaults(settings):
    config = settings(MODEL_PRICING_JSON="{not json")
    assert pricing_table(config)["gpt-4o-mini"] == (0.15, 0.60)


# -------------------------------------------------------------------- stage
async def test_stage_is_a_safe_noop_without_a_tracing_provider():
    async with stage("unit.test", a=1) as record:
        record(done=True)  # must never raise


# ------------------------------------------------------------- payload cost
async def test_answer_payload_and_audit_carry_cost(settings):
    from app.generation import GenerationService
    from tests.test_generation import (
        PRINCIPAL,
        FakeChatClient,
        FakeConversationsRepository,
        FakeRetrievalService,
        make_chunk,
    )

    config = settings()
    repository = FakeConversationsRepository()
    service = GenerationService(
        settings=config,
        retrieval=FakeRetrievalService([make_chunk("Vacation is 20 days.")]),
        chat_client=FakeChatClient(answer="Vacation is 20 days [1]."),
        repository=repository,
    )

    payload = await service.answer(query="vacation?", k=None, principal=PRINCIPAL)

    # FakeChatClient reports usage; gpt-4o-mini pricing applies.
    assert payload["costUsd"] is not None and payload["costUsd"] > 0
    assert repository.audits[-1]["after"]["costUsd"] == payload["costUsd"]
    # trace-id propagation to the persisted message (OB-1 in-scope).
    assert repository.turns[-1]["trace_id"] == payload["traceId"]


# ------------------------------------------------------------- OB-2 metrics
async def test_metrics_endpoint_exposes_prometheus_families(settings):
    import httpx

    from app.main import create_app

    app = create_app(settings())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://rag") as client:
        # Drive one request through the middleware, then scrape.
        await client.get("/health")
        response = await client.get("/metrics")

    assert response.status_code == 200
    body = response.text
    for family in (
        "rag_http_requests_total",
        "rag_http_request_duration_seconds",
        "rag_chat_ttft_seconds",
        "rag_chat_tokens_total",
        "rag_cache_lookups_total",
        "rag_provider_429_total",
        "rag_errors_total",
    ):
        assert family in body
    # The /health request itself was counted.
    assert 'route="/health"' in body


async def test_metrics_endpoint_needs_no_auth_or_rate_limit(settings):
    import httpx

    from app.main import create_app

    app = create_app(settings(RATE_LIMIT_DEFAULT="1/minute"))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://rag") as client:
        for _ in range(5):
            response = await client.get("/metrics")
            assert response.status_code == 200  # never 401/429


def test_json_log_formatter_emits_structured_lines():
    import json as jsonlib
    import logging

    from app.metrics import JsonLogFormatter

    record = logging.LogRecord(
        name="app.access", level=logging.INFO, pathname=__file__, lineno=1,
        msg="request", args=(), exc_info=None,
    )
    record.requestId = "req-1"
    record.route = "POST /internal/chat"
    record.status = 200
    record.durationMs = 12.5

    line = jsonlib.loads(JsonLogFormatter().format(record))
    assert line["message"] == "request"
    assert line["requestId"] == "req-1"
    assert line["route"] == "POST /internal/chat"
    assert line["status"] == 200
    assert line["durationMs"] == 12.5
