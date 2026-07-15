"""Tracing bootstrap (Tech Plan §8): LangSmith by default, Langfuse optional.

Initialized once at startup so every traced operation (`traced(...)`) emits a
trace for the configured provider. Later slices (IN-2/RT-1/GN-1) wrap their
pipeline steps with the same decorator — exactly one trace per request.
"""

import functools
import logging
import os
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any, TypeVar

from app.config import Settings

logger = logging.getLogger(__name__)

T = TypeVar("T")

_langfuse_client: Any = None


def init_tracing(settings: Settings) -> None:
    """Configure the tracing provider. Must run before any traced call."""
    global _langfuse_client
    if settings.tracing_provider == "langsmith":
        # The langsmith SDK is driven by env vars; pin them from validated settings.
        os.environ["LANGSMITH_TRACING"] = "true"
        os.environ["LANGSMITH_API_KEY"] = (
            settings.langsmith_api_key.get_secret_value()
            if settings.langsmith_api_key
            else ""
        )
        os.environ["LANGSMITH_PROJECT"] = settings.langsmith_project
        os.environ["LANGSMITH_ENDPOINT"] = settings.langsmith_endpoint
        logger.info(
            "Tracing initialized: langsmith (project=%s endpoint=%s)",
            settings.langsmith_project,
            settings.langsmith_endpoint,
        )
    else:
        from langfuse import Langfuse

        _langfuse_client = Langfuse(
            public_key=settings.langfuse_public_key,
            secret_key=settings.langfuse_secret_key.get_secret_value()
            if settings.langfuse_secret_key
            else None,
            host=settings.langfuse_host,
        )
        logger.info("Tracing initialized: langfuse (host=%s)", settings.langfuse_host)


def shutdown_tracing() -> None:
    """Flush pending traces on shutdown."""
    if _langfuse_client is not None:
        _langfuse_client.flush()


def traced(
    name: str,
) -> Callable[[Callable[..., Awaitable[T]]], Callable[..., Awaitable[T]]]:
    """Decorator emitting one trace span per call for the active provider."""

    def decorate(fn: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
        from langsmith import traceable

        # No-op unless LANGSMITH_TRACING is set by init_tracing (langsmith provider).
        langsmith_wrapped = traceable(name=name)(fn)

        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> T:
            if _langfuse_client is not None:
                with _langfuse_client.start_as_current_span(name=name):
                    return await fn(*args, **kwargs)
            return await langsmith_wrapped(*args, **kwargs)

        return wrapper

    return decorate


@asynccontextmanager
async def stage(name: str, **attributes: Any):
    """OB-1: one named child span per Flow B stage, provider-agnostic.

    Yields a setter for output attributes: `outputs = yield`-style usage:

        async with stage("cache.lookup", query_len=n) as record:
            ...
            record(hit=True, layer="exact")

    Tracing failures NEVER break the request (best-effort)."""
    outputs: dict[str, Any] = {}

    def record(**values: Any) -> None:
        outputs.update(values)

    if _langfuse_client is not None:
        try:
            with _langfuse_client.start_as_current_span(name=name) as span:
                try:
                    yield record
                finally:
                    try:
                        span.update(input=attributes or None, output=outputs or None)
                    except Exception:
                        logger.debug("tracing.stage_update_failed", exc_info=True)
            return
        except Exception:
            logger.debug("tracing.stage_failed", exc_info=True)
            yield record
            return

    span_cm = None
    run_tree = None
    try:
        from langsmith.run_helpers import trace as ls_trace

        span_cm = ls_trace(name=name, inputs=attributes or {})
        run_tree = span_cm.__enter__()
    except Exception:
        span_cm = None
        run_tree = None
    try:
        yield record
    finally:
        if span_cm is not None:
            try:
                if run_tree is not None:
                    run_tree.end(outputs=outputs or {})
            except Exception:
                pass
            try:
                span_cm.__exit__(None, None, None)
            except Exception:
                logger.debug("tracing.stage_exit_failed", exc_info=True)
