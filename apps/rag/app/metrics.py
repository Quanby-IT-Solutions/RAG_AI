"""Prometheus metrics + structured JSON logs (OB-2).

Every instrument is a prometheus-client metric on the default registry —
never an in-process dict. Metrics are per-replica by design (Prometheus
aggregates across scrape targets); shared Redis counters (CP-1/CP-4) remain
the cross-replica source RP-4 reads.

The ASGI middleware stamps a request id, measures duration, feeds the HTTP
instruments, and emits ONE structured JSON access-log line per request
(requestId, principal sub — the masked-world identifier, never PII — route,
status, durationMs). GET /metrics itself carries no auth/rate-limit
dependency (scrape-path exemption, SC-2 note).
"""

import json
import logging
import time
import uuid
from typing import Any

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)
from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger("app.access")

HTTP_REQUESTS = Counter(
    "rag_http_requests_total",
    "HTTP requests by route/method/status",
    ["route", "method", "status"],
)
HTTP_DURATION = Histogram(
    "rag_http_request_duration_seconds",
    "HTTP request duration",
    ["route", "method"],
)
CHAT_TTFT = Histogram(
    "rag_chat_ttft_seconds",
    "Time to first streamed token",
)
CHAT_TOKENS = Counter(
    "rag_chat_tokens_total",
    "LLM tokens by kind",
    ["kind"],
)
CACHE_LOOKUPS = Counter(
    "rag_cache_lookups_total",
    "Response-cache lookups by result",
    ["result"],
)
PROVIDER_429 = Counter(
    "rag_provider_429_total",
    "Provider 429 responses",
    ["provider"],
)
BUCKET_EVENTS = Counter(
    "rag_provider_bucket_events_total",
    "Provider-bucket events (acquired/waited/shed)",
    ["provider", "event"],
)
BREAKER_OPEN = Gauge(
    "rag_breaker_open",
    "1 while a provider circuit is open (OB-3 alert source)",
    ["provider"],
)
ERRORS = Counter(
    "rag_errors_total",
    "Requests that ended >= 500",
    ["route"],
)


def observe_tokens(token_usage: dict[str, int] | None) -> None:
    if not token_usage:
        return
    CHAT_TOKENS.labels(kind="prompt").inc(token_usage.get("promptTokens", 0))
    CHAT_TOKENS.labels(kind="completion").inc(token_usage.get("completionTokens", 0))


def render_latest() -> tuple[bytes, str]:
    return generate_latest(), CONTENT_TYPE_LATEST


class JsonLogFormatter(logging.Formatter):
    """Structured JSON lines suitable for aggregation (OB-2 AC)."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key in ("requestId", "principal", "route", "status", "durationMs"):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def configure_json_logging() -> None:
    """Route ALL app logs through the JSON formatter (idempotent)."""
    root = logging.getLogger()
    handler = logging.StreamHandler()
    handler.setFormatter(JsonLogFormatter())
    root.handlers = [handler]
    root.setLevel(logging.INFO)


class MetricsMiddleware:
    """ASGI middleware: request id + duration + HTTP metrics + access log."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        route = scope.get("path", "unknown")
        method = scope.get("method", "GET")
        headers = {k.decode(): v.decode() for k, v in scope.get("headers", [])}
        request_id = headers.get("x-request-id") or uuid.uuid4().hex
        started = time.perf_counter()
        status_holder = {"status": 500}

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                status_holder["status"] = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            duration = time.perf_counter() - started
            status = status_holder["status"]
            HTTP_REQUESTS.labels(route=route, method=method, status=str(status)).inc()
            HTTP_DURATION.labels(route=route, method=method).observe(duration)
            if status >= 500:
                ERRORS.labels(route=route).inc()
            logger.info(
                "request",
                extra={
                    "requestId": request_id,
                    "route": f"{method} {route}",
                    "status": status,
                    "durationMs": round(duration * 1000, 2),
                },
            )
