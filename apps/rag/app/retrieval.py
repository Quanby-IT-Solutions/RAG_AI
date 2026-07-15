"""Vector top-k retrieval service (Flow B steps 3-4, RT-1).

Embeds the incoming query with the SAME pinned EmbeddingsClient as indexing
(one shared source of truth — index and query embeddings can never diverge),
runs the pgvector HNSW top-k search (ChunksRepository), and logs the retrieved
chunk ids/scores per request. "Retrieved" chunks are DISTINCT from the
"used in prompt" chunks GN-2 logs later — this record is retrieval-only.

No HTTP surface here: GN-1 builds the /chat path and calls this service
in-process (get_retrieval_service is the FastAPI dependency seam).
"""

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


class RetrievalService:
    """Query embed -> HNSW top-k -> retrieved-chunk log, as one traced step."""

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
    async def retrieve(self, query: str, *, k: int | None = None) -> list[dict[str, Any]]:
        """Return the top-k chunks (id, document_id, content, metadata,
        chunk_index, similarity) for the query, most similar first.

        k: per-request override; defaults to RETRIEVAL_TOP_K. Empty corpus ->
        [] cleanly (GN-1 answers "I don't know")."""
        top_k = self._settings.retrieval_top_k if k is None else k
        if top_k < 1:
            raise ValueError(f"k must be >= 1; got {k}")

        # Same pinned client as indexing (D1): model + dimensions enforced there.
        vectors, _ = await self._embeddings.embed_texts([query])
        chunks = await self._repository.search_similar(
            embedding=vectors[0],
            k=top_k,
            ef_search=self._settings.hnsw_ef_search,
        )

        # Per-request retrieved-chunk record (RT-1 observability). stage is
        # "retrieved" — NOT "used in prompt" (GN-2 adds that separately). The
        # traced return value also lands on the rag.retrieve trace span.
        logger.info(
            "retrieval.retrieved_chunks %s",
            json.dumps(
                {
                    "stage": "retrieved",
                    "k": top_k,
                    "ef_search": self._settings.hnsw_ef_search,
                    "chunks": [
                        {"id": chunk["id"], "similarity": chunk["similarity"]} for chunk in chunks
                    ],
                }
            ),
        )
        return chunks


def get_chunks_repository(request: Request) -> ChunksRepository:
    """Dependency seam so tests can override persistence."""
    return ChunksRepository(request.app.state.settings)


def get_retrieval_service(request: Request) -> RetrievalService:
    """Dependency seam for the (future) GN-1 /chat path — reuses the shared
    per-app EmbeddingsClient so index and query embeddings share one client."""
    return RetrievalService(
        settings=request.app.state.settings,
        embeddings=get_embeddings_client(request),
        repository=get_chunks_repository(request),
    )
