"""Agentic self-correcting retrieval (CE-3, Flow H — RAG_MODE=agentic).

A LangGraph decision loop replaces the single-shot retrieve inside Flow B's
cache-miss branch: retrieve → grade relevance (cheap model, structured
verdict) → if insufficient, rewrite the query and retrieve again — hard-capped
at MAX_REWRITES. Everything else in Flow B (security, cache, budget, routing,
validation) is untouched, and the generation prompt always uses the ORIGINAL
question; rewrites only diversify retrieval.

Failure contract (Flow H): any grading or rewrite failure falls through to
the linear behavior over the current retrieval set — the user gets a
baseline-quality answer, never an error. The loop can only ever ADD retrieval
attempts, never lose the ones already made.
"""

import json
import logging
import time
from typing import Any, Protocol, TypedDict

from prometheus_client import Counter, Histogram

from app.config import Settings
from app.tracing import stage

logger = logging.getLogger(__name__)

# Hard cap on query rewrites per request (Flow H) — a bounded loop is the
# contract; anything unbounded is a cost/latency bomb.
MAX_REWRITES = 2

AGENTIC_HOPS = Histogram(
    "rag_agentic_hops",
    "Retrieval attempts per agentic request (1 = no rewrite)",
    buckets=(1, 2, 3),
)
AGENTIC_EVENTS = Counter(
    "rag_agentic_events_total",
    "Agentic loop events (CE-3)",
    ["event"],  # sufficient | insufficient | rewrite | grade_failed | rewrite_failed
)

_GRADE_SYSTEM_PROMPT = (
    "You grade whether retrieved context can answer a question. Reply with "
    'ONLY a JSON object: {"sufficient": true/false, "reason": "<short>"}. '
    "sufficient=true when at least one block contains the information needed "
    "to answer the question."
)

_REWRITE_SYSTEM_PROMPT = (
    "A document search failed to find useful passages for the user's "
    "question. Rewrite the question once, using different words likely to "
    "match how documents phrase it. Reply with ONLY the rewritten question."
)

# CE-4 groundedness gate (Flow H second half).
_GROUNDED_SYSTEM_PROMPT = (
    "You verify that an answer is fully supported by its context. Reply with "
    'ONLY a JSON object: {"grounded": true/false, "reason": "<short>"}. '
    "grounded=true only when every factual claim in the answer appears in "
    "the numbered context blocks."
)

# Preview length per chunk in the grading prompt — enough to judge topical
# relevance without spending answer-sized context on the grader.
_GRADE_PREVIEW_CHARS = 300


class SupportsComplete(Protocol):
    model: str

    async def complete(
        self, messages: list[dict[str, str]]
    ) -> tuple[str, dict[str, int] | None]: ...


class SupportsRetrieve(Protocol):
    async def retrieve(
        self, query: str, *, k: int | None = None, filters: Any = None
    ) -> list[dict[str, Any]]: ...


class LoopState(TypedDict, total=False):
    """LangGraph state channels — partial node returns merge per key."""

    query: str
    original: str
    k: int | None
    filters: Any
    chunks: list[dict[str, Any]]
    hops: list[dict[str, Any]]
    tried: list[str]
    rewrites: int
    sufficient: bool
    rewrite_failed: bool


def _parse_json_flag(text: str, key: str) -> dict[str, Any] | None:
    """Extract a JSON verdict whose `key` is a bool; None when unparseable
    (both graders fail open — Flow H)."""
    try:
        start = text.index("{")
        end = text.rindex("}") + 1
        verdict = json.loads(text[start:end])
    except (ValueError, json.JSONDecodeError):
        return None
    if not isinstance(verdict, dict) or not isinstance(verdict.get(key), bool):
        return None
    return verdict


def _parse_verdict(text: str) -> dict[str, Any] | None:
    return _parse_json_flag(text, "sufficient")


class AgenticRetriever:
    """The CE-3 loop behind the GenerationService retrieval seam."""

    def __init__(
        self,
        settings: Settings,
        retrieval: SupportsRetrieve,
        client: SupportsComplete,
    ) -> None:
        self._settings = settings
        self._retrieval = retrieval
        self._client = client
        self._graph = self._build_graph()
        # CE-4: per-request status callback (instances are per-request — the
        # generation seam builds one AgenticRetriever per chat call).
        self._on_status: Any = None

    async def _emit(self, stage_name: str, detail: dict[str, Any] | None = None) -> None:
        """CE-4: loop-status event for the SSE stream; emission failures never
        touch the loop itself."""
        if self._on_status is None:
            return
        try:
            await self._on_status(stage_name, detail or {})
        except Exception:
            logger.warning("agentic.status_emit_failed", exc_info=True)

    # ------------------------------------------------------------- nodes
    async def _node_retrieve(self, state: dict[str, Any]) -> dict[str, Any]:
        started = time.perf_counter()
        await self._emit(
            "retrieving",
            {"rewritten": state["query"] != state["original"]},
        )
        chunks = await self._retrieval.retrieve(
            state["query"], k=state["k"], filters=state["filters"]
        )
        hop = {
            "query": state["query"],
            "rewritten": state["query"] != state["original"],
            "chunkCount": len(chunks),
            "durationMs": round((time.perf_counter() - started) * 1000, 3),
        }
        return {"chunks": chunks, "hops": [*state["hops"], hop]}

    async def _node_grade(self, state: dict[str, Any]) -> dict[str, Any]:
        """Structured relevance verdict; grader failure = sufficient (Flow H
        fall-through: degrade to the linear path over the current set)."""
        started = time.perf_counter()
        await self._emit("grading", {})
        previews = "\n".join(
            f"[{index + 1}] {chunk['content'][:_GRADE_PREVIEW_CHARS]}"
            for index, chunk in enumerate(state["chunks"])
        ) or "(no chunks retrieved)"
        try:
            text, _ = await self._client.complete(
                [
                    {"role": "system", "content": _GRADE_SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": (
                            f"Question: {state['original']}\n\nContext blocks:\n{previews}"
                        ),
                    },
                ]
            )
            verdict = _parse_verdict(text)
        except Exception:
            logger.warning("agentic.grade_failed", exc_info=True)
            verdict = None
        if verdict is None:
            AGENTIC_EVENTS.labels(event="grade_failed").inc()
            verdict = {"sufficient": True, "reason": "grader unavailable (fail-open)"}
        else:
            AGENTIC_EVENTS.labels(
                event="sufficient" if verdict["sufficient"] else "insufficient"
            ).inc()
        hops = list(state["hops"])
        hops[-1] = {
            **hops[-1],
            "verdict": verdict,
            "gradeDurationMs": round((time.perf_counter() - started) * 1000, 3),
        }
        return {"sufficient": verdict["sufficient"], "hops": hops}

    async def _node_rewrite(self, state: dict[str, Any]) -> dict[str, Any]:
        """One query rewrite; failure or a non-novel rewrite ends the loop
        with the current chunks (Flow H — never an error)."""
        await self._emit("rewriting", {})
        graded_out = "\n".join(
            f"- {chunk['content'][:120]}" for chunk in state["chunks"][:5]
        ) or "(nothing retrieved)"
        try:
            text, _ = await self._client.complete(
                [
                    {"role": "system", "content": _REWRITE_SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": (
                            f"Question: {state['original']}\n\n"
                            f"Passages judged irrelevant:\n{graded_out}\n\n"
                            f"Queries already tried:\n"
                            + "\n".join(f"- {q}" for q in state["tried"])
                        ),
                    },
                ]
            )
            rewritten = text.strip().strip('"').strip()
        except Exception:
            logger.warning("agentic.rewrite_failed", exc_info=True)
            AGENTIC_EVENTS.labels(event="rewrite_failed").inc()
            return {"rewrite_failed": True}
        if not rewritten or rewritten.lower() in {q.lower() for q in state["tried"]}:
            AGENTIC_EVENTS.labels(event="rewrite_failed").inc()
            return {"rewrite_failed": True}
        AGENTIC_EVENTS.labels(event="rewrite").inc()
        return {
            "query": rewritten,
            "rewrites": state["rewrites"] + 1,
            "tried": [*state["tried"], rewritten],
        }

    # ------------------------------------------------------------- edges
    def _after_grade(self, state: dict[str, Any]) -> str:
        if state["sufficient"] or state["rewrites"] >= MAX_REWRITES:
            return "done"
        return "rewrite"

    def _after_rewrite(self, state: dict[str, Any]) -> str:
        return "done" if state.get("rewrite_failed") else "retrieve"

    def _build_graph(self) -> Any:
        from langgraph.graph import END, StateGraph

        graph = StateGraph(LoopState)
        graph.add_node("retrieve", self._node_retrieve)
        graph.add_node("grade", self._node_grade)
        graph.add_node("rewrite", self._node_rewrite)
        graph.set_entry_point("retrieve")
        graph.add_edge("retrieve", "grade")
        graph.add_conditional_edges(
            "grade", self._after_grade, {"done": END, "rewrite": "rewrite"}
        )
        graph.add_conditional_edges(
            "rewrite", self._after_rewrite, {"done": END, "retrieve": "retrieve"}
        )
        return graph.compile()

    # --------------------------------------------------------------- API
    async def retrieve(
        self,
        query: str,
        *,
        k: int | None = None,
        filters: Any = None,
        on_status: Any = None,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Run the loop; returns (chunks, loop trace). The trace records each
        hop's query text, verdict, and timing (AC) and lands on the request
        span + audit record. `on_status` (CE-4) receives (stage, detail)
        per loop step for SSE progress events."""
        self._on_status = on_status
        async with stage("agentic.loop") as record:
            state = await self._graph.ainvoke(
                {
                    "query": query,
                    "original": query,
                    "k": k,
                    "filters": filters,
                    "chunks": [],
                    "hops": [],
                    "tried": [query],
                    "rewrites": 0,
                    "sufficient": False,
                }
            )
            trace = {
                "hops": state["hops"],
                "rewrites": state["rewrites"],
                "sufficient": state["sufficient"],
            }
            record(
                hopCount=len(state["hops"]),
                rewrites=state["rewrites"],
                sufficient=state["sufficient"],
            )
        AGENTIC_HOPS.observe(len(state["hops"]))
        logger.info("agentic.loop %s", json.dumps(trace))
        return state["chunks"], trace

    async def groundedness_verdict(
        self,
        *,
        question: str,
        answer: str,
        chunks: list[dict[str, Any]],
    ) -> bool | None:
        """CE-4 groundedness check over the draft + USED chunks. Returns
        None when the checker itself fails or replies unparseably — the
        caller ships the draft with a trace marker (fail-open, Flow H: the
        checker is an enhancer, never an availability gate)."""
        blocks = "\n".join(
            f"[{index + 1}] {chunk['content'][:_GRADE_PREVIEW_CHARS * 2]}"
            for index, chunk in enumerate(chunks)
        )
        try:
            text, _ = await self._client.complete(
                [
                    {"role": "system", "content": _GROUNDED_SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": (
                            f"Question: {question}\n\nContext blocks:\n{blocks}\n\n"
                            f"Answer to verify:\n{answer}"
                        ),
                    },
                ]
            )
        except Exception:
            logger.warning("agentic.groundedness_check_failed", exc_info=True)
            AGENTIC_EVENTS.labels(event="groundedness_check_failed").inc()
            return None
        verdict = _parse_json_flag(text, "grounded")
        if verdict is None:
            AGENTIC_EVENTS.labels(event="groundedness_check_failed").inc()
            return None
        AGENTIC_EVENTS.labels(
            event="grounded" if verdict["grounded"] else "ungrounded"
        ).inc()
        return verdict["grounded"]


def build_agentic_client(settings: Settings, bucket: Any = None) -> SupportsComplete:
    """Cheap-fast grader/rewriter client, CP-4 bucket-gated like every other
    provider call."""
    from app.providers import OpenAIChatClient

    client: Any = OpenAIChatClient(settings)
    if bucket is not None:
        from app.provider_bucket import ThrottledChatClient

        client = ThrottledChatClient(client, bucket)
    return client
