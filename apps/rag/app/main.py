"""FastAPI application — RAG service walking skeleton (F1).

Internal-only service: reachable exclusively on the Docker `app-network`
(no Nginx route). Config is validated fail-fast at import/boot; tracing is
initialized at startup; `/health` performs real DB + Redis checks.
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Response

from app import generation, ingest, retrieval
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
    return checks


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        init_tracing(settings)
        # Boot-time dimension guard (Tech Plan §4): configured
        # EMBEDDING_DIMENSIONS must match the chunks.embedding column —
        # a mismatch aborts boot before any request is served.
        await assert_embedding_dimensions(settings, DocumentsRepository(settings))
        logger.info(
            "RAG service starting (embedding=%s dims=%s)",
            settings.embedding_model,
            settings.embedding_dimensions,
        )
        yield
        shutdown_tracing()

    app = FastAPI(title="RAG Service", version="0.0.1", lifespan=lifespan)
    app.state.settings = settings

    # Internal-only ingestion (Flow A) + chat (Flow B) surfaces; every
    # /internal/* route is protected by the service-JWT dependency.
    app.include_router(ingest.router)
    app.include_router(generation.router)
    app.include_router(retrieval.router)

    @app.get("/health")
    async def health(response: Response) -> dict:
        checks = await run_health_checks(settings)
        ready = all(status == "up" for status in checks.values())
        if not ready:
            response.status_code = 503
        return {
            "status": "ok" if ready else "unavailable",
            "live": True,
            "ready": ready,
            "checks": checks,
        }

    return app


# uvicorn entrypoint: `uvicorn --factory app.main:create_app`. Boot aborts
# (SystemExit naming the settings) when required configuration is missing.
