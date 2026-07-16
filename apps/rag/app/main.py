"""FastAPI application — RAG service walking skeleton (F1).

Internal-only service: reachable exclusively on the Docker `app-network`
(no Nginx route). Config is validated fail-fast at import/boot; tracing is
initialized at startup; `/health` performs real DB + Redis checks.
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response

from app import evaluation, generation, ingest, reembed, report_render, retrieval
from app import health as health_checks
from app.config import Settings, get_settings
from app.documents import DocumentsRepository
from app.embeddings import assert_embedding_dimensions
from app.tracing import init_tracing, shutdown_tracing, traced

logger = logging.getLogger(__name__)


@traced(name="rag.health")
async def run_health_checks(settings: Settings) -> dict[str, str]:
    """Probe every readiness dependency; return per-dependency status."""
    checks: dict[str, str] = {}
    try:
        await health_checks.check_database(settings)
        checks["database"] = "up"
    except Exception as exc:
        logger.warning("Database health check failed: %s", exc)
        checks["database"] = "down"
    try:
        await health_checks.check_redis(settings)
        checks["redis"] = "up"
    except Exception as exc:
        logger.warning("Redis health check failed: %s", exc)
        checks["redis"] = "down"
    # CE-5: the graph store is a component ONLY when the flag is on —
    # baseline deployments never touch (or report on) Neo4j.
    if settings.graph_rag_enabled:
        from app.graph import GraphRepository

        graph_repository = GraphRepository(settings)
        try:
            await graph_repository.verify_connectivity()
            checks["neo4j"] = "up"
        except Exception as exc:
            logger.warning("Neo4j health check failed: %s", exc)
            checks["neo4j"] = "down"
        finally:
            await graph_repository.close()
    return checks


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    # OB-2: ALL app logs are structured JSON lines (aggregation-ready);
    # uvicorn only configures its own loggers.
    from app.metrics import MetricsMiddleware, configure_json_logging

    configure_json_logging()
    app_middleware = MetricsMiddleware

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        init_tracing(settings)
        # Boot-time dimension guard (Tech Plan §4): configured
        # EMBEDDING_DIMENSIONS must match the chunks.embedding column —
        # a mismatch aborts boot before any request is served.
        await assert_embedding_dimensions(settings, DocumentsRepository(settings))
        # CE-0: state the resolved profile + flags so a deployment's variant
        # is readable straight from the boot log.
        logger.info(
            "RAG service starting (profile=%s contextual=%s mode=%s graph=%s "
            "multimodal=%s embedding=%s dims=%s)",
            settings.rag_profile,
            settings.contextual_chunks_enabled,
            settings.rag_mode,
            settings.graph_rag_enabled,
            settings.multimodal_enabled,
            settings.embedding_model,
            settings.embedding_dimensions,
        )
        yield
        shutdown_tracing()

    app = FastAPI(title="RAG Service", version="0.0.1", lifespan=lifespan)
    app.state.settings = settings

    # Internal-only ingestion (Flow A) + chat (Flow B) surfaces; every
    # /internal/* route is protected by the service-JWT dependency.
    # SC-2: shared per-principal limiter (Redis-backed; /health is exempt
    # because it carries no rate-limit dependency).
    from app.rate_limit import build_limiter

    app.state.limiter = build_limiter(settings)

    # CP-4 backpressure: a provider-budget shed is a CLEAN 429 with
    # Retry-After (CH-2 renders the rate-limited state), never a raw 500.
    from starlette.responses import JSONResponse

    from app.provider_bucket import ProviderBucketExhausted

    @app.exception_handler(ProviderBucketExhausted)
    async def _bucket_exhausted(request, exc):  # type: ignore[no-untyped-def]
        return JSONResponse(
            status_code=429,
            content={"detail": str(exc)},
            headers={"Retry-After": str(settings.bucket_window_seconds)},
        )

    app.include_router(ingest.router)
    app.include_router(generation.router)
    app.include_router(retrieval.router)
    app.include_router(evaluation.router)
    app.include_router(reembed.router)
    app.include_router(report_render.router)

    # OB-2: request-id + latency + HTTP counters + JSON access log.
    app.add_middleware(app_middleware)

    # OB-4: shedding sits OUTERMOST (added last = runs first) so overload and
    # drain refuse work before any expensive stage.
    from app.load_shed import DrainState, LoadShedMiddleware

    drain = DrainState()
    drain.install()
    app.state.drain = drain
    app.add_middleware(LoadShedMiddleware, settings=settings, drain=drain)

    @app.get("/internal/config")
    async def internal_config(request: Request) -> dict:
        """OB-5: effective configuration, secrets masked BY TYPE (read-only).
        Gateway-gated by ops.config.view; the service JWT is verified here."""
        from app.config_view import effective_config
        from app.security import verify_service_jwt

        verify_service_jwt(request)
        return effective_config(settings)

    @app.get("/metrics")
    async def metrics() -> Response:
        """Prometheus scrape target — no auth/rate-limit (SC-2 exemption).
        OB-4: DB pressure gauges refresh at scrape time (bounded probe)."""
        from app.load_shed import sample_db_pressure
        from app.metrics import render_latest

        await sample_db_pressure(settings)
        payload, content_type = render_latest()
        return Response(content=payload, media_type=content_type)

    @app.get("/health")
    async def health(response: Response) -> dict:
        checks = await run_health_checks(settings)
        # OB-3: readiness gates on DB + Redis only; provider reachability is
        # informational (breaker owns runtime failover) and interval-cached.
        # OB-4: a draining replica fails readiness so the LB stops routing.
        # CE-5 (Flow I): the graph store is an ACCELERATOR — neo4j down means
        # degraded (hybrid continues without it), never unavailable.
        ready = (
            all(status == "up" for name, status in checks.items() if name != "neo4j")
            and not drain.draining
        )
        providers = await health_checks.check_providers(settings)
        degraded = any(status != "up" for status in providers.values()) or (
            checks.get("neo4j") == "down"
        )
        if not ready:
            response.status_code = 503
        return {
            "status": "unavailable" if not ready else ("degraded" if degraded else "ok"),
            "live": True,
            "ready": ready,
            "draining": drain.draining,
            "checks": checks,
            "providers": providers,
        }

    return app


# uvicorn entrypoint: `uvicorn --factory app.main:create_app`. Boot aborts
# (SystemExit naming the settings) when required configuration is missing.
