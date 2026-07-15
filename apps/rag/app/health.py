"""Real DB + Redis connectivity checks backing GET /health (Tech Plan §8).

Redis has no functional consumer in R1 — it is infra baseline, checked here so
readiness reflects the full runtime foundation.
"""

import asyncpg
import redis.asyncio as aioredis

from app.config import Settings
from app.timeouts import with_hard_timeout


async def check_database(settings: Settings) -> None:
    """Open a real connection and run SELECT 1. Raises on any failure."""

    async def _probe() -> None:
        conn = await asyncpg.connect(dsn=settings.database_url)
        try:
            await conn.fetchval("SELECT 1")
        finally:
            await conn.close()

    await with_hard_timeout(
        _probe(), seconds=settings.db_timeout_seconds, operation="postgres.health"
    )


async def check_redis(settings: Settings) -> None:
    """PING the Redis server. Raises on any failure."""

    async def _probe() -> None:
        client = aioredis.from_url(settings.redis_url)
        try:
            await client.ping()
        finally:
            await client.aclose()

    await with_hard_timeout(
        _probe(), seconds=settings.redis_timeout_seconds, operation="redis.health"
    )
