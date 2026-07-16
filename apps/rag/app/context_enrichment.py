"""Contextual chunk enrichment (CE-1, Flow G — index-time only).

Before embedding, each chunk gets a short LLM-generated description of how it
fits its parent document; the enriched text (context + chunk) feeds BOTH
retrieval indexes (embedding here, tsv in the DB), while the stored chunk
`content` stays the raw citation/display surface.

Economics mirror the embedding cache (IN-6): contexts are content-addressed
in `context_cache` on (chunk content_hash, model, prompt version) — identical
content is never re-described, and a prompt/model change is a clean namespace
by construction. Enrichment is fail-soft (Flow G): a failed generation falls
back to the raw chunk text for that chunk, counted, never blocking the job.
"""

import asyncio
import hashlib
import logging
from typing import Any, Protocol

import asyncpg
from prometheus_client import Counter

from app.config import Settings
from app.costs import compute_cost_usd
from app.timeouts import with_hard_timeout

logger = logging.getLogger(__name__)

# Part of the context_cache key AND the enriched-embedding cache key: editing
# the prompt bumps this so stale contexts are never mixed with new ones.
CONTEXT_PROMPT_VERSION = "v1"

_CONTEXT_SYSTEM_PROMPT = (
    "You situate a document chunk for a search index. Given a document "
    "excerpt and one chunk from it, reply with 1-2 short sentences (at most "
    "~80 tokens) stating what the chunk covers and how it fits the document. "
    "Reply with the description only — no preamble, no quotes."
)

# Leading window of the document supplied as situating context to the model.
_DOC_WINDOW_CHARS = 4000

# Concurrent enrichment calls per document; the client itself is already
# CP-4 bucket-throttled, this only bounds local fan-out.
_MAX_CONCURRENT_CALLS = 5

CONTEXT_ENRICHMENT = Counter(
    "rag_context_enrichment_total",
    "Contextual chunk enrichment outcomes (CE-1)",
    ["outcome"],  # hit | miss | fallback
)

_LOOKUP_SQL = """
SELECT chunk_hash, context FROM context_cache
WHERE model = $1 AND prompt_version = $2 AND chunk_hash = ANY($3::text[])
"""

_UPSERT_SQL = """
INSERT INTO context_cache (chunk_hash, model, prompt_version, context)
VALUES ($1, $2, $3, $4)
ON CONFLICT (chunk_hash, model, prompt_version) DO NOTHING
"""


class SupportsComplete(Protocol):
    model: str

    async def complete(
        self, messages: list[dict[str, str]]
    ) -> tuple[str, dict[str, int] | None]: ...


class ContextCacheRepository:
    """asyncpg persistence over the Drizzle-owned `context_cache` table."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def _connect(self) -> asyncpg.Connection:
        return await with_hard_timeout(
            asyncpg.connect(dsn=self._settings.database_url),
            seconds=self._settings.db_timeout_seconds,
            operation="postgres.connect",
        )

    async def fetch(
        self, *, model: str, prompt_version: str, hashes: list[str]
    ) -> dict[str, str]:
        if not hashes:
            return {}
        conn = await self._connect()
        try:
            rows = await with_hard_timeout(
                conn.fetch(_LOOKUP_SQL, model, prompt_version, hashes),
                seconds=self._settings.db_timeout_seconds,
                operation="context_cache.fetch",
            )
        finally:
            await conn.close()
        return {row["chunk_hash"]: row["context"] for row in rows}

    async def store(
        self, *, model: str, prompt_version: str, entries: list[tuple[str, str]]
    ) -> None:
        if not entries:
            return
        conn = await self._connect()
        try:
            for chunk_hash, context in entries:
                await with_hard_timeout(
                    conn.execute(_UPSERT_SQL, chunk_hash, model, prompt_version, context),
                    seconds=self._settings.db_timeout_seconds,
                    operation="context_cache.store",
                )
        finally:
            await conn.close()


def build_context_client(settings: Settings) -> SupportsComplete:
    """Cheap-fast chat client for enrichment, CP-4 bucket-gated like every
    other provider call (mirrors the worker's embeddings construction)."""
    from app.providers import OpenAIChatClient

    client: Any = OpenAIChatClient(settings)
    if settings.provider_bucket_enabled:
        from app.provider_bucket import ThrottledChatClient, build_provider_bucket

        bucket = build_provider_bucket(settings)
        if bucket is not None:
            client = ThrottledChatClient(client, bucket)
    return client


def enriched_embed_key(chunk_hash: str, context: str) -> str:
    """Embedding-cache key for enriched text: derived from the raw chunk hash
    plus the exact context, so identical (chunk, context) pairs never re-embed
    while raw-text entries (flag off) keep their original key untouched."""
    digest = hashlib.sha256(
        f"{chunk_hash}:{CONTEXT_PROMPT_VERSION}:{context}".encode()
    ).hexdigest()
    return f"ctx-{digest}"


async def enrich_chunks(
    *,
    document_text: str,
    chunks: list[Any],
    client: SupportsComplete,
    cache: ContextCacheRepository,
    settings: Settings,
) -> tuple[list[str | None], dict[str, Any]]:
    """Situating context per chunk, cache-first (Flow G).

    Returns contexts aligned with `chunks` (None = fallback to raw text) plus
    stats {hits, misses, fallbacks, costUsd}. A fully-cached input makes ZERO
    LLM calls; any per-chunk failure yields None for that chunk only."""
    if not chunks:
        return [], {"hits": 0, "misses": 0, "fallbacks": 0, "costUsd": 0.0}

    hashes = [chunk.content_hash for chunk in chunks]
    cached = await cache.fetch(
        model=client.model,
        prompt_version=CONTEXT_PROMPT_VERSION,
        hashes=list(dict.fromkeys(hashes)),
    )
    window = document_text[:_DOC_WINDOW_CHARS]
    semaphore = asyncio.Semaphore(_MAX_CONCURRENT_CALLS)
    cost_total = 0.0
    cost_lock = asyncio.Lock()

    async def generate(chunk: Any) -> str | None:
        nonlocal cost_total
        async with semaphore:
            try:
                text, usage = await client.complete(
                    [
                        {"role": "system", "content": _CONTEXT_SYSTEM_PROMPT},
                        {
                            "role": "user",
                            "content": (
                                f"<document>\n{window}\n</document>\n\n"
                                f"<chunk>\n{chunk.content}\n</chunk>"
                            ),
                        },
                    ]
                )
            except Exception:
                logger.warning(
                    "context_enrichment.failed chunk=%s", chunk.content_hash, exc_info=True
                )
                return None
            cost = compute_cost_usd(settings, model=client.model, token_usage=usage)
            if cost is not None:
                async with cost_lock:
                    cost_total += cost
            return text.strip() or None

    # Unique misses in first-seen order; duplicates within a document enrich once.
    miss_chunks: dict[str, Any] = {}
    for chunk in chunks:
        if chunk.content_hash not in cached and chunk.content_hash not in miss_chunks:
            miss_chunks[chunk.content_hash] = chunk

    generated = await asyncio.gather(*(generate(c) for c in miss_chunks.values()))
    fresh = [
        (chunk_hash, context)
        for chunk_hash, context in zip(miss_chunks, generated, strict=True)
        if context is not None
    ]
    await cache.store(
        model=client.model, prompt_version=CONTEXT_PROMPT_VERSION, entries=fresh
    )
    resolved = {**cached, **dict(fresh)}

    contexts = [resolved.get(chunk_hash) for chunk_hash in hashes]
    stats = {
        "hits": sum(1 for h in hashes if h in cached),
        "misses": len(miss_chunks),
        "fallbacks": sum(1 for c in contexts if c is None),
        "costUsd": round(cost_total, 8),
    }
    CONTEXT_ENRICHMENT.labels(outcome="hit").inc(stats["hits"])
    CONTEXT_ENRICHMENT.labels(outcome="miss").inc(stats["misses"])
    CONTEXT_ENRICHMENT.labels(outcome="fallback").inc(stats["fallbacks"])
    logger.info(
        "context_enrichment.stats model=%s hits=%s misses=%s fallbacks=%s cost=%s",
        client.model,
        stats["hits"],
        stats["misses"],
        stats["fallbacks"],
        stats["costUsd"],
    )
    return contexts, stats
