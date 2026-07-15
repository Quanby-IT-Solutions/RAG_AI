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
from app.expansion import SupportsComplete, generate_rewordings, mmr_select
from app.ingest import get_embeddings_client
from app.rate_limit import rate_limit
from app.reranking import Reranker, build_reranker, rerank_with_fallback
from app.security import verify_service_jwt
from app.tracing import stage, traced

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
        expansion_client: SupportsComplete | None = None,
    ) -> None:
        self._settings = settings
        self._embeddings = embeddings
        self._repository = repository
        # RT-3: config-built cross-encoder (None when RERANK_ENABLED is off).
        self._reranker = reranker if reranker is not None else build_reranker(settings)
        # RT-5: cheap-fast rewording client (None -> multi-query unavailable).
        self._expansion_client = expansion_client

    @traced(name="rag.retrieve")
    async def retrieve(
        self,
        query: str,
        *,
        k: int | None = None,
        vector_weight: float | None = None,
        keyword_weight: float | None = None,
        rerank: bool | None = None,
        filters: dict[str, Any] | None = None,
        multi_query: bool | None = None,
        mmr: bool | None = None,
        parent_doc: bool | None = None,
    ) -> list[dict[str, Any]]:
        """Return the top-k chunks (id, document_id, content, metadata,
        chunk_index, similarity, rrf_score[, rerank_score]) for the query,
        best first.

        k / weights / rerank: per-request overrides; defaults come from
        Settings (rerank=None honors the configured reranker; False forces
        fused order for A/B comparison; True requires a configured reranker).
        filters (RT-4): metadata predicates applied PRE-FUSION in both arms
        (docTypes/source/dateFrom/dateTo/section); a filter matching nothing
        returns [] — never an unfiltered fallback.
        multi_query / mmr (RT-5): independent optional stages, default from
        Settings (both off pending eval evidence). Multi-query retrieves for
        the cheap-model rewordings too, union deduped inside RRF; MMR
        re-selects the final top-k balancing relevance vs redundancy.
        parent_doc (RT-6): after selection, matched children are swapped for
        their larger parent chunks (deduped, child order preserved, child
        scores carried); chunks without a parent pass through unchanged.
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

        # ── RT-5 multi-query stage: cheap-model rewordings retrieve too.
        use_multi_query = (
            self._settings.multi_query_enabled if multi_query is None else multi_query
        )
        rewordings: list[str] = []
        if use_multi_query and self._expansion_client is not None:
            rewordings = await generate_rewordings(
                query,
                client=self._expansion_client,
                count=self._settings.multi_query_count,
            )
        elif use_multi_query:
            logger.warning("multi_query.requested_without_expansion_client")
        queries = [query, *rewordings]

        async def vector_arm(vector: list[float] | None) -> list[dict[str, Any]]:
            if w_vector == 0 or vector is None:
                return []
            return await self._repository.search_similar(
                embedding=vector,
                k=pool,
                ef_search=self._settings.hnsw_ef_search,
                filters=filters,
            )

        async def keyword_arm(text: str) -> list[dict[str, Any]]:
            if w_keyword == 0:
                return []
            return await self._repository.search_keyword(query=text, k=pool, filters=filters)

        # Same pinned client as indexing (D1): all variants embed in ONE call.
        query_vectors: list[list[float] | None] = [None] * len(queries)
        if w_vector != 0:
            vectors, _ = await self._embeddings.embed_texts(queries)
            query_vectors = list(vectors)

        # RT-2 AC: both searches execute concurrently (across all variants).
        async with stage("retrieval.arms", variants=len(queries)) as record:
            arm_results = await asyncio.gather(
                *[vector_arm(query_vectors[index]) for index in range(len(queries))],
                *[keyword_arm(text) for text in queries],
            )
            record(
                vectorHits=sum(len(r) for r in arm_results[: len(queries)]),
                keywordHits=sum(len(r) for r in arm_results[len(queries) :]),
            )
        vector_results = arm_results[: len(queries)]
        keyword_results = arm_results[len(queries) :]
        vector_chunks = vector_results[0]
        keyword_chunks = keyword_results[0]

        # Union of every variant's rankings; RRF dedups by chunk id.
        rankings = [
            *[(w_vector, chunks) for chunks in vector_results],
            *[(w_keyword, chunks) for chunks in keyword_results],
        ]
        async with stage("retrieval.fusion", rankings=len(rankings)) as record:
            fused_pool = reciprocal_rank_fusion(rankings, rrf_k=self._settings.rrf_k)
            record(fusedCount=len(fused_pool))

        # ── RT-3 rerank stage: re-score the top window jointly with the query.
        # rerank=False (eval A/B) degrades gracefully to fused order; failures
        # under the hard timeout fall back to fused order too.
        use_rerank = self._reranker is not None if rerank is None else rerank
        reranked = False
        if use_rerank and self._reranker is not None:
            window = self._settings.rerank_window
            async with stage(
                "retrieval.rerank", window=window, provider=self._reranker.provider
            ) as record:
                reordered, reranked = await rerank_with_fallback(
                    self._reranker,
                    query=query,
                    chunks=fused_pool[:window],
                    timeout_seconds=self._settings.rerank_timeout_seconds,
                )
                record(applied=reranked)
            fused_pool = [*reordered, *fused_pool[window:]]

        # ── RT-5 MMR stage: greedy relevance-vs-redundancy re-selection of the
        # final top-k using the chunks' stored embeddings.
        use_mmr = self._settings.mmr_enabled if mmr is None else mmr
        mmr_applied = False
        if use_mmr and len(fused_pool) > top_k:
            embeddings_by_id = await self._repository.fetch_embeddings(
                [chunk["id"] for chunk in fused_pool]
            )
            fused = mmr_select(
                fused_pool,
                embeddings_by_id=embeddings_by_id,
                k=top_k,
                lambda_=self._settings.mmr_lambda,
            )
            mmr_applied = True
        else:
            fused = fused_pool[:top_k]

        # ── RT-6 parent-doc stage: swap matched children for their parents.
        use_parent_doc = (
            self._settings.parent_doc_enabled if parent_doc is None else parent_doc
        )
        matched_children: list[dict[str, str]] = []
        if use_parent_doc:
            parent_ids = [
                chunk["parent_chunk_id"]
                for chunk in fused
                if chunk.get("parent_chunk_id")
            ]
            parents_by_id = (
                await self._repository.fetch_by_ids(list(dict.fromkeys(parent_ids)))
                if parent_ids
                else {}
            )
            swapped: list[dict[str, Any]] = []
            seen_parents: set[str] = set()
            for chunk in fused:
                parent_id = chunk.get("parent_chunk_id")
                parent = parents_by_id.get(parent_id) if parent_id else None
                if parent is None:
                    swapped.append(chunk)
                    continue
                matched_children.append({"childId": chunk["id"], "parentId": parent_id})
                if parent_id in seen_parents:
                    continue  # dedup: several children may share one parent
                seen_parents.add(parent_id)
                # The parent inherits the matching child's ranking scores.
                swapped.append(
                    {
                        **parent,
                        "similarity": chunk.get("similarity"),
                        "rrf_score": chunk.get("rrf_score"),
                        **(
                            {"rerank_score": chunk["rerank_score"]}
                            if "rerank_score" in chunk
                            else {}
                        ),
                        "arm_ranks": chunk.get("arm_ranks"),
                    }
                )
            fused = swapped

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
                    # RT-4: the trace records exactly which corpus subset was
                    # eligible for this request.
                    "filters": filters or None,
                    "rerank": {
                        "requested": use_rerank,
                        "applied": reranked,
                        "window": self._settings.rerank_window if use_rerank else None,
                        "provider": self._reranker.provider
                        if use_rerank and self._reranker
                        else None,
                    },
                    # RT-5: rewordings + per-reword hits are trace-visible.
                    "multiQuery": {
                        "requested": use_multi_query,
                        "rewordings": rewordings,
                        "perQueryHits": [
                            {
                                "query": text,
                                "vectorHits": len(vector_results[index]),
                                "keywordHits": len(keyword_results[index]),
                            }
                            for index, text in enumerate(queries)
                        ],
                    }
                    if use_multi_query
                    else {"requested": False},
                    "mmr": {
                        "requested": use_mmr,
                        "applied": mmr_applied,
                        "lambda": self._settings.mmr_lambda if use_mmr else None,
                    },
                    # RT-6: the matching children behind each returned parent.
                    "parentDoc": {
                        "requested": use_parent_doc,
                        "matchedChildren": matched_children,
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
    settings = request.app.state.settings
    expansion_client = None
    if settings.multi_query_enabled:
        # Cheap-fast model rewrites queries (RT-5); built only when enabled.
        from app.providers import OpenAIChatClient

        expansion_client = OpenAIChatClient(settings)
    return RetrievalService(
        settings=settings,
        embeddings=get_embeddings_client(request),
        repository=get_chunks_repository(request),
        expansion_client=expansion_client,
    )


router = APIRouter(prefix="/internal", dependencies=[Depends(verify_service_jwt)])


class RetrievalFilters(BaseModel):
    """RT-4 metadata filters — restrict retrieval to a corpus subset."""

    doc_types: list[str] | None = Field(default=None, alias="docTypes")
    source: str | None = Field(default=None, max_length=500)
    date_from: str | None = Field(default=None, alias="dateFrom")
    date_to: str | None = Field(default=None, alias="dateTo")
    section: str | None = Field(default=None, max_length=200)

    model_config = {"populate_by_name": True}

    def to_query_filters(self) -> dict[str, Any] | None:
        payload = {
            "docTypes": self.doc_types,
            "source": self.source,
            "dateFrom": self.date_from,
            "dateTo": self.date_to,
            "section": self.section,
        }
        cleaned = {key: value for key, value in payload.items() if value}
        return cleaned or None


class RetrieveRequest(BaseModel):
    query: str = Field(min_length=1, max_length=4000)
    k: int | None = Field(default=None, ge=1, le=50)
    filters: RetrievalFilters | None = Field(default=None)


@router.post("/retrieve", dependencies=[Depends(rate_limit("rate_limit_default"))])
async def retrieve_endpoint(
    payload: RetrieveRequest,
    retrieval: Annotated[RetrievalService, Depends(get_retrieval_service)],
) -> dict[str, Any]:
    """Run the real hybrid retrieval pipeline (EV-1 chunk picker, EV-2 harness).

    Returns the fused top-k chunks including each chunk's content_hash — the
    stable binding key golden items store."""
    chunks = await retrieval.retrieve(
        payload.query,
        k=payload.k,
        filters=payload.filters.to_query_filters() if payload.filters else None,
    )
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
