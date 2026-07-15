"""Single-flight request coalescing (CP-3, Flow C stampede protection).

N concurrent misses on one cache key trigger exactly ONE computation: the
leader wins a Redis SET-NX lock (TTL-guarded, owner-checked — a dead leader
can never deadlock waiters) and publishes its result under a short-lived
result key; the N−1 followers poll that key with backoff and reuse the
result. Locks and results live in Redis, so coalescing holds ACROSS
replicas, not just within one process.

Failure semantics (AC): a failing leader publishes an error marker and
releases the lock; each waiting follower then retries ONCE (one of them
becomes the next leader). Waiters never hang on a dead leader — the lock's
TTL expires and the retry path takes over.

Streaming interaction (v1, per ticket note): stream followers receive the
COMPLETED answer replayed as one token + final; no fan-out streaming.
"""

import asyncio
import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from app.config import Settings
from app.timeouts import with_hard_timeout

logger = logging.getLogger(__name__)

_RESULT_TTL_SECONDS = 30


class SingleFlightError(Exception):
    """The shared computation failed (leader error or wait timeout)."""


class SingleFlight:
    """Redis-backed single-flight executor."""

    def __init__(self, settings: Settings, redis_client: Any) -> None:
        self._settings = settings
        self._redis = redis_client

    def _lock_key(self, key: str) -> str:
        return f"sf:lock:{key}"

    def _result_key(self, key: str) -> str:
        return f"sf:result:{key}"

    async def try_acquire(self, key: str, owner: str) -> bool:
        acquired = await with_hard_timeout(
            self._redis.set(
                self._lock_key(key),
                owner,
                nx=True,
                ex=self._settings.single_flight_lock_ttl_seconds,
            ),
            seconds=self._settings.redis_timeout_seconds,
            operation="single_flight.acquire",
        )
        return bool(acquired)

    async def release(self, key: str, owner: str) -> None:
        try:
            current = await self._redis.get(self._lock_key(key))
            if current == owner:
                await self._redis.delete(self._lock_key(key))
        except Exception:
            logger.warning("single_flight.release_failed", exc_info=True)

    async def publish(self, key: str, payload: dict[str, Any]) -> None:
        await with_hard_timeout(
            self._redis.set(self._result_key(key), json.dumps(payload), ex=_RESULT_TTL_SECONDS),
            seconds=self._settings.redis_timeout_seconds,
            operation="single_flight.publish",
        )

    async def await_result(self, key: str) -> dict[str, Any] | None:
        """Poll for the leader's result until timeout; None when the lock
        vanished without a result (dead leader — caller retries)."""
        deadline = (
            asyncio.get_event_loop().time() + self._settings.single_flight_wait_timeout_seconds
        )
        interval = self._settings.single_flight_poll_interval_seconds
        while asyncio.get_event_loop().time() < deadline:
            raw = await self._redis.get(self._result_key(key))
            if raw:
                return json.loads(raw)
            lock_held = await self._redis.get(self._lock_key(key))
            if not lock_held:
                return None  # leader died without publishing — take over
            await asyncio.sleep(interval)
        raise SingleFlightError("Timed out waiting for the in-flight computation")

    async def run(
        self,
        key: str,
        compute: Callable[[], Awaitable[dict[str, Any]]],
        *,
        _retry: bool = True,
    ) -> tuple[dict[str, Any], str]:
        """Execute `compute` exactly once per key across all replicas.

        Returns (result, role) with role 'leader' | 'follower'."""
        owner = uuid.uuid4().hex
        if await self.try_acquire(key, owner):
            # Clear any stale result (e.g. a previous leader's error marker)
            # so waiters poll for THIS computation's outcome.
            try:
                await self._redis.delete(self._result_key(key))
            except Exception:
                logger.warning("single_flight.stale_result_clear_failed", exc_info=True)
            try:
                result = await compute()
            except Exception as exc:
                # Followers fail fast (they retry once themselves).
                await self.publish(key, {"error": str(exc) or exc.__class__.__name__})
                await self.release(key, owner)
                raise
            await self.publish(key, {"result": result})
            await self.release(key, owner)
            return result, "leader"

        outcome = await self.await_result(key)
        if outcome is None or "error" in outcome:
            # Dead or failed leader: retry ONCE (one waiter becomes leader).
            if _retry:
                logger.info(
                    "single_flight.retry %s",
                    {"key": key, "reason": "leader-died" if outcome is None else "leader-failed"},
                )
                return await self.run(key, compute, _retry=False)
            raise SingleFlightError(
                outcome["error"] if outcome else "In-flight computation abandoned"
            )
        return outcome["result"], "follower"
