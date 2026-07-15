"""Documents + chunks persistence (Flow A) — asyncpg over the Drizzle-owned schema.

Python runs NO migrations: the `documents`, `chunks`, and `audit_log` tables
are created by the Drizzle migration chain (packages/db). Every query goes
through the shared hard-timeout helper. The idempotency invariants live in the
SQL: documents upsert ON CONFLICT (content_hash) never updating an `indexed`
row; chunks upsert ON CONFLICT (content_hash) so re-indexing never duplicates.
"""

import json
from typing import Any

import asyncpg

from app.config import Settings
from app.timeouts import with_hard_timeout

_UPSERT_SQL = """
INSERT INTO documents (source_uri, doc_type, content_hash, status, metadata, error, ingested_at)
VALUES ($1, $2, $3, $4, $5, $6, now())
ON CONFLICT (content_hash) DO UPDATE SET
    source_uri = EXCLUDED.source_uri,
    doc_type = EXCLUDED.doc_type,
    status = EXCLUDED.status,
    metadata = EXCLUDED.metadata,
    error = EXCLUDED.error,
    ingested_at = now()
WHERE documents.status <> 'indexed'
RETURNING id, source_uri, doc_type, content_hash, status, metadata, error, ingested_at
"""

_SELECT_BY_HASH_SQL = """
SELECT id, source_uri, doc_type, content_hash, status, metadata, error, ingested_at
FROM documents WHERE content_hash = $1
"""

_SELECT_BY_ID_SQL = """
SELECT id, source_uri, doc_type, content_hash, status, metadata, error, ingested_at
FROM documents WHERE id = $1
"""

_AUDIT_INSERT_SQL = """
INSERT INTO audit_log (actor_id, actor_type, action, resource_type, resource_id, before, after)
VALUES ($1, $2, $3, $4, $5, $6, $7)
"""

# Re-chunking a document changes its chunk hashes (IN-4): stale rows that are
# no longer part of the new chunk set must go in the SAME transaction, or
# orphans poison retrieval. An empty new set deletes all of the document's rows.
_CHUNK_DELETE_STALE_SQL = """
DELETE FROM chunks
WHERE document_id = $1 AND NOT (content_hash = ANY($2::text[]))
"""

# Chunk-level idempotency (Flow A / IN-2): upsert on the chunk content_hash so
# re-running indexing never duplicates rows. The `tsv` column is DB-generated.
_CHUNK_UPSERT_SQL = """
INSERT INTO chunks
    (document_id, content, embedding, metadata, content_hash, chunk_index, embedding_model)
VALUES ($1, $2, $3::vector, $4, $5, $6, $7)
ON CONFLICT (content_hash) DO UPDATE SET
    document_id = EXCLUDED.document_id,
    content = EXCLUDED.content,
    embedding = EXCLUDED.embedding,
    metadata = EXCLUDED.metadata,
    chunk_index = EXCLUDED.chunk_index,
    embedding_model = EXCLUDED.embedding_model
"""

_SET_DOCUMENT_STATUS_SQL = """
UPDATE documents SET status = $2, error = $3 WHERE id = $1
RETURNING id, source_uri, doc_type, content_hash, status, metadata, error, ingested_at
"""

# Indexed flip + chunking-metadata merge (IN-4: strategy/fallback recorded on
# the document row) in one statement.
_SET_INDEXED_WITH_METADATA_SQL = """
UPDATE documents
SET status = 'indexed', error = NULL,
    metadata = COALESCE(metadata, '{}'::jsonb) || $2::jsonb
WHERE id = $1
RETURNING id, source_uri, doc_type, content_hash, status, metadata, error, ingested_at
"""

# pgvector stores the vector dimension directly in atttypmod (no header offset).
_EMBEDDING_COLUMN_TYPMOD_SQL = """
SELECT a.atttypmod
FROM pg_attribute a
JOIN pg_class c ON c.oid = a.attrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public' AND c.relname = 'chunks'
  AND a.attname = 'embedding' AND NOT a.attisdropped
"""


def _vector_literal(embedding: list[float]) -> str:
    """Serialize a vector for pgvector's text input format: [a,b,...]."""
    return "[" + ",".join(map(str, embedding)) + "]"


def _row_to_dict(row: asyncpg.Record) -> dict[str, Any]:
    metadata = row["metadata"]
    return {
        "id": str(row["id"]),
        "source_uri": row["source_uri"],
        "doc_type": row["doc_type"],
        "content_hash": row["content_hash"],
        "status": row["status"],
        "metadata": json.loads(metadata) if isinstance(metadata, str) else metadata,
        "error": row["error"],
        "ingested_at": row["ingested_at"].isoformat(),
    }


class DocumentsRepository:
    """INSERT/UPSERT-only access to `documents` plus append-only audit writes."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def _connect(self) -> asyncpg.Connection:
        return await with_hard_timeout(
            asyncpg.connect(dsn=self._settings.database_url),
            seconds=self._settings.db_timeout_seconds,
            operation="postgres.connect",
        )

    async def fetch_by_content_hash(self, content_hash: str) -> dict[str, Any] | None:
        conn = await self._connect()
        try:
            row = await with_hard_timeout(
                conn.fetchrow(_SELECT_BY_HASH_SQL, content_hash),
                seconds=self._settings.db_timeout_seconds,
                operation="documents.fetch_by_content_hash",
            )
        finally:
            await conn.close()
        return _row_to_dict(row) if row else None

    async def fetch_by_id(self, document_id: str) -> dict[str, Any] | None:
        conn = await self._connect()
        try:
            row = await with_hard_timeout(
                conn.fetchrow(_SELECT_BY_ID_SQL, document_id),
                seconds=self._settings.db_timeout_seconds,
                operation="documents.fetch_by_id",
            )
        finally:
            await conn.close()
        return _row_to_dict(row) if row else None

    async def upsert_document(
        self,
        *,
        source_uri: str,
        doc_type: str,
        content_hash: str,
        status: str,
        metadata: dict[str, Any],
        error: str | None,
    ) -> dict[str, Any] | None:
        """Idempotent upsert on content_hash (Flow A / alternate A1).

        Returns the written row, or None when the existing row is `indexed`
        (the WHERE guard makes re-ingest of indexed content a strict no-op).
        """
        conn = await self._connect()
        try:
            row = await with_hard_timeout(
                conn.fetchrow(
                    _UPSERT_SQL,
                    source_uri,
                    doc_type,
                    content_hash,
                    status,
                    json.dumps(metadata),
                    error,
                ),
                seconds=self._settings.db_timeout_seconds,
                operation="documents.upsert",
            )
        finally:
            await conn.close()
        return _row_to_dict(row) if row else None

    async def index_chunks(
        self,
        *,
        document_id: str,
        chunks: list[dict[str, Any]],
        metadata_patch: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Delete stale chunk rows, upsert the new set, and flip the document
        to `indexed` in ONE transaction (Flow A steps 6-7) — a partial write
        can never leave an `indexed` document with missing chunks, and a
        re-chunk (IN-4: new hashes) never leaves orphans behind.

        Each chunk dict carries content, embedding (list[float]), metadata,
        content_hash, chunk_index, embedding_model. `metadata_patch` (e.g.
        IN-4 chunking strategy/fallback info) merges into the document's
        metadata jsonb. Returns the indexed document row."""
        conn = await self._connect()
        try:
            async with conn.transaction():
                await with_hard_timeout(
                    conn.execute(
                        _CHUNK_DELETE_STALE_SQL,
                        document_id,
                        [chunk["content_hash"] for chunk in chunks],
                    ),
                    seconds=self._settings.db_timeout_seconds,
                    operation="chunks.delete_stale",
                )
                for chunk in chunks:
                    await with_hard_timeout(
                        conn.execute(
                            _CHUNK_UPSERT_SQL,
                            document_id,
                            chunk["content"],
                            _vector_literal(chunk["embedding"]),
                            json.dumps(chunk["metadata"]),
                            chunk["content_hash"],
                            chunk["chunk_index"],
                            chunk["embedding_model"],
                        ),
                        seconds=self._settings.db_timeout_seconds,
                        operation="chunks.upsert",
                    )
                row = await with_hard_timeout(
                    conn.fetchrow(
                        _SET_INDEXED_WITH_METADATA_SQL,
                        document_id,
                        json.dumps(metadata_patch or {}),
                    ),
                    seconds=self._settings.db_timeout_seconds,
                    operation="documents.set_indexed",
                )
        finally:
            await conn.close()
        return _row_to_dict(row) if row else None

    async def mark_document_failed(self, document_id: str, reason: str) -> dict[str, Any] | None:
        """Record a chunk/embed/index failure — the document must never be
        left stuck in `processing` (Flow A failure branch)."""
        conn = await self._connect()
        try:
            row = await with_hard_timeout(
                conn.fetchrow(_SET_DOCUMENT_STATUS_SQL, document_id, "failed", reason),
                seconds=self._settings.db_timeout_seconds,
                operation="documents.mark_failed",
            )
        finally:
            await conn.close()
        return _row_to_dict(row) if row else None

    async def fetch_embedding_column_dimensions(self) -> int | None:
        """Introspect the chunks.embedding vector dimension (atttypmod) for the
        boot-time dimension guard (Tech Plan §4). None when the column is
        missing (migrations not applied)."""
        conn = await self._connect()
        try:
            value = await with_hard_timeout(
                conn.fetchval(_EMBEDDING_COLUMN_TYPMOD_SQL),
                seconds=self._settings.db_timeout_seconds,
                operation="chunks.embedding_dimensions",
            )
        finally:
            await conn.close()
        return int(value) if value is not None and int(value) > 0 else None

    async def write_audit(
        self,
        *,
        actor_id: str,
        actor_type: str,
        action: str,
        resource_type: str,
        resource_id: str,
        before: dict[str, Any] | None,
        after: dict[str, Any] | None,
    ) -> None:
        """Append one entry to the shared audit_log spine (INSERT-only; the DB
        trigger rejects UPDATE/DELETE)."""
        conn = await self._connect()
        try:
            await with_hard_timeout(
                conn.execute(
                    _AUDIT_INSERT_SQL,
                    actor_id,
                    actor_type,
                    action,
                    resource_type,
                    resource_id,
                    json.dumps(before) if before is not None else None,
                    json.dumps(after) if after is not None else None,
                ),
                seconds=self._settings.db_timeout_seconds,
                operation="audit_log.insert",
            )
        finally:
            await conn.close()
