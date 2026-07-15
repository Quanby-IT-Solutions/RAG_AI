"""Provider reliability (GN-5, Flow B failure branch).

Wraps the GN-4 provider-client seam with:

- **Retry with exponential backoff + jitter** (bounded attempts) for
  transient provider errors — timeouts, connection failures, 429s, 5xx.
- **A per-provider circuit breaker whose state lives in Redis**, so every
  replica shares one view of provider health (DOMAIN-MODEL state machine:
  closed -> open on threshold failures -> half-open probe after cooldown ->
  closed on success / reopen on failure). While a circuit is OPEN, calls
  fail fast (no timeout wait) and the router fails over.
- **Failover routing**: when the selected provider's circuit is open, the
  request routes to the other configured provider immediately; with every
  circuit open the call fails fast and the generation layer returns its
  structured degradation response (never a silent hang).
- **Output validation** (validate_output) run before returning/flushing:
  non-empty, citation markers resolve to real context blocks, no leaked
  prompt scaffolding. Failures are replaced by a safe response and traced.

The hard-timeout audit (every LLM/embedding/DB call under the F1 helper) is
enforced by tests/test_reliability.py::test_every_external_call_module_uses_the_hard_timeout_helper.
"""

import asyncio
import logging
import random
import time
from typing import TYPE_CHECKING, Any

from app.config import Settings
from app.providers import ChatModelClient, ModelRouter
from app.timeouts import ExternalCallTimeoutError

if TYPE_CHECKING:
    from redis.asyncio import Redis

logger = logging.getLogger(__name__)


class BreakerOpenError(RuntimeError):
    """Raised (fail fast) when a provider's circuit is open."""

    def __init__(self, provider: str) -> None:
        super().__init__(f"Circuit breaker open for provider '{provider}'")
        self.provider = provider


def is_transient(error: BaseException) -> bool:
    """Retry only what a retry can fix: timeouts, connection drops, rate
    limits, and provider 5xx. Anything else (auth, bad request, validation)
    fails immediately."""
    if isinstance(error, ExternalCallTimeoutError | asyncio.TimeoutError | ConnectionError):
        return True
    status = getattr(error, "status_code", None)
    if isinstance(status, int) and (status == 429 or status >= 500):
        return True
    name = type(error).__name__
    return name in {
        "APIConnectionError",
        "APITimeoutError",
        "RateLimitError",
        "InternalServerError",
        "ServiceUnavailableError",
    }


class CircuitBreaker:
    """Per-provider breaker with shared state in Redis (replica-safe).

    closed: calls flow, failures counted in a rolling window.
    open:   threshold failures crossed -> fail fast for cooldown seconds.
    half-open: after cooldown ONE probe call is allowed (SET NX claim);
    success closes the circuit, failure reopens it."""

    def __init__(self, settings: Settings, redis: "Redis | Any | None" = None) -> None:
        self._settings = settings
        self._redis = redis
        self.failure_threshold = settings.breaker_failure_threshold
        self.cooldown_seconds = settings.breaker_cooldown_seconds

    def _r(self) -> Any:
        if self._redis is None:
            from redis.asyncio import Redis

            self._redis = Redis.from_url(self._settings.redis_url)
        return self._redis

    @staticmethod
    def _keys(provider: str) -> dict[str, str]:
        return {
            "open": f"breaker:open:{provider}",
            "half": f"breaker:half:{provider}",
            "probe": f"breaker:probe:{provider}",
            "failures": f"breaker:failures:{provider}",
        }

    async def state(self, provider: str) -> str:
        keys = self._keys(provider)
        if await self._r().exists(keys["open"]):
            return "open"
        if await self._r().exists(keys["half"]):
            return "half_open"
        return "closed"

    async def allow(self, provider: str) -> bool:
        """True when a call may proceed. In half-open, exactly one caller
        (across all replicas) wins the probe slot."""
        keys = self._keys(provider)
        redis = self._r()
        if await redis.exists(keys["open"]):
            return False
        if await redis.exists(keys["half"]):
            claimed = await redis.set(keys["probe"], "1", nx=True, ex=self.cooldown_seconds)
            return bool(claimed)
        return True

    async def record_success(self, provider: str) -> None:
        keys = self._keys(provider)
        await self._r().delete(keys["failures"], keys["half"], keys["probe"], keys["open"])

    async def record_failure(self, provider: str) -> None:
        keys = self._keys(provider)
        redis = self._r()
        probe_failed = bool(await redis.getdel(keys["probe"]))
        failures = int(await redis.incr(keys["failures"]))
        await redis.expire(keys["failures"], max(60, self.cooldown_seconds * 2))
        if probe_failed or failures >= self.failure_threshold:
            await redis.set(keys["open"], "1", ex=self.cooldown_seconds)
            # half flag outlives the open window so the next caller probes.
            await redis.set(keys["half"], "1", ex=self.cooldown_seconds * 20)
            await redis.delete(keys["failures"])
            logger.warning(
                "breaker.opened provider=%s (probe_failed=%s failures=%s)",
                provider,
                probe_failed,
                failures,
            )


class ReliableChatClient:
    """Retry/backoff + breaker bookkeeping around one provider client.

    Same interface as the wrapped client (the GN-4 seam holds). Streaming
    retries only apply BEFORE the first delta; a mid-stream failure
    propagates so the caller's degraded-final-frame path handles it."""

    def __init__(
        self,
        inner: ChatModelClient,
        *,
        breaker: CircuitBreaker,
        settings: Settings,
        sleep: Any = asyncio.sleep,
        rng: Any = random.random,
    ) -> None:
        self._inner = inner
        self._breaker = breaker
        self._settings = settings
        self._sleep = sleep
        self._rng = rng

    @property
    def model(self) -> str:
        return self._inner.model

    @property
    def provider(self) -> str:
        return self._inner.provider

    def _backoff(self, attempt: int) -> float:
        base = self._settings.retry_base_delay_seconds * (2**attempt)
        return base * (0.5 + self._rng())  # jitter in [0.5x, 1.5x)

    async def complete(self, messages: list[dict[str, str]]) -> tuple[str, dict[str, int] | None]:
        if not await self._breaker.allow(self.provider):
            raise BreakerOpenError(self.provider)  # fail fast, no timeout wait
        attempts = self._settings.retry_max_attempts
        for attempt in range(attempts):
            try:
                result = await self._inner.complete(messages)
            except Exception as exc:
                await self._breaker.record_failure(self.provider)
                if attempt + 1 >= attempts or not is_transient(exc):
                    raise
                delay = self._backoff(attempt)
                logger.warning(
                    "provider.retry provider=%s attempt=%s delay=%.2fs reason=%s",
                    self.provider,
                    attempt + 1,
                    delay,
                    exc,
                )
                await self._sleep(delay)
            else:
                await self._breaker.record_success(self.provider)
                return result
        raise RuntimeError("unreachable")  # pragma: no cover

    async def stream(self, messages: list[dict[str, str]]) -> Any:
        if not await self._breaker.allow(self.provider):
            raise BreakerOpenError(self.provider)
        attempts = self._settings.retry_max_attempts
        for attempt in range(attempts):
            emitted = False
            try:
                async for item in self._inner.stream(messages):
                    emitted = True
                    yield item
            except Exception as exc:
                await self._breaker.record_failure(self.provider)
                # Once deltas flowed, a retry would duplicate output — let the
                # caller degrade the final frame instead.
                if emitted or attempt + 1 >= attempts or not is_transient(exc):
                    raise
                delay = self._backoff(attempt)
                logger.warning(
                    "provider.stream_retry provider=%s attempt=%s delay=%.2fs reason=%s",
                    self.provider,
                    attempt + 1,
                    delay,
                    exc,
                )
                await self._sleep(delay)
            else:
                await self._breaker.record_success(self.provider)
                return


class ReliabilityRouter:
    """GN-4 routing + GN-5 failover: when the selected provider's circuit is
    open, route to the other configured provider immediately (no waiting on
    timeouts). Both open -> the wrapped client fails fast with
    BreakerOpenError and the generation layer degrades gracefully."""

    def __init__(
        self,
        inner: ModelRouter,
        *,
        breaker: CircuitBreaker,
        settings: Settings,
    ) -> None:
        self._settings = settings
        self._breaker = breaker
        self.cheap = ReliableChatClient(inner.cheap, breaker=breaker, settings=settings)
        self.strong = (
            ReliableChatClient(inner.strong, breaker=breaker, settings=settings)
            if inner.strong is not None
            else None
        )
        self._inner = inner

    async def route(
        self, *, query: str, chunks: list[dict[str, Any]]
    ) -> tuple[ChatModelClient, dict[str, Any]]:
        raw_client, decision = self._inner.route(query=query, chunks=chunks)
        selected = self.strong if decision["tier"] == "strong" else self.cheap
        assert selected is not None

        if await self._breaker.allow(selected.provider):
            decision["breaker"] = await self._breaker.state(selected.provider)
            return selected, decision

        # Circuit open on the selected provider: fail over to the OTHER
        # configured provider without waiting for timeouts.
        alternates = [
            client
            for client in (self.cheap, self.strong)
            if client is not None and client.provider != selected.provider
        ]
        for alternate in alternates:
            if await self._breaker.allow(alternate.provider):
                decision = {
                    **decision,
                    "tier": "failover",
                    "model": alternate.model,
                    "provider": alternate.provider,
                    "reason": f"breaker-open:{selected.provider}",
                    "breaker": "open",
                }
                logger.warning(
                    "chat.failover from=%s to=%s", selected.provider, alternate.provider
                )
                return alternate, decision

        # Everything is open: return the selected client anyway — its allow()
        # gate raises BreakerOpenError immediately (structured degradation).
        decision = {**decision, "breaker": "open", "reason": "all-breakers-open"}
        return selected, decision


# ------------------------------------------------------------ output validation

# Fragments of the grounded prompt that must never leak into an answer.
_SCAFFOLDING_MARKERS = (
    "Context blocks:",
    "retrieval-grounded assistant",
    "numbered context blocks",
)


def validate_output(
    answer: str, *, citations: list[dict[str, Any]], chunk_count: int
) -> list[str]:
    """GN-5 output validation, run before returning / final flush.

    Returns a list of problems (empty = valid): non-empty answer, citation
    ids resolve to a real context block, no leaked prompt scaffolding."""
    problems: list[str] = []
    if not answer.strip():
        problems.append("empty-answer")
    for marker in _SCAFFOLDING_MARKERS:
        if marker in answer:
            problems.append(f"leaked-scaffolding:{marker}")
    for citation in citations:
        if not citation.get("chunkId"):
            problems.append("citation-missing-chunk-id")
    import re as _re

    for match in _re.findall(r"\[(\d+)\]", answer):
        position = int(match)
        if position < 1 or position > chunk_count:
            problems.append(f"citation-out-of-range:[{position}]")
    return problems


def measure_validation(answer: str, **kwargs: Any) -> tuple[list[str], float]:
    started = time.perf_counter()
    problems = validate_output(answer, **kwargs)
    return problems, round((time.perf_counter() - started) * 1000, 3)
