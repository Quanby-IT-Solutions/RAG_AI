"""Shared provider rate control (CP-4, Flow C) — Redis window bucket, 429-aware.

Every provider call from EVERY replica (chat completions, streams, embedding
batches) draws from one shared Redis budget per provider, capped below the
account RPM. Accounting is an atomic INCR on a fixed window key — atomic
without Lua, replica-safe by construction. Callers wait (bounded) for the
next window; a caller that exhausts its wait budget SHEDS with
ProviderBucketExhausted (backpressure, never an uncounted provider call).

429-aware adaptation (AC): a provider 429 temporarily SHRINKS the bucket
(BUCKET_SHRINK_FACTOR for BUCKET_SHRINK_TTL_SECONDS) on top of GN-5's
jittered retry backoff, and is counted. Metrics (acquired/waited/shed/429
per provider) are Redis counters for RP-4.

Order is contractual: bucket → breaker → provider call. The bucket wraps the
same ChatModelClient interface GN-5 wraps; ReliableChatClient sits OUTSIDE,
so its retries re-enter the bucket each attempt.
"""

import asyncio
import logging
import math
import random
import time
from typing import Any

from app.config import Settings
from app.timeouts import with_hard_timeout

logger = logging.getLogger(__name__)


class ProviderBucketExhausted(Exception):
    """Backpressure shed: no provider budget within the wait timeout."""

    def __init__(self, provider: str) -> None:
        super().__init__(f"Provider budget exhausted for {provider} — request shed")
        self.provider = provider


class ProviderBucket:
    """Shared fixed-window request budget per provider."""

    def __init__(self, settings: Settings, redis_client: Any) -> None:
        self._settings = settings
        self._redis = redis_client

    def _rpm(self, provider: str) -> int:
        return (
            self._settings.provider_rpm_anthropic
            if provider == "anthropic"
            else self._settings.provider_rpm_openai
        )

    async def _incr_metric(self, metric: str, provider: str) -> None:
        try:
            from app.metrics import BUCKET_EVENTS, PROVIDER_429

            if metric == "429":
                PROVIDER_429.labels(provider=provider).inc()
            else:
                BUCKET_EVENTS.labels(provider=provider, event=metric).inc()
        except Exception:
            pass
        try:
            await self._redis.incr(f"bucket:metrics:{metric}:{provider}")
        except Exception:
            logger.warning("bucket.metric_incr_failed", exc_info=True)

    async def _shrink_factor(self, provider: str) -> float:
        raw = await self._redis.get(f"bucket:shrink:{provider}")
        return float(raw) if raw else 1.0

    async def acquire(self, provider: str) -> None:
        """Take one request token; waits (bounded) across windows, then sheds."""
        window_seconds = self._settings.bucket_window_seconds
        deadline = time.monotonic() + self._settings.bucket_wait_timeout_seconds
        waited = False
        while True:
            shrink = await self._shrink_factor(provider)
            allowed = max(
                1, math.floor(self._rpm(provider) * shrink * window_seconds / 60.0)
            )
            window = int(time.time() // window_seconds)
            key = f"bucket:window:{provider}:{window}"
            count = await with_hard_timeout(
                self._redis.incr(key),
                seconds=self._settings.redis_timeout_seconds,
                operation="bucket.incr",
            )
            try:
                await self._redis.expire(key, window_seconds * 2)
            except Exception:
                pass
            if int(count) <= allowed:
                await self._incr_metric("acquired", provider)
                if waited:
                    await self._incr_metric("waited", provider)
                return

            # Window full: wait for the next window (with jitter), bounded.
            now = time.time()
            next_window = (window + 1) * window_seconds
            sleep_for = max(0.01, next_window - now) + random.random() * 0.1
            if time.monotonic() + sleep_for > deadline:
                await self._incr_metric("shed", provider)
                logger.warning(
                    "bucket.shed %s",
                    {"provider": provider, "allowed": allowed, "count": int(count)},
                )
                raise ProviderBucketExhausted(provider)
            waited = True
            await asyncio.sleep(sleep_for)

    async def on_429(self, provider: str) -> None:
        """AC: a provider 429 temporarily shrinks the bucket (adaptive)."""
        try:
            await self._redis.set(
                f"bucket:shrink:{provider}",
                str(self._settings.bucket_shrink_factor),
                ex=self._settings.bucket_shrink_ttl_seconds,
            )
            await self._incr_metric("429", provider)
            logger.warning(
                "bucket.shrunk_on_429 %s",
                {
                    "provider": provider,
                    "factor": self._settings.bucket_shrink_factor,
                    "ttl": self._settings.bucket_shrink_ttl_seconds,
                },
            )
        except Exception:
            logger.warning("bucket.shrink_failed", exc_info=True)


def _looks_like_429(exc: Exception) -> bool:
    status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
    if status == 429:
        return True
    text = f"{exc.__class__.__name__}: {exc}".lower()
    return "429" in text or "rate limit" in text or "rate_limit" in text


class ThrottledChatClient:
    """Bucket-gated ChatModelClient wrapper (bucket → breaker → call)."""

    def __init__(self, inner: Any, bucket: ProviderBucket) -> None:
        self._inner = inner
        self._bucket = bucket

    @property
    def model(self) -> str:
        return self._inner.model

    @property
    def provider(self) -> str:
        return self._inner.provider

    async def complete(self, messages: list[dict[str, str]]) -> Any:
        await self._bucket.acquire(self.provider)
        try:
            return await self._inner.complete(messages)
        except Exception as exc:
            if _looks_like_429(exc):
                await self._bucket.on_429(self.provider)
            raise

    async def stream(self, messages: list[dict[str, str]]) -> Any:
        # One token per stream START (the account bills the call, not deltas).
        await self._bucket.acquire(self.provider)
        try:
            async for item in self._inner.stream(messages):
                yield item
        except Exception as exc:
            if _looks_like_429(exc):
                await self._bucket.on_429(self.provider)
            raise


class ThrottledEmbeddingsClient:
    """Embedding batches draw from the SAME shared budget as chat (AC)."""

    def __init__(self, inner: Any, bucket: ProviderBucket) -> None:
        self._inner = inner
        self._bucket = bucket

    @property
    def model(self) -> str:
        return self._inner.model

    @property
    def dimensions(self) -> int:
        return self._inner.dimensions

    async def embed_texts(self, texts: list[str]) -> tuple[list[list[float]], int]:
        await self._bucket.acquire("openai")
        try:
            return await self._inner.embed_texts(texts)
        except Exception as exc:
            if _looks_like_429(exc):
                await self._bucket.on_429("openai")
            raise


def build_provider_bucket(settings: Settings, redis_client: Any = None) -> "ProviderBucket | None":
    """Config-selected bucket; None when PROVIDER_BUCKET_ENABLED is off."""
    if not settings.provider_bucket_enabled:
        return None
    if redis_client is None:
        import redis.asyncio as aioredis

        redis_client = aioredis.from_url(settings.redis_url, decode_responses=True)
    return ProviderBucket(settings, redis_client)
