"""Per-client rate limiting (SC-2) — SlowAPI/limits over shared Redis storage.

Limits are keyed by the AUTHENTICATED principal (user id or apikey:<id>)
from the verified service JWT — one hot client can never consume another's
budget — and counters live in Redis so every replica shares one view
(RATE_LIMIT_STORAGE overrides for tests, e.g. memory://). /health (and
future /metrics) are exempt: they simply carry no rate-limit dependency.

429 responses carry Retry-After plus a JSON detail; the gateway forwards
the status/message and CH-2 renders the rate-limited state.
"""

import logging
import time
from typing import Any

import jwt
from fastapi import HTTPException, Request
from slowapi import Limiter

from app.config import Settings

logger = logging.getLogger(__name__)


def principal_key(request: Request) -> str:
    """Rate-limit key: the VERIFIED JWT subject. An unverifiable token keys
    to a shared 'anonymous' bucket for garbage traffic (it will 401 in the
    route anyway — it must never pollute a real principal's budget)."""
    settings: Settings = request.app.state.settings
    authorization = request.headers.get("authorization", "")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() == "bearer" and token:
        try:
            payload = jwt.decode(
                token,
                settings.rag_service_token_secret,
                algorithms=["HS256"],
                options={"require": ["exp", "sub"]},
            )
            return f"principal:{payload['sub']}"
        except jwt.InvalidTokenError:
            pass
    return "anonymous"


def build_limiter(settings: Settings) -> Limiter:
    """SlowAPI limiter: Redis-backed (multi-replica safe) unless
    RATE_LIMIT_STORAGE overrides (tests use memory://)."""
    return Limiter(
        key_func=principal_key,
        storage_uri=settings.rate_limit_storage or settings.redis_url,
        enabled=settings.rate_limit_enabled,
        headers_enabled=True,
    )


def rate_limit(setting_name: str) -> Any:
    """FastAPI dependency enforcing the named per-deployment limit (e.g.
    'rate_limit_chat' = '30/minute') per principal. Routes without this
    dependency — /health — are exempt by construction."""
    from limits import parse

    async def dependency(request: Request) -> None:
        settings: Settings = request.app.state.settings
        if not settings.rate_limit_enabled:
            return
        limiter: Limiter = request.app.state.limiter
        limit = parse(getattr(settings, setting_name))
        key = principal_key(request)
        # Namespaced per route-class so chat and ingest budgets are separate.
        if limiter.limiter.hit(limit, setting_name, key):
            return
        window = limiter.limiter.get_window_stats(limit, setting_name, key)
        retry_after = max(1, int(window.reset_time - time.time()) + 1)
        logger.info(
            "rate_limit.exceeded %s",
            {"key": key, "scope": setting_name, "limit": str(limit)},
        )
        # 429 + Retry-After: the shape CH-2's rate-limited state consumes.
        raise HTTPException(
            status_code=429,
            detail=f"Rate limit exceeded ({limit}). Try again shortly.",
            headers={"Retry-After": str(retry_after)},
        )

    return dependency
