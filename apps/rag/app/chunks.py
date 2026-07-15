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

# ── RT-4 metadata filters ──────────────────────────────────────────────────
# Applied PRE-FUSION in BOTH arms (Flow B filter stage) so RRF only ranks
# eligible chunks; a filter matching nothing returns [] and the answer path
# yields a grounded "I don't know" — never an unfiltered fallback.


def build_metadata_filter(
    filters: dict[str, Any] | None, *, start_index: int
) -> tuple[str, list[Any]]:
    """Parameterized SQL predicates over documents/chunk metadata.

    Supported keys: docTypes (list[str], documents.doc_type), source
    (substring of documents.source_uri, case-insensitive), dateFrom/dateTo
    (documents.ingested_at range, ISO strings), section (substring of
    chunks.metadata->>'section', case-insensitive). Returns ("", []) when
    no filter is set."""
    if not filters:
        return "", []
    clauses: list[str] = []
    params: list[Any] = []
    index = start_index

    doc_types = filters.get("docTypes")
    if doc_types:
        clauses.append(f"d.doc_type = ANY(${index}::text[])")
        params.append(list(doc_types))
        index += 1
    source = filters.get("source")
    if source:
        clauses.append(f"d.source_uri ILIKE ${index}")
        params.append(f"%{source}%")
        index += 1
    date_from = filters.get("dateFrom")
    if date_from:
        clauses.append(f"d.ingested_at >= ${index}::timestamptz")
        params.append(date_from)
        index += 1
    date_to = filters.get("dateTo")
    if date_to:
        clauses.append(f"d.ingested_at <= ${index}::timestamptz")
        params.append(date_to)
        index += 1
    section = filters.get("section")
    if section:
        clauses.append(f"chunks.metadata->>'section' ILIKE ${index}")
        params.append(f"%{section}%")
        index += 1

    if not clauses:
        return "", []
    return " AND ".join(clauses), params


def _vector_sql(filter_clause: str) -> str:
    """Cosine similarity = 1 - cosine distance. ORDER BY the raw `<=>`
    distance so the HNSW index (vector_cosine_ops) drives the scan; LIMIT
    applies top-k. Filters join `documents` and predicate pre-fusion."""
    join = "JOIN documents d ON d.id = chunks.document_id" if filter_clause else ""
    where = f"WHERE {filter_clause}" if filter_clause else ""
    return f"""
SELECT chunks.id, chunks.document_id, chunks.content, chunks.metadata,
       chunks.chunk_index, chunks.content_hash,
       1 - (chunks.embedding <=> $1::vector) AS similarity
FROM chunks {join}
{where}
ORDER BY chunks.embedding <=> $1::vector
LIMIT $2
"""


def _keyword_sql(filter_clause: str) -> str:
    """Keyword arm (RT-2): full-text search over the DB-generated `tsv`
    column (GIN-indexed, IN-2). websearch_to_tsquery parses free-form user
    queries safely (quoted phrases, OR, -exclusions) and never raises on odd
    input. Fully DB-resident — no in-memory index to lose on restart."""
    join = "JOIN documents d ON d.id = chunks.document_id" if filter_clause else ""
    extra = f"AND {filter_clause}" if filter_clause else ""
    return f"""
SELECT chunks.id, chunks.document_id, chunks.content, chunks.metadata,
       chunks.chunk_index, chunks.content_hash,
       ts_rank_cd(chunks.tsv, websearch_to_tsquery('english', $1)) AS rank
FROM chunks {join}
WHERE chunks.tsv @@ websearch_to_tsquery('english', $1) {extra}
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
        filters: dict[str, Any] | None = None,
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
                clause, filter_params = build_metadata_filter(filters, start_index=3)
                rows = await with_hard_timeout(
                    conn.fetch(_vector_sql(clause), _vector_literal(embedding), k, *filter_params),
                    seconds=self._settings.db_timeout_seconds,
                    operation="chunks.search_similar",
                )
        finally:
            await conn.close()
        return [_row_to_chunk(row) for row in rows]

    async def search_keyword(
        self, *, query: str, k: int, filters: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        """Full-text top-k over `tsv` (RT-2 keyword arm). Chunks matching no
        term return [] cleanly; `rank` maps onto the shared `similarity` key
        so both arms produce the same row shape."""
        conn = await self._connect()
        try:
            clause, filter_params = build_metadata_filter(filters, start_index=3)
            rows = await with_hard_timeout(
                conn.fetch(_keyword_sql(clause), query, k, *filter_params),
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
