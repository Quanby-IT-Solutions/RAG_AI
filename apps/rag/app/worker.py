"""arq worker (IN-7) — background ingestion decoupled from the query path.

Runs as a SEPARATE process (`arq app.worker.WorkerSettings`) so heavy corpus
work never blocks or degrades /chat request workers. Jobs are enqueued on
Redis by the API's upload/re-run endpoints; each job walks its pending
job_documents through the shared indexing core (Flow A — upsert-idempotent,
so re-running any job is safe and never duplicates rows).

A document failure records the reason on the document AND the job_documents
row and the job CONTINUES with the remaining documents (Flow A failure
branch). The job itself only reports `failed` when every document failed or
the run aborted unexpectedly; partial failures land in stats.failures.
"""

import logging
from typing import Any

from arq import cron
from arq.connections import RedisSettings

from app.config import get_settings
from app.conversations import ConversationsRepository
from app.documents import DocumentsRepository
from app.embeddings import EmbeddingsClient
from app.indexing import index_document_content
from app.jobs import JobsRepository
from app.reembed import ReembedRepository, process_reembed_migration
from app.report_scheduler import process_report_schedules
from app.tracing import init_tracing, shutdown_tracing, traced

logger = logging.getLogger(__name__)

AUDIT_ACTION_INGEST_RUN = "corpus.ingest.run"


@traced(name="rag.job")
async def process_ingestion_job(ctx: dict[str, Any], job_id: str) -> dict[str, Any]:
    settings = ctx["settings"]
    # ctx carries test doubles when present; the arq startup hook only sets
    # settings + embeddings, so production builds the real repositories.
    jobs = ctx.get("jobs") or JobsRepository(settings)
    repository = ctx.get("repository") or DocumentsRepository(settings)
    embeddings: EmbeddingsClient = ctx["embeddings"]
    embedding_cache = ctx.get("embedding_cache")

    await jobs.mark_running(job_id)
    job = await jobs.fetch_job(job_id)
    job_documents = await jobs.fetch_job_documents(job_id)

    stats: dict[str, Any] = {
        "documents": len(job_documents),
        "chunks": 0,
        "embedBatches": 0,
        # IN-6: real cache counters — a no-change re-index shows 100% hits.
        "cacheHits": 0,
        "cacheMisses": 0,
        "failures": [],
    }

    for entry in job_documents:
        if entry["status"] == "indexed":
            continue  # re-run of a partially completed job skips done work
        document = await repository.fetch_by_id(entry["document_id"])
        if document is None:
            await jobs.set_document_outcome(
                job_id=job_id,
                document_id=entry["document_id"],
                status="failed",
                chunk_count=None,
                error="Document row disappeared before indexing",
            )
            stats["failures"].append(
                {"documentId": entry["document_id"], "error": "document missing"}
            )
            continue

        outcome = await index_document_content(
            document=document,
            settings=settings,
            repository=repository,
            embeddings=embeddings,
            embedding_cache=embedding_cache,
            # IN-8 dual-write: new chunks also get shadow embeddings while a
            # re-embed migration is active.
            reembed_repository=ctx.get("reembed_repository") or ReembedRepository(settings),
        )
        await jobs.set_document_outcome(
            job_id=job_id,
            document_id=entry["document_id"],
            status=outcome["status"],
            chunk_count=outcome["chunkCount"],
            error=outcome["error"],
        )
        if outcome["status"] == "indexed":
            stats["chunks"] += outcome["chunkCount"] or 0
            stats["embedBatches"] += outcome["embedBatches"]
            stats["cacheHits"] += outcome.get("cacheHits", 0)
            stats["cacheMisses"] += outcome.get("cacheMisses", 0)
        else:
            stats["failures"].append(
                {"documentId": entry["document_id"], "error": outcome["error"]}
            )

    processed = [e for e in job_documents if e["status"] != "indexed"]
    # CP-2: a completed ingestion/re-index changed the corpus — retire every
    # cached answer from the previous generation (embedding cache untouched).
    indexed_any = bool(processed) and len(stats["failures"]) < len(processed)
    if indexed_any:
        try:
            from app.response_cache import bump_corpus_generation

            await bump_corpus_generation(settings, ctx.get("respcache_redis"))
        except Exception:
            logger.warning("respcache.generation_bump_failed", exc_info=True)

    all_failed = bool(processed) and len(stats["failures"]) == len(processed)
    status = "failed" if all_failed else "completed"
    error = stats["failures"][0]["error"] if all_failed else None
    await jobs.finish(job_id, status=status, stats=stats, error=error)

    # Job completion is a privileged corpus action -> audited (Flow E).
    await repository.write_audit(
        actor_id=(job or {}).get("triggered_by") or "worker",
        actor_type="user",
        action=AUDIT_ACTION_INGEST_RUN,
        resource_type="ingestion_job",
        resource_id=job_id,
        before=None,
        after={"status": status, **stats},
    )
    logger.info("Ingestion job %s finished: %s (%s)", job_id, status, stats)
    return {"status": status, **stats}


@traced(name="rag.retention_purge")
async def purge_expired_conversations(ctx: dict[str, Any]) -> dict[str, Any]:
    """CH-3 retention: nightly purge of conversations past the deployment
    window (default 1 year — PRD). Messages cascade with the conversation."""
    settings = ctx["settings"]
    repository = ctx.get("conversations") or ConversationsRepository(settings)
    purged = await repository.purge_expired(settings.chat_retention_days)
    logger.info(
        "retention.purged conversations=%s retention_days=%s",
        purged,
        settings.chat_retention_days,
    )
    return {"purged": purged, "retentionDays": settings.chat_retention_days}


async def startup(ctx: dict[str, Any]) -> None:
    settings = get_settings()
    init_tracing(settings)
    ctx["settings"] = settings
    embeddings_client = EmbeddingsClient(settings)
    if settings.provider_bucket_enabled:
        # CP-4: worker embedding batches draw from the same shared budget.
        from app.provider_bucket import ThrottledEmbeddingsClient, build_provider_bucket

        bucket = build_provider_bucket(settings)
        if bucket is not None:
            embeddings_client = ThrottledEmbeddingsClient(embeddings_client, bucket)
    ctx["embeddings"] = embeddings_client
    logger.info("arq worker started (redis=%s)", settings.redis_url)


async def shutdown(ctx: dict[str, Any]) -> None:
    shutdown_tracing()


def _redis_settings() -> RedisSettings:
    """Resolved at import for the arq CLI; tests import this module without a
    full environment, so a missing config falls back to a placeholder that is
    never connected to (arq only runs via the CLI with real settings)."""
    try:
        return RedisSettings.from_dsn(get_settings().redis_url)
    except SystemExit:
        return RedisSettings()


class WorkerSettings:
    """`arq app.worker.WorkerSettings` — the worker process entrypoint."""

    functions = [process_ingestion_job, process_reembed_migration]
    cron_jobs = [
        # CH-3 retention purge, nightly at 03:10 (worker-local time).
        cron(purge_expired_conversations, hour=3, minute=10),
        # RP-6: check for due report schedules once a minute (second=0 is the
        # arq default, so this fires at every minute boundary).
        cron(process_report_schedules, max_tries=1),
    ]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = _redis_settings()
    # One failed attempt is final for now: re-runs are explicit (console
    # button / rerun endpoint) because the pipeline is upsert-idempotent.
    max_tries = 1
