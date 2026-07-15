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

GN-1's /chat path calls this service in-process (get_retrieval_service is
the FastAPI dependency seam). EV-1 adds ONE thin HTTP surface —
POST /internal/retrieve — so the golden-set console (and the EV-2 harness)
can run the REAL retrieval pipeline, not a reimplementation.
"""

import asyncio
import json
import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from app.chunks import ChunksRepository
from app.config import Settings
from app.embeddings import EmbeddingsClient
from app.ingest import get_embeddings_client
from app.reranking import Reranker, build_reranker, rerank_with_fallback
from app.security import verify_service_jwt
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
        reranker: Reranker | None = None,
    ) -> None:
        self._settings = settings
        self._embeddings = embeddings
        self._repository = repository
        # RT-3: config-built cross-encoder (None when RERANK_ENABLED is off).
        self._reranker = reranker if reranker is not None else build_reranker(settings)

    @traced(name="rag.retrieve")
    async def retrieve(
        self,
        query: str,
        *,
        k: int | None = None,
        vector_weight: float | None = None,
        keyword_weight: float | None = None,
        rerank: bool | None = None,
    ) -> list[dict[str, Any]]:
        """Return the top-k chunks (id, document_id, content, metadata,
        chunk_index, similarity, rrf_score[, rerank_score]) for the query,
        best first.

        k / weights / rerank: per-request overrides; defaults come from
        Settings (rerank=None honors the configured reranker; False forces
        fused order for A/B comparison; True requires a configured reranker).
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

        fused_pool = reciprocal_rank_fusion(
            [(w_vector, vector_chunks), (w_keyword, keyword_chunks)],
            rrf_k=self._settings.rrf_k,
        )

        # ── RT-3 rerank stage: re-score the top window jointly with the query.
        # rerank=False (eval A/B) degrades gracefully to fused order; failures
        # under the hard timeout fall back to fused order too.
        use_rerank = self._reranker is not None if rerank is None else rerank
        reranked = False
        if use_rerank and self._reranker is not None:
            window = self._settings.rerank_window
            reordered, reranked = await rerank_with_fallback(
                self._reranker,
                query=query,
                chunks=fused_pool[:window],
                timeout_seconds=self._settings.rerank_timeout_seconds,
            )
            fused_pool = [*reordered, *fused_pool[window:]]
        fused = fused_pool[:top_k]

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
                    "rerank": {
                        "requested": use_rerank,
                        "applied": reranked,
                        "window": self._settings.rerank_window if use_rerank else None,
                        "provider": self._reranker.provider
                        if use_rerank and self._reranker
                        else None,
                    },
                    "vector_arm": [
                        {"id": chunk["id"], "similarity": chunk["similarity"]}
                        for chunk in vector_chunks
                    ],
                    "keyword_arm": [
                        {"id": chunk["id"], "rank_score": chunk["similarity"]}
                        for chunk in keyword_chunks
                    ],
                    "chunks": [
                        {
                            "id": chunk["id"],
                            "rrf_score": chunk["rrf_score"],
                            **(
                                {"rerank_score": chunk["rerank_score"]}
                                if "rerank_score" in chunk
                                else {}
                            ),
                        }
                        for chunk in fused
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


router = APIRouter(prefix="/internal", dependencies=[Depends(verify_service_jwt)])


class RetrieveRequest(BaseModel):
    query: str = Field(min_length=1, max_length=4000)
    k: int | None = Field(default=None, ge=1, le=50)


@router.post("/retrieve")
async def retrieve_endpoint(
    payload: RetrieveRequest,
    retrieval: Annotated[RetrievalService, Depends(get_retrieval_service)],
) -> dict[str, Any]:
    """Run the real hybrid retrieval pipeline (EV-1 chunk picker, EV-2 harness).

    Returns the fused top-k chunks including each chunk's content_hash — the
    stable binding key golden items store."""
    chunks = await retrieval.retrieve(payload.query, k=payload.k)
    return {
        "chunks": [
            {
                "id": chunk["id"],
                "documentId": chunk["document_id"],
                "contentHash": chunk["content_hash"],
                "content": chunk["content"],
                "score": chunk.get("rrf_score") or chunk.get("similarity"),
            }
            for chunk in chunks
        ]
    }
