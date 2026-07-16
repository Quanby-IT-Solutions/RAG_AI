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

import asyncio
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
from app.costs import compute_cost_usd
from app.providers import (
    ChatModelClient,
    ModelRouter,
    OpenAIChatClient,
    SingleClientRouter,
    build_model_router,
)
from app.rate_limit import rate_limit
from app.reliability import CircuitBreaker, ReliabilityRouter, validate_output
from app.response_cache import ResponseCache, build_response_cache
from app.retrieval import RetrievalFilters, RetrievalService, get_retrieval_service
from app.security import ServicePrincipal, verify_service_jwt
from app.security_screen import QueryBlockedError, screen_query
from app.single_flight import SingleFlight, SingleFlightError
from app.tracing import stage, traced

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

# CE-4 (Flow H): appended to the system prompt for the ONE regeneration after
# a failed groundedness check — stricter grounding, honest refusal otherwise.
_STRICT_GROUNDING_SUFFIX = (
    " STRICT MODE: Only state facts that appear in the context blocks. If the "
    "context does not fully support an answer, reply exactly: \"I don't know.\""
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


def _sharable_entry(payload: dict[str, Any]) -> dict[str, Any]:
    """CP-3: the cross-request-sharable slice of an answer payload (no
    conversation/message ids — each follower persists its own turn)."""
    return {
        key: payload[key]
        for key in ("answer", "citations", "model", "isIDontKnow", "degraded")
        if key in payload
    }


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
        response_cache: "ResponseCache | None" = None,
        agentic: Any = None,
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
        # CP-1: Redis response/semantic cache (None when off / unit tests).
        self._response_cache = response_cache
        # CE-3 (Flow H): AgenticRetriever when RAG_MODE=agentic — replaces the
        # single-shot retrieve INSIDE the cache-miss branch only; None keeps
        # the linear path byte-identical.
        self._agentic = agentic

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

    async def _serve_cached(
        self,
        *,
        cached: dict[str, Any],
        query: str,
        principal: ServicePrincipal,
        conversation_id: str | None,
        trace_id: str,
        started: float,
    ) -> dict[str, Any]:
        """CP-1 hit path: the cached grounded answer returns WITHOUT any
        retrieval or LLM call; the turn is still persisted (the user asked)
        and the request is audited + traced with cache_hit."""
        logger.info(
            "chat.cache %s",
            json.dumps(
                {
                    "cache_hit": True,
                    "layer": cached.get("cacheLayer"),
                    "similarity": cached.get("cacheSimilarity"),
                }
            ),
        )
        conversation_id, message_id = await self._repository.persist_turn(
            user_id=principal.sub,
            query=query,
            answer=cached["answer"],
            citations=cached.get("citations", []),
            retrieved_chunks=[],
            used_chunks=[],
            token_usage=None,
            trace_id=trace_id,
            model_used=cached.get("model", "cache"),
            conversation_id=conversation_id,
        )
        await self._repository.write_audit(
            actor_id=principal.sub,
            actor_type=principal.actor_type,
            action=AUDIT_ACTION_CHAT,
            resource_type="conversation",
            resource_id=conversation_id,
            before=None,
            after={
                "messageId": message_id,
                "model": cached.get("model", "cache"),
                "cacheHit": True,
                "cacheLayer": cached.get("cacheLayer"),
                "traceId": trace_id,
            },
        )
        return {
            "answer": cached["answer"],
            "citations": cached.get("citations", []),
            "conversationId": conversation_id,
            "messageId": message_id,
            "traceId": trace_id,
            "model": cached.get("model", "cache"),
            "tokenUsage": None,
            # CP-1 entries are never idk/degraded; CP-3 shared results can be.
            "isIDontKnow": bool(cached.get("isIDontKnow", False)),
            "degraded": bool(cached.get("degraded", False)),
            "cached": True,
            "latencyMs": round((time.perf_counter() - started) * 1000, 3),
        }

    async def screen(self, query: str, principal: ServicePrincipal, trace_id: str) -> str:
        """SC-1, Flow B stage 1 (position contractual): sanitize + injection
        check + PII mask BEFORE cache/retrieval/prompt/persistence/logs.
        Blocked queries are audited then raised; otherwise the MASKED query
        is the only form allowed downstream."""
        async with stage("security.screen") as record:
            result = screen_query(query, self._settings)
            record(
                blocked=result.blocked,
                injection=result.injection,
                piiTypes=result.pii_types,
            )
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

    def _strict_messages(self, messages: list[dict[str, str]]) -> list[dict[str, str]]:
        """CE-4: the regeneration prompt = original messages with the strict
        grounding suffix on the system turn."""
        return [
            {**m, "content": m["content"] + _STRICT_GROUNDING_SUFFIX}
            if m["role"] == "system"
            else m
            for m in messages
        ]

    async def _regenerate_strict(
        self, client: Any, messages: list[dict[str, str]]
    ) -> tuple[str, dict[str, int] | None] | None:
        """CE-4: the ONE regeneration attempt; None on any failure (the
        caller ships the draft — Flow H fail-open)."""
        try:
            async with stage("model.regenerate", model=client.model) as record:
                answer, usage = await client.complete(self._strict_messages(messages))
                record(tokenUsage=usage)
                return answer, usage
        except Exception:
            logger.warning("chat.regenerate_failed", exc_info=True)
            return None

    async def _prepare(
        self,
        *,
        query: str,
        k: int | None,
        filters: dict[str, Any] | None = None,
        on_status: Any = None,
    ) -> dict[str, Any]:
        """Shared Flow B steps 3-5 for both response modes: hybrid retrieve
        (RT-1/RT-2), hard token budget (GN-2, rank-then-select), grounded
        prompt assembly. Returns everything the generate stage needs."""
        # CE-3 (Flow H): in agentic mode the grade/rewrite loop produces the
        # retrieval set; ANY loop failure falls through to the linear
        # single-shot retrieve — never an error, never a lost answer.
        agentic_trace: dict[str, Any] | None = None
        chunks: list[dict[str, Any]] | None = None
        if self._agentic is not None:
            try:
                chunks, agentic_trace = await self._agentic.retrieve(
                    query, k=k, filters=filters, on_status=on_status
                )
            except Exception:
                logger.warning("agentic.loop_degraded_to_linear", exc_info=True)
                agentic_trace = None
                chunks = None
        if chunks is None:
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
            async with stage("compression", chunkCount=len(chunks)) as record:
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
                record(**compression)

        # Budget resolved per request so a config change applies without
        # restart; the order (rank -> select) is contractual per §5.3.
        budget = current_context_budget(self._settings)
        async with stage("budget.selection", budget=budget, retrieved=len(chunks)) as record:
            used_chunks, selection = select_within_budget(chunks, budget=budget)
            record(
                usedCount=len(used_chunks),
                contextTokens=selection["contextTokens"],
                # OB-1: retrieved vs used ids recorded SEPARATELY on the span.
                retrievedIds=[chunk["id"] for chunk in chunks],
                usedIds=[chunk["id"] for chunk in used_chunks],
            )
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
            # CE-3: loop trace (None in linear mode — audit shape unchanged).
            "agentic": agentic_trace,
        }

    @traced(name="rag.chat")
    async def _grounding_gate(
        self,
        *,
        query: str,
        answer_text: str,
        citations: list[dict[str, Any]],
        used_chunks: list[dict[str, Any]],
        client: Any,
        messages: list[dict[str, str]],
        on_regenerating: Any = None,
    ) -> tuple[str, list[dict[str, Any]], dict[str, Any]]:
        """CE-4 (Flow H second half): groundedness verdict → ONE strict
        regeneration → re-verdict → honest "I don't know". The checker is an
        enhancer, never an availability gate: checker/regeneration failures
        ship the draft with a trace marker. Returns (answer, citations,
        marker)."""
        marker: dict[str, Any] = {"grounded": None, "regenerated": False, "outcome": "draft"}
        verdict = await self._agentic.groundedness_verdict(
            question=query, answer=answer_text, chunks=used_chunks
        )
        if verdict is None:
            marker["outcome"] = "checker-failed"
            return answer_text, citations, marker
        marker["grounded"] = verdict
        if verdict:
            return answer_text, citations, marker
        if on_regenerating is not None:
            await on_regenerating()
        marker["regenerated"] = True
        regenerated = await self._regenerate_strict(client, messages)
        if regenerated is None:
            marker["outcome"] = "regenerate-failed-draft-kept"
            return answer_text, citations, marker
        answer2, usage2 = regenerated
        marker["regenTokenUsage"] = usage2
        if _is_i_dont_know(answer2):
            marker["outcome"] = "idk"
            return I_DONT_KNOW_ANSWER, [], marker
        verdict2 = await self._agentic.groundedness_verdict(
            question=query, answer=answer2, chunks=used_chunks
        )
        if verdict2 is False:
            # Second failure: NEVER the ungrounded draft (AC).
            marker["outcome"] = "idk"
            return I_DONT_KNOW_ANSWER, [], marker
        citations2 = await self._build_citations(answer2, used_chunks)
        problems = validate_output(
            answer2, citations=citations2, chunk_count=len(used_chunks)
        )
        if problems:
            logger.warning("chat.regenerated_validation_failed %s", json.dumps(problems))
            marker["outcome"] = "idk"
            return I_DONT_KNOW_ANSWER, [], marker
        marker["outcome"] = "regenerated"
        return answer2, citations2, marker

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

        # ── CP-1 cache stage (AFTER SC-1, BEFORE retrieval/LLM — contractual).
        if self._response_cache is not None:
            async with stage("cache.lookup") as record:
                cached = await self._response_cache.lookup(query=query, filters=filters)
                record(
                    hit=cached is not None,
                    layer=cached.get("cacheLayer") if cached else None,
                )
            if cached is not None:
                return await self._serve_cached(
                    cached=cached,
                    query=query,
                    principal=principal,
                    conversation_id=conversation_id,
                    trace_id=trace_id,
                    started=started,
                )

        # ── CP-3 single-flight stage (Flow C): concurrent identical misses
        # compute exactly once across replicas; followers reuse the leader's
        # result (their own turn is still persisted + audited). Any
        # coalescing failure degrades to an uncoalesced computation.
        flight = await self._single_flight()
        if flight is not None:
            holder: dict[str, Any] = {}

            async def compute() -> dict[str, Any]:
                payload = await self._generate(
                    query=query,
                    k=k,
                    principal=principal,
                    conversation_id=conversation_id,
                    trace_id=trace_id,
                    started=started,
                    filters=filters,
                )
                holder["payload"] = payload
                return _sharable_entry(payload)

            try:
                entry, role = await flight.run(
                    self._response_cache.flight_key(query, filters), compute
                )
            except SingleFlightError:
                logger.warning("single_flight.degraded_to_uncoalesced", exc_info=True)
            else:
                if "payload" in holder:
                    return holder["payload"]  # this request was the leader
                return await self._serve_cached(
                    cached={**entry, "cacheLayer": "single-flight"},
                    query=query,
                    principal=principal,
                    conversation_id=conversation_id,
                    trace_id=trace_id,
                    started=started,
                )

        return await self._generate(
            query=query,
            k=k,
            principal=principal,
            conversation_id=conversation_id,
            trace_id=trace_id,
            started=started,
            filters=filters,
        )

    async def _single_flight(self) -> "SingleFlight | None":
        """CP-3 seam: coalescing needs the shared cache's Redis + the toggle."""
        if self._response_cache is None or not self._settings.single_flight_enabled:
            return None
        try:
            return SingleFlight(self._settings, await self._response_cache.redis())
        except Exception:
            logger.warning("single_flight.unavailable", exc_info=True)
            return None

    async def _generate(
        self,
        *,
        query: str,
        k: int | None,
        principal: ServicePrincipal,
        conversation_id: str | None,
        trace_id: str,
        started: float,
        filters: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """The uncoalesced miss path: retrieve → budget → route → generate →
        validate → persist → audit → (CP-1 store)."""
        prep = await self._prepare(query=query, k=k, filters=filters)
        chunks = prep["chunks"]
        retrieved_record = prep["retrieved_record"]
        used_chunks = prep["used_chunks"]
        used_record = prep["used_record"]
        selection = prep["selection"]

        # Flow B route stage (GN-4/GN-5): decision + breaker state recorded.
        async with stage("routing") as record:
            client, routing = await self._route(query=query, chunks=chunks)
            record(**{k: routing.get(k) for k in ("tier", "model", "provider", "reason")})
        logger.info("chat.routing %s", json.dumps(routing))

        token_usage: dict[str, int] | None = None
        cost_usd: float | None = None
        degraded = False
        if not used_chunks:
            # Alternate B1: nothing retrieved (or nothing fits the budget) ->
            # grounded refusal, model not called, NO invented citations.
            answer_text = I_DONT_KNOW_ANSWER
            citations: list[dict[str, Any]] = []
        else:
            try:
                async with stage(
                    "model.call", model=client.model, provider=client.provider
                ) as record:
                    answer_text, token_usage = await client.complete(prep["messages"])
                    cost_usd = compute_cost_usd(
                        self._settings, model=client.model, token_usage=token_usage
                    )
                    record(tokenUsage=token_usage, costUsd=cost_usd)
                    from app.metrics import observe_tokens

                    observe_tokens(token_usage)
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

        # ── CE-4 groundedness gate (agentic mode only, Flow H second half).
        grounding: dict[str, Any] | None = None
        if (
            self._agentic is not None
            and used_chunks
            and not degraded
            and not _is_i_dont_know(answer_text)
        ):
            try:
                async with stage("grounding.gate") as record:
                    answer_text, citations, grounding = await self._grounding_gate(
                        query=query,
                        answer_text=answer_text,
                        citations=citations,
                        used_chunks=used_chunks,
                        client=client,
                        messages=prep["messages"],
                    )
                    record(
                        **{k_: grounding[k_] for k_ in ("grounded", "regenerated", "outcome")}
                    )
            except Exception:
                # Flow H: the gate is an enhancer — a broken gate ships the
                # draft, never an error.
                logger.warning("chat.grounding_gate_degraded", exc_info=True)
                grounding = None

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
                "costUsd": cost_usd,
                "traceId": trace_id,
                # CE-3: hop summary only when the loop ran (linear audit
                # payloads keep their exact pre-CE-3 shape).
                **(
                    {
                        "agentic": {
                            "hopCount": len(prep["agentic"]["hops"]),
                            "rewrites": prep["agentic"]["rewrites"],
                            "sufficient": prep["agentic"]["sufficient"],
                        }
                    }
                    if prep.get("agentic")
                    else {}
                ),
                # CE-4: groundedness outcome only when the gate ran.
                **(
                    {
                        "grounding": {
                            k_: grounding[k_]
                            for k_ in ("grounded", "regenerated", "outcome")
                        }
                    }
                    if grounding
                    else {}
                ),
            },
        )

        from app.metrics import CHAT_MODE_LATENCY

        CHAT_MODE_LATENCY.labels(
            mode="agentic" if self._agentic is not None else "linear"
        ).observe(latency_ms / 1000.0)

        payload = {
            "answer": answer_text,
            "citations": citations,
            "conversationId": conversation_id,
            "messageId": message_id,
            "traceId": trace_id,
            "model": client.model,
            "tokenUsage": token_usage,
            "isIDontKnow": is_i_dont_know,
            "degraded": degraded,
            "cached": False,
            "costUsd": cost_usd,
            "latencyMs": latency_ms,
        }
        # CP-1: successful grounded answers become cache entries (the cache
        # itself refuses I-don't-know/degraded payloads).
        if self._response_cache is not None:
            await self._response_cache.store(query=query, filters=filters, payload=payload)

        # The traced return value lands on the rag.chat span — the one trace
        # per request records model, tokens, and latency (Tech Plan §8).
        return payload

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

        # ── CP-1 cache stage: a hit replays as one token event + final.
        if self._response_cache is not None:
            cached = await self._response_cache.lookup(query=query, filters=filters)
            if cached is not None:
                payload = await self._serve_cached(
                    cached=cached,
                    query=query,
                    principal=principal,
                    conversation_id=conversation_id,
                    trace_id=trace_id,
                    started=started,
                )
                yield _sse("token", {"delta": payload["answer"]})
                yield _sse(
                    "final",
                    {**payload, "ttftMs": payload["latencyMs"]},
                )
                return

        # ── CP-3 single-flight (Flow C, v1 streaming rule): a follower whose
        # key is already being computed waits for the COMPLETED answer and
        # replays it as one token + final; the leader streams normally and
        # publishes its result at the final flush.
        flight = await self._single_flight()
        flight_owner: str | None = None
        flight_key = ""
        if flight is not None:
            flight_key = self._response_cache.flight_key(query, filters)
            flight_owner = uuid.uuid4().hex
            if not await flight.try_acquire(flight_key, flight_owner):
                flight_owner = None
                try:
                    outcome = await flight.await_result(flight_key)
                except SingleFlightError:
                    outcome = None  # wait failed -> stream uncoalesced
                if outcome is not None and "result" in outcome:
                    payload = await self._serve_cached(
                        cached={**outcome["result"], "cacheLayer": "single-flight"},
                        query=query,
                        principal=principal,
                        conversation_id=conversation_id,
                        trace_id=trace_id,
                        started=started,
                    )
                    yield _sse("token", {"delta": payload["answer"]})
                    yield _sse("final", {**payload, "ttftMs": payload["latencyMs"]})
                    return

        # ── CE-4: in agentic mode, loop-status events stream out WHILE the
        # retrieve/grade/rewrite loop runs — perceived progress instead of
        # silence before the first token. Linear mode: SSE contract unchanged.
        if self._agentic is not None:
            status_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

            async def on_status(stage_name: str, detail: dict[str, Any]) -> None:
                status_queue.put_nowait({"stage": stage_name, **detail})

            prep_task = asyncio.create_task(
                self._prepare(query=query, k=k, filters=filters, on_status=on_status)
            )
            while not prep_task.done() or not status_queue.empty():
                if not status_queue.empty():
                    yield _sse("status", status_queue.get_nowait())
                    continue
                getter = asyncio.create_task(status_queue.get())
                done, _ = await asyncio.wait(
                    {prep_task, getter}, return_when=asyncio.FIRST_COMPLETED
                )
                if getter in done:
                    yield _sse("status", getter.result())
                else:
                    getter.cancel()
            prep = await prep_task  # re-raises any _prepare failure
        else:
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
                            from app.metrics import CHAT_TTFT

                            CHAT_TTFT.observe(ttft_ms / 1000.0)
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

        # ── CE-4 groundedness gate before the final flush (agentic only).
        # Like GN-5 validation, the FINAL frame carries any replacement; a
        # `regenerating` status explains the pause between stream end and
        # final (already-streamed deltas cannot be unsent).
        grounding: dict[str, Any] | None = None
        if (
            self._agentic is not None
            and used_chunks
            and not degraded
            and not _is_i_dont_know(answer_text)
        ):
            regen_signal: list[bool] = []

            async def on_regenerating() -> None:
                regen_signal.append(True)

            verdict_started = time.perf_counter()
            gate_task = asyncio.create_task(
                self._grounding_gate(
                    query=query,
                    answer_text=answer_text,
                    citations=citations,
                    used_chunks=used_chunks,
                    client=client,
                    messages=prep["messages"],
                    on_regenerating=on_regenerating,
                )
            )
            emitted_regen = False
            while not gate_task.done():
                if regen_signal and not emitted_regen:
                    emitted_regen = True
                    yield _sse("status", {"stage": "regenerating"})
                await asyncio.sleep(0.05)
            if regen_signal and not emitted_regen:
                yield _sse("status", {"stage": "regenerating"})
            try:
                answer_text, citations, grounding = await gate_task
                logger.info(
                    "chat.grounding %s",
                    json.dumps(
                        {
                            **{
                                k_: grounding[k_]
                                for k_ in ("grounded", "regenerated", "outcome")
                            },
                            "durationMs": round(
                                (time.perf_counter() - verdict_started) * 1000, 3
                            ),
                        }
                    ),
                )
            except Exception:
                # Flow H: a broken gate ships the streamed draft, never an error.
                logger.warning("chat.grounding_gate_degraded", exc_info=True)
                grounding = None

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

        # CP-3 leader: publish the completed answer for any waiters, then
        # release the lock (best-effort — waiters also survive via lock TTL).
        if flight is not None and flight_owner is not None:
            try:
                await flight.publish(
                    flight_key,
                    {
                        "result": {
                            "answer": answer_text,
                            "citations": citations,
                            "model": client.model,
                            "isIDontKnow": is_i_dont_know,
                            "degraded": degraded,
                        }
                    },
                )
            except Exception:
                logger.warning("single_flight.stream_publish_failed", exc_info=True)
            await flight.release(flight_key, flight_owner)
            flight_owner = None

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
                # CE-3: hop summary only when the loop ran.
                **(
                    {
                        "agentic": {
                            "hopCount": len(prep["agentic"]["hops"]),
                            "rewrites": prep["agentic"]["rewrites"],
                            "sufficient": prep["agentic"]["sufficient"],
                        }
                    }
                    if prep.get("agentic")
                    else {}
                ),
                # CE-4: groundedness outcome only when the gate ran.
                **(
                    {
                        "grounding": {
                            k_: grounding[k_]
                            for k_ in ("grounded", "regenerated", "outcome")
                        }
                    }
                    if grounding
                    else {}
                ),
            },
        )

        from app.metrics import CHAT_MODE_LATENCY

        CHAT_MODE_LATENCY.labels(
            mode="agentic" if self._agentic is not None else "linear"
        ).observe(latency_ms / 1000.0)

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


def get_provider_bucket(request: Request) -> Any:
    """CP-4 seam: ONE shared bucket per app (None when disabled)."""
    if not hasattr(request.app.state, "provider_bucket"):
        from app.provider_bucket import build_provider_bucket

        request.app.state.provider_bucket = build_provider_bucket(
            request.app.state.settings
        )
    return request.app.state.provider_bucket


def get_chat_client(request: Request) -> "ModelRouter | SingleClientRouter | ChatModelClient":
    """Dependency seam so tests can override the provider (never call real
    providers in tests). One router per app — reused across requests. The
    name is kept from GN-1; since GN-4 it returns the ModelRouter."""
    client = getattr(request.app.state, "chat_client", None)
    if client is None:
        settings = request.app.state.settings
        client = ReliabilityRouter(
            build_model_router(settings, bucket=get_provider_bucket(request)),
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


def get_response_cache(request: Request) -> "ResponseCache | None":
    """CP-1 seam: shared per-app cache (None when CACHE_ENABLED is off).
    Reuses the pinned per-app EmbeddingsClient for the semantic layer."""
    settings = request.app.state.settings
    if not settings.cache_enabled:
        return None
    from app.ingest import get_embeddings_client

    return build_response_cache(settings, get_embeddings_client(request))


def get_agentic(request: Request, retrieval: RetrievalService) -> Any:
    """CE-3 seam: AgenticRetriever when RAG_MODE=agentic, else None (linear
    path untouched). The grader/rewriter client is shared per app and CP-4
    bucket-gated; the retriever wraps the per-request RetrievalService."""
    settings = request.app.state.settings
    if settings.rag_mode != "agentic":
        return None
    from app.agentic import AgenticRetriever, build_agentic_client

    client = getattr(request.app.state, "agentic_client", None)
    if client is None:
        client = build_agentic_client(settings, bucket=get_provider_bucket(request))
        request.app.state.agentic_client = client
    return AgenticRetriever(settings, retrieval, client)


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


@router.post("/chat", dependencies=[Depends(rate_limit("rate_limit_chat"))])
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
        response_cache=get_response_cache(request),
        agentic=get_agentic(request, retrieval),
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


@router.post("/chat/stream", dependencies=[Depends(rate_limit("rate_limit_chat"))])
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
        response_cache=get_response_cache(request),
        agentic=get_agentic(request, retrieval),
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
