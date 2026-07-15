"""CI evaluation regression gate (EV-4, Flow F).

Compares a candidate eval run against the CURRENT approved baseline (newest
`eval_baselines` row): every metric shared by both runs must be at least
`baseline − tolerance` or the gate FAILS with a per-metric diff report and
deployment is blocked. The baseline NEVER updates implicitly — promotion is
an explicit, audited console/API action by a holder of `eval.gate.approve`.

CLI (invoked by CI): `python -m app.eval_gate [--generation] [--k N]`
  exit 0 — gate passed (run stored for RP-3 trending)
  exit 1 — regression: at least one metric below baseline − tolerance
  exit 2 — not gateable (no approved baseline yet, or empty golden set)
"""

import json
import logging
from typing import Any

import asyncpg

from app.config import Settings
from app.timeouts import with_hard_timeout

logger = logging.getLogger(__name__)

# Higher-is-better metrics the gate compares when present in BOTH runs.
GATED_METRICS = (
    "hitRate",
    "mrr",
    "precisionAtK",
    "recallAtK",
    "faithfulness",
    "answerRelevancy",
)

_BASELINE_SQL = """
SELECT b.run_id, b.created_at AS promoted_at, b.promoted_by,
       r.metrics, r.git_sha
FROM eval_baselines b
JOIN eval_runs r ON r.id = b.run_id
ORDER BY b.created_at DESC
LIMIT 1
"""


async def fetch_current_baseline(settings: Settings) -> dict[str, Any] | None:
    conn = await with_hard_timeout(
        asyncpg.connect(dsn=settings.database_url),
        seconds=settings.db_timeout_seconds,
        operation="postgres.connect",
    )
    try:
        row = await with_hard_timeout(
            conn.fetchrow(_BASELINE_SQL),
            seconds=settings.db_timeout_seconds,
            operation="eval_gate.fetch_baseline",
        )
    finally:
        await conn.close()
    if row is None:
        return None
    metrics = row["metrics"]
    return {
        "runId": str(row["run_id"]),
        "promotedAt": row["promoted_at"].isoformat(),
        "promotedBy": row["promoted_by"],
        "gitSha": row["git_sha"],
        "metrics": json.loads(metrics) if isinstance(metrics, str) else (metrics or {}),
    }


def evaluate_gate(
    *,
    candidate_metrics: dict[str, float],
    baseline_metrics: dict[str, float],
    tolerance: float,
) -> dict[str, Any]:
    """Per-metric comparison: candidate must be >= baseline − tolerance for
    every gated metric present in BOTH runs. Returns the diff report."""
    report: list[dict[str, Any]] = []
    failed = False
    for metric in GATED_METRICS:
        baseline_value = baseline_metrics.get(metric)
        candidate_value = candidate_metrics.get(metric)
        if baseline_value is None or candidate_value is None:
            continue
        floor = baseline_value - tolerance
        ok = candidate_value >= floor
        failed = failed or not ok
        report.append(
            {
                "metric": metric,
                "baseline": baseline_value,
                "candidate": candidate_value,
                "delta": candidate_value - baseline_value,
                "floor": floor,
                "status": "pass" if ok else "FAIL",
            }
        )
    return {"passed": not failed and bool(report), "comparisons": report}


async def _main() -> int:
    import argparse

    from app.chunks import ChunksRepository
    from app.config import get_settings
    from app.embeddings import EmbeddingsClient
    from app.evaluation import EvalRepository, EvalRunError, run_retrieval_eval
    from app.providers import OpenAIChatClient
    from app.retrieval import RetrievalService

    parser = argparse.ArgumentParser(description="EV-4 regression gate (Flow F)")
    parser.add_argument("--k", type=int, default=None)
    parser.add_argument("--generation", action="store_true", help="include EV-3 metrics")
    args = parser.parse_args()

    settings = get_settings()
    baseline = await fetch_current_baseline(settings)
    if baseline is None:
        print(
            "GATE NOT EVALUABLE: no approved baseline. Promote a run via the "
            "evaluation console (requires eval.gate.approve) to arm the gate."
        )
        return 2

    retrieval = RetrievalService(
        settings=settings,
        embeddings=EmbeddingsClient(settings),
        repository=ChunksRepository(settings),
    )
    try:
        run = await run_retrieval_eval(
            settings=settings,
            retrieval=retrieval,
            repository=EvalRepository(settings),
            triggered_by="ci-gate",
            k=args.k,
            generation=args.generation,
            chat_client=OpenAIChatClient(settings) if args.generation else None,
        )
    except EvalRunError as exc:
        print(f"GATE NOT EVALUABLE: {exc}")
        return 2

    result = evaluate_gate(
        candidate_metrics=run["metrics"] or {},
        baseline_metrics=baseline["metrics"],
        tolerance=settings.eval_gate_tolerance,
    )
    report = {
        "gate": "pass" if result["passed"] else "FAIL",
        "tolerance": settings.eval_gate_tolerance,
        "candidateRunId": run["id"],
        "candidateGitSha": run["gitSha"],
        "baselineRunId": baseline["runId"],
        "baselineGitSha": baseline["gitSha"],
        "comparisons": result["comparisons"],
    }
    print(json.dumps(report, indent=2))
    if not result["passed"]:
        print("GATE FAILED: metric(s) below baseline - tolerance. Deployment blocked.")
        return 1
    print("Gate passed; the candidate run is stored for trending (RP-3).")
    return 0


if __name__ == "__main__":
    import asyncio

    raise SystemExit(asyncio.run(_main()))
