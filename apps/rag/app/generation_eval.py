"""Generation metrics — faithfulness and answer relevancy (EV-3, §5.7).

RAGAS-style evaluators implemented against OUR provider-client interface
(technical note: evaluator calls go through the same seam CP-4/GN-5 wrap),
so they inherit the hard timeout today and the shared throttle when CP-4
lands. Answers for eval are generated OUTSIDE the user chat path — nothing
is persisted to conversations/messages, and every evaluator/generator token
is tallied as EVAL SPEND (logged + aggregated per run), never user spend.

Metrics:
- faithfulness: the evaluator lists each factual claim in the answer and
  labels it SUPPORTED/UNSUPPORTED strictly by the provided context;
  faithfulness = supported / total (an answer inventing claims absent from
  its chunks scores lower — seeded negative case in tests/live checks).
- answerRelevancy: the evaluator scores 0..1 how directly the answer
  addresses the question (ignoring correctness).
"""

import json
import logging
import re
from typing import Any, Protocol

from app.generation import _format_context, build_grounded_prompt

logger = logging.getLogger(__name__)

_FAITHFULNESS_PROMPT = (
    "You are a strict evaluator. Given a CONTEXT and an ANSWER, list every "
    "factual claim the answer makes and label each SUPPORTED or UNSUPPORTED "
    "based ONLY on the context. Respond with JSON: "
    '{"claims": [{"claim": "...", "supported": true|false}]}. No other output.'
)

_RELEVANCY_PROMPT = (
    "You are a strict evaluator. Score from 0.0 to 1.0 how directly the "
    "ANSWER addresses the QUESTION (1.0 = fully addresses it, 0.0 = "
    "unrelated or a refusal). Judge focus, not factual correctness. Respond "
    'with JSON: {"score": <number>}. No other output.'
)


class SupportsComplete(Protocol):
    model: str

    async def complete(
        self, messages: list[dict[str, str]]
    ) -> tuple[str, dict[str, int] | None]: ...


class EvalSpend:
    """Token tally for evaluator/generator calls — EVAL spend, not user spend."""

    def __init__(self) -> None:
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.calls = 0

    def add(self, usage: dict[str, int] | None) -> None:
        self.calls += 1
        if usage:
            self.prompt_tokens += usage.get("promptTokens", 0)
            self.completion_tokens += usage.get("completionTokens", 0)

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


def _parse_json(text: str) -> Any:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned)
    return json.loads(cleaned)


async def generate_answer(
    *,
    client: SupportsComplete,
    question: str,
    chunks: list[dict[str, Any]],
    spend: EvalSpend,
) -> str:
    """Grounded answer for eval scoring via the SAME prompt as Flow B, but
    never persisted (no conversation/message rows — eval spend only)."""
    prompt = build_grounded_prompt().format_messages(
        context=_format_context(chunks), question=question
    )
    messages = [
        {"role": "system" if m.type == "system" else "user", "content": m.content}
        for m in prompt
    ]
    answer, usage = await client.complete(messages)
    spend.add(usage)
    return answer


async def evaluate_faithfulness(
    *,
    client: SupportsComplete,
    answer: str,
    contexts: list[str],
    spend: EvalSpend,
) -> float | None:
    """supported claims / total claims; None when unscorable (no claims or
    evaluator failure — recorded per item, never silently perfect)."""
    try:
        text, usage = await client.complete(
            [
                {"role": "system", "content": _FAITHFULNESS_PROMPT},
                {
                    "role": "user",
                    "content": f"CONTEXT:\n{chr(10).join(contexts)}\n\nANSWER:\n{answer}",
                },
            ]
        )
        spend.add(usage)
        claims = _parse_json(text)["claims"]
    except Exception:
        logger.warning("eval.faithfulness_failed", exc_info=True)
        return None
    if not claims:
        return None
    supported = sum(1 for claim in claims if claim.get("supported") is True)
    return supported / len(claims)


async def evaluate_relevancy(
    *,
    client: SupportsComplete,
    question: str,
    answer: str,
    spend: EvalSpend,
) -> float | None:
    try:
        text, usage = await client.complete(
            [
                {"role": "system", "content": _RELEVANCY_PROMPT},
                {"role": "user", "content": f"QUESTION:\n{question}\n\nANSWER:\n{answer}"},
            ]
        )
        spend.add(usage)
        score = float(_parse_json(text)["score"])
    except Exception:
        logger.warning("eval.relevancy_failed", exc_info=True)
        return None
    return min(1.0, max(0.0, score))
