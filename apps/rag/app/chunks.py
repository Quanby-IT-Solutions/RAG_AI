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


def _vector_sql(filter_clause: str, column: str = "embedding") -> str:
    """Cosine similarity = 1 - cosine distance. ORDER BY the raw `<=>`
    distance so the HNSW index (vector_cosine_ops) drives the scan; LIMIT
    applies top-k. Filters join `documents` and predicate pre-fusion."""
    # IN-8: the active column comes from a WHITELIST, never interpolated raw.
    assert column in ("embedding", "embedding_shadow"), column
    join = "JOIN documents d ON d.id = chunks.document_id" if filter_clause else ""
    # RT-6: parent chunks exist only as swap targets — never searched.
    not_parent = "chunks.metadata->>'role' IS DISTINCT FROM 'parent'"
    where = f"WHERE {not_parent}" + (f" AND {filter_clause}" if filter_clause else "")
    return f"""
SELECT chunks.id, chunks.document_id, chunks.content, chunks.metadata,
       chunks.chunk_index, chunks.content_hash, chunks.parent_chunk_id,
       1 - (chunks.{column} <=> $1::vector) AS similarity
FROM chunks {join}
{where}
ORDER BY chunks.{column} <=> $1::vector
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
       chunks.chunk_index, chunks.content_hash, chunks.parent_chunk_id,
       ts_rank_cd(chunks.tsv, websearch_to_tsquery('english', $1)) AS rank
FROM chunks {join}
WHERE chunks.tsv @@ websearch_to_tsquery('english', $1)
  AND chunks.metadata->>'role' IS DISTINCT FROM 'parent' {extra}
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
        # RT-6: present on child chunks in parent-doc mode.
        "parent_chunk_id": str(row["parent_chunk_id"]) if row.get("parent_chunk_id") else None,
        "similarity": float(row["similarity"]),
    }


_EMBEDDINGS_SQL = """
SELECT id, embedding FROM chunks WHERE id = ANY($1::uuid[])
"""

_BY_IDS_SQL = """
SELECT id, document_id, content, metadata, chunk_index, content_hash,
       parent_chunk_id
FROM chunks WHERE id = ANY($1::uuid[])
"""


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
        column: str = "embedding",
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
                    conn.fetch(
                        _vector_sql(clause, column), _vector_literal(embedding), k, *filter_params
                    ),
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

    async def fetch_by_ids(self, chunk_ids: list[str]) -> dict[str, dict[str, Any]]:
        """Chunk rows by id (RT-6 parent swap). No scores — callers carry the
        matching child's scores onto the parent."""
        if not chunk_ids:
            return {}
        conn = await self._connect()
        try:
            rows = await with_hard_timeout(
                conn.fetch(_BY_IDS_SQL, chunk_ids),
                seconds=self._settings.db_timeout_seconds,
                operation="chunks.fetch_by_ids",
            )
        finally:
            await conn.close()
        result = {}
        for row in rows:
            metadata = row["metadata"]
            result[str(row["id"])] = {
                "id": str(row["id"]),
                "document_id": str(row["document_id"]),
                "content": row["content"],
                "metadata": json.loads(metadata) if isinstance(metadata, str) else metadata,
                "chunk_index": row["chunk_index"],
                "content_hash": row["content_hash"],
                "parent_chunk_id": str(row["parent_chunk_id"])
                if row["parent_chunk_id"]
                else None,
            }
        return result

    async def fetch_by_hashes(
        self, hashes: list[str], *, filters: dict[str, Any] | None = None
    ) -> dict[str, dict[str, Any]]:
        """CE-6 graph-arm resolution: provenance chunk hashes -> chunk rows.
        RT-4 filters apply here too (pre-fusion, like the other arms); parents
        are excluded by construction. The graph FINDS chunks — the answer text
        always comes from these Postgres rows, never from the graph itself."""
        if not hashes:
            return {}
        clause, filter_params = build_metadata_filter(filters, start_index=2)
        join = "JOIN documents d ON d.id = chunks.document_id" if clause else ""
        extra = f"AND {clause}" if clause else ""
        sql = f"""
SELECT chunks.id, chunks.document_id, chunks.content, chunks.metadata,
       chunks.chunk_index, chunks.content_hash, chunks.parent_chunk_id
FROM chunks {join}
WHERE chunks.content_hash = ANY($1::text[])
  AND chunks.metadata->>'role' IS DISTINCT FROM 'parent' {extra}
"""
        conn = await self._connect()
        try:
            rows = await with_hard_timeout(
                conn.fetch(sql, hashes, *filter_params),
                seconds=self._settings.db_timeout_seconds,
                operation="chunks.fetch_by_hashes",
            )
        finally:
            await conn.close()
        result: dict[str, dict[str, Any]] = {}
        for row in rows:
            metadata = row["metadata"]
            result[row["content_hash"]] = {
                "id": str(row["id"]),
                "document_id": str(row["document_id"]),
                "content": row["content"],
                "metadata": json.loads(metadata) if isinstance(metadata, str) else metadata,
                "chunk_index": row["chunk_index"],
                "content_hash": row["content_hash"],
                "parent_chunk_id": str(row["parent_chunk_id"])
                if row["parent_chunk_id"]
                else None,
                # Graph-arm chunks carry no cosine score; RRF ranks by arm order.
                "similarity": None,
            }
        return result

    async def fetch_embeddings(self, chunk_ids: list[str]) -> dict[str, list[float]]:
        """Stored embeddings for MMR diversification (RT-5). pgvector returns
        a '[x,y,...]' literal, which is valid JSON."""
        if not chunk_ids:
            return {}
        conn = await self._connect()
        try:
            rows = await with_hard_timeout(
                conn.fetch(_EMBEDDINGS_SQL, chunk_ids),
                seconds=self._settings.db_timeout_seconds,
                operation="chunks.fetch_embeddings",
            )
        finally:
            await conn.close()
        return {str(row["id"]): json.loads(row["embedding"]) for row in rows}
