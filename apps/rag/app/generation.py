"""POST /internal/chat — grounded, cited answer with "I don't know" (Flow B, GN-1).

The generate stage: retrieve top-k chunks (RT-1 RetrievalService), assemble a
GROUNDED LCEL prompt (answer ONLY from the provided context; say "I don't
know" when it is insufficient), call the single OpenAI chat model (cheap-fast
default, GENERATION_MODEL) NON-streamed, and build citations that resolve each
cited chunk -> {chunkId, documentId, source, page/section}. Alternate B1:
empty/irrelevant retrieval -> grounded "I don't know" with NO invented
citations (the model is not even called on empty retrieval). Alternate B2:
provider error/timeout -> bounded graceful-degradation message; the trace
records the failure path (retry/circuit-breaker/failover is GN-5 — absent by
design). Streaming is GN-3; token budgeting is GN-2.

Every LLM/DB call goes through the shared hard-timeout helper; the whole
request runs in ONE trace (model, tokens, latency). Each request persists a
NEW conversation (single-turn R1) with the user/assistant message pair and
writes ONE RAG-action audit entry attributed to the service-JWT principal.
"""

import json
import logging
import re
import time
import uuid
from typing import TYPE_CHECKING, Annotated, Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from app.budget import current_context_budget, select_within_budget
from app.config import Settings
from app.conversations import ConversationsRepository
from app.retrieval import RetrievalService, get_retrieval_service
from app.security import ServicePrincipal, verify_service_jwt
from app.timeouts import with_hard_timeout
from app.tracing import traced

if TYPE_CHECKING:
    from openai import AsyncOpenAI

logger = logging.getLogger(__name__)

AUDIT_ACTION_CHAT = "chat.query.submit"

# Grounded refusal (B1) and bounded graceful degradation (B2) — both fixed
# strings so the client never sees provider internals or a silent failure.
I_DONT_KNOW_ANSWER = "I don't know."
DEGRADED_ANSWER = (
    "I couldn't generate an answer right now because the language model is "
    "unavailable. Please try again in a moment."
)

# Grounded LCEL prompt (langchain-core ChatPromptTemplate): the model may use
# ONLY the numbered context blocks, must cite them as [n], and must answer
# exactly "I don't know." when the context is insufficient.
_SYSTEM_PROMPT = (
    "You are a retrieval-grounded assistant. Answer the user's question using "
    "ONLY the numbered context blocks provided. Cite every statement with the "
    "bracketed number(s) of the supporting block(s), e.g. [1] or [1][3]. Never "
    "use outside knowledge and never invent citations. If the context does not "
    'contain the information needed to answer, reply exactly: "I don\'t know."'
)

_CITATION_MARKER = re.compile(r"\[(\d+)\]")


def _sse(event: str, data: dict[str, Any]) -> str:
    """One Server-Sent Events frame."""
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def build_grounded_prompt() -> Any:
    """The GN-1 grounded prompt as an LCEL ChatPromptTemplate."""
    from langchain_core.prompts import ChatPromptTemplate

    return ChatPromptTemplate.from_messages(
        [
            ("system", _SYSTEM_PROMPT),
            ("human", "Context blocks:\n{context}\n\nQuestion: {question}"),
        ]
    )


def _format_context(chunks: list[dict[str, Any]]) -> str:
    """Number the retrieved chunks [1..n] so answer citations resolve back."""
    blocks = []
    for position, chunk in enumerate(chunks, start=1):
        blocks.append(f"[{position}]\n{chunk['content']}")
    return "\n\n".join(blocks)


def _is_i_dont_know(answer: str) -> bool:
    normalized = answer.strip().strip('"').rstrip(".!").lower()
    return normalized in ("i don't know", "i dont know", "i do not know")


def _cited_positions(answer: str, chunk_count: int) -> list[int]:
    """Ordered, de-duplicated [n] markers that point at a real context block."""
    seen: list[int] = []
    for match in _CITATION_MARKER.findall(answer):
        position = int(match)
        if 1 <= position <= chunk_count and position not in seen:
            seen.append(position)
    return seen


class ChatClient:
    """Thin non-streamed wrapper over the OpenAI chat completions endpoint
    (single provider, GN-1). Model routing/failover is GN-4/GN-5."""

    def __init__(self, settings: Settings, client: "AsyncOpenAI | None" = None) -> None:
        self._settings = settings
        self._client = client
        self.model = settings.generation_model

    def _openai(self) -> "AsyncOpenAI":
        if self._client is None:
            from openai import AsyncOpenAI

            self._client = AsyncOpenAI(api_key=self._settings.openai_api_key)
        return self._client

    async def complete(self, messages: list[dict[str, str]]) -> tuple[str, dict[str, int] | None]:
        """One non-streamed chat completion under the hard timeout; returns
        (answer text, token usage) and validates the output is non-empty."""
        response = await with_hard_timeout(
            self._openai().chat.completions.create(
                model=self.model,
                messages=messages,  # type: ignore[arg-type]
                stream=False,
            ),
            seconds=self._settings.external_call_timeout_seconds,
            operation="openai.chat.completions.create",
        )
        content = (response.choices[0].message.content or "").strip()
        if not content:
            raise RuntimeError(f"OpenAI returned an empty completion (model={self.model})")
        usage = getattr(response, "usage", None)
        token_usage = (
            {
                "promptTokens": usage.prompt_tokens,
                "completionTokens": usage.completion_tokens,
                "totalTokens": usage.total_tokens,
            }
            if usage is not None
            else None
        )
        return content, token_usage

    async def stream(self, messages: list[dict[str, str]]) -> Any:
        """Streamed chat completion (GN-3): yields text deltas, then a final
        ("__usage__", token_usage) sentinel tuple. Creating the stream runs
        under the hard timeout; the provider stream is closed on generator
        close (client disconnect -> no orphaned spend)."""
        stream = await with_hard_timeout(
            self._openai().chat.completions.create(
                model=self.model,
                messages=messages,  # type: ignore[arg-type]
                stream=True,
                stream_options={"include_usage": True},
            ),
            seconds=self._settings.external_call_timeout_seconds,
            operation="openai.chat.completions.stream",
        )
        token_usage: dict[str, int] | None = None
        try:
            async for chunk in stream:
                usage = getattr(chunk, "usage", None)
                if usage is not None:
                    token_usage = {
                        "promptTokens": usage.prompt_tokens,
                        "completionTokens": usage.completion_tokens,
                        "totalTokens": usage.total_tokens,
                    }
                if chunk.choices:
                    delta = chunk.choices[0].delta.content
                    if delta:
                        yield delta
        finally:
            # Cancellation propagation: closing this generator closes the
            # provider stream, aborting the completion server-side.
            await stream.close()
        yield ("__usage__", token_usage)


class GenerationService:
    """Retrieve -> grounded prompt -> single-provider completion -> citations
    -> persist turn -> audit, as one traced request."""

    def __init__(
        self,
        settings: Settings,
        retrieval: RetrievalService,
        chat_client: ChatClient,
        repository: ConversationsRepository,
    ) -> None:
        self._settings = settings
        self._retrieval = retrieval
        self._chat_client = chat_client
        self._repository = repository

    async def _build_citations(
        self, answer: str, chunks: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Resolve each cited context block -> {chunkId, documentId, source,
        page, section} using the chunk metadata + documents.source_uri."""
        positions = _cited_positions(answer, len(chunks))
        if not positions:
            return []
        cited = [chunks[position - 1] for position in positions]
        sources = await self._repository.fetch_document_sources(
            sorted({chunk["document_id"] for chunk in cited})
        )
        citations: list[dict[str, Any]] = []
        for chunk in cited:
            metadata = chunk.get("metadata") or {}
            if not isinstance(metadata, dict):
                metadata = {}
            citations.append(
                {
                    "chunkId": chunk["id"],
                    "documentId": chunk["document_id"],
                    "source": sources.get(chunk["document_id"]),
                    "page": metadata.get("page"),
                    "section": metadata.get("section"),
                }
            )
        return citations

    async def _prepare(self, *, query: str, k: int | None) -> dict[str, Any]:
        """Shared Flow B steps 3-5 for both response modes: hybrid retrieve
        (RT-1/RT-2), hard token budget (GN-2, rank-then-select), grounded
        prompt assembly. Returns everything the generate stage needs."""
        chunks = await self._retrieval.retrieve(query, k=k)
        retrieved_record = [
            {
                "id": chunk["id"],
                "documentId": chunk["document_id"],
                "similarity": chunk["similarity"],
            }
            for chunk in chunks
        ]

        # Budget resolved per request so a config change applies without
        # restart; the order (rank -> select) is contractual per §5.3.
        budget = current_context_budget(self._settings)
        used_chunks, selection = select_within_budget(chunks, budget=budget)
        used_record = [
            {
                "id": chunk["id"],
                "documentId": chunk["document_id"],
                "similarity": chunk["similarity"],
                "contextTokens": chunk["context_tokens"],
            }
            for chunk in used_chunks
        ]
        # The USED record is logged separately from the RETRIEVED record —
        # this split is what makes retrieval failures diagnosable
        # independent of generation (PRD constraint).
        logger.info(
            "generation.used_chunks %s",
            json.dumps({"stage": "used", **selection, "chunks": used_record}),
        )

        messages: list[dict[str, str]] | None = None
        if used_chunks:
            prompt = build_grounded_prompt().format_messages(
                context=_format_context(used_chunks), question=query
            )
            messages = [
                {"role": "system" if m.type == "system" else "user", "content": m.content}
                for m in prompt
            ]
        return {
            "chunks": chunks,
            "retrieved_record": retrieved_record,
            "used_chunks": used_chunks,
            "used_record": used_record,
            "selection": selection,
            "messages": messages,
        }

    @traced(name="rag.chat")
    async def answer(
        self, *, query: str, k: int | None, principal: ServicePrincipal
    ) -> dict[str, Any]:
        started = time.perf_counter()
        trace_id = principal.trace_id or str(uuid.uuid4())

        prep = await self._prepare(query=query, k=k)
        chunks = prep["chunks"]
        retrieved_record = prep["retrieved_record"]
        used_chunks = prep["used_chunks"]
        used_record = prep["used_record"]
        selection = prep["selection"]

        token_usage: dict[str, int] | None = None
        degraded = False
        if not used_chunks:
            # Alternate B1: nothing retrieved (or nothing fits the budget) ->
            # grounded refusal, model not called, NO invented citations.
            answer_text = I_DONT_KNOW_ANSWER
            citations: list[dict[str, Any]] = []
        else:
            try:
                answer_text, token_usage = await self._chat_client.complete(prep["messages"])
            except Exception as exc:
                # Alternate B2: provider error/timeout -> bounded degradation,
                # no silent failure; the trace records the failure path.
                # Retry/circuit-breaker/failover is GN-5 — deliberately absent.
                logger.warning(
                    "chat.generation_degraded model=%s reason=%s",
                    self._chat_client.model,
                    exc,
                )
                answer_text = DEGRADED_ANSWER
                degraded = True

            if degraded or _is_i_dont_know(answer_text):
                # Grounded "I don't know" / degraded answers carry NO citations.
                citations = []
            else:
                # Citations resolve against the USED context blocks.
                citations = await self._build_citations(answer_text, used_chunks)

        # Flow B step 7: persist the turn (NEW conversation per request, R1
        # single-turn) — user + assistant rows written atomically.
        conversation_id, message_id = await self._repository.persist_turn(
            user_id=principal.sub,
            query=query,
            answer=answer_text,
            citations=citations,
            retrieved_chunks=retrieved_record,
            used_chunks=used_record,
            token_usage=token_usage,
            trace_id=trace_id,
            model_used=self._chat_client.model,
        )

        is_i_dont_know = _is_i_dont_know(answer_text)
        latency_ms = round((time.perf_counter() - started) * 1000, 3)

        # ONE RAG-action audit entry per query, attributed to the JWT principal.
        await self._repository.write_audit(
            actor_id=principal.sub,
            actor_type=principal.actor_type,
            action=AUDIT_ACTION_CHAT,
            resource_type="conversation",
            resource_id=conversation_id,
            before=None,
            after={
                "messageId": message_id,
                "model": self._chat_client.model,
                "retrievedChunkCount": len(chunks),
                "usedChunkCount": len(used_chunks),
                "contextTokens": selection["contextTokens"],
                "citationCount": len(citations),
                "isIDontKnow": is_i_dont_know,
                "degraded": degraded,
                "traceId": trace_id,
            },
        )

        # The traced return value lands on the rag.chat span — the one trace
        # per request records model, tokens, and latency (Tech Plan §8).
        return {
            "answer": answer_text,
            "citations": citations,
            "conversationId": conversation_id,
            "messageId": message_id,
            "traceId": trace_id,
            "model": self._chat_client.model,
            "tokenUsage": token_usage,
            "isIDontKnow": is_i_dont_know,
            "degraded": degraded,
            "latencyMs": latency_ms,
        }

    async def answer_stream(
        self, *, query: str, k: int | None, principal: ServicePrincipal
    ) -> Any:
        """SSE mode (GN-3): yields `token` events as the provider streams,
        then ONE `final` event carrying citations, usage, ids, TTFT and
        latency. The persisted assistant message is exactly the streamed
        text. Client disconnect cancels this generator, whose provider
        stream closes in ChatClient.stream's finally — no orphaned spend.
        A mid-stream provider failure ends in a `final` event with
        degraded=true (never a silent hang)."""
        started = time.perf_counter()
        trace_id = principal.trace_id or str(uuid.uuid4())

        prep = await self._prepare(query=query, k=k)
        used_chunks = prep["used_chunks"]

        token_usage: dict[str, int] | None = None
        degraded = False
        ttft_ms: float | None = None

        if not used_chunks:
            # Alternate B1: grounded refusal — streamed as one token event so
            # clients render it like any other answer.
            answer_text = I_DONT_KNOW_ANSWER
            yield _sse("token", {"delta": answer_text})
        else:
            parts: list[str] = []
            try:
                # aclosing: when the CLIENT disconnects, closing this outer
                # generator must aclose the provider stream too (its finally
                # aborts the completion — no orphaned spend).
                from contextlib import aclosing

                async with aclosing(self._chat_client.stream(prep["messages"])) as stream:
                    async for item in stream:
                        if isinstance(item, tuple):  # ("__usage__", usage) sentinel
                            token_usage = item[1]
                            continue
                        if ttft_ms is None:
                            ttft_ms = round((time.perf_counter() - started) * 1000, 3)
                            logger.info(
                                "chat.stream_ttft %s",
                                json.dumps({"ttftMs": ttft_ms, "model": self._chat_client.model}),
                            )
                        parts.append(item)
                        yield _sse("token", {"delta": item})
                answer_text = "".join(parts).strip()
                if not answer_text:
                    raise RuntimeError(
                        f"OpenAI returned an empty stream (model={self._chat_client.model})"
                    )
            except Exception as exc:
                # Alternate B2 mid-stream: bounded degradation on the final
                # frame; the trace records the failure path.
                logger.warning(
                    "chat.stream_degraded model=%s reason=%s", self._chat_client.model, exc
                )
                answer_text = DEGRADED_ANSWER
                degraded = True

        if degraded or _is_i_dont_know(answer_text):
            citations: list[dict[str, Any]] = []
        else:
            citations = await self._build_citations(answer_text, used_chunks)

        conversation_id, message_id = await self._repository.persist_turn(
            user_id=principal.sub,
            query=query,
            answer=answer_text,
            citations=citations,
            retrieved_chunks=prep["retrieved_record"],
            used_chunks=prep["used_record"],
            token_usage=token_usage,
            trace_id=trace_id,
            model_used=self._chat_client.model,
        )

        is_i_dont_know = _is_i_dont_know(answer_text)
        latency_ms = round((time.perf_counter() - started) * 1000, 3)

        await self._repository.write_audit(
            actor_id=principal.sub,
            actor_type=principal.actor_type,
            action=AUDIT_ACTION_CHAT,
            resource_type="conversation",
            resource_id=conversation_id,
            before=None,
            after={
                "messageId": message_id,
                "model": self._chat_client.model,
                "retrievedChunkCount": len(prep["chunks"]),
                "usedChunkCount": len(used_chunks),
                "contextTokens": prep["selection"]["contextTokens"],
                "citationCount": len(citations),
                "isIDontKnow": is_i_dont_know,
                "degraded": degraded,
                "streamed": True,
                "ttftMs": ttft_ms,
                "traceId": trace_id,
            },
        )

        # Final frame: citations + usage + ids (GN-3 contract with CH-2).
        yield _sse(
            "final",
            {
                "answer": answer_text,
                "citations": citations,
                "conversationId": conversation_id,
                "messageId": message_id,
                "traceId": trace_id,
                "model": self._chat_client.model,
                "tokenUsage": token_usage,
                "isIDontKnow": is_i_dont_know,
                "degraded": degraded,
                "ttftMs": ttft_ms,
                "latencyMs": latency_ms,
            },
        )


router = APIRouter(prefix="/internal", dependencies=[Depends(verify_service_jwt)])


def get_conversations_repository(request: Request) -> ConversationsRepository:
    """Dependency seam so tests can override persistence."""
    return ConversationsRepository(request.app.state.settings)


def get_chat_client(request: Request) -> ChatClient:
    """Dependency seam so tests can override the provider (never call real
    OpenAI in tests). One client per app — reused across requests."""
    client = getattr(request.app.state, "chat_client", None)
    if client is None:
        client = ChatClient(request.app.state.settings)
        request.app.state.chat_client = client
    return client


class ChatRequest(BaseModel):
    query: str = Field(min_length=1, max_length=4000)
    k: int | None = Field(default=None, ge=1, le=50)


@router.post("/chat")
async def chat(
    payload: ChatRequest,
    request: Request,
    principal: Annotated[ServicePrincipal, Depends(verify_service_jwt)],
    retrieval: Annotated[RetrievalService, Depends(get_retrieval_service)],
    chat_client: Annotated[ChatClient, Depends(get_chat_client)],
    repository: Annotated[ConversationsRepository, Depends(get_conversations_repository)],
) -> dict[str, Any]:
    """Non-streamed JSON mode — preserved for integrators (GN-3 AC)."""
    service = GenerationService(
        settings=request.app.state.settings,
        retrieval=retrieval,
        chat_client=chat_client,
        repository=repository,
    )
    return await service.answer(query=payload.query, k=payload.k, principal=principal)


@router.post("/chat/stream")
async def chat_stream(
    payload: ChatRequest,
    request: Request,
    principal: Annotated[ServicePrincipal, Depends(verify_service_jwt)],
    retrieval: Annotated[RetrievalService, Depends(get_retrieval_service)],
    chat_client: Annotated[ChatClient, Depends(get_chat_client)],
    repository: Annotated[ConversationsRepository, Depends(get_conversations_repository)],
) -> Any:
    """SSE mode (GN-3): `token` events then one `final` event. Client
    disconnect cancels the generator, which closes the provider stream."""
    from fastapi.responses import StreamingResponse

    service = GenerationService(
        settings=request.app.state.settings,
        retrieval=retrieval,
        chat_client=chat_client,
        repository=repository,
    )
    return StreamingResponse(
        service.answer_stream(query=payload.query, k=payload.k, principal=principal),
        media_type="text/event-stream",
        headers={"cache-control": "no-cache", "x-accel-buffering": "no"},
    )
