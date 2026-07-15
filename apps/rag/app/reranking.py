"""Cross-encoder reranking of hybrid candidates (RT-3, Flow B rerank stage).

The top RERANK_WINDOW fused candidates are re-scored JOINTLY with the query
by a cross-encoder and re-ordered before top-k selection. The reranker sits
behind a small interface so providers swap by config (§XI no-lock-in):

- "cohere": hosted /v2/rerank over httpx (no SDK dependency)
- "local":  sentence-transformers CrossEncoder (optional `rerank` dep group)

Failure policy (AC): every rerank call runs under a hard timeout and ANY
failure degrades gracefully to the fused order — retrieval never breaks
because reranking did.
"""

import asyncio
import logging
from typing import Any, Protocol

import httpx

from app.config import Settings
from app.timeouts import with_hard_timeout

logger = logging.getLogger(__name__)


class Reranker(Protocol):
    """Scores candidates jointly with the query; returns them best-first."""

    model: str
    provider: str

    async def rerank(self, query: str, chunks: list[dict[str, Any]]) -> list[dict[str, Any]]: ...


class CohereReranker:
    """Hosted cross-encoder: POST https://api.cohere.com/v2/rerank."""

    def __init__(self, settings: Settings) -> None:
        if not settings.cohere_api_key:
            raise ValueError("RERANK_PROVIDER=cohere requires COHERE_API_KEY")
        self._settings = settings
        self.model = settings.rerank_model
        self.provider = "cohere"

    async def rerank(self, query: str, chunks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        api_key = self._settings.cohere_api_key
        assert api_key is not None
        async with httpx.AsyncClient(timeout=self._settings.rerank_timeout_seconds) as client:
            response = await client.post(
                "https://api.cohere.com/v2/rerank",
                headers={"authorization": f"Bearer {api_key.get_secret_value()}"},
                json={
                    "model": self.model,
                    "query": query,
                    "documents": [chunk["content"] for chunk in chunks],
                },
            )
            response.raise_for_status()
        results = response.json()["results"]
        reordered = []
        for entry in results:
            chunk = dict(chunks[entry["index"]])
            chunk["rerank_score"] = float(entry["relevance_score"])
            reordered.append(chunk)
        return reordered


class LocalCrossEncoderReranker:
    """Local sentence-transformers CrossEncoder (optional dependency).

    The model loads lazily on first use and scoring runs in a worker thread
    (CrossEncoder.predict is CPU-bound/synchronous)."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self.model = settings.rerank_model
        self.provider = "local"
        self._encoder: Any = None

    def _load(self) -> Any:
        if self._encoder is None:
            try:
                from sentence_transformers import CrossEncoder
            except ImportError as exc:  # pragma: no cover - dependency guard
                raise RuntimeError(
                    "RERANK_PROVIDER=local requires the optional `rerank` dependency "
                    "group (uv sync --group rerank)"
                ) from exc
            self._encoder = CrossEncoder(self.model)
        return self._encoder

    async def rerank(self, query: str, chunks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        def _score() -> list[float]:
            encoder = self._load()
            pairs = [(query, chunk["content"]) for chunk in chunks]
            return [float(s) for s in encoder.predict(pairs)]

        scores = await asyncio.to_thread(_score)
        reordered = [
            {**chunk, "rerank_score": score} for chunk, score in zip(chunks, scores, strict=True)
        ]
        reordered.sort(key=lambda chunk: chunk["rerank_score"], reverse=True)
        return reordered


def build_reranker(settings: Settings) -> Reranker | None:
    """Config-selected reranker; None when RERANK_ENABLED is off."""
    if not settings.rerank_enabled:
        return None
    if settings.rerank_provider == "local":
        return LocalCrossEncoderReranker(settings)
    return CohereReranker(settings)


async def rerank_with_fallback(
    reranker: Reranker,
    *,
    query: str,
    chunks: list[dict[str, Any]],
    timeout_seconds: float,
) -> tuple[list[dict[str, Any]], bool]:
    """Rerank under a hard timeout; ANY failure falls back to fused order.

    Returns (chunks, reranked): `reranked` is False when the fallback fired
    (recorded in the retrieval log so eval comparisons stay honest)."""
    if not chunks:
        return chunks, False
    try:
        reordered = await with_hard_timeout(
            reranker.rerank(query, chunks),
            seconds=timeout_seconds,
            operation=f"rerank.{reranker.provider}",
        )
        if len(reordered) != len(chunks):
            raise ValueError(
                f"reranker returned {len(reordered)} chunks for {len(chunks)} candidates"
            )
        return reordered, True
    except Exception:
        logger.warning(
            "rerank.fallback_to_fused_order",
            extra={"provider": reranker.provider, "model": reranker.model},
            exc_info=True,
        )
        return chunks, False
