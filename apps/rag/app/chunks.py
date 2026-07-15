"""Read-only chunks retrieval persistence (Flow B / RT-1) — asyncpg over the
Drizzle-owned schema.

Python runs NO migrations and the `ingestion` epic owns the `chunks` schema
(DOMAIN-MODEL cross-epic rule): this repository only READS. The top-k query
uses pgvector cosine distance (`<=>`), matching the IN-2 HNSW index built with
`vector_cosine_ops`, and sets `hnsw.ef_search` per query via SET LOCAL inside
the same transaction. Every call goes through the shared hard-timeout helper.
"""

import json
from typing import Any

import asyncpg

from app.config import Settings
from app.documents import _vector_literal
from app.timeouts import with_hard_timeout

# Cosine similarity = 1 - cosine distance. ORDER BY the raw `<=>` distance so
# the HNSW index (vector_cosine_ops) drives the scan; LIMIT applies top-k.
_TOP_K_SQL = """
SELECT id, document_id, content, metadata, chunk_index, content_hash,
       1 - (embedding <=> $1::vector) AS similarity
FROM chunks
ORDER BY embedding <=> $1::vector
LIMIT $2
"""

# Keyword arm (RT-2): full-text search over the DB-generated `tsv` column
# (GIN-indexed, IN-2). websearch_to_tsquery parses free-form user queries
# safely (quoted phrases, OR, -exclusions) and never raises on odd input.
# Fully DB-resident — no in-memory index to lose on replica restart.
_KEYWORD_SQL = """
SELECT id, document_id, content, metadata, chunk_index, content_hash,
       ts_rank_cd(tsv, websearch_to_tsquery('english', $1)) AS rank
FROM chunks
WHERE tsv @@ websearch_to_tsquery('english', $1)
ORDER BY rank DESC
LIMIT $2
"""


def _row_to_chunk(row: Any) -> dict[str, Any]:
    metadata = row["metadata"]
    return {
        "id": str(row["id"]),
        "document_id": str(row["document_id"]),
        "content": row["content"],
        "metadata": json.loads(metadata) if isinstance(metadata, str) else metadata,
        "chunk_index": row["chunk_index"],
        # EV-1: expected-chunk binding key — stable across re-indexing of
        # unchanged content (doc-scoped chunk hash, IN-2).
        "content_hash": row["content_hash"],
        "similarity": float(row["similarity"]),
    }


class ChunksRepository:
    """Read-only similarity search over `chunks` (ingestion owns writes)."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def _connect(self) -> asyncpg.Connection:
        return await with_hard_timeout(
            asyncpg.connect(dsn=self._settings.database_url),
            seconds=self._settings.db_timeout_seconds,
            operation="postgres.connect",
        )

    async def search_similar(
        self,
        *,
        embedding: list[float],
        k: int,
        ef_search: int,
    ) -> list[dict[str, Any]]:
        """HNSW top-k cosine similarity search. Empty corpus -> [] (no error).

        `SET LOCAL hnsw.ef_search` scopes the search breadth to this
        transaction only — SET LOCAL takes no bind parameters, so the value is
        validated as a positive int before interpolation."""
        ef = int(ef_search)
        if ef < 1:
            raise ValueError(f"ef_search must be >= 1; got {ef_search}")
        conn = await self._connect()
        try:
            async with conn.transaction():
                await with_hard_timeout(
                    conn.execute(f"SET LOCAL hnsw.ef_search = {ef}"),
                    seconds=self._settings.db_timeout_seconds,
                    operation="chunks.set_ef_search",
                )
                rows = await with_hard_timeout(
                    conn.fetch(_TOP_K_SQL, _vector_literal(embedding), k),
                    seconds=self._settings.db_timeout_seconds,
                    operation="chunks.search_similar",
                )
        finally:
            await conn.close()
        return [_row_to_chunk(row) for row in rows]

    async def search_keyword(self, *, query: str, k: int) -> list[dict[str, Any]]:
        """Full-text top-k over `tsv` (RT-2 keyword arm). Chunks matching no
        term return [] cleanly; `rank` maps onto the shared `similarity` key
        so both arms produce the same row shape."""
        conn = await self._connect()
        try:
            rows = await with_hard_timeout(
                conn.fetch(_KEYWORD_SQL, query, k),
                seconds=self._settings.db_timeout_seconds,
                operation="chunks.search_keyword",
            )
        finally:
            await conn.close()
        results = []
        for row in rows:
            chunk = _row_to_chunk({**dict(row), "similarity": row["rank"]})
            results.append(chunk)
        return results
