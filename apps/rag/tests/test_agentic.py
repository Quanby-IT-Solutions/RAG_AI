"""CE-3 agentic self-correcting retrieval tests (Flow H)."""

from typing import Any

import pytest

from app.agentic import MAX_REWRITES, AgenticRetriever, _parse_verdict
from app.generation import GenerationService
from tests.conftest import REQUIRED_ENV
from tests.test_generation import (
    PRINCIPAL,
    FakeChatClient,
    FakeConversationsRepository,
    FakeRetrievalService,
    make_chunk,
)


@pytest.fixture
def settings(monkeypatch):
    def build(**env: str):
        for key, value in REQUIRED_ENV.items():
            monkeypatch.setenv(key, value)
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        from app.config import Settings

        return Settings()

    return build


class ScriptedLoopClient:
    """Grader/rewriter stand-in: routes on the system prompt, replays scripts."""

    model = "gpt-4o-mini"
    provider = "openai"

    def __init__(
        self,
        grades: list[Any],
        rewrites: list[Any] | None = None,
    ) -> None:
        self.grades = list(grades)
        self.rewrites = list(rewrites or [])
        self.grade_calls: list[str] = []
        self.rewrite_calls: list[str] = []

    async def complete(self, messages):
        system = messages[0]["content"]
        user = messages[-1]["content"]
        if "grade whether retrieved context" in system:
            self.grade_calls.append(user)
            item = self.grades.pop(0)
        else:
            self.rewrite_calls.append(user)
            item = self.rewrites.pop(0)
        if isinstance(item, Exception):
            raise item
        return item, {"promptTokens": 50, "completionTokens": 10, "totalTokens": 60}


SUFFICIENT = '{"sufficient": true, "reason": "covers it"}'
INSUFFICIENT = '{"sufficient": false, "reason": "off-topic"}'


def make_loop(settings, *, chunks=None, grades, rewrites=None):
    retrieval = FakeRetrievalService(chunks or [make_chunk("refunds take 30 days.")])
    client = ScriptedLoopClient(grades, rewrites)
    loop = AgenticRetriever(settings(RAG_MODE="agentic"), retrieval, client)
    return loop, retrieval, client


# ------------------------------------------------------------ loop behavior
async def test_sufficient_first_pass_costs_one_retrieval_and_one_grade(settings):
    loop, retrieval, client = make_loop(settings, grades=[SUFFICIENT])

    chunks, trace = await loop.retrieve("refund policy?")

    assert len(retrieval.calls) == 1
    assert len(client.grade_calls) == 1
    assert client.rewrite_calls == []
    assert trace["rewrites"] == 0
    assert trace["sufficient"] is True
    assert len(trace["hops"]) == 1
    assert chunks


async def test_perpetual_insufficiency_is_hard_capped_at_two_rewrites(settings):
    loop, retrieval, client = make_loop(
        settings,
        grades=[INSUFFICIENT, INSUFFICIENT, INSUFFICIENT],
        rewrites=["how do I get my money back?", "returns and reimbursements"],
    )

    chunks, trace = await loop.retrieve("refund policy?")

    # 1 original + exactly MAX_REWRITES retrievals; grader ran on each hop.
    assert len(retrieval.calls) == 1 + MAX_REWRITES
    assert len(client.grade_calls) == 1 + MAX_REWRITES
    assert len(client.rewrite_calls) == MAX_REWRITES
    assert trace["rewrites"] == MAX_REWRITES
    assert trace["sufficient"] is False
    # Rewritten queries actually drove retrieval.
    assert retrieval.calls[1]["query"] == "how do I get my money back?"
    assert retrieval.calls[2]["query"] == "returns and reimbursements"
    # Chunks from the LAST hop are still returned (never an error).
    assert chunks


async def test_insufficient_then_sufficient_stops_after_one_rewrite(settings):
    loop, retrieval, client = make_loop(
        settings,
        grades=[INSUFFICIENT, SUFFICIENT],
        rewrites=["money back rules"],
    )

    _, trace = await loop.retrieve("refund policy?")

    assert len(retrieval.calls) == 2
    assert trace["rewrites"] == 1
    assert trace["sufficient"] is True
    # AC: the trace records each hop's query text and verdict.
    assert trace["hops"][0]["query"] == "refund policy?"
    assert trace["hops"][0]["verdict"]["sufficient"] is False
    assert trace["hops"][1]["query"] == "money back rules"
    assert trace["hops"][1]["verdict"]["sufficient"] is True
    assert "durationMs" in trace["hops"][0]


async def test_grader_failure_falls_open_to_the_current_set(settings):
    loop, retrieval, client = make_loop(settings, grades=[RuntimeError("model down")])

    chunks, trace = await loop.retrieve("refund policy?")

    assert len(retrieval.calls) == 1
    assert client.rewrite_calls == []
    assert trace["sufficient"] is True  # fail-open (Flow H)
    assert chunks


async def test_unparseable_verdict_falls_open(settings):
    loop, retrieval, _ = make_loop(settings, grades=["definitely not json"])

    chunks, trace = await loop.retrieve("refund policy?")

    assert len(retrieval.calls) == 1
    assert trace["sufficient"] is True
    assert chunks


async def test_rewrite_failure_ends_loop_with_current_chunks(settings):
    loop, retrieval, _ = make_loop(
        settings, grades=[INSUFFICIENT], rewrites=[RuntimeError("model down")]
    )

    chunks, trace = await loop.retrieve("refund policy?")

    assert len(retrieval.calls) == 1
    assert trace["rewrites"] == 0
    assert chunks


async def test_non_novel_rewrite_ends_loop(settings):
    loop, retrieval, _ = make_loop(
        settings, grades=[INSUFFICIENT], rewrites=["Refund Policy?"]  # same, case-folded
    )

    _, trace = await loop.retrieve("refund policy?")

    assert len(retrieval.calls) == 1
    assert trace["rewrites"] == 0


def test_parse_verdict_shapes():
    assert _parse_verdict('{"sufficient": true}') == {"sufficient": True}
    assert _parse_verdict('noise {"sufficient": false, "reason": "x"} noise')["sufficient"] is False
    assert _parse_verdict("[]") is None
    assert _parse_verdict('{"sufficient": "yes"}') is None


# ---------------------------------------------- GenerationService integration
async def test_agentic_service_answers_from_loop_chunks_with_original_question(settings):
    config = settings(RAG_MODE="agentic")
    retrieval = FakeRetrievalService([make_chunk("Refunds are processed in 30 days.")])
    loop_client = ScriptedLoopClient(
        grades=[INSUFFICIENT, SUFFICIENT], rewrites=["money back timing"]
    )
    agentic = AgenticRetriever(config, retrieval, loop_client)
    chat = FakeChatClient(answer="Refunds take 30 days. [1]")
    repository = FakeConversationsRepository()
    service = GenerationService(
        config, retrieval, chat, repository, agentic=agentic
    )  # type: ignore[arg-type]

    result = await service.answer(query="refund policy?", k=None, principal=PRINCIPAL)

    assert result["answer"] == "Refunds take 30 days. [1]"
    assert result["citations"]
    # The generation prompt asks the ORIGINAL question, not the rewrite.
    assert "refund policy?" in chat.calls[0][-1]["content"]
    assert "money back timing" not in chat.calls[0][-1]["content"]
    # AC: audit carries the hop summary in agentic mode.
    audit = repository.audits[-1]["after"]
    assert audit["agentic"] == {"hopCount": 2, "rewrites": 1, "sufficient": True}


async def test_linear_mode_audit_shape_is_unchanged(settings):
    config = settings()  # baseline profile -> RAG_MODE=linear
    retrieval = FakeRetrievalService([make_chunk("Refunds are processed in 30 days.")])
    repository = FakeConversationsRepository()
    service = GenerationService(
        config, retrieval, FakeChatClient(), repository
    )  # type: ignore[arg-type]

    await service.answer(query="refund policy?", k=None, principal=PRINCIPAL)

    assert len(retrieval.calls) == 1
    assert "agentic" not in repository.audits[-1]["after"]


async def test_total_loop_failure_falls_through_to_linear_retrieve(settings):
    config = settings(RAG_MODE="agentic")
    retrieval = FakeRetrievalService([make_chunk("Refunds are processed in 30 days.")])

    class ExplodingLoop:
        async def retrieve(self, query, *, k=None, filters=None):
            raise RuntimeError("graph broke")

    repository = FakeConversationsRepository()
    service = GenerationService(
        config, retrieval, FakeChatClient(), repository, agentic=ExplodingLoop()
    )  # type: ignore[arg-type]

    result = await service.answer(query="refund policy?", k=None, principal=PRINCIPAL)

    # Baseline-quality answer, never an error (Flow H failure rule).
    assert result["degraded"] is False
    assert len(retrieval.calls) == 1
    assert "agentic" not in repository.audits[-1]["after"]
