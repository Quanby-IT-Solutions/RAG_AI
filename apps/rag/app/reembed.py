"""Zero-downtime re-embedding — shadow index and cutover (IN-8, Flow D).

State machine (one non-terminal migration at a time, every transition
audited): shadowing → verifying → ready → cutover | aborted | failed.

- SHADOWING: an arq job embeds every chunk's content with the TARGET model
  into `embedding_shadow` (IN-6 cache rules apply — the model key is a clean
  namespace) while the live query path continues on `embedding` untouched.
- DUAL-WRITE: ingestion during the window also writes shadow embeddings for
  new chunks (indexing checks the active migration).
- VERIFYING: the EV-2 harness runs golden-set retrieval against the SHADOW
  column with target-model query embeddings; results below the approved
  baseline − tolerance mark the migration FAILED (cutover blocked).
- CUTOVER (explicit confirm): the Redis active-embedding pointer flips
  atomically — the query path switches column AND query model in the same
  instant, preserving the pinned-model invariant (index model == query
  model at every moment).
- ABORT (any pre-cutover step): shadow artifacts are nulled; serving is
  never touched.
"""

import json
import logging
from typing import Any

import asyncpg

from app.config import PINNED_EMBEDDING_DIMENSIONS, Settings
from app.timeouts import with_hard_timeout

logger = logging.getLogger(__name__)

# Redis pointer consulted by retrieval: {"column": ..., "model": ...}.
ACTIVE_EMBEDDING_KEY = "embedding:active"
ALLOWED_COLUMNS = ("embedding", "embedding_shadow")

_ACTIVE_MIGRATION_SQL = """
SELECT id, target_model, status, metrics, error, triggered_by, created_at, updated_at
FROM reembed_migrations
WHERE status IN ('shadowing', 'verifying', 'ready')
ORDER BY created_at DESC LIMIT 1
"""

_LATEST_MIGRATION_SQL = """
SELECT id, target_model, status, metrics, error, triggered_by, created_at, updated_at
FROM reembed_migrations ORDER BY created_at DESC LIMIT 1
"""

_CREATE_MIGRATION_SQL = """
INSERT INTO reembed_migrations (target_model, triggered_by)
VALUES ($1, $2) RETURNING id
"""

_SET_STATUS_SQL = """
UPDATE reembed_migrations
SET status = $2, metrics = COALESCE($3, metrics), error = $4, updated_at = now()
WHERE id = $1
"""

_CHUNKS_TO_SHADOW_SQL = """
SELECT id, content FROM chunks
WHERE embedding_shadow IS NULL OR shadow_embedding_model IS DISTINCT FROM $1
ORDER BY id LIMIT $2
"""

_WRITE_SHADOW_SQL = """
UPDATE chunks SET embedding_shadow = $2::vector, shadow_embedding_model = $3
WHERE id = $1
"""

_CLEAR_SHADOW_SQL = """
UPDATE chunks SET embedding_shadow = NULL, shadow_embedding_model = NULL
WHERE embedding_shadow IS NOT NULL
"""


def _row_to_migration(row: Any) -> dict[str, Any]:
    metrics = row["metrics"]
    return {
        "id": str(row["id"]),
        "targetModel": row["target_model"],
        "status": row["status"],
        "metrics": json.loads(metrics) if isinstance(metrics, str) else metrics,
        "error": row["error"],
        "triggeredBy": row["triggered_by"],
        "createdAt": row["created_at"].isoformat(),
        "updatedAt": row["updated_at"].isoformat(),
    }


class ReembedRepository:
    """asyncpg persistence over the Drizzle-owned tables."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def _connect(self) -> asyncpg.Connection:
        return await with_hard_timeout(
            asyncpg.connect(dsn=self._settings.database_url),
            seconds=self._settings.db_timeout_seconds,
            operation="postgres.connect",
        )

    async def active_migration(self) -> dict[str, Any] | None:
        conn = await self._connect()
        try:
            row = await conn.fetchrow(_ACTIVE_MIGRATION_SQL)
        finally:
            await conn.close()
        return _row_to_migration(row) if row else None

    async def latest_migration(self) -> dict[str, Any] | None:
        conn = await self._connect()
        try:
            row = await conn.fetchrow(_LATEST_MIGRATION_SQL)
        finally:
            await conn.close()
        return _row_to_migration(row) if row else None

    async def create_migration(self, *, target_model: str, triggered_by: str) -> str:
        conn = await self._connect()
        try:
            row = await conn.fetchrow(_CREATE_MIGRATION_SQL, target_model, triggered_by)
        finally:
            await conn.close()
        return str(row["id"])

    async def set_status(
        self,
        migration_id: str,
        *,
        status: str,
        metrics: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        conn = await self._connect()
        try:
            await conn.execute(
                _SET_STATUS_SQL,
                migration_id,
                status,
                json.dumps(metrics) if metrics is not None else None,
                error,
            )
        finally:
            await conn.close()

    async def next_shadow_batch(self, target_model: str, limit: int) -> list[dict[str, Any]]:
        conn = await self._connect()
        try:
            rows = await conn.fetch(_CHUNKS_TO_SHADOW_SQL, target_model, limit)
        finally:
            await conn.close()
        return [{"id": str(row["id"]), "content": row["content"]} for row in rows]

    async def write_shadow(
        self, entries: list[tuple[str, list[float]]], target_model: str
    ) -> None:
        from app.documents import _vector_literal

        conn = await self._connect()
        try:
            async with conn.transaction():
                for chunk_id, vector in entries:
                    await conn.execute(
                        _WRITE_SHADOW_SQL, chunk_id, _vector_literal(vector), target_model
                    )
        finally:
            await conn.close()

    async def clear_shadow(self) -> None:
        conn = await self._connect()
        try:
            await conn.execute(_CLEAR_SHADOW_SQL)
        finally:
            await conn.close()


async def get_active_embedding(settings: Settings, redis_client: Any) -> dict[str, str]:
    """The query path's active (column, model) pointer — defaults to the
    pinned live column. Whitelisted columns only (never interpolate raw)."""
    try:
        raw = await redis_client.get(ACTIVE_EMBEDDING_KEY)
        if raw:
            pointer = json.loads(raw)
            if pointer.get("column") in ALLOWED_COLUMNS and pointer.get("model"):
                return {"column": pointer["column"], "model": pointer["model"]}
    except Exception:
        logger.warning("reembed.pointer_read_failed", exc_info=True)
    return {"column": "embedding", "model": settings.embedding_model}


async def set_active_embedding(redis_client: Any, *, column: str, model: str) -> None:
    assert column in ALLOWED_COLUMNS
    await redis_client.set(ACTIVE_EMBEDDING_KEY, json.dumps({"column": column, "model": model}))


class TargetEmbeddingsClient:
    """Embeddings client for the TARGET model — same pinned dimensions
    (the vector(1536) column is the type contract; a different-dimension
    target requires a column migration and is rejected up front)."""

    def __init__(self, settings: Settings, model: str, client: Any = None) -> None:
        from app.embeddings import EmbeddingsClient

        self._inner = EmbeddingsClient(settings, client=client)
        self._inner.model = model
        self.model = model
        self.dimensions = PINNED_EMBEDDING_DIMENSIONS

    async def embed_texts(self, texts: list[str]) -> tuple[list[list[float]], int]:
        return await self._inner.embed_texts(texts)


SHADOW_BATCH_SIZE = 100
REEMBED_TASK_NAME = "process_reembed_migration"


async def run_shadow_pass(
    *,
    settings: Settings,
    repository: ReembedRepository,
    migration: dict[str, Any],
    embeddings: Any,
    embedding_cache: Any,
) -> int:
    """Embed every chunk into the shadow column with the TARGET model (IN-6
    cache rules — the model key is a clean namespace). Returns chunk count."""
    from app.embedding_cache import embed_with_cache

    total = 0
    while True:
        batch = await repository.next_shadow_batch(migration["targetModel"], SHADOW_BATCH_SIZE)
        if not batch:
            return total
        # Cache key = chunk content hash is unavailable here (content only);
        # hash the content directly — identical content still dedups.
        import hashlib

        items = [
            (hashlib.sha256(entry["content"].encode()).hexdigest(), entry["content"])
            for entry in batch
        ]
        vectors, _ = await embed_with_cache(
            embeddings=embeddings, cache=embedding_cache, items=items
        )
        await repository.write_shadow(
            [(entry["id"], vector) for entry, vector in zip(batch, vectors, strict=True)],
            migration["targetModel"],
        )
        total += len(batch)
        logger.info("reembed.shadow_progress %s", {"written": total})


async def verify_shadow(
    *,
    settings: Settings,
    repository: ReembedRepository,
    migration: dict[str, Any],
    eval_repository: Any,
    embeddings: Any,
) -> tuple[dict[str, Any], bool, str | None]:
    """EV-2 golden-set retrieval against the SHADOW column with target-model
    query embeddings; compared to the approved baseline when one exists.
    Returns (metrics, cutover_allowed, blocking_reason)."""
    from app.chunks import ChunksRepository
    from app.eval_gate import evaluate_gate, fetch_current_baseline
    from app.evaluation import EvalRunError, run_retrieval_eval
    from app.retrieval import RetrievalService

    async def shadow_pointer() -> dict[str, str]:
        return {"column": "embedding_shadow", "model": migration["targetModel"]}

    retrieval = RetrievalService(
        settings=settings,
        embeddings=embeddings,
        repository=ChunksRepository(settings),
        active_embedding_resolver=shadow_pointer,
    )
    try:
        run = await run_retrieval_eval(
            settings=settings,
            retrieval=retrieval,
            repository=eval_repository,
            triggered_by=f"reembed-verify:{migration['id']}",
        )
    except EvalRunError as exc:
        return {}, False, f"verification not evaluable: {exc}"

    metrics = dict(run["metrics"] or {})
    baseline = await fetch_current_baseline(settings)
    if baseline is None:
        metrics["baselineCompared"] = 0.0
        return metrics, True, None
    metrics["baselineCompared"] = 1.0
    gate = evaluate_gate(
        candidate_metrics=metrics,
        baseline_metrics=baseline["metrics"],
        tolerance=settings.eval_gate_tolerance,
    )
    if not gate["passed"]:
        return metrics, False, "shadow retrieval metrics below baseline - tolerance"
    return metrics, True, None


async def process_reembed_migration(ctx: dict[str, Any], migration_id: str) -> dict[str, Any]:
    """arq task: shadowing -> verifying -> ready | failed (Flow D)."""
    settings: Settings = ctx["settings"]
    repository: ReembedRepository = ctx.get("reembed_repository") or ReembedRepository(settings)
    migration = await repository.latest_migration()
    assert migration is not None and migration["id"] == migration_id

    embeddings = ctx.get("reembed_embeddings") or TargetEmbeddingsClient(
        settings, migration["targetModel"]
    )
    from app.embedding_cache import EmbeddingCacheRepository
    from app.evaluation import EvalRepository

    embedding_cache = ctx.get("embedding_cache") or EmbeddingCacheRepository(settings)
    eval_repository = ctx.get("eval_repository") or EvalRepository(settings)

    try:
        written = await run_shadow_pass(
            settings=settings,
            repository=repository,
            migration=migration,
            embeddings=embeddings,
            embedding_cache=embedding_cache,
        )
        await repository.set_status(migration_id, status="verifying")
        metrics, allowed, reason = await verify_shadow(
            settings=settings,
            repository=repository,
            migration=migration,
            eval_repository=eval_repository,
            embeddings=embeddings,
        )
        if allowed:
            await repository.set_status(migration_id, status="ready", metrics=metrics)
            status = "ready"
        else:
            await repository.set_status(
                migration_id, status="failed", metrics=metrics, error=reason
            )
            status = "failed"
        logger.info(
            "reembed.migration %s",
            {"id": migration_id, "status": status, "chunksShadowed": written},
        )
        return {"status": status, "chunksShadowed": written, "metrics": metrics}
    except Exception as exc:
        await repository.set_status(migration_id, status="failed", error=str(exc))
        raise


# ---------------------------------------------------------------- HTTP surface
from typing import Annotated  # noqa: E402

from fastapi import APIRouter, Depends, HTTPException, Request  # noqa: E402

from app.security import ServicePrincipal, verify_service_jwt  # noqa: E402

router = APIRouter(prefix="/internal", dependencies=[Depends(verify_service_jwt)])


def get_reembed_repository(request: Request) -> ReembedRepository:
    return ReembedRepository(request.app.state.settings)


async def _audit(settings: Settings, principal: ServicePrincipal, action: str,
                 resource_id: str, after: dict[str, Any]) -> None:
    from app.conversations import ConversationsRepository

    await ConversationsRepository(settings).write_audit(
        actor_id=principal.sub,
        actor_type=principal.actor_type,
        action=action,
        resource_type="reembed_migration",
        resource_id=resource_id,
        before=None,
        after=after,
    )


from pydantic import BaseModel, Field  # noqa: E402


class StartReembedRequest(BaseModel):
    target_model: str = Field(alias="targetModel", min_length=1)

    model_config = {"populate_by_name": True}


@router.post("/reembed")
async def start_reembed(
    payload: StartReembedRequest,
    request: Request,
    principal: Annotated[ServicePrincipal, Depends(verify_service_jwt)],
    repository: Annotated[ReembedRepository, Depends(get_reembed_repository)],
) -> dict[str, Any]:
    settings = request.app.state.settings
    if payload.target_model == settings.embedding_model:
        raise HTTPException(status_code=400, detail="Target model equals the live model")
    active = await repository.active_migration()
    if active is not None:
        raise HTTPException(
            status_code=409, detail=f"A migration is already {active['status']}"
        )
    migration_id = await repository.create_migration(
        target_model=payload.target_model, triggered_by=principal.sub
    )
    from app.ingest import get_job_queue

    queue = await get_job_queue(request)
    await queue.enqueue_job(REEMBED_TASK_NAME, migration_id)
    await _audit(settings, principal, "corpus.reembed.start", migration_id,
                 {"targetModel": payload.target_model})
    migration = await repository.latest_migration()
    assert migration is not None
    return migration


@router.get("/reembed")
async def reembed_status(
    repository: Annotated[ReembedRepository, Depends(get_reembed_repository)],
) -> dict[str, Any]:
    migration = await repository.latest_migration()
    return migration or {"status": "none"}


@router.post("/reembed/cutover")
async def cutover_reembed(
    request: Request,
    principal: Annotated[ServicePrincipal, Depends(verify_service_jwt)],
    repository: Annotated[ReembedRepository, Depends(get_reembed_repository)],
) -> dict[str, Any]:
    settings = request.app.state.settings
    migration = await repository.latest_migration()
    if migration is None or migration["status"] != "ready":
        raise HTTPException(
            status_code=409,
            detail="Cutover requires a migration in 'ready' (verified) state",
        )
    import redis.asyncio as aioredis

    client = aioredis.from_url(settings.redis_url, decode_responses=True)
    # ATOMIC switch: one SET flips column + query model together.
    await set_active_embedding(
        client, column="embedding_shadow", model=migration["targetModel"]
    )
    await repository.set_status(migration["id"], status="cutover")
    await _audit(settings, principal, "corpus.reembed.cutover", migration["id"],
                 {"targetModel": migration["targetModel"], "column": "embedding_shadow"})
    updated = await repository.latest_migration()
    assert updated is not None
    return updated


@router.post("/reembed/abort")
async def abort_reembed(
    request: Request,
    principal: Annotated[ServicePrincipal, Depends(verify_service_jwt)],
    repository: Annotated[ReembedRepository, Depends(get_reembed_repository)],
) -> dict[str, Any]:
    settings = request.app.state.settings
    migration = await repository.latest_migration()
    if migration is None or migration["status"] in ("cutover", "aborted"):
        raise HTTPException(status_code=409, detail="No abortable migration")
    # Serving path untouched: shadow artifacts null out, pointer never moved.
    await repository.clear_shadow()
    await repository.set_status(migration["id"], status="aborted")
    await _audit(settings, principal, "corpus.reembed.abort", migration["id"],
                 {"targetModel": migration["targetModel"]})
    updated = await repository.latest_migration()
    assert updated is not None
    return updated
