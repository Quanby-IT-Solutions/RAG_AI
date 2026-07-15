"""Multi-query expansion + MMR diversification (RT-5, Flow B optional stages).

Multi-query: the cheap-fast model rewrites the user's question into N
alternative phrasings; every variant retrieves through BOTH hybrid arms and
the union feeds one RRF fusion (dedup by chunk id happens inside RRF).
Expansion failure NEVER breaks retrieval — it degrades to the single
original query, logged.

MMR (Maximal Marginal Relevance): greedy re-selection over the fused (and
possibly reranked) pool balancing relevance against redundancy:
    score(c) = λ · rel(c) − (1 − λ) · max_{s ∈ selected} cos(c, s)
Relevance is rank-based (scale-free across rrf/rerank scores); diversity
uses the chunks' own stored embeddings.
"""

import logging
import math
from typing import Any, Protocol

logger = logging.getLogger(__name__)

_REWORDING_PROMPT = (
    "Rewrite the user's question into {n} alternative phrasings that could "
    "surface different relevant passages in a document search. Keep each "
    "rewording short, self-contained, and in the same language. Return ONLY "
    "the rewordings, one per line, no numbering."
)


class SupportsComplete(Protocol):
    async def complete(
        self, messages: list[dict[str, str]]
    ) -> tuple[str, dict[str, int] | None]: ...


async def generate_rewordings(
    query: str, *, client: SupportsComplete, count: int
) -> list[str]:
    """N alternative phrasings from the cheap-fast model; [] on ANY failure
    (multi-query degrades to the original query, never breaks retrieval)."""
    try:
        text, _ = await client.complete(
            [
                {"role": "system", "content": _REWORDING_PROMPT.format(n=count)},
                {"role": "user", "content": query},
            ]
        )
    except Exception:
        logger.warning("multi_query.expansion_failed", exc_info=True)
        return []
    rewordings = []
    for line in text.splitlines():
        cleaned = line.strip().lstrip("-•*0123456789. ").strip()
        if cleaned and cleaned.lower() != query.strip().lower():
            rewordings.append(cleaned)
    return rewordings[:count]


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def mmr_select(
    candidates: list[dict[str, Any]],
    *,
    embeddings_by_id: dict[str, list[float]],
    k: int,
    lambda_: float,
) -> list[dict[str, Any]]:
    """Greedy MMR over an already relevance-ordered candidate pool.

    Relevance is rank-based: rel_i = 1 − i/len(pool) (monotone with the
    incoming rrf/rerank order, scale-free). Candidates missing an embedding
    contribute zero similarity (they can't be judged redundant)."""
    if k <= 0 or not candidates:
        return []
    if len(candidates) <= k or lambda_ >= 1.0:
        return candidates[:k]

    pool = list(candidates)
    relevance = {id(c): 1.0 - index / len(pool) for index, c in enumerate(pool)}
    selected: list[dict[str, Any]] = []
    while pool and len(selected) < k:
        best = None
        best_score = -math.inf
        for candidate in pool:
            redundancy = 0.0
            embedding = embeddings_by_id.get(candidate["id"])
            if embedding is not None and selected:
                sims = [
                    _cosine(embedding, embeddings_by_id[s["id"]])
                    for s in selected
                    if s["id"] in embeddings_by_id
                ]
                redundancy = max(sims, default=0.0)
            score = lambda_ * relevance[id(candidate)] - (1.0 - lambda_) * redundancy
            if score > best_score:
                best = candidate
                best_score = score
        assert best is not None
        selected.append(best)
        pool.remove(best)
    return selected
