"""One-command retrieval eval run (EV-2 AC): `uv run python -m app.eval_cli`.

Runs the SAME harness the endpoint/console use (run_retrieval_eval against
the real RetrievalService) and prints the persisted run as JSON. Exit codes:
0 completed, 2 fail-loud (empty/fully-stale golden set), 1 anything else.
"""

import argparse
import asyncio
import json
import sys

from app.chunks import ChunksRepository
from app.config import get_settings
from app.embeddings import EmbeddingsClient
from app.evaluation import EvalRepository, EvalRunError, run_retrieval_eval
from app.retrieval import RetrievalService


async def _main() -> int:
    parser = argparse.ArgumentParser(description="Score retrieval against the golden set")
    parser.add_argument("--k", type=int, default=None, help="top-k override")
    parser.add_argument("--vector-weight", type=float, default=None)
    parser.add_argument("--keyword-weight", type=float, default=None)
    args = parser.parse_args()

    settings = get_settings()
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
            triggered_by="cli",
            k=args.k,
            vector_weight=args.vector_weight,
            keyword_weight=args.keyword_weight,
        )
    except EvalRunError as exc:
        print(f"eval run refused: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(run, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
