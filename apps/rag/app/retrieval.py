"""Hybrid retrieval service (Flow B steps 3-4, RT-1 + RT-2).

Embeds the incoming query with the SAME pinned EmbeddingsClient as indexing
(one shared source of truth — index and query embeddings can never diverge),
runs the pgvector HNSW vector search AND the tsvector full-text search
CONCURRENTLY (asyncio.gather), and fuses the two rankings with Reciprocal
Rank Fusion under configurable weights (default 50/50 — PRD decision). Both
arms are plain SQL against `chunks` — fully DB-resident, so a replica restart
changes nothing (no in-memory index exists to lose).

The retrieved chunk ids/scores are logged per request. "Retrieved" chunks are
DISTINCT from the "used in prompt" chunks GN-2 logs later — this record is
retrieval-only.

No HTTP surface here: GN-1's /chat path calls this service in-process
(get_retrieval_service is the FastAPI dependency seam).
"""

import asyncio
import json
import logging
from typing import Any

from fastapi import Request

from app.chunks import ChunksRepository
from app.config import Settings
from app.embeddings import EmbeddingsClient
from app.ingest import get_embeddings_client
from app.tracing import traced

logger = logging.getLogger(__name__)


def reciprocal_rank_fusion(
    rankings: list[tuple[float, list[dict[str, Any]]]],
    *,
    rrf_k: int,
) -> list[dict[str, Any]]:
    """Fuse (weight, ranked-chunks) lists: score = Σ weight / (rrf_k + rank).

    rank is 1-based within each arm; a chunk absent from an arm contributes
    nothing for it. Returns unique chunks ordered by fused score (desc), each
    annotated with `rrf_score` and the per-arm ranks it appeared at."""
    fused: dict[str, dict[str, Any]] = {}
    for arm_index, (weight, chunks) in enumerate(rankings):
        for rank, chunk in enumerate(chunks, start=1):
            entry = fused.setdefault(
                chunk["id"],
                {**chunk, "rrf_score": 0.0, "arm_ranks": {}},
            )
            entry["rrf_score"] += weight / (rrf_k + rank)
            entry["arm_ranks"][arm_index] = rank
    return sorted(fused.values(), key=lambda entry: entry["rrf_score"], reverse=True)


class RetrievalService:
    """Query embed -> (vector ∥ keyword) -> RRF -> retrieved-chunk log."""

    def __init__(
        self,
        settings: Settings,
        embeddings: EmbeddingsClient,
        repository: ChunksRepository,
    ) -> None:
        self._settings = settings
        self._embeddings = embeddings
        self._repository = repository

    @traced(name="rag.retrieve")
    async def retrieve(
        self,
        query: str,
        *,
        k: int | None = None,
        vector_weight: float | None = None,
        keyword_weight: float | None = None,
    ) -> list[dict[str, Any]]:
        """Return the top-k fused chunks (id, document_id, content, metadata,
        chunk_index, similarity, rrf_score) for the query, best first.

        k / weights: per-request overrides; defaults come from Settings.
        Empty corpus (or no matches in either arm) -> [] cleanly (GN-1
        answers "I don't know")."""
        top_k = self._settings.retrieval_top_k if k is None else k
        if top_k < 1:
            raise ValueError(f"k must be >= 1; got {k}")
        w_vector = (
            self._settings.retrieval_vector_weight if vector_weight is None else vector_weight
        )
        w_keyword = (
            self._settings.retrieval_keyword_weight if keyword_weight is None else keyword_weight
        )
        # Each arm feeds the fusion a candidate pool at least as deep as k
        # (RT-3's rerank window reads the same pool).
        pool = max(top_k, self._settings.retrieval_candidate_pool)

        async def vector_arm() -> list[dict[str, Any]]:
            if w_vector == 0:
                return []
            # Same pinned client as indexing (D1): model + dims enforced there.
            vectors, _ = await self._embeddings.embed_texts([query])
            return await self._repository.search_similar(
                embedding=vectors[0],
                k=pool,
                ef_search=self._settings.hnsw_ef_search,
            )

        async def keyword_arm() -> list[dict[str, Any]]:
            if w_keyword == 0:
                return []
            return await self._repository.search_keyword(query=query, k=pool)

        # RT-2 AC: both searches execute concurrently.
        vector_chunks, keyword_chunks = await asyncio.gather(vector_arm(), keyword_arm())

        fused = reciprocal_rank_fusion(
            [(w_vector, vector_chunks), (w_keyword, keyword_chunks)],
            rrf_k=self._settings.rrf_k,
        )[:top_k]

        # Per-request retrieved-chunk record (RT-1/RT-2 observability). stage
        # is "retrieved" — NOT "used in prompt" (GN-2 logs that separately).
        logger.info(
            "retrieval.retrieved_chunks %s",
            json.dumps(
                {
                    "stage": "retrieved",
                    "k": top_k,
                    "ef_search": self._settings.hnsw_ef_search,
                    "weights": {"vector": w_vector, "keyword": w_keyword},
                    "rrf_k": self._settings.rrf_k,
                    "vector_arm": [
                        {"id": chunk["id"], "similarity": chunk["similarity"]}
                        for chunk in vector_chunks
                    ],
                    "keyword_arm": [
                        {"id": chunk["id"], "rank_score": chunk["similarity"]}
                        for chunk in keyword_chunks
                    ],
                    "chunks": [
                        {"id": chunk["id"], "rrf_score": chunk["rrf_score"]} for chunk in fused
                    ],
                }
            ),
        )
        return fused


def get_chunks_repository(request: Request) -> ChunksRepository:
    """Dependency seam so tests can override persistence."""
    return ChunksRepository(request.app.state.settings)


def get_retrieval_service(request: Request) -> RetrievalService:
    """Dependency seam for the GN-1 /chat path — reuses the shared per-app
    EmbeddingsClient so index and query embeddings share one client."""
    return RetrievalService(
        settings=request.app.state.settings,
        embeddings=get_embeddings_client(request),
        repository=get_chunks_repository(request),
    )
