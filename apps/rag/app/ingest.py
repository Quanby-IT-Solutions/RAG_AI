"""POST /internal/ingest — upload, normalize & queue one document (Flow A, IN-1..IN-4, IN-7).

Verifies the gateway service JWT, defensively re-checks type/size (the
gateway's Multer limits are the first line), computes the sha256 content
hash, normalizes the upload with the format's loader (PDF/HTML/DOCX/CSV —
app.loaders, IN-3), and idempotently upserts the `documents` row (upsert on
content_hash — alternate A1). Corrupt/unreadable files are recorded as
status=failed with the parse error (alternate A2).

IN-7 decouples the heavy work from the request: a successful normalize
creates an `ingestion_jobs` row (queued) with its `job_documents` entry,
enqueues an arq task on Redis, and returns IMMEDIATELY with the job id —
the worker process (app.worker) runs chunk -> batch-embed -> index (Flow A
steps 5-7 via app.indexing) so corpus work never blocks /chat request
workers. Re-running any job is safe: the pipeline is upsert-idempotent.

POST /internal/jobs/{id}/rerun requeues a finished job (per-document rows
reset to pending) and enqueues it again.
"""

import asyncio
import hashlib
import json
import logging
from datetime import UTC, datetime
from typing import Annotated, Any, Protocol

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile

from app.documents import DocumentsRepository
from app.embeddings import EmbeddingsClient
from app.jobs import JobsRepository
from app.loaders import LoaderSpec, resolve_loader, supported_mime_types
from app.rate_limit import rate_limit
from app.security import ServicePrincipal, verify_service_jwt
from app.tracing import traced

logger = logging.getLogger(__name__)

MAX_UPLOAD_BYTES = 25 * 1024 * 1024

AUDIT_ACTION_UPLOAD = "corpus.document.upload"
AUDIT_ACTION_JOB_RERUN = "corpus.ingest.rerun"

INGESTION_TASK_NAME = "process_ingestion_job"

router = APIRouter(prefix="/internal", dependencies=[Depends(verify_service_jwt)])


class JobQueue(Protocol):
    async def enqueue_job(self, function: str, *args: Any) -> Any: ...


def get_documents_repository(request: Request) -> DocumentsRepository:
    """Dependency seam so tests can override persistence."""
    return DocumentsRepository(request.app.state.settings)


def get_jobs_repository(request: Request) -> JobsRepository:
    """Dependency seam so tests can override job persistence."""
    return JobsRepository(request.app.state.settings)


async def get_job_queue(request: Request) -> JobQueue:
    """Lazy shared arq pool on Redis (IN-7) — tests override this seam."""
    queue = getattr(request.app.state, "job_queue", None)
    if queue is None:
        from arq import create_pool
        from arq.connections import RedisSettings

        queue = await create_pool(
            RedisSettings.from_dsn(request.app.state.settings.redis_url)
        )
        request.app.state.job_queue = queue
    return queue


def get_embeddings_client(request: Request) -> EmbeddingsClient:
    """Dependency seam so tests can override the provider (never call real
    OpenAI in tests). One client per app — reused across requests. CP-4:
    embedding batches draw from the SAME shared provider budget as chat."""
    client = getattr(request.app.state, "embeddings_client", None)
    if client is None:
        settings = request.app.state.settings
        client = EmbeddingsClient(settings)
        if settings.provider_bucket_enabled:
            from app.generation import get_provider_bucket
            from app.provider_bucket import ThrottledEmbeddingsClient

            bucket = get_provider_bucket(request)
            if bucket is not None:
                client = ThrottledEmbeddingsClient(client, bucket)
        request.app.state.embeddings_client = client
    return client


def _document_response(
    document: dict[str, Any],
    *,
    reused: bool,
    chunk_count: int | None = None,
    job_id: str | None = None,
) -> dict[str, Any]:
    metadata = document.get("metadata") or {}
    return {
        "id": document["id"],
        "sourceUri": document["source_uri"],
        "docType": document["doc_type"],
        "contentHash": document["content_hash"],
        "status": document["status"],
        "error": document["error"],
        "ingestedAt": document["ingested_at"],
        "pageCount": metadata.get("pageCount") if isinstance(metadata, dict) else None,
        "chunkCount": chunk_count,
        "reused": reused,
        "jobId": job_id,
    }


@traced(name="rag.ingest")
async def _ingest_document(
    *,
    raw: bytes,
    filename: str,
    loader: LoaderSpec,
    principal: ServicePrincipal,
    repository: DocumentsRepository,
    jobs: JobsRepository,
    queue: JobQueue,
    settings: Any = None,
) -> dict[str, Any]:
    content_hash = hashlib.sha256(raw).hexdigest()
    source_uri = f"upload://{filename}"

    async def _audit(document: dict[str, Any], after_extra: dict[str, Any]) -> None:
        await repository.write_audit(
            actor_id=principal.sub,
            actor_type=principal.actor_type,
            action=AUDIT_ACTION_UPLOAD,
            resource_type="document",
            resource_id=document["id"],
            before=None,
            after={
                "contentHash": content_hash,
                "status": document["status"],
                **after_extra,
            },
        )

    # Alternate A1: identical content already indexed -> strict no-op (no
    # duplicate row, status untouched).
    existing = await repository.fetch_by_content_hash(content_hash)
    if existing is not None and existing["status"] == "indexed":
        await _audit(existing, {"noop": True})
        return _document_response(existing, reused=True)

    # Alternate A2: corrupt/unreadable file -> the row is recorded as failed
    # with the parse reason (no partial normalized payload), UI shows Failed.
    try:
        metadata = await asyncio.to_thread(loader.normalize, raw, filename, source_uri)
    except Exception as exc:
        label = f"{loader.doc_type.upper()} parse failed"
        reason = f"{label}: {exc}" if str(exc) else label
        failed = await repository.upsert_document(
            source_uri=source_uri,
            doc_type=loader.doc_type,
            content_hash=content_hash,
            status="failed",
            metadata={
                "originalFilename": filename,
                "ingestedAt": datetime.now(UTC).isoformat(),
            },
            error=reason,
        )
        if failed is None:  # same hash raced to indexed; treat as no-op
            existing = await repository.fetch_by_content_hash(content_hash)
            return _document_response(existing or {}, reused=True)
        await _audit(failed, {"error": reason})
        return _document_response(failed, reused=False)

    # New hash -> insert as processing; previously failed (or stale
    # processing) -> re-enter processing (R1 retry). Indexed rows are shielded
    # by the SQL WHERE guard.
    document = await repository.upsert_document(
        source_uri=source_uri,
        doc_type=loader.doc_type,
        content_hash=content_hash,
        status="processing",
        metadata=metadata,
        error=None,
    )
    if document is None:  # raced to indexed between the check and the upsert
        existing = await repository.fetch_by_content_hash(content_hash)
        return _document_response(existing or {}, reused=True)

    # CE-7: vision-worthy PDF pages render to stored images at upload time
    # (the worker captions + indexes them). Fail-soft — a render failure
    # never blocks the text pipeline. Identical re-uploads no-op above, so
    # unchanged content never re-renders (Flow A economics).
    if settings is not None and settings.multimodal_enabled and loader.doc_type == "pdf":
        try:
            from app.multimodal import PageImagesRepository, detect_vision_pages

            pages = await asyncio.to_thread(
                detect_vision_pages,
                raw,
                min_text_chars=settings.multimodal_min_page_text_chars,
                render_scale=settings.multimodal_render_scale,
                max_pages=settings.multimodal_max_pages,
            )
            stored = await PageImagesRepository(settings).store(
                document_id=document["id"], pages=pages
            )
            if stored:
                logger.info(
                    "multimodal.pages_stored %s",
                    json.dumps({"documentId": document["id"], "pages": stored}),
                )
        except Exception:
            logger.warning("multimodal.page_extraction_failed", exc_info=True)

    # IN-7: upload accepted -> ingestion_jobs row (queued) + arq enqueue.
    # The API returns immediately with the job id; the worker process runs
    # Flow A steps 5-7 (chunk -> batch-embed -> index) off the request path.
    job = await jobs.create_job(
        kind="ingest",
        triggered_by=principal.sub,
        document_ids=[document["id"]],
    )
    await queue.enqueue_job(INGESTION_TASK_NAME, job["id"])
    await _audit(
        document,
        {"jobId": job["id"], "queued": True, "retried": existing is not None},
    )
    return _document_response(document, reused=False, job_id=job["id"])


@router.post("/ingest", dependencies=[Depends(rate_limit("rate_limit_ingest"))])
async def ingest_document(
    file: UploadFile,
    request: Request,
    principal: Annotated[ServicePrincipal, Depends(verify_service_jwt)],
    repository: Annotated[DocumentsRepository, Depends(get_documents_repository)],
    jobs: Annotated[JobsRepository, Depends(get_jobs_repository)],
    queue: Annotated[JobQueue, Depends(get_job_queue)],
) -> dict[str, Any]:
    # Defensive re-checks (gateway already enforces both via Multer + Nginx).
    loader = resolve_loader(file.content_type)
    if loader is None:
        supported = ", ".join(supported_mime_types())
        raise HTTPException(
            status_code=415,
            detail=(
                f'Unsupported file type "{file.content_type}". '
                f"Accepted types: {supported}."
            ),
        )

    raw = await file.read()
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"File exceeds the {MAX_UPLOAD_BYTES} byte (25 MB) limit",
        )
    if not raw:
        raise HTTPException(status_code=400, detail="Uploaded file is empty")

    return await _ingest_document(
        raw=raw,
        filename=file.filename or f"upload.{loader.doc_type}",
        loader=loader,
        principal=principal,
        repository=repository,
        jobs=jobs,
        queue=queue,
        settings=request.app.state.settings,
    )


@router.post("/jobs/{job_id}/rerun", dependencies=[Depends(rate_limit("rate_limit_ingest"))])
async def rerun_job(
    job_id: str,
    principal: Annotated[ServicePrincipal, Depends(verify_service_jwt)],
    repository: Annotated[DocumentsRepository, Depends(get_documents_repository)],
    jobs: Annotated[JobsRepository, Depends(get_jobs_repository)],
    queue: Annotated[JobQueue, Depends(get_job_queue)],
) -> dict[str, Any]:
    """Re-run a finished job (IN-7). Safe by construction: upserts make the
    re-run idempotent and never duplicate rows (Flow A). Queued/running jobs
    are not re-runnable (409)."""
    requeued = await jobs.requeue(job_id)
    if not requeued:
        raise HTTPException(
            status_code=409,
            detail="Job not found or not in a re-runnable state (must be completed or failed)",
        )
    await queue.enqueue_job(INGESTION_TASK_NAME, job_id)
    await repository.write_audit(
        actor_id=principal.sub,
        actor_type=principal.actor_type,
        action=AUDIT_ACTION_JOB_RERUN,
        resource_type="ingestion_job",
        resource_id=job_id,
        before=None,
        after={"status": "queued"},
    )
    return {"id": job_id, "status": "queued"}
