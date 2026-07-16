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
    """Grader/rewriter/groundedness stand-in: routes on the system prompt."""

    model = "gpt-4o-mini"
    provider = "openai"

    def __init__(
        self,
        grades: list[Any] | None = None,
        rewrites: list[Any] | None = None,
        grounds: list[Any] | None = None,
    ) -> None:
        self.grades = list(grades or [])
        self.rewrites = list(rewrites or [])
        self.grounds = list(grounds or [])
        self.grade_calls: list[str] = []
        self.rewrite_calls: list[str] = []
        self.ground_calls: list[str] = []

    async def complete(self, messages):
        system = messages[0]["content"]
        user = messages[-1]["content"]
        if "grade whether retrieved context" in system:
            self.grade_calls.append(user)
            item = self.grades.pop(0)
        elif "verify that an answer" in system:
            self.ground_calls.append(user)
            item = self.grounds.pop(0)
        else:
            self.rewrite_calls.append(user)
            item = self.rewrites.pop(0)
        if isinstance(item, Exception):
            raise item
        return item, {"promptTokens": 50, "completionTokens": 10, "totalTokens": 60}


SUFFICIENT = '{"sufficient": true, "reason": "covers it"}'
INSUFFICIENT = '{"sufficient": false, "reason": "off-topic"}'
GROUNDED = '{"grounded": true, "reason": "supported"}'
UNGROUNDED = '{"grounded": false, "reason": "invented facts"}'


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


# ------------------------------------------------- CE-4 groundedness gate
def agentic_service(settings, *, grades=None, grounds=None, rewrites=None, chat=None):
    config = settings(RAG_MODE="agentic")
    retrieval = FakeRetrievalService([make_chunk("Refunds are processed in 30 days.")])
    loop_client = ScriptedLoopClient(
        grades=grades or [SUFFICIENT], rewrites=rewrites, grounds=grounds
    )
    agentic = AgenticRetriever(config, retrieval, loop_client)
    chat = chat or FakeChatClient(answer="Refunds take 30 days. [1]")
    repository = FakeConversationsRepository()
    service = GenerationService(
        config, retrieval, chat, repository, agentic=agentic
    )  # type: ignore[arg-type]
    return service, loop_client, chat, repository


async def test_grounded_draft_ships_without_regeneration(settings):
    service, loop_client, chat, repository = agentic_service(settings, grounds=[GROUNDED])

    result = await service.answer(query="refund policy?", k=None, principal=PRINCIPAL)

    assert result["answer"] == "Refunds take 30 days. [1]"
    assert len(chat.calls) == 1  # no regeneration
    assert repository.audits[-1]["after"]["grounding"]["outcome"] == "draft"


async def test_ungrounded_draft_regenerates_exactly_once(settings):
    class TwoAnswerChat(FakeChatClient):
        def __init__(self):
            super().__init__()
            self.answers = ["Wrong invented claim. [1]", "Refunds take 30 days. [1]"]

        async def complete(self, messages):
            self.calls.append(messages)
            return self.answers[len(self.calls) - 1], {
                "promptTokens": 10,
                "completionTokens": 5,
                "totalTokens": 15,
            }

    service, loop_client, chat, repository = agentic_service(
        settings, grounds=[UNGROUNDED, GROUNDED], chat=TwoAnswerChat()
    )

    result = await service.answer(query="refund policy?", k=None, principal=PRINCIPAL)

    # Exactly one regeneration, with the strict suffix on the system turn.
    assert len(chat.calls) == 2
    assert "STRICT MODE" in chat.calls[1][0]["content"]
    assert "STRICT MODE" not in chat.calls[0][0]["content"]
    assert result["answer"] == "Refunds take 30 days. [1]"
    assert result["citations"]
    grounding = repository.audits[-1]["after"]["grounding"]
    assert grounding == {"grounded": False, "regenerated": True, "outcome": "regenerated"}


async def test_second_groundedness_failure_returns_i_dont_know_never_the_draft(settings):
    service, loop_client, chat, repository = agentic_service(
        settings, grounds=[UNGROUNDED, UNGROUNDED]
    )

    result = await service.answer(query="refund policy?", k=None, principal=PRINCIPAL)

    assert result["answer"] == "I don't know."
    assert result["isIDontKnow"] is True
    assert result["citations"] == []
    assert "Refunds take 30 days" not in result["answer"]
    assert repository.audits[-1]["after"]["grounding"]["outcome"] == "idk"


async def test_checker_failure_ships_draft_with_marker(settings):
    service, loop_client, chat, repository = agentic_service(
        settings, grounds=[RuntimeError("checker down")]
    )

    result = await service.answer(query="refund policy?", k=None, principal=PRINCIPAL)

    # Fail-open (Flow H): the checker is an enhancer, never a gate.
    assert result["answer"] == "Refunds take 30 days. [1]"
    assert repository.audits[-1]["after"]["grounding"]["outcome"] == "checker-failed"


async def test_linear_mode_never_calls_the_groundedness_checker(settings):
    config = settings()  # linear
    retrieval = FakeRetrievalService([make_chunk("Refunds are processed in 30 days.")])
    repository = FakeConversationsRepository()
    service = GenerationService(
        config, retrieval, FakeChatClient(), repository
    )  # type: ignore[arg-type]

    await service.answer(query="refund policy?", k=None, principal=PRINCIPAL)

    assert "grounding" not in repository.audits[-1]["after"]


# ---------------------------------------------------- CE-4 SSE status events
def parse_frames(raw_frames: list[str]) -> list[tuple[str, dict]]:
    import json as _json

    parsed = []
    for frame in raw_frames:
        event = "message"
        data = ""
        for line in frame.split("\n"):
            if line.startswith("event: "):
                event = line[7:].strip()
            elif line.startswith("data: "):
                data += line[6:]
        parsed.append((event, _json.loads(data)))
    return parsed


async def test_agentic_stream_emits_status_before_tokens(settings):
    from tests.test_generation import FakeStreamingChatClient

    config = settings(RAG_MODE="agentic")
    retrieval = FakeRetrievalService([make_chunk("Refunds are processed in 30 days.")])
    loop_client = ScriptedLoopClient(grades=[SUFFICIENT], grounds=[GROUNDED])
    agentic = AgenticRetriever(config, retrieval, loop_client)
    repository = FakeConversationsRepository()
    service = GenerationService(
        config,
        retrieval,
        FakeStreamingChatClient(answer="Refunds take 30 days. [1]"),
        repository,
        agentic=agentic,
    )  # type: ignore[arg-type]

    frames = parse_frames(
        [
            f
            async for f in service.answer_stream(
                query="refund policy?", k=None, principal=PRINCIPAL
            )
        ]
    )

    events = [event for event, _ in frames]
    first_token = events.index("token")
    statuses_before = [e for e in events[:first_token] if e == "status"]
    assert statuses_before, "status events must precede token streaming"
    stages = [data["stage"] for event, data in frames if event == "status"]
    assert "retrieving" in stages and "grading" in stages
    assert events[-1] == "final"


async def test_agentic_stream_regenerates_on_final_frame(settings):
    from tests.test_generation import FakeStreamingChatClient

    config = settings(RAG_MODE="agentic")
    retrieval = FakeRetrievalService([make_chunk("Refunds are processed in 30 days.")])
    loop_client = ScriptedLoopClient(grades=[SUFFICIENT], grounds=[UNGROUNDED, GROUNDED])
    agentic = AgenticRetriever(config, retrieval, loop_client)

    class RegeneratingStreamClient(FakeStreamingChatClient):
        async def complete(self, messages):
            self.calls.append(messages)
            return "Refunds take 30 days. [1]", None

    client = RegeneratingStreamClient(answer="Invented draft claim. [1]")
    repository = FakeConversationsRepository()
    service = GenerationService(
        config, retrieval, client, repository, agentic=agentic
    )  # type: ignore[arg-type]

    frames = parse_frames(
        [
            f
            async for f in service.answer_stream(
                query="refund policy?", k=None, principal=PRINCIPAL
            )
        ]
    )

    stages = [data["stage"] for event, data in frames if event == "status"]
    assert "regenerating" in stages
    final = next(data for event, data in frames if event == "final")
    # The final frame carries the regenerated answer, not the streamed draft.
    assert final["answer"] == "Refunds take 30 days. [1]"
    assert repository.turns[-1]["answer"] == "Refunds take 30 days. [1]"


async def test_linear_stream_contract_has_no_status_events(settings):
    from tests.test_generation import FakeStreamingChatClient

    config = settings()  # linear
    retrieval = FakeRetrievalService([make_chunk("Refunds are processed in 30 days.")])
    repository = FakeConversationsRepository()
    service = GenerationService(
        config,
        retrieval,
        FakeStreamingChatClient(answer="Refunds take 30 days. [1]"),
        repository,
    )  # type: ignore[arg-type]

    frames = parse_frames(
        [
            f
            async for f in service.answer_stream(
                query="refund policy?", k=None, principal=PRINCIPAL
            )
        ]
    )

    events = [event for event, _ in frames]
    assert "status" not in events
    assert events[-1] == "final"


async def test_total_loop_failure_falls_through_to_linear_retrieve(settings):
    config = settings(RAG_MODE="agentic")
    retrieval = FakeRetrievalService([make_chunk("Refunds are processed in 30 days.")])

    class ExplodingLoop:
        async def retrieve(self, query, *, k=None, filters=None, on_status=None):
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
