"""Provider clients + model routing (GN-4, Flow B route stage).

ONE provider-client interface (`complete` / `stream` / `model`) fronts both
providers — this is the seam GN-5 wraps with retry/breaker/failover and CP-4
wraps with the shared token bucket. The ModelRouter picks the cheap-fast or
strong client per request from a length + retrieval-confidence heuristic
(PRD Annex A#14 resolution); thresholds and model ids are config, so routing
policy changes without code changes.

Streaming contract (shared with GN-3): `stream(messages)` yields text deltas,
then a final ("__usage__", token_usage) sentinel tuple; closing the generator
closes the provider stream (client disconnect -> no orphaned spend).
"""

import logging
from typing import TYPE_CHECKING, Any, Protocol

from app.config import Settings
from app.timeouts import with_hard_timeout

if TYPE_CHECKING:
    from anthropic import AsyncAnthropic
    from openai import AsyncOpenAI

logger = logging.getLogger(__name__)


class ChatModelClient(Protocol):
    """The single provider-client interface (GN-4/GN-5 seam)."""

    model: str
    provider: str

    async def complete(
        self, messages: list[dict[str, str]]
    ) -> tuple[str, dict[str, int] | None]: ...

    def stream(self, messages: list[dict[str, str]]) -> Any: ...


class OpenAIChatClient:
    """Chat completions against OpenAI (moved from generation.py, GN-1/GN-3)."""

    provider = "openai"

    def __init__(
        self, settings: Settings, *, model: str | None = None, client: "AsyncOpenAI | None" = None
    ) -> None:
        self._settings = settings
        self._client = client
        self.model = model or settings.generation_model

    def _openai(self) -> "AsyncOpenAI":
        if self._client is None:
            from openai import AsyncOpenAI

            self._client = AsyncOpenAI(api_key=self._settings.openai_api_key.get_secret_value())
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
        """Streamed completion (GN-3): yields text deltas, then a final
        ("__usage__", token_usage) sentinel tuple."""
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


class AnthropicChatClient:
    """Chat against Anthropic's Messages API behind the SAME interface.

    System entries in the shared messages shape map to the Messages API's
    top-level `system` parameter; usage maps input/output tokens onto the
    prompt/completion shape the rest of the pipeline records.
    """

    provider = "anthropic"

    def __init__(
        self,
        settings: Settings,
        *,
        model: str | None = None,
        client: "AsyncAnthropic | None" = None,
    ) -> None:
        self._settings = settings
        self._client = client
        self.model = model or settings.strong_model

    def _anthropic(self) -> "AsyncAnthropic":
        if self._client is None:
            from anthropic import AsyncAnthropic

            api_key = self._settings.anthropic_api_key
            assert api_key is not None
            self._client = AsyncAnthropic(api_key=api_key.get_secret_value())
        return self._client

    @staticmethod
    def _split_messages(
        messages: list[dict[str, str]],
    ) -> tuple[str, list[dict[str, str]]]:
        system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
        chat = [m for m in messages if m["role"] != "system"]
        return system, chat

    @staticmethod
    def _usage(usage: Any) -> dict[str, int] | None:
        if usage is None:
            return None
        return {
            "promptTokens": usage.input_tokens,
            "completionTokens": usage.output_tokens,
            "totalTokens": usage.input_tokens + usage.output_tokens,
        }

    async def complete(self, messages: list[dict[str, str]]) -> tuple[str, dict[str, int] | None]:
        system, chat = self._split_messages(messages)
        request_kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self._settings.generation_max_tokens,
            "messages": chat,
        }
        if system:
            request_kwargs["system"] = system
        response = await with_hard_timeout(
            self._anthropic().messages.create(**request_kwargs),
            seconds=self._settings.external_call_timeout_seconds,
            operation="anthropic.messages.create",
        )
        content = "".join(
            block.text for block in response.content if getattr(block, "type", "") == "text"
        ).strip()
        if not content:
            raise RuntimeError(f"Anthropic returned an empty completion (model={self.model})")
        return content, self._usage(getattr(response, "usage", None))

    async def stream(self, messages: list[dict[str, str]]) -> Any:
        """Streamed completion: same delta + ("__usage__", usage) contract.
        Opening the stream runs under the hard timeout (GN-5 timeout audit);
        the stream itself is unbounded by design (long answers)."""
        system, chat = self._split_messages(messages)
        request_kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self._settings.generation_max_tokens,
            "messages": chat,
        }
        if system:
            request_kwargs["system"] = system
        manager = self._anthropic().messages.stream(**request_kwargs)
        token_usage: dict[str, int] | None = None
        stream = await with_hard_timeout(
            manager.__aenter__(),
            seconds=self._settings.external_call_timeout_seconds,
            operation="anthropic.messages.stream",
        )
        try:
            async for delta in stream.text_stream:
                if delta:
                    yield delta
            final = await stream.get_final_message()
            token_usage = self._usage(getattr(final, "usage", None))
        finally:
            await manager.__aexit__(None, None, None)
        yield ("__usage__", token_usage)


class ModelRouter:
    """Cheap-fast vs strong routing (GN-4) behind the same call surface.

    Heuristic (config-driven): route STRONG when the query is long (length
    threshold) or retrieval confidence is low (top fused similarity below the
    floor — a hard question the cheap model will likely fumble). Everything
    else stays on cheap-fast. When no strong client is configured (e.g. no
    ANTHROPIC_API_KEY in this deployment), every request routes cheap and the
    decision records why — never a hard failure."""

    def __init__(
        self,
        settings: Settings,
        *,
        cheap: ChatModelClient,
        strong: ChatModelClient | None,
    ) -> None:
        self._settings = settings
        self.cheap = cheap
        self.strong = strong

    def route(
        self, *, query: str, chunks: list[dict[str, Any]]
    ) -> tuple[ChatModelClient, dict[str, Any]]:
        top_similarity = max(
            (c.get("similarity") or 0.0 for c in chunks),
            default=0.0,
        )
        long_query = len(query) >= self._settings.routing_query_length_threshold
        low_confidence = bool(chunks) and (
            top_similarity < self._settings.routing_min_top_similarity
        )
        wants_strong = long_query or low_confidence

        if wants_strong and self.strong is not None:
            client: ChatModelClient = self.strong
            tier = "strong"
            reason = "query-length" if long_query else "low-retrieval-confidence"
        elif wants_strong:
            client = self.cheap
            tier = "cheap-fast"
            reason = "strong-unavailable"
        else:
            client = self.cheap
            tier = "cheap-fast"
            reason = "simple-query"

        decision = {
            "tier": tier,
            "model": client.model,
            "provider": client.provider,
            "reason": reason,
            "queryLength": len(query),
            "topSimilarity": round(top_similarity, 4),
            "thresholds": {
                "queryLength": self._settings.routing_query_length_threshold,
                "minTopSimilarity": self._settings.routing_min_top_similarity,
            },
        }
        return client, decision


class SingleClientRouter:
    """Degenerate router around one client — keeps GenerationService working
    with a bare client (tests, single-provider deployments)."""

    def __init__(self, client: ChatModelClient) -> None:
        self.cheap = client
        self.strong = None

    def route(
        self, *, query: str, chunks: list[dict[str, Any]]
    ) -> tuple[ChatModelClient, dict[str, Any]]:
        return self.cheap, {
            "tier": "cheap-fast",
            "model": self.cheap.model,
            "provider": getattr(self.cheap, "provider", "openai"),
            "reason": "single-client",
        }


def build_model_router(settings: Settings, bucket: Any = None) -> ModelRouter:
    """Assemble the deployment's router from config (GN-4).

    Strong tier: Anthropic when STRONG_MODEL_PROVIDER=anthropic and an API
    key is present; OpenAI when =openai; absent otherwise (router degrades to
    cheap-only and records the reason per request).

    CP-4: with a ProviderBucket, every client is bucket-gated BENEATH the
    GN-5 reliability layer (order: bucket → breaker → provider call)."""
    cheap: ChatModelClient = OpenAIChatClient(settings)
    strong: ChatModelClient | None = None
    if settings.strong_model_provider == "anthropic" and settings.anthropic_api_key:
        strong = AnthropicChatClient(settings)
    elif settings.strong_model_provider == "openai":
        strong = OpenAIChatClient(settings, model=settings.strong_model)
    if bucket is not None:
        from app.provider_bucket import ThrottledChatClient

        cheap = ThrottledChatClient(cheap, bucket)
        if strong is not None:
            strong = ThrottledChatClient(strong, bucket)
    return ModelRouter(settings, cheap=cheap, strong=strong)
