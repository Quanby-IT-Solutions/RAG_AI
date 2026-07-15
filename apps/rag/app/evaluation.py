"""Retrieval metrics harness (EV-2, §5.7).

Scores the REAL retrieval pipeline (Flow B stages via RetrievalService — not
a reimplementation) against the EV-1 golden set: hit rate, MRR, precision@k,
and recall@k per item, aggregated into one `eval_runs` row stamped with the
git sha and an exact config snapshot (what was evaluated, with which knobs).

Fail-loud rules (AC): an empty golden set — or one where every active item is
stale (all expected hashes orphaned by re-chunking) — raises instead of
reporting perfect scores on nothing. Items with ANY stale hash are skipped
and counted per run, never silently scored.
"""

import json
import logging
from typing import Annotated, Any, Protocol

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.config import Settings
from app.retrieval import RetrievalService, get_retrieval_service
from app.security import ServicePrincipal, verify_service_jwt
from app.timeouts import with_hard_timeout

logger = logging.getLogger(__name__)

_ACTIVE_GOLDEN_SQL = """
SELECT id, question, ideal_answer, expected_chunk_hashes, tags
FROM golden_items
WHERE status = 'active'
ORDER BY created_at
"""

_LIVE_HASHES_SQL = """
SELECT content_hash FROM chunks WHERE content_hash = ANY($1::text[])
"""

_CREATE_RUN_SQL = """
INSERT INTO eval_runs (kind, status, git_sha, config, triggered_by)
VALUES ($1, 'running', $2, $3, $4)
RETURNING id
"""

_FINISH_RUN_SQL = """
UPDATE eval_runs
SET status = $2, metrics = $3, per_item = $4, error = $5, finished_at = now()
WHERE id = $1
"""

_RUN_SQL = """
SELECT id, kind, status, git_sha, config, metrics, per_item, error,
       triggered_by, created_at, finished_at
FROM eval_runs WHERE id = $1
"""


class EvalRunError(Exception):
    """Fail-loud condition: nothing evaluable (empty or fully stale set)."""


def score_item(
    expected_hashes: list[str], retrieved_hashes: list[str], k: int
) -> dict[str, float]:
    """Standard retrieval metrics for one golden item.

    - hit: 1.0 if ANY expected hash appears in the top-k, else 0.0
    - reciprocalRank: 1/rank of the FIRST expected hash found (0.0 if none)
    - precisionAtK: |expected ∩ retrieved| / k
    - recallAtK: |expected ∩ retrieved| / |expected|
    """
    expected = set(expected_hashes)
    top_k = retrieved_hashes[:k]
    relevant_found = [h for h in top_k if h in expected]

    reciprocal_rank = 0.0
    for rank, chunk_hash in enumerate(top_k, start=1):
        if chunk_hash in expected:
            reciprocal_rank = 1.0 / rank
            break

    return {
        "hit": 1.0 if relevant_found else 0.0,
        "reciprocalRank": reciprocal_rank,
        "precisionAtK": len(set(relevant_found)) / k,
        "recallAtK": len(set(relevant_found)) / len(expected) if expected else 0.0,
    }


def aggregate_metrics(per_item_scores: list[dict[str, float]]) -> dict[str, float]:
    """Mean of each metric over the evaluated (non-skipped) items."""
    count = len(per_item_scores)
    if count == 0:
        return {}
    return {
        "hitRate": sum(s["hit"] for s in per_item_scores) / count,
        "mrr": sum(s["reciprocalRank"] for s in per_item_scores) / count,
        "precisionAtK": sum(s["precisionAtK"] for s in per_item_scores) / count,
        "recallAtK": sum(s["recallAtK"] for s in per_item_scores) / count,
    }


def _row_to_run(row: Any) -> dict[str, Any]:
    def _json(value: Any) -> Any:
        return json.loads(value) if isinstance(value, str) else value

    return {
        "id": str(row["id"]),
        "kind": row["kind"],
        "status": row["status"],
        "gitSha": row["git_sha"],
        "config": _json(row["config"]),
        "metrics": _json(row["metrics"]),
        "perItem": _json(row["per_item"]),
        "error": row["error"],
        "triggeredBy": row["triggered_by"],
        "createdAt": row["created_at"].isoformat() if row["created_at"] else None,
        "finishedAt": row["finished_at"].isoformat() if row["finished_at"] else None,
    }


class EvalRepository:
    """asyncpg persistence for golden reads + eval_runs writes (Drizzle owns DDL)."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def _connect(self) -> asyncpg.Connection:
        return await with_hard_timeout(
            asyncpg.connect(dsn=self._settings.database_url),
            seconds=self._settings.db_timeout_seconds,
            operation="postgres.connect",
        )

    async def fetch_active_golden_items(self) -> list[dict[str, Any]]:
        conn = await self._connect()
        try:
            rows = await with_hard_timeout(
                conn.fetch(_ACTIVE_GOLDEN_SQL),
                seconds=self._settings.db_timeout_seconds,
                operation="eval.fetch_golden",
            )
            all_hashes: set[str] = set()
            items = []
            for row in rows:
                hashes = row["expected_chunk_hashes"]
                hashes = json.loads(hashes) if isinstance(hashes, str) else hashes
                all_hashes.update(hashes)
                items.append(
                    {
                        "id": str(row["id"]),
                        "question": row["question"],
                        "idealAnswer": row["ideal_answer"],
                        "expectedChunkHashes": hashes,
                    }
                )
            live_rows = await with_hard_timeout(
                conn.fetch(_LIVE_HASHES_SQL, list(all_hashes)),
                seconds=self._settings.db_timeout_seconds,
                operation="eval.fetch_live_hashes",
            )
            live = {row["content_hash"] for row in live_rows}
            for item in items:
                item["staleHashes"] = [
                    h for h in item["expectedChunkHashes"] if h not in live
                ]
        finally:
            await conn.close()
        return items

    async def create_run(
        self, *, kind: str, git_sha: str, config: dict[str, Any], triggered_by: str
    ) -> str:
        conn = await self._connect()
        try:
            row = await with_hard_timeout(
                conn.fetchrow(_CREATE_RUN_SQL, kind, git_sha, json.dumps(config), triggered_by),
                seconds=self._settings.db_timeout_seconds,
                operation="eval.create_run",
            )
        finally:
            await conn.close()
        return str(row["id"])

    async def finish_run(
        self,
        run_id: str,
        *,
        status: str,
        metrics: dict[str, Any] | None,
        per_item: list[dict[str, Any]] | None,
        error: str | None,
    ) -> None:
        conn = await self._connect()
        try:
            await with_hard_timeout(
                conn.execute(
                    _FINISH_RUN_SQL,
                    run_id,
                    status,
                    json.dumps(metrics) if metrics is not None else None,
                    json.dumps(per_item) if per_item is not None else None,
                    error,
                ),
                seconds=self._settings.db_timeout_seconds,
                operation="eval.finish_run",
            )
        finally:
            await conn.close()

    async def fetch_run(self, run_id: str) -> dict[str, Any] | None:
        conn = await self._connect()
        try:
            row = await with_hard_timeout(
                conn.fetchrow(_RUN_SQL, run_id),
                seconds=self._settings.db_timeout_seconds,
                operation="eval.fetch_run",
            )
        finally:
            await conn.close()
        return _row_to_run(row) if row else None


class SupportsRetrieve(Protocol):
    async def retrieve(
        self,
        query: str,
        *,
        k: int | None = None,
        vector_weight: float | None = None,
        keyword_weight: float | None = None,
        rerank: bool | None = None,
    ) -> list[dict[str, Any]]: ...


async def run_retrieval_eval(
    *,
    settings: Settings,
    retrieval: SupportsRetrieve,
    repository: EvalRepository,
    triggered_by: str,
    k: int | None = None,
    vector_weight: float | None = None,
    keyword_weight: float | None = None,
    rerank: bool | None = None,
) -> dict[str, Any]:
    """Execute one retrieval eval run and persist it. Returns the run row.

    Raises EvalRunError (fail loud, AC) BEFORE creating a run when the golden
    set is empty; a fully-stale set is recorded as a FAILED run."""
    items = await repository.fetch_active_golden_items()
    if not items:
        raise EvalRunError("Golden set is empty — nothing to evaluate")

    # RT-3 A/B: rerank=None honors config; True demands a configured reranker
    # (fail loud — a run claiming rerank-on while silently skipping it lies).
    effective_rerank = settings.rerank_enabled if rerank is None else rerank
    if effective_rerank and not settings.rerank_enabled:
        raise EvalRunError(
            "rerank=true requested but RERANK_ENABLED is off — configure the reranker first"
        )

    effective_k = k if k is not None else settings.retrieval_top_k
    # Exact snapshot of what this run evaluated (AC: parameterized toggles).
    config = {
        "k": effective_k,
        "vectorWeight": vector_weight
        if vector_weight is not None
        else settings.retrieval_vector_weight,
        "keywordWeight": keyword_weight
        if keyword_weight is not None
        else settings.retrieval_keyword_weight,
        "candidatePool": settings.retrieval_candidate_pool,
        "efSearch": settings.hnsw_ef_search,
        # Exact rerank stage snapshot (RT-3): what ran, with which knobs.
        "rerank": effective_rerank,
        "rerankProvider": settings.rerank_provider if effective_rerank else None,
        "rerankModel": settings.rerank_model if effective_rerank else None,
        "rerankWindow": settings.rerank_window if effective_rerank else None,
        # RT-5: deployment-level toggles snapshotted per run.
        "multiQuery": settings.multi_query_enabled,
        "multiQueryCount": settings.multi_query_count if settings.multi_query_enabled else None,
        "mmr": settings.mmr_enabled,
        "mmrLambda": settings.mmr_lambda if settings.mmr_enabled else None,
    }

    run_id = await repository.create_run(
        kind="retrieval",
        git_sha=settings.git_sha,
        config=config,
        triggered_by=triggered_by,
    )

    per_item: list[dict[str, Any]] = []
    scores: list[dict[str, float]] = []
    try:
        for item in items:
            if item["staleHashes"]:
                # Stale items are skipped and visible, never silently scored.
                per_item.append(
                    {
                        "goldenItemId": item["id"],
                        "question": item["question"],
                        "skipped": True,
                        "staleHashes": item["staleHashes"],
                    }
                )
                continue
            retrieved = await retrieval.retrieve(
                item["question"],
                k=effective_k,
                vector_weight=vector_weight,
                keyword_weight=keyword_weight,
                rerank=effective_rerank,
            )
            retrieved_hashes = [chunk["content_hash"] for chunk in retrieved]
            item_scores = score_item(item["expectedChunkHashes"], retrieved_hashes, effective_k)
            scores.append(item_scores)
            per_item.append(
                {
                    "goldenItemId": item["id"],
                    "question": item["question"],
                    "skipped": False,
                    "retrievedHashes": retrieved_hashes,
                    **item_scores,
                }
            )

        skipped = sum(1 for entry in per_item if entry["skipped"])
        if not scores:
            # Fail loud: every active item was stale.
            message = (
                f"All {skipped} active golden item(s) are stale (expected chunk "
                "hashes orphaned by re-chunking) — nothing evaluable"
            )
            await repository.finish_run(
                run_id, status="failed", metrics=None, per_item=per_item, error=message
            )
            raise EvalRunError(message)

        metrics = {
            **aggregate_metrics(scores),
            "itemCount": float(len(scores)),
            "skippedStaleCount": float(skipped),
        }
        await repository.finish_run(
            run_id, status="completed", metrics=metrics, per_item=per_item, error=None
        )
    except EvalRunError:
        raise
    except Exception as exc:
        await repository.finish_run(
            run_id, status="failed", metrics=None, per_item=per_item, error=str(exc)
        )
        raise

    run = await repository.fetch_run(run_id)
    assert run is not None
    logger.info(
        "eval.retrieval.completed",
        extra={"runId": run_id, "metrics": metrics, "gitSha": settings.git_sha},
    )
    return run


# ---------------------------------------------------------------- HTTP surface
router = APIRouter(prefix="/internal", dependencies=[Depends(verify_service_jwt)])


def get_eval_repository(request: Request) -> EvalRepository:
    """Dependency seam so tests can override persistence."""
    return EvalRepository(request.app.state.settings)


class RetrievalEvalRequest(BaseModel):
    k: int | None = Field(default=None, ge=1, le=50)
    vector_weight: float | None = Field(default=None, ge=0, alias="vectorWeight")
    keyword_weight: float | None = Field(default=None, ge=0, alias="keywordWeight")
    rerank: bool | None = Field(default=None)

    model_config = {"populate_by_name": True}


@router.post("/eval/retrieval")
async def execute_retrieval_eval(
    payload: RetrievalEvalRequest,
    request: Request,
    principal: Annotated[ServicePrincipal, Depends(verify_service_jwt)],
    retrieval: Annotated[RetrievalService, Depends(get_retrieval_service)],
    repository: Annotated[EvalRepository, Depends(get_eval_repository)],
) -> dict[str, Any]:
    try:
        return await run_retrieval_eval(
            settings=request.app.state.settings,
            retrieval=retrieval,
            repository=repository,
            triggered_by=principal.sub,
            k=payload.k,
            vector_weight=payload.vector_weight,
            keyword_weight=payload.keyword_weight,
            rerank=payload.rerank,
        )
    except EvalRunError as exc:
        # Fail loud (AC): empty/fully-stale golden set is a client-visible 409.
        raise HTTPException(status_code=409, detail=str(exc)) from exc
