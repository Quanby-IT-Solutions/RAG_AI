"""POST /internal/ingest — upload, normalize & index one document (Flow A, IN-1+IN-2+IN-3).

Verifies the gateway service JWT, defensively re-checks type/size (the
gateway's Multer limits are the first line), computes the sha256 content
hash, normalizes the upload with the format's loader (PDF/HTML/DOCX/CSV —
app.loaders, IN-3), and idempotently upserts the `documents` row (upsert on
content_hash — alternate A1). Corrupt/unreadable files are recorded as
status=failed with the parse error (alternate A2).

R1 fuses upload + index into one synchronous action (async jobs are IN-7):
a successful normalize proceeds straight to chunk (recursive splitter) ->
batch-embed (pinned model @ 1536 dims) -> upsert `chunks` on chunk
content_hash -> documents.status = indexed. A chunk/embed/index failure marks
the document `failed` with the reason — never stuck in `processing`. The whole
request runs inside one trace (F1 tracing) and writes ONE RAG-action audit
entry (upload + ingest, per the two-writer rule) into the append-only
audit_log, recording chunk count / embed batch info.
"""

import asyncio
import hashlib
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile

from app.chunking import build_chunks
from app.documents import DocumentsRepository
from app.embeddings import EmbeddingsClient
from app.loaders import LoaderSpec, resolve_loader, supported_mime_types
from app.security import ServicePrincipal, verify_service_jwt
from app.tracing import traced

MAX_UPLOAD_BYTES = 25 * 1024 * 1024

AUDIT_ACTION_UPLOAD = "corpus.document.upload"

router = APIRouter(prefix="/internal", dependencies=[Depends(verify_service_jwt)])


def get_documents_repository(request: Request) -> DocumentsRepository:
    """Dependency seam so tests can override persistence."""
    return DocumentsRepository(request.app.state.settings)


def get_embeddings_client(request: Request) -> EmbeddingsClient:
    """Dependency seam so tests can override the provider (never call real
    OpenAI in tests). One client per app — reused across requests."""
    client = getattr(request.app.state, "embeddings_client", None)
    if client is None:
        client = EmbeddingsClient(request.app.state.settings)
        request.app.state.embeddings_client = client
    return client


def _document_response(
    document: dict[str, Any], *, reused: bool, chunk_count: int | None = None
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
    }


@traced(name="rag.index")
async def _index_document(
    *,
    document: dict[str, Any],
    text: str,
    pages: list[dict[str, Any]] | None,
    document_hash: str,
    repository: DocumentsRepository,
    embeddings: EmbeddingsClient,
    audit: Any,
    retried: bool,
) -> dict[str, Any]:
    """Flow A steps 5-7: chunk -> batch-embed -> upsert chunks -> indexed.

    Any chunk/embed/index error marks the document `failed` with the reason
    (Flow A failure branch — never left stuck in `processing`)."""
    try:
        chunk_list = build_chunks(text=text, pages=pages, document_hash=document_hash)
        vectors: list[list[float]] = []
        batches = 0
        if chunk_list:
            vectors, batches = await embeddings.embed_texts([chunk.content for chunk in chunk_list])
        rows = [
            {
                "content": chunk.content,
                "embedding": vector,
                "metadata": chunk.metadata,
                "content_hash": chunk.content_hash,
                "chunk_index": chunk.chunk_index,
                "embedding_model": embeddings.model,
            }
            for chunk, vector in zip(chunk_list, vectors, strict=True)
        ]
        indexed = await repository.index_chunks(document_id=document["id"], chunks=rows)
    except Exception as exc:
        reason = f"Indexing failed: {exc}" if str(exc) else "Indexing failed"
        failed = await repository.mark_document_failed(document["id"], reason)
        failed = failed or {**document, "status": "failed", "error": reason}
        await audit(failed, {"error": reason, "retried": retried})
        return _document_response(failed, reused=False)

    indexed = indexed or {**document, "status": "indexed", "error": None}
    await audit(
        indexed,
        {
            "chunkCount": len(chunk_list),
            "embedBatches": batches,
            "embeddingModel": embeddings.model,
            "retried": retried,
        },
    )
    return _document_response(indexed, reused=False, chunk_count=len(chunk_list))


@traced(name="rag.ingest")
async def _ingest_document(
    *,
    raw: bytes,
    filename: str,
    loader: LoaderSpec,
    principal: ServicePrincipal,
    repository: DocumentsRepository,
    embeddings: EmbeddingsClient,
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

    # Flow A steps 5-7 (IN-2): chunk -> batch-embed -> index, synchronously in
    # the same request (R1 fuses upload + index; async jobs are IN-7). The one
    # audit entry for the fused upload+ingest action is written at the end.
    return await _index_document(
        document=document,
        text=metadata["text"],
        pages=metadata.get("pages"),
        document_hash=content_hash,
        repository=repository,
        embeddings=embeddings,
        audit=_audit,
        retried=existing is not None,
    )


@router.post("/ingest")
async def ingest_document(
    file: UploadFile,
    principal: Annotated[ServicePrincipal, Depends(verify_service_jwt)],
    repository: Annotated[DocumentsRepository, Depends(get_documents_repository)],
    embeddings: Annotated[EmbeddingsClient, Depends(get_embeddings_client)],
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
        embeddings=embeddings,
    )
