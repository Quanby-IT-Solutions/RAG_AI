"""Hard per-request context token budget (Flow B step 5, GN-2).

Rank-then-select is contractual (§5.3): candidates arrive ALREADY ranked
(RT-2 fused order), and chunks are added whole, in rank order, until the next
chunk would push the running total past the budget — never truncated
mid-chunk, never exceeding the budget. Selection stops at the first overflow
so the budget is always spent on the best-ranked evidence.

The budget itself is read per request (environment override first, settings
default second) so a deployment-level change applies on the next request —
no restart required (ticket AC).

Token counts use tiktoken's o200k_base encoding (the GPT-4o family). An
unknown/failed encoding falls back to a conservative chars/4 estimate rather
than failing the request.
"""

import logging
import os
from typing import Any

from app.config import Settings

logger = logging.getLogger(__name__)

CONTEXT_TOKEN_BUDGET_ENV = "CONTEXT_TOKEN_BUDGET"

_encoder: Any | None = None


def _encoding() -> Any | None:
    global _encoder
    if _encoder is None:
        try:
            import tiktoken

            _encoder = tiktoken.get_encoding("o200k_base")
        except Exception as exc:  # missing data files, etc.
            logger.warning("tiktoken unavailable (%s); falling back to chars/4", exc)
            _encoder = False
    return _encoder or None


def count_tokens(text: str) -> int:
    encoder = _encoding()
    if encoder is None:
        return max(1, len(text) // 4)
    return len(encoder.encode(text))


def current_context_budget(settings: Settings) -> int:
    """Per-request budget resolution: env override -> settings default.

    Reading the environment on every request is what makes a budget change
    apply to the NEXT request without a restart."""
    raw = os.environ.get(CONTEXT_TOKEN_BUDGET_ENV)
    if raw is not None:
        try:
            value = int(raw)
            if value >= 1:
                return value
        except ValueError:
            pass
        logger.warning("Ignoring invalid %s=%r", CONTEXT_TOKEN_BUDGET_ENV, raw)
    return settings.context_token_budget


def select_within_budget(
    chunks: list[dict[str, Any]], *, budget: int
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Rank-order selection up to the hard budget.

    Returns (used chunks, selection record). Selection stops at the FIRST
    chunk that would exceed the budget (rank order is the priority order —
    a later smaller chunk never jumps the queue). A first-ranked chunk larger
    than the entire budget selects nothing (the caller answers grounded
    "I don't know" rather than blowing the cap)."""
    used: list[dict[str, Any]] = []
    total = 0
    for chunk in chunks:
        tokens = count_tokens(chunk["content"])
        if total + tokens > budget:
            break
        used.append({**chunk, "context_tokens": tokens})
        total += tokens
    record = {
        "budget": budget,
        "contextTokens": total,
        "retrievedCount": len(chunks),
        "usedCount": len(used),
    }
    return used, record
