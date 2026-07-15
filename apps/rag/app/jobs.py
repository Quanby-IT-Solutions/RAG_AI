"""Ingestion-job persistence (IN-7) — asyncpg over the Drizzle-owned schema.

`ingestion_jobs` tracks the background run lifecycle (queued -> running ->
completed | failed); `job_documents` records the per-document outcome the jobs
console lists. Python runs NO migrations (Drizzle owns DDL). Every query goes
through the shared hard-timeout helper. Re-running a job is safe by
construction: the underlying pipeline is upsert-idempotent (Flow A).
"""

import json
from typing import Any

import asyncpg

from app.config import Settings
from app.timeouts import with_hard_timeout

_CREATE_JOB_SQL = """
INSERT INTO ingestion_jobs (kind, status, triggered_by)
VALUES ($1, 'queued', $2)
RETURNING id, kind, status, triggered_by, stats, error, created_at, started_at, finished_at
"""

_ADD_JOB_DOCUMENT_SQL = """
INSERT INTO job_documents (job_id, document_id, status)
VALUES ($1, $2, 'pending')
"""

_MARK_RUNNING_SQL = """
UPDATE ingestion_jobs SET status = 'running', started_at = now(), error = NULL
WHERE id = $1
"""

_FINISH_JOB_SQL = """
UPDATE ingestion_jobs
SET status = $2, stats = $3, error = $4, finished_at = now()
WHERE id = $1
"""

_REQUEUE_JOB_SQL = """
UPDATE ingestion_jobs
SET status = 'queued', error = NULL, started_at = NULL, finished_at = NULL
WHERE id = $1 AND status IN ('completed', 'failed')
RETURNING id
"""

_RESET_JOB_DOCUMENTS_SQL = """
UPDATE job_documents SET status = 'pending', error = NULL, updated_at = now()
WHERE job_id = $1
"""

_SET_JOB_DOCUMENT_SQL = """
UPDATE job_documents
SET status = $3, chunk_count = $4, error = $5, updated_at = now()
WHERE job_id = $1 AND document_id = $2
"""

_JOB_SQL = """
SELECT id, kind, status, triggered_by, stats, error, created_at, started_at, finished_at
FROM ingestion_jobs WHERE id = $1
"""

_JOB_DOCUMENTS_SQL = """
SELECT jd.document_id, jd.status, jd.chunk_count, jd.error, jd.updated_at,
       d.source_uri, d.doc_type, d.metadata
FROM job_documents jd
JOIN documents d ON d.id = jd.document_id
WHERE jd.job_id = $1
ORDER BY jd.updated_at
"""


def _job_to_dict(row: asyncpg.Record) -> dict[str, Any]:
    stats = row["stats"]
    return {
        "id": str(row["id"]),
        "kind": row["kind"],
        "status": row["status"],
        "triggered_by": row["triggered_by"],
        "stats": json.loads(stats) if isinstance(stats, str) else stats,
        "error": row["error"],
        "created_at": row["created_at"].isoformat(),
        "started_at": row["started_at"].isoformat() if row["started_at"] else None,
        "finished_at": row["finished_at"].isoformat() if row["finished_at"] else None,
    }


class JobsRepository:
    """Lifecycle writes + console reads for ingestion jobs."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def _connect(self) -> asyncpg.Connection:
        return await with_hard_timeout(
            asyncpg.connect(dsn=self._settings.database_url),
            seconds=self._settings.db_timeout_seconds,
            operation="postgres.connect",
        )

    async def create_job(
        self, *, kind: str, triggered_by: str, document_ids: list[str]
    ) -> dict[str, Any]:
        """One job row + its pending job_documents, atomically."""
        conn = await self._connect()
        try:
            async with conn.transaction():
                row = await with_hard_timeout(
                    conn.fetchrow(_CREATE_JOB_SQL, kind, triggered_by),
                    seconds=self._settings.db_timeout_seconds,
                    operation="jobs.create",
                )
                for document_id in document_ids:
                    await with_hard_timeout(
                        conn.execute(_ADD_JOB_DOCUMENT_SQL, row["id"], document_id),
                        seconds=self._settings.db_timeout_seconds,
                        operation="jobs.add_document",
                    )
        finally:
            await conn.close()
        return _job_to_dict(row)

    async def mark_running(self, job_id: str) -> None:
        conn = await self._connect()
        try:
            await with_hard_timeout(
                conn.execute(_MARK_RUNNING_SQL, job_id),
                seconds=self._settings.db_timeout_seconds,
                operation="jobs.mark_running",
            )
        finally:
            await conn.close()

    async def finish(
        self, job_id: str, *, status: str, stats: dict[str, Any], error: str | None
    ) -> None:
        conn = await self._connect()
        try:
            await with_hard_timeout(
                conn.execute(_FINISH_JOB_SQL, job_id, status, json.dumps(stats), error),
                seconds=self._settings.db_timeout_seconds,
                operation="jobs.finish",
            )
        finally:
            await conn.close()

    async def requeue(self, job_id: str) -> bool:
        """Re-run gate: only finished (completed/failed) jobs requeue; returns
        False when the job is unknown or still queued/running."""
        conn = await self._connect()
        try:
            async with conn.transaction():
                row = await with_hard_timeout(
                    conn.fetchrow(_REQUEUE_JOB_SQL, job_id),
                    seconds=self._settings.db_timeout_seconds,
                    operation="jobs.requeue",
                )
                if row is None:
                    return False
                await with_hard_timeout(
                    conn.execute(_RESET_JOB_DOCUMENTS_SQL, job_id),
                    seconds=self._settings.db_timeout_seconds,
                    operation="jobs.reset_documents",
                )
        finally:
            await conn.close()
        return True

    async def set_document_outcome(
        self,
        *,
        job_id: str,
        document_id: str,
        status: str,
        chunk_count: int | None,
        error: str | None,
    ) -> None:
        conn = await self._connect()
        try:
            await with_hard_timeout(
                conn.execute(
                    _SET_JOB_DOCUMENT_SQL, job_id, document_id, status, chunk_count, error
                ),
                seconds=self._settings.db_timeout_seconds,
                operation="jobs.set_document_outcome",
            )
        finally:
            await conn.close()

    async def fetch_job(self, job_id: str) -> dict[str, Any] | None:
        conn = await self._connect()
        try:
            row = await with_hard_timeout(
                conn.fetchrow(_JOB_SQL, job_id),
                seconds=self._settings.db_timeout_seconds,
                operation="jobs.fetch",
            )
        finally:
            await conn.close()
        return _job_to_dict(row) if row else None

    async def fetch_job_documents(self, job_id: str) -> list[dict[str, Any]]:
        conn = await self._connect()
        try:
            rows = await with_hard_timeout(
                conn.fetch(_JOB_DOCUMENTS_SQL, job_id),
                seconds=self._settings.db_timeout_seconds,
                operation="jobs.fetch_documents",
            )
        finally:
            await conn.close()
        results = []
        for row in rows:
            metadata = row["metadata"]
            results.append(
                {
                    "document_id": str(row["document_id"]),
                    "status": row["status"],
                    "chunk_count": row["chunk_count"],
                    "error": row["error"],
                    "updated_at": row["updated_at"].isoformat(),
                    "source_uri": row["source_uri"],
                    "doc_type": row["doc_type"],
                    "metadata": json.loads(metadata) if isinstance(metadata, str) else metadata,
                }
            )
        return results
