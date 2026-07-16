"""CE-9 profile evaluation matrix.

Runs the EV-2/EV-3 harness once per RAG_PROFILE (baseline / contextual /
agentic / full) against the live corpus and prints a markdown matrix of
retrieval metrics, generation metrics, per-item retrieval latency (p50/p95),
and eval LLM spend. Every leg is a STORED eval run (kind=generation) whose
config carries the profile identity, so each profile's gate baseline can be
promoted from its own leg.

Honesty notes (also printed):
- The harness exercises retrieval + the shared grounded generation path.
  The agentic loop and the vision answer path live in the CHAT pipeline —
  their latency/quality effects are measured live in
  docs/CONTEXTUAL-RETRIEVAL-RESULTS.md, not here.
- Profiles that change the INDEX (contextual/multimodal) assume the corpus
  was already rolled out per the RUNNING.md runbook; this script never
  re-indexes.

Usage: uv run python -m app.profile_matrix   (requires NEO4J_PASSWORD for
the `full` leg; that leg degrades to hybrid-only if Neo4j is down — flagged
in the output.)
"""

import asyncio
import json
import statistics
import time
from typing import Any

PROFILES = ("baseline", "contextual", "agentic", "full")


class TimedRetrieval:
    """RetrievalService wrapper recording per-call wall time."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.timings_ms: list[float] = []

    async def retrieve(self, query: str, **kwargs: Any) -> Any:
        started = time.perf_counter()
        try:
            return await self._inner.retrieve(query, **kwargs)
        finally:
            self.timings_ms.append((time.perf_counter() - started) * 1000)


async def _run_leg(profile: str) -> dict[str, Any]:
    import os

    from app.chunks import ChunksRepository
    from app.config import Settings
    from app.embeddings import EmbeddingsClient
    from app.evaluation import EvalRepository, run_retrieval_eval
    from app.providers import OpenAIChatClient
    from app.reembed import get_active_embedding
    from app.retrieval import RetrievalService

    overrides = {"RAG_PROFILE": profile}
    for key in (
        "CONTEXTUAL_CHUNKS_ENABLED",
        "RAG_MODE",
        "GRAPH_RAG_ENABLED",
        "MULTIMODAL_ENABLED",
    ):
        os.environ.pop(key, None)
    os.environ["RAG_PROFILE"] = profile
    settings = Settings(**overrides)

    async def resolve_active() -> dict[str, Any]:
        import redis.asyncio as aioredis

        client = aioredis.from_url(settings.redis_url, decode_responses=True)
        try:
            return await get_active_embedding(settings, client)
        finally:
            await client.aclose()

    graph_retriever = None
    graph_note = ""
    if settings.graph_rag_enabled:
        from app.graph import GraphRepository

        graph_retriever = GraphRepository(settings)
        try:
            await graph_retriever.verify_connectivity()
        except Exception:
            graph_note = " (graph store unreachable — leg ran hybrid-only)"

    retrieval = TimedRetrieval(
        RetrievalService(
            settings=settings,
            embeddings=EmbeddingsClient(settings),
            repository=ChunksRepository(settings),
            active_embedding_resolver=resolve_active,
            graph_retriever=graph_retriever,
        )
    )
    run = await run_retrieval_eval(
        settings=settings,
        retrieval=retrieval,
        repository=EvalRepository(settings),
        triggered_by=f"profile-matrix:{profile}",
        generation=True,
        chat_client=OpenAIChatClient(settings),
    )
    if graph_retriever is not None:
        await graph_retriever.close()

    timings = sorted(retrieval.timings_ms)

    def percentile(p: float) -> float:
        if not timings:
            return 0.0
        index = min(len(timings) - 1, max(0, round(p * (len(timings) - 1))))
        return timings[index]

    metrics = run["metrics"] or {}
    return {
        "profile": profile + graph_note,
        "runId": run["id"],
        "hitRate": metrics.get("hitRate"),
        "mrr": metrics.get("mrr"),
        "faithfulness": metrics.get("faithfulness"),
        "answerRelevancy": metrics.get("answerRelevancy"),
        "retrievalP50Ms": round(statistics.median(timings), 1) if timings else None,
        "retrievalP95Ms": round(percentile(0.95), 1) if timings else None,
        "evalPromptTokens": metrics.get("evalPromptTokens"),
        "evalCompletionTokens": metrics.get("evalCompletionTokens"),
    }


async def _main() -> int:
    rows = []
    for profile in PROFILES:
        print(f"-- running {profile} leg...", flush=True)
        rows.append(await _run_leg(profile))

    header = (
        "| Profile | Hit rate | MRR | Faithfulness | Relevancy | "
        "Retrieval p50 | Retrieval p95 | Eval tokens (in/out) | Run |"
    )
    print("\n" + header)
    print("|" + "---|" * 9)
    for row in rows:
        print(
            f"| {row['profile']} | {row['hitRate']:.3f} | {row['mrr']:.3f} | "
            f"{row['faithfulness']:.3f} | {row['answerRelevancy']:.3f} | "
            f"{row['retrievalP50Ms']} ms | {row['retrievalP95Ms']} ms | "
            f"{int(row['evalPromptTokens'])}/{int(row['evalCompletionTokens'])} | "
            f"`{row['runId'][:8]}` |"
        )
    print()
    print(json.dumps(rows, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
