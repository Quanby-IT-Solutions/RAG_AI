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
from app.embedding_cache import EmbeddingCacheRepository, embed_with_cache
from app.embeddings import EmbeddingsClient
from app.tracing import traced


@traced(name="rag.index")
async def index_document_content(
    *,
    document: dict[str, Any],
    settings: Settings,
    repository: DocumentsRepository,
    embeddings: EmbeddingsClient,
    embedding_cache: "EmbeddingCacheRepository | None" = None,
    reembed_repository: Any = None,
    shadow_embeddings: Any = None,
) -> dict[str, Any]:
    """Index one normalized document (text/pages live in its metadata jsonb).

    Returns an outcome dict:
      {status: indexed|failed, document, chunkCount, embedBatches,
       cacheHits, cacheMisses, chunking, error}
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
        # RT-6 parent-doc mode: group consecutive children under larger
        # parent chunks. Parents are hashed and upserted like any chunk
        # (Flow A stays idempotent); children reference them by hash and
        # the repository resolves ids inside the indexing transaction.
        parents: list[dict[str, Any]] = []
        parent_hash_by_child: dict[int, str] = {}
        if settings.parent_doc_enabled and chunk_list:
            from app.chunking import _chunk_hash

            group = settings.parent_group_size
            for start in range(0, len(chunk_list), group):
                members = chunk_list[start : start + group]
                content = "\n\n".join(chunk.content for chunk in members)
                parent_index = 100_000 + start // group
                parent_hash = _chunk_hash(document["content_hash"], parent_index, content)
                parents.append(
                    {
                        "content": content,
                        "metadata": {
                            "role": "parent",
                            "childIndexes": [chunk.chunk_index for chunk in members],
                        },
                        "content_hash": parent_hash,
                        "chunk_index": parent_index,
                    }
                )
                for chunk in members:
                    parent_hash_by_child[chunk.chunk_index] = parent_hash

        vectors: list[list[float]] = []
        batches = 0
        cache_stats = {"cacheHits": 0, "cacheMisses": 0}
        # IN-6 Flow A cache branch: lookup-before-batch keyed on the chunk
        # content hash — unchanged content never re-embeds.
        items = [(chunk.content_hash, chunk.content) for chunk in chunk_list] + [
            (row["content_hash"], row["content"]) for row in parents
        ]
        if items:
            cache = embedding_cache or EmbeddingCacheRepository(settings)
            vectors, embed_stats = await embed_with_cache(
                embeddings=embeddings, cache=cache, items=items
            )
            batches = embed_stats["embedBatches"]
            cache_stats = {
                "cacheHits": embed_stats["cacheHits"],
                "cacheMisses": embed_stats["cacheMisses"],
            }
        rows = [
            {
                "content": chunk.content,
                "embedding": vector,
                "metadata": chunk.metadata,
                "content_hash": chunk.content_hash,
                "chunk_index": chunk.chunk_index,
                "embedding_model": embeddings.model,
                **(
                    {"parent_hash": parent_hash_by_child[chunk.chunk_index]}
                    if chunk.chunk_index in parent_hash_by_child
                    else {}
                ),
            }
            for chunk, vector in zip(chunk_list, vectors[: len(chunk_list)], strict=True)
        ]
        rows.extend(
            {
                **parent,
                "embedding": vector,
                "embedding_model": embeddings.model,
            }
            for parent, vector in zip(parents, vectors[len(chunk_list) :], strict=True)
        )
        # IN-8 dual-write: while a re-embed migration is active, new chunks
        # also get shadow embeddings (target model, IN-6 cache namespace).
        migration = None
        if reembed_repository is not None:
            migration = await reembed_repository.active_migration()
        if migration is not None:
            from app.reembed import TargetEmbeddingsClient

            target = shadow_embeddings or TargetEmbeddingsClient(
                settings, migration["targetModel"]
            )
            shadow_vectors, _ = await embed_with_cache(
                embeddings=target,
                cache=embedding_cache or EmbeddingCacheRepository(settings),
                items=[(row["content_hash"], row["content"]) for row in rows],
            )
            for row, vector in zip(rows, shadow_vectors, strict=True):
                row["shadow_embedding"] = vector
                row["shadow_model"] = migration["targetModel"]

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
            "cacheHits": 0,
            "cacheMisses": 0,
            "chunking": None,
            "error": reason,
        }

    indexed = indexed or {**document, "status": "indexed", "error": None}
    return {
        "status": "indexed",
        "document": indexed,
        "chunkCount": len(chunk_list),
        "embedBatches": batches,
        "cacheHits": cache_stats["cacheHits"],
        "cacheMisses": cache_stats["cacheMisses"],
        "chunking": chunking_meta,
        "error": None,
    }
