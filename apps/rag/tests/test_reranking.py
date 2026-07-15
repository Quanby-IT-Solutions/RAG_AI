"""RT-3 cross-encoder reranking tests."""

import asyncio
from typing import Any

import pytest

from app.reranking import (
    CohereReranker,
    build_reranker,
    rerank_with_fallback,
)
from tests.conftest import REQUIRED_ENV


def make_settings(monkeypatch, **env: str):
    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    from app.config import Settings

    return Settings()


def chunk(content: str, **extra: Any) -> dict[str, Any]:
    return {"id": content, "document_id": "d", "content": content, "rrf_score": 0.1, **extra}


# --------------------------------------------------------------- build_reranker
def test_build_reranker_returns_none_when_disabled(monkeypatch):
    settings = make_settings(monkeypatch, RERANK_ENABLED="false")
    assert build_reranker(settings) is None


def test_build_reranker_cohere_requires_api_key(monkeypatch):
    settings = make_settings(monkeypatch, RERANK_ENABLED="true", RERANK_PROVIDER="cohere")
    with pytest.raises(ValueError, match="COHERE_API_KEY"):
        build_reranker(settings)


def test_build_reranker_selects_provider_by_config(monkeypatch):
    settings = make_settings(
        monkeypatch,
        RERANK_ENABLED="true",
        RERANK_PROVIDER="cohere",
        COHERE_API_KEY="key-1",
    )
    reranker = build_reranker(settings)
    assert isinstance(reranker, CohereReranker)
    assert reranker.provider == "cohere"

    settings = make_settings(monkeypatch, RERANK_ENABLED="true", RERANK_PROVIDER="local")
    assert build_reranker(settings).provider == "local"


def test_rerank_window_is_clamped_to_spec_range(monkeypatch):
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        make_settings(monkeypatch, RERANK_WINDOW="51")
    settings = make_settings(monkeypatch, RERANK_WINDOW="50")
    assert settings.rerank_window == 50


# --------------------------------------------------------- rerank_with_fallback
class FakeReranker:
    model = "fake-model"
    provider = "fake"

    def __init__(self, *, scores: dict[str, float] | None = None, error: Exception | None = None,
                 delay: float = 0.0):
        self.scores = scores or {}
        self.error = error
        self.delay = delay
        self.calls: list[tuple[str, int]] = []

    async def rerank(self, query: str, chunks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        self.calls.append((query, len(chunks)))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error:
            raise self.error
        reordered = [
            {**c, "rerank_score": self.scores.get(c["content"], 0.0)} for c in chunks
        ]
        reordered.sort(key=lambda c: c["rerank_score"], reverse=True)
        return reordered


async def test_rerank_reorders_by_joint_score():
    reranker = FakeReranker(scores={"a": 0.1, "b": 0.9, "c": 0.5})
    chunks = [chunk("a"), chunk("b"), chunk("c")]

    reordered, reranked = await rerank_with_fallback(
        reranker, query="q", chunks=chunks, timeout_seconds=5.0
    )

    assert reranked is True
    assert [c["content"] for c in reordered] == ["b", "c", "a"]
    assert reordered[0]["rerank_score"] == 0.9


async def test_reranker_failure_falls_back_to_fused_order():
    reranker = FakeReranker(error=RuntimeError("provider down"))
    chunks = [chunk("a"), chunk("b")]

    reordered, reranked = await rerank_with_fallback(
        reranker, query="q", chunks=chunks, timeout_seconds=5.0
    )

    assert reranked is False
    assert [c["content"] for c in reordered] == ["a", "b"]


async def test_reranker_timeout_falls_back_to_fused_order():
    reranker = FakeReranker(delay=0.2)
    chunks = [chunk("a"), chunk("b")]

    reordered, reranked = await rerank_with_fallback(
        reranker, query="q", chunks=chunks, timeout_seconds=0.05
    )

    assert reranked is False
    assert [c["content"] for c in reordered] == ["a", "b"]


async def test_wrong_cardinality_from_provider_falls_back():
    class DroppingReranker(FakeReranker):
        async def rerank(self, query, chunks):
            return chunks[:1]

    reordered, reranked = await rerank_with_fallback(
        DroppingReranker(), query="q", chunks=[chunk("a"), chunk("b")], timeout_seconds=5.0
    )
    assert reranked is False
    assert len(reordered) == 2


async def test_empty_candidates_skip_the_provider_call():
    reranker = FakeReranker()
    reordered, reranked = await rerank_with_fallback(
        reranker, query="q", chunks=[], timeout_seconds=5.0
    )
    assert reordered == []
    assert reranked is False
    assert reranker.calls == []


# ------------------------------------------------------------- CohereReranker
async def test_cohere_reranker_parses_v2_rerank_response(monkeypatch):
    settings = make_settings(
        monkeypatch,
        RERANK_ENABLED="true",
        RERANK_PROVIDER="cohere",
        COHERE_API_KEY="key-1",
        RERANK_MODEL="rerank-english-v3.0",
    )
    reranker = CohereReranker(settings)

    captured: dict[str, Any] = {}

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "results": [
                    {"index": 1, "relevance_score": 0.98},
                    {"index": 0, "relevance_score": 0.12},
                ]
            }

    class FakeClient:
        def __init__(self, *args, **kwargs):
            captured["timeout"] = kwargs.get("timeout")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, *, headers, json):
            captured["url"] = url
            captured["headers"] = headers
            captured["json"] = json
            return FakeResponse()

    import app.reranking

    monkeypatch.setattr(app.reranking.httpx, "AsyncClient", FakeClient)

    chunks = [chunk("first"), chunk("second")]
    reordered = await reranker.rerank("the query", chunks)

    assert captured["url"] == "https://api.cohere.com/v2/rerank"
    assert captured["headers"]["authorization"] == "Bearer key-1"
    assert captured["json"] == {
        "model": "rerank-english-v3.0",
        "query": "the query",
        "documents": ["first", "second"],
    }
    assert [c["content"] for c in reordered] == ["second", "first"]
    assert reordered[0]["rerank_score"] == 0.98
