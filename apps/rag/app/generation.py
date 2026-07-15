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
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from app.budget import current_context_budget, select_within_budget
from app.compression import Compressor, build_compressor, compress_with_fallback
from app.config import Settings
from app.conversations import ConversationsRepository
from app.providers import (
    ChatModelClient,
    ModelRouter,
    OpenAIChatClient,
    SingleClientRouter,
    build_model_router,
)
from app.reliability import CircuitBreaker, ReliabilityRouter, validate_output
from app.retrieval import RetrievalFilters, RetrievalService, get_retrieval_service
from app.security import ServicePrincipal, verify_service_jwt
from app.security_screen import QueryBlockedError, screen_query
from app.tracing import traced

# Backward-compat alias: the GN-1 ChatClient moved to app.providers (GN-4).
ChatClient = OpenAIChatClient

logger = logging.getLogger(__name__)

AUDIT_ACTION_CHAT = "chat.query.submit"

# Grounded refusal (B1) and bounded graceful degradation (B2) — both fixed
# strings so the client never sees provider internals or a silent failure.
I_DONT_KNOW_ANSWER = "I don't know."
DEGRADED_ANSWER = (
    "I couldn't generate an answer right now because the language model is "
    "unavailable. Please try again in a moment."
)
# GN-5 output validation failure: replace the invalid answer with a safe,
# bounded response — never return leaked scaffolding or broken citations.
VALIDATION_FAILED_ANSWER = (
    "I generated an answer but it failed output validation, so it was "
    "withheld. Please try asking again."
)


class ConversationAccessError(Exception):
    """CH-3 own-scope rule: the conversation exists but belongs to someone
    else (or doesn't exist) — the endpoint returns 403/404 and the denial is
    audited."""

    def __init__(self, conversation_id: str, *, found: bool) -> None:
        super().__init__(f"Conversation {conversation_id} is not accessible")
        self.conversation_id = conversation_id
        self.found = found

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


class GenerationService:
    """Retrieve -> grounded prompt -> single-provider completion -> citations
    -> persist turn -> audit, as one traced request."""

    def __init__(
        self,
        settings: Settings,
        retrieval: RetrievalService,
        chat_client: "ModelRouter | SingleClientRouter | ChatModelClient",
        repository: ConversationsRepository,
        compressor: "Compressor | None" = None,
    ) -> None:
        self._settings = settings
        self._retrieval = retrieval
        # GN-4: accept a router (production) or a bare client (tests /
        # single-provider deployments) behind one seam.
        self._router = (
            chat_client if hasattr(chat_client, "route") else SingleClientRouter(chat_client)
        )
        self._repository = repository
        # RT-7: post-retrieval contextual compression (None when off).
        self._compressor = compressor

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

    async def screen(self, query: str, principal: ServicePrincipal, trace_id: str) -> str:
        """SC-1, Flow B stage 1 (position contractual): sanitize + injection
        check + PII mask BEFORE cache/retrieval/prompt/persistence/logs.
        Blocked queries are audited then raised; otherwise the MASKED query
        is the only form allowed downstream."""
        result = screen_query(query, self._settings)
        if result.blocked:
            assert result.injection is not None
            await self._repository.write_audit(
                actor_id=principal.sub,
                actor_type=principal.actor_type,
                action="chat.query.blocked",
                resource_type="query",
                resource_id=trace_id,
                before=None,
                after={"injection": result.injection, "action": "block"},
            )
            raise QueryBlockedError(result.injection)
        return result.query

    async def _assert_conversation_access(
        self, conversation_id: str | None, principal: ServicePrincipal
    ) -> None:
        """CH-3 continue: appending requires OWNING the conversation. The
        denial is audited before raising (Flow E)."""
        if conversation_id is None:
            return
        owner = await self._repository.fetch_conversation_owner(conversation_id)
        if owner == principal.sub:
            return
        await self._repository.write_audit(
            actor_id=principal.sub,
            actor_type=principal.actor_type,
            action="chat.history.denied",
            resource_type="conversation",
            resource_id=conversation_id,
            before=None,
            after={"reason": "not-owner" if owner else "not-found"},
        )
        raise ConversationAccessError(conversation_id, found=owner is not None)

    async def _route(
        self, *, query: str, chunks: list[dict[str, Any]]
    ) -> tuple[Any, dict[str, Any]]:
        """Route via the configured router; supports both the sync GN-4
        routers and the async GN-5 ReliabilityRouter."""
        import inspect

        routed = self._router.route(query=query, chunks=chunks)
        if inspect.isawaitable(routed):
            routed = await routed
        return routed

    async def _prepare(
        self, *, query: str, k: int | None, filters: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Shared Flow B steps 3-5 for both response modes: hybrid retrieve
        (RT-1/RT-2), hard token budget (GN-2, rank-then-select), grounded
        prompt assembly. Returns everything the generate stage needs."""
        chunks = await self._retrieval.retrieve(query, k=k, filters=filters)
        retrieved_record = [
            {
                "id": chunk["id"],
                "documentId": chunk["document_id"],
                "similarity": chunk["similarity"],
            }
            for chunk in chunks
        ]

        # ── RT-7 compress stage (retrieve → compress → budget, contractual):
        # each chunk shrinks to its query-relevant sentences; chunk identity
        # is preserved so citations still resolve to the ORIGINAL chunk.
        # Irrelevant chunks are dropped; any compressor failure falls back to
        # the uncompressed set, noted in the trace below.
        compression: dict[str, Any] = {"requested": self._compressor is not None}
        if self._compressor is not None:
            compression["provider"] = self._compressor.provider
            compressed, applied = await compress_with_fallback(
                self._compressor,
                query=query,
                chunks=chunks,
                timeout_seconds=self._settings.compression_timeout_seconds,
            )
            compression["applied"] = applied
            if applied:
                compression["dropped"] = len(chunks) - len(compressed)
                chunks = compressed

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
            json.dumps(
                {"stage": "used", **selection, "compression": compression, "chunks": used_record}
            ),
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
        self,
        *,
        query: str,
        k: int | None,
        principal: ServicePrincipal,
        conversation_id: str | None = None,
        filters: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        started = time.perf_counter()
        trace_id = principal.trace_id or str(uuid.uuid4())
        # SC-1 stage 1 runs before EVERYTHING (cache/retrieval/persistence).
        query = await self.screen(query, principal, trace_id)
        await self._assert_conversation_access(conversation_id, principal)

        prep = await self._prepare(query=query, k=k, filters=filters)
        chunks = prep["chunks"]
        retrieved_record = prep["retrieved_record"]
        used_chunks = prep["used_chunks"]
        used_record = prep["used_record"]
        selection = prep["selection"]

        # Flow B route stage (GN-4/GN-5): decision + breaker state recorded.
        client, routing = await self._route(query=query, chunks=chunks)
        logger.info("chat.routing %s", json.dumps(routing))

        token_usage: dict[str, int] | None = None
        degraded = False
        if not used_chunks:
            # Alternate B1: nothing retrieved (or nothing fits the budget) ->
            # grounded refusal, model not called, NO invented citations.
            answer_text = I_DONT_KNOW_ANSWER
            citations: list[dict[str, Any]] = []
        else:
            try:
                answer_text, token_usage = await client.complete(prep["messages"])
            except Exception as exc:
                # Alternate B2: provider error/timeout -> bounded degradation,
                # no silent failure; the trace records the failure path.
                # Retry/circuit-breaker/failover is GN-5 — deliberately absent.
                logger.warning(
                    "chat.generation_degraded model=%s reason=%s",
                    client.model,
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
                # GN-5: validate BEFORE returning — invalid output is replaced
                # by a safe response, never served (traced via the log/audit).
                problems = validate_output(
                    answer_text, citations=citations, chunk_count=len(used_chunks)
                )
                if problems:
                    logger.warning("chat.output_validation_failed %s", json.dumps(problems))
                    answer_text = VALIDATION_FAILED_ANSWER
                    citations = []
                    degraded = True

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
            model_used=client.model,
            conversation_id=conversation_id,
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
                "model": client.model,
                "provider": client.provider,
                "routing": {"tier": routing["tier"], "reason": routing["reason"]},
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
            "model": client.model,
            "tokenUsage": token_usage,
            "isIDontKnow": is_i_dont_know,
            "degraded": degraded,
            "latencyMs": latency_ms,
        }

    async def answer_stream(
        self,
        *,
        query: str,
        k: int | None,
        principal: ServicePrincipal,
        conversation_id: str | None = None,
        filters: dict[str, Any] | None = None,
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
        # SC-1 stage 1 runs before EVERYTHING (cache/retrieval/persistence).
        query = await self.screen(query, principal, trace_id)
        await self._assert_conversation_access(conversation_id, principal)

        prep = await self._prepare(query=query, k=k, filters=filters)
        used_chunks = prep["used_chunks"]

        # Flow B route stage (GN-4/GN-5): decision recorded before generation.
        client, routing = await self._route(query=query, chunks=prep["chunks"])
        logger.info("chat.routing %s", json.dumps(routing))

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

                async with aclosing(client.stream(prep["messages"])) as stream:
                    async for item in stream:
                        if isinstance(item, tuple):  # ("__usage__", usage) sentinel
                            token_usage = item[1]
                            continue
                        if ttft_ms is None:
                            ttft_ms = round((time.perf_counter() - started) * 1000, 3)
                            logger.info(
                                "chat.stream_ttft %s",
                                json.dumps({"ttftMs": ttft_ms, "model": client.model}),
                            )
                        parts.append(item)
                        yield _sse("token", {"delta": item})
                answer_text = "".join(parts).strip()
                if not answer_text:
                    raise RuntimeError(
                        f"Provider returned an empty stream (model={client.model})"
                    )
            except Exception as exc:
                # Alternate B2 mid-stream: bounded degradation on the final
                # frame; the trace records the failure path.
                logger.warning(
                    "chat.stream_degraded model=%s reason=%s", client.model, exc
                )
                answer_text = DEGRADED_ANSWER
                degraded = True

        if degraded or _is_i_dont_know(answer_text):
            citations: list[dict[str, Any]] = []
        else:
            citations = await self._build_citations(answer_text, used_chunks)
            # GN-5: validate before the FINAL FLUSH — the final frame carries
            # the safe replacement, flagged degraded (already-streamed deltas
            # cannot be unsent; the client renders the final frame's answer).
            problems = validate_output(
                answer_text, citations=citations, chunk_count=len(used_chunks)
            )
            if problems:
                logger.warning("chat.output_validation_failed %s", json.dumps(problems))
                answer_text = VALIDATION_FAILED_ANSWER
                citations = []
                degraded = True

        conversation_id, message_id = await self._repository.persist_turn(
            user_id=principal.sub,
            query=query,
            answer=answer_text,
            citations=citations,
            retrieved_chunks=prep["retrieved_record"],
            used_chunks=prep["used_record"],
            token_usage=token_usage,
            trace_id=trace_id,
            model_used=client.model,
            conversation_id=conversation_id,
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
                "model": client.model,
                "provider": client.provider,
                "routing": {"tier": routing["tier"], "reason": routing["reason"]},
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
                "model": client.model,
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


def get_chat_client(request: Request) -> "ModelRouter | SingleClientRouter | ChatModelClient":
    """Dependency seam so tests can override the provider (never call real
    providers in tests). One router per app — reused across requests. The
    name is kept from GN-1; since GN-4 it returns the ModelRouter."""
    client = getattr(request.app.state, "chat_client", None)
    if client is None:
        settings = request.app.state.settings
        client = ReliabilityRouter(
            build_model_router(settings),
            breaker=CircuitBreaker(settings),
            settings=settings,
        )
        request.app.state.chat_client = client
    return client


class ChatRequest(BaseModel):
    query: str = Field(min_length=1, max_length=4000)
    k: int | None = Field(default=None, ge=1, le=50)
    # CH-3: continue an existing conversation (must be owned by the caller).
    conversation_id: str | None = Field(default=None, alias="conversationId")
    # RT-4: restrict retrieval to a corpus subset (never an unfiltered fallback).
    filters: RetrievalFilters | None = Field(default=None)

    model_config = {"populate_by_name": True}


def get_compressor(request: Request) -> "Compressor | None":
    """RT-7 seam: config-built compressor (None when COMPRESSION_ENABLED off).
    The embeddings filter reuses the pinned per-app EmbeddingsClient; the LLM
    extractor uses the cheap-fast OpenAI client."""
    settings = request.app.state.settings
    if not settings.compression_enabled:
        return None
    from app.ingest import get_embeddings_client
    from app.providers import OpenAIChatClient

    llm_client = OpenAIChatClient(settings) if settings.compression_provider == "llm" else None
    return build_compressor(
        settings, embeddings=get_embeddings_client(request), llm_client=llm_client
    )


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
        compressor=get_compressor(request),
    )
    try:
        return await service.answer(
            query=payload.query,
            k=payload.k,
            principal=principal,
            conversation_id=payload.conversation_id,
            filters=payload.filters.to_query_filters() if payload.filters else None,
        )
    except QueryBlockedError as exc:
        from fastapi import HTTPException

        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ConversationAccessError as exc:
        from fastapi import HTTPException

        raise HTTPException(
            status_code=403 if exc.found else 404,
            detail="You do not have access to that conversation"
            if exc.found
            else "Conversation not found",
        ) from exc


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
        compressor=get_compressor(request),
    )
    # Screening (SC-1) and ownership are checked BEFORE the stream starts so
    # a refusal is a clean 400/403/404 response, not a broken SSE stream.
    try:
        screened_query = await service.screen(
            payload.query, principal, principal.trace_id or str(uuid.uuid4())
        )
        await service._assert_conversation_access(payload.conversation_id, principal)
    except QueryBlockedError as exc:
        from fastapi import HTTPException

        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ConversationAccessError as exc:
        from fastapi import HTTPException

        raise HTTPException(
            status_code=403 if exc.found else 404,
            detail="You do not have access to that conversation"
            if exc.found
            else "Conversation not found",
        ) from exc

    return StreamingResponse(
        service.answer_stream(
            query=screened_query,
            k=payload.k,
            principal=principal,
            conversation_id=payload.conversation_id,
            filters=payload.filters.to_query_filters() if payload.filters else None,
        ),
        media_type="text/event-stream",
        headers={"cache-control": "no-cache", "x-accel-buffering": "no"},
    )
