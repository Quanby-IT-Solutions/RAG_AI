"""Embedding cache keyed on content hash (IN-6, Flow A cache branch).

Postgres-backed (PRD decision — not Redis): `embedding_cache` rows are
`(content_hash, model, embedding)` with a composite PK, so identical content
is never re-embedded and a future model change (IN-8) is a clean namespace
by construction. Lookup-before-batch: cached hashes skip the embedding API
entirely; only misses go out in one batch call, and their vectors are
written back for next time.
"""

import json
import logging
from typing import Protocol

import asyncpg

from app.config import Settings
from app.documents import _vector_literal
from app.timeouts import with_hard_timeout

logger = logging.getLogger(__name__)

_LOOKUP_SQL = """
SELECT content_hash, embedding FROM embedding_cache
WHERE model = $1 AND content_hash = ANY($2::text[])
"""

_UPSERT_SQL = """
INSERT INTO embedding_cache (content_hash, model, embedding)
VALUES ($1, $2, $3::vector)
ON CONFLICT (content_hash, model) DO NOTHING
"""


class SupportsEmbedTexts(Protocol):
    model: str

    async def embed_texts(self, texts: list[str]) -> tuple[list[list[float]], int]: ...


class EmbeddingCacheRepository:
    """asyncpg persistence over the Drizzle-owned `embedding_cache` table."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def _connect(self) -> asyncpg.Connection:
        return await with_hard_timeout(
            asyncpg.connect(dsn=self._settings.database_url),
            seconds=self._settings.db_timeout_seconds,
            operation="postgres.connect",
        )

    async def fetch(self, *, model: str, hashes: list[str]) -> dict[str, list[float]]:
        if not hashes:
            return {}
        conn = await self._connect()
        try:
            rows = await with_hard_timeout(
                conn.fetch(_LOOKUP_SQL, model, hashes),
                seconds=self._settings.db_timeout_seconds,
                operation="embedding_cache.fetch",
            )
        finally:
            await conn.close()
        return {row["content_hash"]: json.loads(row["embedding"]) for row in rows}

    async def store(self, *, model: str, entries: list[tuple[str, list[float]]]) -> None:
        if not entries:
            return
        conn = await self._connect()
        try:
            for content_hash, vector in entries:
                await with_hard_timeout(
                    conn.execute(_UPSERT_SQL, content_hash, model, _vector_literal(vector)),
                    seconds=self._settings.db_timeout_seconds,
                    operation="embedding_cache.store",
                )
        finally:
            await conn.close()


async def embed_with_cache(
    *,
    embeddings: SupportsEmbedTexts,
    cache: EmbeddingCacheRepository,
    items: list[tuple[str, str]],
) -> tuple[list[list[float]], dict[str, int]]:
    """Embed `(content_hash, text)` items through the cache.

    Returns vectors aligned with `items` plus stats
    {cacheHits, cacheMisses, embedBatches}. A fully-cached input makes ZERO
    embedding API calls (AC). Duplicate hashes within one call embed once."""
    if not items:
        return [], {"cacheHits": 0, "cacheMisses": 0, "embedBatches": 0}

    hashes = [content_hash for content_hash, _ in items]
    cached = await cache.fetch(model=embeddings.model, hashes=list(dict.fromkeys(hashes)))

    # Unique misses, in first-seen order.
    miss_order: list[str] = []
    miss_text: dict[str, str] = {}
    for content_hash, text in items:
        if content_hash not in cached and content_hash not in miss_text:
            miss_order.append(content_hash)
            miss_text[content_hash] = text

    batches = 0
    if miss_order:
        vectors, batches = await embeddings.embed_texts(
            [miss_text[content_hash] for content_hash in miss_order]
        )
        fresh = list(zip(miss_order, vectors, strict=True))
        await cache.store(model=embeddings.model, entries=fresh)
        cached.update(dict(fresh))

    stats = {
        "cacheHits": sum(1 for content_hash in hashes if content_hash not in miss_text),
        "cacheMisses": len(miss_order),
        "embedBatches": batches,
    }
    logger.info("embedding_cache.stats %s", json.dumps({"model": embeddings.model, **stats}))
    return [cached[content_hash] for content_hash in hashes], stats
