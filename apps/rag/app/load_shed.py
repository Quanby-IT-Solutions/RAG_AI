"""Load shedding + graceful drain + DB scaling signal (OB-4).

- Shedding sits at the ASGI entry (BEFORE any expensive Flow B stage): when
  in-flight requests reach MAX_INFLIGHT_REQUESTS — or the replica is
  draining — new work gets a FAST 503 + Retry-After (no queue buildup),
  counted in OB-2. /health and /metrics are always admitted (the LB and
  Prometheus must see a draining replica).
- Drain: SIGTERM flips the draining flag, readiness goes false (the LB stops
  routing), new work sheds, and uvicorn's graceful shutdown finishes the
  in-flight requests (GN-3 stream cancellation closes provider streams).
- DB signal: gauges sampled from pg_stat_activity at scrape time so DB
  pressure is distinguishable from API pressure on the OB-2 dashboards.
"""

import logging
import signal
from typing import Any

import asyncpg
from prometheus_client import Counter, Gauge
from starlette.types import ASGIApp, Receive, Scope, Send

from app.config import Settings
from app.timeouts import with_hard_timeout

logger = logging.getLogger(__name__)

INFLIGHT = Gauge("rag_inflight_requests", "In-flight HTTP requests")
LOAD_SHED = Counter("rag_load_shed_total", "Requests shed under overload/drain", ["reason"])
DB_CONNECTIONS = Gauge(
    "rag_db_connections", "Postgres connections by state", ["state"]
)
DB_MAX_CONNECTIONS = Gauge("rag_db_max_connections", "Postgres max_connections")
DB_LONGEST_QUERY_SECONDS = Gauge(
    "rag_db_longest_query_seconds", "Age of the oldest active query"
)

_ALWAYS_ADMIT = ("/health", "/metrics")


class DrainState:
    """Process-wide drain flag flipped by SIGTERM/SIGINT."""

    def __init__(self) -> None:
        self.draining = False

    def install(self) -> None:
        def _handler(signum: int, frame: Any) -> None:
            if not self.draining:
                self.draining = True
                logger.warning(
                    "drain.started %s", {"signal": signal.Signals(signum).name}
                )

        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                previous = signal.getsignal(sig)

                def chained(signum: int, frame: Any, _prev=previous) -> None:
                    _handler(signum, frame)
                    if callable(_prev):
                        _prev(signum, frame)

                signal.signal(sig, chained)
            except (ValueError, OSError):  # non-main thread / unsupported
                logger.debug("drain.signal_install_skipped", exc_info=True)


class LoadShedMiddleware:
    """Fast 503 + Retry-After above the in-flight cap or while draining."""

    def __init__(self, app: ASGIApp, *, settings: Settings, drain: DrainState) -> None:
        self.app = app
        self.settings = settings
        self.drain = drain
        self.in_flight = 0

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") in _ALWAYS_ADMIT:
            await self.app(scope, receive, send)
            return

        reason = None
        if self.drain.draining:
            reason = "draining"
        elif self.in_flight >= self.settings.max_inflight_requests:
            reason = "overload"

        if reason is not None:
            LOAD_SHED.labels(reason=reason).inc()
            logger.warning(
                "load_shed %s",
                {"reason": reason, "inFlight": self.in_flight, "path": scope.get("path")},
            )
            await send(
                {
                    "type": "http.response.start",
                    "status": 503,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"retry-after", str(self.settings.shed_retry_after_seconds).encode()),
                    ],
                }
            )
            await send(
                {
                    "type": "http.response.body",
                    "body": b'{"detail": "Service overloaded - request shed. Retry shortly."}',
                }
            )
            return

        self.in_flight += 1
        INFLIGHT.set(self.in_flight)
        try:
            await self.app(scope, receive, send)
        finally:
            self.in_flight -= 1
            INFLIGHT.set(self.in_flight)


async def sample_db_pressure(settings: Settings) -> None:
    """Refresh DB gauges from pg_stat_activity (scrape-time, bounded, and a
    failure never breaks /metrics)."""
    try:

        async def _probe() -> None:
            conn = await asyncpg.connect(dsn=settings.database_url)
            try:
                rows = await conn.fetch(
                    "SELECT state, count(*) AS n FROM pg_stat_activity "
                    "WHERE datname = current_database() GROUP BY state"
                )
                for row in rows:
                    DB_CONNECTIONS.labels(state=row["state"] or "unknown").set(row["n"])
                max_connections = await conn.fetchval("SHOW max_connections")
                DB_MAX_CONNECTIONS.set(int(max_connections))
                longest = await conn.fetchval(
                    "SELECT coalesce(max(extract(epoch FROM now() - query_start)), 0) "
                    "FROM pg_stat_activity WHERE state = 'active' "
                    "AND datname = current_database()"
                )
                DB_LONGEST_QUERY_SECONDS.set(float(longest or 0))
            finally:
                await conn.close()

        await with_hard_timeout(
            _probe(), seconds=settings.db_timeout_seconds, operation="db.pressure_sample"
        )
    except Exception:
        logger.warning("db.pressure_sample_failed", exc_info=True)
