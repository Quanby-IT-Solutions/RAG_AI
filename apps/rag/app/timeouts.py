"""Shared hard-timeout helper for ALL external calls (Tech Plan §8).

Every OpenAI/DB/Redis/HTTP call must go through `with_hard_timeout` so no
external dependency can hang the service. The NestJS gateway mirrors this with
`AbortSignal.timeout(...)`.
"""

import asyncio
from collections.abc import Awaitable


class ExternalCallTimeoutError(TimeoutError):
    """An external call exceeded its hard deadline."""

    def __init__(self, operation: str, seconds: float) -> None:
        self.operation = operation
        self.seconds = seconds
        super().__init__(f"External call '{operation}' timed out after {seconds}s")


async def with_hard_timeout[T](awaitable: Awaitable[T], *, seconds: float, operation: str) -> T:
    try:
        async with asyncio.timeout(seconds):
            return await awaitable
    except TimeoutError as exc:
        raise ExternalCallTimeoutError(operation, seconds) from exc
