"""Redis response/semantic cache (CP-1, Flow B cache stage).

Checked AFTER the SC-1 security screen (position contractual — keys are
computed from the MASKED query, raw PII never enters Redis) and BEFORE
retrieval/LLM. Shared across replicas: everything lives in Redis, nothing
in-process.

Two layers under one interface:
- exact: sha256 of (normalized masked query + filter set), namespaced by a
  CONFIG FINGERPRINT (models, weights, pipeline toggles, budget) so a config
  change can never serve a stale-config answer
- semantic: recent entries also index their query embedding; a lookup whose
  cosine similarity clears CACHE_SEMANTIC_THRESHOLD hits too

Hit/miss/semantic-hit counters are first-class Redis counters (OB-2/RP-4
consume them). "I don't know" and degraded answers are NEVER cached.
"""

import hashlib
import json
import logging
from typing import Any, Protocol

from app.config import Settings
from app.expansion import _cosine
from app.timeouts import with_hard_timeout

logger = logging.getLogger(__name__)

_METRIC_KEYS = ("hits", "semanticHits", "misses", "stores")


class SupportsEmbedTexts(Protocol):
    async def embed_texts(self, texts: list[str]) -> tuple[list[list[float]], int]: ...


def normalize_query(query: str) -> str:
    return " ".join(query.lower().split())


def config_fingerprint(settings: Settings) -> str:
    """Everything that changes WHAT a query answers with. A new fingerprint
    is a clean cache namespace."""
    payload = {
        "generationModel": settings.generation_model,
        "strongModel": settings.strong_model,
        "strongProvider": settings.strong_model_provider,
        "k": settings.retrieval_top_k,
        "weights": [settings.retrieval_vector_weight, settings.retrieval_keyword_weight],
        "rrfK": settings.rrf_k,
        "rerank": settings.rerank_enabled,
        "multiQuery": settings.multi_query_enabled,
        "mmr": settings.mmr_enabled,
        "parentDoc": settings.parent_doc_enabled,
        "compression": settings.compression_enabled,
        "budget": settings.context_token_budget,
        "embeddingModel": settings.embedding_model,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]


class ResponseCache:
    """Redis-backed exact + semantic response cache."""

    def __init__(
        self,
        settings: Settings,
        embeddings: SupportsEmbedTexts,
        redis_client: Any = None,
    ) -> None:
        self._settings = settings
        self._embeddings = embeddings
        self._redis = redis_client
        self._fingerprint = config_fingerprint(settings)

    async def _client(self) -> Any:
        if self._redis is None:
            import redis.asyncio as aioredis

            self._redis = aioredis.from_url(self._settings.redis_url, decode_responses=True)
        return self._redis

    def _entry_key(self, digest: str) -> str:
        return f"respcache:v1:{self._fingerprint}:{digest}"

    def _semantic_key(self, filters_digest: str) -> str:
        return f"respcache:sem:{self._fingerprint}:{filters_digest}"

    @staticmethod
    def _digests(query: str, filters: dict[str, Any] | None) -> tuple[str, str]:
        filters_json = json.dumps(filters or {}, sort_keys=True)
        filters_digest = hashlib.sha256(filters_json.encode()).hexdigest()[:16]
        exact = hashlib.sha256(f"{normalize_query(query)}|{filters_json}".encode()).hexdigest()
        return exact, filters_digest

    async def _incr(self, metric: str) -> None:
        try:
            client = await self._client()
            await with_hard_timeout(
                client.incr(f"respcache:metrics:{metric}"),
                seconds=self._settings.redis_timeout_seconds,
                operation="respcache.incr",
            )
        except Exception:  # metrics are best-effort, never break the request
            logger.warning("respcache.metric_incr_failed", exc_info=True)

    async def lookup(
        self, *, query: str, filters: dict[str, Any] | None
    ) -> dict[str, Any] | None:
        """Exact first, then semantic. Returns the cached payload plus a
        cacheLayer marker, or None (counted as a miss)."""
        try:
            client = await self._client()
            exact_digest, filters_digest = self._digests(query, filters)
            raw = await with_hard_timeout(
                client.get(self._entry_key(exact_digest)),
                seconds=self._settings.redis_timeout_seconds,
                operation="respcache.get",
            )
            if raw:
                await self._incr("hits")
                payload = json.loads(raw)
                payload["cacheLayer"] = "exact"
                return payload

            # Semantic layer: embed the (masked) query, compare with the
            # recent entries indexed under the same filter set.
            entries = await with_hard_timeout(
                client.hgetall(self._semantic_key(filters_digest)),
                seconds=self._settings.redis_timeout_seconds,
                operation="respcache.sem_index",
            )
            if entries:
                vectors, _ = await self._embeddings.embed_texts([normalize_query(query)])
                query_vector = vectors[0]
                threshold = self._settings.cache_semantic_threshold
                best_digest: str | None = None
                best_similarity = threshold
                for digest, meta_raw in entries.items():
                    meta = json.loads(meta_raw)
                    similarity = _cosine(query_vector, meta["embedding"])
                    if similarity >= best_similarity:
                        best_similarity = similarity
                        best_digest = digest
                if best_digest:
                    raw = await with_hard_timeout(
                        client.get(self._entry_key(best_digest)),
                        seconds=self._settings.redis_timeout_seconds,
                        operation="respcache.get_semantic",
                    )
                    if raw:
                        await self._incr("semanticHits")
                        payload = json.loads(raw)
                        payload["cacheLayer"] = "semantic"
                        payload["cacheSimilarity"] = best_similarity
                        return payload
        except Exception:
            logger.warning("respcache.lookup_failed", exc_info=True)
            return None

        await self._incr("misses")
        return None

    async def store(
        self,
        *,
        query: str,
        filters: dict[str, Any] | None,
        payload: dict[str, Any],
    ) -> None:
        """Cache a successful grounded answer. NEVER caches 'I don't know' or
        degraded responses (contractual)."""
        if payload.get("isIDontKnow") or payload.get("degraded"):
            return
        try:
            client = await self._client()
            exact_digest, filters_digest = self._digests(query, filters)
            entry = {
                key: payload[key]
                for key in ("answer", "citations", "model", "isIDontKnow", "degraded")
                if key in payload
            }
            ttl = self._settings.cache_ttl_seconds
            await with_hard_timeout(
                client.set(self._entry_key(exact_digest), json.dumps(entry), ex=ttl),
                seconds=self._settings.redis_timeout_seconds,
                operation="respcache.set",
            )
            vectors, _ = await self._embeddings.embed_texts([normalize_query(query)])
            semantic_key = self._semantic_key(filters_digest)
            await with_hard_timeout(
                client.hset(semantic_key, exact_digest, json.dumps({"embedding": vectors[0]})),
                seconds=self._settings.redis_timeout_seconds,
                operation="respcache.sem_hset",
            )
            await with_hard_timeout(
                client.expire(semantic_key, ttl),
                seconds=self._settings.redis_timeout_seconds,
                operation="respcache.sem_expire",
            )
            await self._incr("stores")
        except Exception:
            logger.warning("respcache.store_failed", exc_info=True)

    async def metrics(self) -> dict[str, int]:
        client = await self._client()
        values = await with_hard_timeout(
            client.mget([f"respcache:metrics:{key}" for key in _METRIC_KEYS]),
            seconds=self._settings.redis_timeout_seconds,
            operation="respcache.metrics",
        )
        return {key: int(value or 0) for key, value in zip(_METRIC_KEYS, values, strict=True)}


def build_response_cache(
    settings: Settings, embeddings: SupportsEmbedTexts
) -> ResponseCache | None:
    """Config-selected cache; None when CACHE_ENABLED is off."""
    if not settings.cache_enabled:
        return None
    return ResponseCache(settings, embeddings)
