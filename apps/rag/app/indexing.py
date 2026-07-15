"""Shared chunk -> batch-embed -> index core (Flow A steps 5-7).

Extracted from the R1 synchronous ingest path so the arq worker (IN-7) and
any future re-index path run EXACTLY the same pipeline — chunking chain
(IN-4), one batched embedding call, transactional chunk upsert with stale-hash
cleanup, document status flip. Any failure marks the document `failed` with
the reason (Flow A failure branch — never left stuck in `processing`).
"""

from typing import Any

from app.chunking import chunk_document
from app.config import Settings
from app.documents import DocumentsRepository
from app.embeddings import EmbeddingsClient
from app.tracing import traced


@traced(name="rag.index")
async def index_document_content(
    *,
    document: dict[str, Any],
    settings: Settings,
    repository: DocumentsRepository,
    embeddings: EmbeddingsClient,
) -> dict[str, Any]:
    """Index one normalized document (text/pages live in its metadata jsonb).

    Returns an outcome dict:
      {status: indexed|failed, document, chunkCount, embedBatches,
       chunking, error}
    """
    metadata = document.get("metadata") or {}
    text = metadata.get("text") or ""
    pages = metadata.get("pages")

    try:
        chunk_list, chunking_meta = await chunk_document(
            text=text,
            pages=pages,
            document_hash=document["content_hash"],
            embeddings=embeddings,
            strategy=settings.chunking_strategy,
            min_chars=settings.chunk_min_chars,
            max_chars=settings.chunk_max_chars,
            breakpoint_percentile=settings.semantic_breakpoint_percentile,
        )
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
        indexed = await repository.index_chunks(
            document_id=document["id"],
            chunks=rows,
            metadata_patch={"chunking": chunking_meta},
        )
    except Exception as exc:
        reason = f"Indexing failed: {exc}" if str(exc) else "Indexing failed"
        failed = await repository.mark_document_failed(document["id"], reason)
        failed = failed or {**document, "status": "failed", "error": reason}
        return {
            "status": "failed",
            "document": failed,
            "chunkCount": None,
            "embedBatches": 0,
            "chunking": None,
            "error": reason,
        }

    indexed = indexed or {**document, "status": "indexed", "error": None}
    return {
        "status": "indexed",
        "document": indexed,
        "chunkCount": len(chunk_list),
        "embedBatches": batches,
        "chunking": chunking_meta,
        "error": None,
    }
