"""Real DB + Redis + provider connectivity checks backing GET /health.

OB-3: provider reachability is probed on an INTERVAL and cached — /health
never pays a provider round-trip per request. Providers being unreachable
degrades status but does NOT fail readiness (the GN-5 breaker owns runtime
failover; the load balancer must keep routing).
"""

import time

import asyncpg
import httpx
import redis.asyncio as aioredis

from app.config import Settings
from app.timeouts import with_hard_timeout
from app.watsonx import WatsonxApiError, WatsonxIamTokenProvider

# provider -> (status, checked_at_monotonic)
_provider_cache: dict[str, tuple[str, float]] = {}


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


async def _probe_provider(
    name: str, url: str, headers: dict[str, str], probe_timeout: float
) -> str:
    try:
        async with httpx.AsyncClient(timeout=probe_timeout) as client:
            response = await client.get(url, headers=headers)
        # Authentication failures prove reachability but expose invalid credentials.
        if response.status_code in (401, 403):
            return "unauthorized"
        return "up" if response.status_code < 500 else "down"
    except Exception:
        return "down"


async def check_providers(settings: Settings) -> dict[str, str]:
    """Cached provider reachability (probed at most once per
    PROVIDER_PROBE_INTERVAL_SECONDS — never per request)."""
    if not settings.provider_probe_enabled:
        return {}
    now = time.monotonic()
    interval = settings.provider_probe_interval_seconds
    results: dict[str, str] = {}

    targets: list[tuple[str, str, dict[str, str]]] = [
        (
            "openai",
            "https://api.openai.com/v1/models",
            {"authorization": f"Bearer {settings.openai_api_key.get_secret_value()}"},
        )
    ]
    if settings.anthropic_api_key:
        targets.append(
            (
                "anthropic",
                "https://api.anthropic.com/v1/models",
                {
                    "x-api-key": settings.anthropic_api_key.get_secret_value(),
                    "anthropic-version": "2023-06-01",
                },
            )
        )

    for name, url, headers in targets:
        cached = _provider_cache.get(name)
        if cached is not None and now - cached[1] < interval:
            results[name] = cached[0]
            continue
        status = await _probe_provider(
            name, url, headers, settings.provider_probe_timeout_seconds
        )
        _provider_cache[name] = (status, now)
        results[name] = status

    if settings.watsonx_enabled:
        cached = _provider_cache.get("watsonx")
        if cached is not None and now - cached[1] < interval:
            results["watsonx"] = cached[0]
        else:
            tokens: WatsonxIamTokenProvider | None = None
            try:
                tokens = WatsonxIamTokenProvider(settings)
                token = await tokens.get_token()
                catalog_url = (
                    f"{settings.watsonx_url.rstrip('/')}"
                    "/ml/v1/foundation_model_specs"
                    f"?version={settings.watsonx_api_version}&limit=1"
                )
                status = await _probe_provider(
                    "watsonx",
                    catalog_url,
                    {"authorization": f"Bearer {token}"},
                    settings.provider_probe_timeout_seconds,
                )
            except WatsonxApiError as exc:
                status = (
                    "unauthorized"
                    if exc.status_code in (401, 403)
                    else "down"
                )
            except Exception:
                status = "down"
            finally:
                if tokens is not None:
                    await tokens.aclose()
            _provider_cache["watsonx"] = (status, now)
            results["watsonx"] = status
    return results
