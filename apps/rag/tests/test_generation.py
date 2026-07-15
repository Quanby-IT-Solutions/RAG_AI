"""Generation tests (GN-1): grounded answer + citations resolving chunkId ->
document source/section, empty retrieval -> "I don't know" with no invented
citations, golden-set spot test (cited chunks actually support the answer),
LLM call under the hard timeout, provider error -> bounded graceful
degradation, turn persistence (new conversation + user/assistant messages),
one audit entry per query, and service-JWT enforcement on /internal/chat.
No real OpenAI, no real DB."""

import asyncio
import logging
import uuid
from typing import Any

import httpx
import pytest

from app.config import Settings
from app.generation import (
    DEGRADED_ANSWER,
    I_DONT_KNOW_ANSWER,
    ChatClient,
    GenerationService,
    get_chat_client,
    get_conversations_repository,
)
from app.main import create_app
from app.retrieval import get_retrieval_service
from app.security import ServicePrincipal
from app.timeouts import ExternalCallTimeoutError
from tests.conftest import REQUIRED_ENV
from tests.test_ingest import make_token

PRINCIPAL = ServicePrincipal(
    sub="user-1",
    permissions=("chat.query.submit",),
    actor_type="user",
    request_id="req-1",
    trace_id="trace-1",
)

DOC_ID = str(uuid.uuid4())
OTHER_DOC_ID = str(uuid.uuid4())
SOURCES = {DOC_ID: "upload://handbook.pdf", OTHER_DOC_ID: "upload://policy.pdf"}


def make_chunk(
    content: str,
    *,
    document_id: str = DOC_ID,
    page: int = 3,
    section: str | None = "Benefits",
    similarity: float = 0.9,
) -> dict[str, Any]:
    return {
        "id": str(uuid.uuid4()),
        "document_id": document_id,
        "content": content,
        "metadata": {"page": page, "section": section},
        "chunk_index": 0,
        "similarity": similarity,
    }


class FakeRetrievalService:
    def __init__(self, chunks: list[dict[str, Any]] | None = None) -> None:
        self.chunks = chunks or []
        self.calls: list[dict[str, Any]] = []

    async def retrieve(self, query: str, *, k: int | None = None) -> list[dict[str, Any]]:
        self.calls.append({"query": query, "k": k})
        return [dict(chunk) for chunk in self.chunks]


class FakeChatClient:
    """Non-streamed provider stand-in; records prompts, can fail."""

    def __init__(
        self,
        answer: str = "The answer. [1]",
        *,
        fail: Exception | None = None,
    ) -> None:
        self.model = "gpt-4o-mini"
        self.answer = answer
        self.fail = fail
        self.calls: list[list[dict[str, str]]] = []

    async def complete(self, messages: list[dict[str, str]]) -> tuple[str, dict[str, int] | None]:
        self.calls.append(messages)
        if self.fail is not None:
            raise self.fail
        return self.answer, {"promptTokens": 100, "completionTokens": 20, "totalTokens": 120}


class FakeConversationsRepository:
    def __init__(self) -> None:
        self.turns: list[dict[str, Any]] = []
        self.audits: list[dict[str, Any]] = []
        self.source_lookups: list[list[str]] = []
        self.conversation_id = str(uuid.uuid4())
        self.message_id = str(uuid.uuid4())

    async def fetch_document_sources(self, document_ids: list[str]) -> dict[str, str]:
        self.source_lookups.append(list(document_ids))
        return {doc_id: SOURCES[doc_id] for doc_id in document_ids if doc_id in SOURCES}

    async def persist_turn(self, **turn: Any) -> tuple[str, str]:
        self.turns.append(turn)
        return self.conversation_id, self.message_id

    async def write_audit(self, **entry: Any) -> None:
        self.audits.append(entry)


def make_service(
    settings: Settings,
    *,
    chunks: list[dict[str, Any]] | None = None,
    chat_client: FakeChatClient | None = None,
) -> tuple[GenerationService, FakeRetrievalService, FakeChatClient, FakeConversationsRepository]:
    retrieval = FakeRetrievalService(chunks)
    client = chat_client or FakeChatClient()
    repository = FakeConversationsRepository()
    service = GenerationService(settings, retrieval, client, repository)  # type: ignore[arg-type]
    return service, retrieval, client, repository


# --------------------------------------- grounded answer + resolved citations
async def test_grounded_answer_with_citations_resolving_to_source_and_section(settings):
    chunks = [
        make_chunk("Employees get 20 vacation days per year.", page=3, section="Benefits"),
        make_chunk(
            "Remote work is allowed 3 days a week.",
            document_id=OTHER_DOC_ID,
            page=7,
            section="Remote Work",
            similarity=0.8,
        ),
    ]
    client = FakeChatClient(answer="You get 20 vacation days [1] and 3 remote days a week [2].")
    service, _, _, repository = make_service(settings, chunks=chunks, chat_client=client)

    result = await service.answer(query="vacation and remote policy?", k=None, principal=PRINCIPAL)

    assert result["answer"] == client.answer
    assert result["isIDontKnow"] is False
    assert result["degraded"] is False
    assert result["model"] == "gpt-4o-mini"
    assert result["tokenUsage"] == {
        "promptTokens": 100,
        "completionTokens": 20,
        "totalTokens": 120,
    }
    assert result["traceId"] == "trace-1"
    # Every cited block resolves chunkId -> documentId -> source + page/section.
    assert result["citations"] == [
        {
            "chunkId": chunks[0]["id"],
            "documentId": DOC_ID,
            "source": "upload://handbook.pdf",
            "page": 3,
            "section": "Benefits",
        },
        {
            "chunkId": chunks[1]["id"],
            "documentId": OTHER_DOC_ID,
            "source": "upload://policy.pdf",
            "page": 7,
            "section": "Remote Work",
        },
    ]
    # Source lookup queried only the cited documents.
    assert repository.source_lookups == [sorted({DOC_ID, OTHER_DOC_ID})]


async def test_only_cited_blocks_become_citations_and_bogus_markers_are_ignored(settings):
    chunks = [make_chunk("Fact one."), make_chunk("Fact two.", similarity=0.7)]
    client = FakeChatClient(answer="Only fact one matters [1]. Ignore [9].")
    service, _, _, _ = make_service(settings, chunks=chunks, chat_client=client)

    result = await service.answer(query="q", k=None, principal=PRINCIPAL)

    assert [c["chunkId"] for c in result["citations"]] == [chunks[0]["id"]]


# ------------------------------------------------ B1: empty retrieval -> IDK
async def test_empty_retrieval_returns_i_dont_know_without_calling_the_model(settings):
    service, retrieval, client, repository = make_service(settings, chunks=[])

    result = await service.answer(query="unknown topic?", k=None, principal=PRINCIPAL)

    assert result["answer"] == I_DONT_KNOW_ANSWER
    assert result["isIDontKnow"] is True
    assert result["citations"] == []  # no invented citations
    assert result["tokenUsage"] is None
    assert client.calls == []  # model never called
    # The refusal turn is still persisted + audited.
    assert len(repository.turns) == 1
    assert repository.turns[0]["answer"] == I_DONT_KNOW_ANSWER
    assert repository.turns[0]["citations"] == []


async def test_model_i_dont_know_yields_no_citations(settings):
    chunks = [make_chunk("Totally unrelated content.")]
    client = FakeChatClient(answer='"I don\'t know."')
    service, _, _, _ = make_service(settings, chunks=chunks, chat_client=client)

    result = await service.answer(query="q", k=None, principal=PRINCIPAL)

    assert result["isIDontKnow"] is True
    assert result["citations"] == []


# ------------------------------------------------- golden-set spot test (R1)
async def test_cited_chunks_actually_contain_the_supporting_content(settings):
    """Answerable question: the chunk the fake model cites really carries the
    supporting fact the answer states."""
    supporting = make_chunk("The employee handbook grants 20 vacation days per year.")
    distractor = make_chunk("The cafeteria opens at 8am.", similarity=0.5)
    client = FakeChatClient(answer="Employees receive 20 vacation days per year [1].")
    service, _, _, _ = make_service(settings, chunks=[supporting, distractor], chat_client=client)

    result = await service.answer(
        query="How many vacation days do employees get?", k=None, principal=PRINCIPAL
    )

    cited_ids = [c["chunkId"] for c in result["citations"]]
    assert cited_ids == [supporting["id"]]
    assert "20 vacation days" in supporting["content"]  # support really present
    assert "20 vacation days" in result["answer"]


# ----------------------------------------- grounded prompt reaches the model
async def test_prompt_is_grounded_with_numbered_context_and_idk_instruction(settings):
    chunks = [make_chunk("Alpha."), make_chunk("Beta.", similarity=0.8)]
    service, _, client, _ = make_service(settings, chunks=chunks)

    await service.answer(query="what is alpha?", k=2, principal=PRINCIPAL)

    [messages] = client.calls
    system = next(m for m in messages if m["role"] == "system")
    human = next(m for m in messages if m["role"] == "user")
    assert "ONLY the numbered context blocks" in system["content"]
    assert 'reply exactly: "I don\'t know."' in system["content"]
    assert "[1]\nAlpha." in human["content"]
    assert "[2]\nBeta." in human["content"]
    assert "what is alpha?" in human["content"]


async def test_k_override_is_forwarded_to_retrieval(settings):
    service, retrieval, _, _ = make_service(settings, chunks=[])

    await service.answer(query="q", k=3, principal=PRINCIPAL)

    assert retrieval.calls == [{"query": "q", "k": 3}]


# ------------------------------------------ B2: provider error -> degradation
async def test_provider_error_returns_bounded_degradation_message(settings, caplog):
    chunks = [make_chunk("Some context.")]
    client = FakeChatClient(fail=RuntimeError("provider exploded"))
    service, _, _, repository = make_service(settings, chunks=chunks, chat_client=client)

    with caplog.at_level(logging.WARNING, logger="app.generation"):
        result = await service.answer(query="q", k=None, principal=PRINCIPAL)

    # No exception leak; bounded, provider-detail-free message; no citations.
    assert result["answer"] == DEGRADED_ANSWER
    assert result["degraded"] is True
    assert result["citations"] == []
    assert result["tokenUsage"] is None
    # The failure path is recorded (trace/log) and the turn is still persisted.
    assert any("chat.generation_degraded" in r.getMessage() for r in caplog.records)
    assert len(repository.turns) == 1
    assert repository.audits[0]["after"]["degraded"] is True


async def test_provider_timeout_degrades_the_same_way(settings):
    chunks = [make_chunk("Some context.")]
    client = FakeChatClient(fail=ExternalCallTimeoutError("openai.chat.completions.create", 0.05))
    service, _, _, _ = make_service(settings, chunks=chunks, chat_client=client)

    result = await service.answer(query="q", k=None, principal=PRINCIPAL)

    assert result["answer"] == DEGRADED_ANSWER
    assert result["degraded"] is True


# --------------------------------------------- LLM call under the hard timeout
class HangingCompletions:
    async def create(self, **kwargs: Any) -> Any:
        await asyncio.sleep(60)


class HangingOpenAI:
    def __init__(self) -> None:
        self.chat = type("Chat", (), {"completions": HangingCompletions()})()


async def test_chat_completion_runs_under_the_hard_timeout():
    fast = Settings(_env_file=None, EXTERNAL_CALL_TIMEOUT_SECONDS=0.05, **REQUIRED_ENV)
    client = ChatClient(fast, client=HangingOpenAI())  # type: ignore[arg-type]

    with pytest.raises(ExternalCallTimeoutError) as excinfo:
        await client.complete([{"role": "user", "content": "hi"}])

    assert excinfo.value.operation == "openai.chat.completions.create"


class UsageResponse:
    def __init__(self) -> None:
        message = type("Message", (), {"content": "Answer [1]."})()
        self.choices = [type("Choice", (), {"message": message})()]
        self.usage = type(
            "Usage", (), {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}
        )()


class OneShotCompletions:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return UsageResponse()


async def test_chat_client_calls_configured_model_non_streamed(settings):
    completions = OneShotCompletions()
    fake_openai = type("OpenAI", (), {"chat": type("Chat", (), {"completions": completions})()})()
    client = ChatClient(settings, client=fake_openai)  # type: ignore[arg-type]

    answer, usage = await client.complete([{"role": "user", "content": "hi"}])

    assert answer == "Answer [1]."
    assert usage == {"promptTokens": 11, "completionTokens": 7, "totalTokens": 18}
    [call] = completions.calls
    assert call["model"] == settings.generation_model == "gpt-4o-mini"
    assert call["stream"] is False  # GN-1 is non-streamed (streaming is GN-3)


# --------------------------------------------------- persistence of the turn
async def test_turn_persisted_as_new_conversation_with_both_messages(settings):
    chunks = [make_chunk("Fact.")]
    client = FakeChatClient(answer="Fact stated [1].")
    service, _, _, repository = make_service(settings, chunks=chunks, chat_client=client)

    result = await service.answer(query="what fact?", k=None, principal=PRINCIPAL)

    [turn] = repository.turns
    assert turn["user_id"] == "user-1"
    assert turn["query"] == "what fact?"
    assert turn["answer"] == "Fact stated [1]."
    assert turn["citations"] == result["citations"]
    assert turn["retrieved_chunks"] == [
        {"id": chunks[0]["id"], "documentId": DOC_ID, "similarity": 0.9}
    ]
    assert turn["token_usage"] == {
        "promptTokens": 100,
        "completionTokens": 20,
        "totalTokens": 120,
    }
    assert turn["trace_id"] == "trace-1"
    assert turn["model_used"] == "gpt-4o-mini"
    assert result["conversationId"] == repository.conversation_id
    assert result["messageId"] == repository.message_id


async def test_missing_gateway_trace_id_falls_back_to_a_generated_one(settings):
    principal = ServicePrincipal(
        sub="user-1",
        permissions=(),
        actor_type="user",
        request_id=None,
        trace_id=None,
    )
    service, _, _, repository = make_service(settings, chunks=[])

    result = await service.answer(query="q", k=None, principal=principal)

    assert result["traceId"]
    assert repository.turns[0]["trace_id"] == result["traceId"]


# --------------------------------------------------------- one audit per query
async def test_chat_query_audited_once_with_the_jwt_principal(settings):
    chunks = [make_chunk("Fact.")]
    service, _, _, repository = make_service(settings, chunks=chunks)

    result = await service.answer(query="q", k=None, principal=PRINCIPAL)

    assert len(repository.audits) == 1
    audit = repository.audits[0]
    assert audit["actor_id"] == "user-1"
    assert audit["actor_type"] == "user"
    assert audit["action"] == "chat.query.submit"
    assert audit["resource_type"] == "conversation"
    assert audit["resource_id"] == result["conversationId"]
    assert audit["after"]["messageId"] == result["messageId"]
    assert audit["after"]["model"] == "gpt-4o-mini"
    assert audit["after"]["traceId"] == "trace-1"


# ----------------------------------------------------- endpoint + service JWT
@pytest.fixture
def chat_app(settings):
    application = create_app(settings)
    retrieval = FakeRetrievalService([make_chunk("Vacation is 20 days.")])
    client = FakeChatClient(answer="20 days of vacation [1].")
    repository = FakeConversationsRepository()
    application.dependency_overrides[get_retrieval_service] = lambda: retrieval
    application.dependency_overrides[get_chat_client] = lambda: client
    application.dependency_overrides[get_conversations_repository] = lambda: repository
    return application


async def post_chat(app, *, token: str | None, json: dict[str, Any]) -> httpx.Response:
    headers = {"authorization": f"Bearer {token}"} if token else {}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://rag") as client:
        return await client.post("/internal/chat", headers=headers, json=json)


async def test_chat_endpoint_requires_service_jwt(chat_app):
    response = await post_chat(chat_app, token=None, json={"query": "hello"})
    assert response.status_code == 401


async def test_chat_endpoint_rejects_bad_signature(chat_app):
    token = make_token(secret="wrong-secret")
    response = await post_chat(chat_app, token=token, json={"query": "hello"})
    assert response.status_code == 401


async def test_chat_endpoint_returns_the_generation_payload(chat_app):
    response = await post_chat(
        chat_app, token=make_token(), json={"query": "vacation days?", "k": 2}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["answer"] == "20 days of vacation [1]."
    assert body["isIDontKnow"] is False
    assert body["model"] == "gpt-4o-mini"
    assert body["conversationId"]
    assert body["messageId"]
    assert body["traceId"] == "trace-1"  # correlation id from the service JWT
    assert body["tokenUsage"]["totalTokens"] == 120
    assert [c["source"] for c in body["citations"]] == ["upload://handbook.pdf"]


async def test_chat_endpoint_validates_the_query(chat_app):
    response = await post_chat(chat_app, token=make_token(), json={"query": ""})
    assert response.status_code == 422
