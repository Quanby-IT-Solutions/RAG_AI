"""GN-4 routing tests: length + retrieval-confidence heuristic, config-driven
thresholds and model ids, graceful cheap-only degradation without a strong
provider, the shared provider-client interface, and the Anthropic client's
message/usage mapping. No real providers are ever called."""

from types import SimpleNamespace

import pytest

from app.config import Settings
from app.providers import (
    AnthropicChatClient,
    ModelRouter,
    OpenAIChatClient,
    build_model_router,
)
from tests.conftest import REQUIRED_ENV


def make_settings(**overrides) -> Settings:
    return Settings(_env_file=None, **{**REQUIRED_ENV, **overrides})


class StubClient:
    def __init__(self, model: str, provider: str) -> None:
        self.model = model
        self.provider = provider


def chunk(similarity: float) -> dict:
    return {"id": "c", "document_id": "d", "content": "x", "similarity": similarity}


def make_router(settings: Settings, *, with_strong: bool = True) -> ModelRouter:
    return ModelRouter(
        settings,
        cheap=StubClient("gpt-4o-mini", "openai"),
        strong=StubClient("claude-opus-4-8", "anthropic") if with_strong else None,
    )


# ------------------------------------------------------------------ heuristic
def test_short_simple_query_routes_cheap_fast():
    router = make_router(make_settings())

    client, decision = router.route(query="What is the leave policy?", chunks=[chunk(0.9)])

    assert client.model == "gpt-4o-mini"
    assert decision["tier"] == "cheap-fast"
    assert decision["reason"] == "simple-query"
    assert decision["provider"] == "openai"


def test_long_query_routes_strong():
    router = make_router(make_settings())
    long_query = "Compare the policies and summarize the differences. " * 10

    client, decision = router.route(query=long_query, chunks=[chunk(0.9)])

    assert client.model == "claude-opus-4-8"
    assert decision["tier"] == "strong"
    assert decision["reason"] == "query-length"
    assert decision["provider"] == "anthropic"


def test_low_retrieval_confidence_routes_strong():
    router = make_router(make_settings())

    client, decision = router.route(query="short but hard", chunks=[chunk(0.05)])

    assert client.model == "claude-opus-4-8"
    assert decision["reason"] == "low-retrieval-confidence"


def test_empty_retrieval_does_not_trigger_low_confidence():
    """No chunks -> the B1 refusal path handles it; confidence doesn't apply."""
    router = make_router(make_settings())

    client, decision = router.route(query="short", chunks=[])

    assert decision["tier"] == "cheap-fast"
    assert decision["reason"] == "simple-query"


def test_thresholds_come_from_config_not_code():
    settings = make_settings(
        ROUTING_QUERY_LENGTH_THRESHOLD="10", ROUTING_MIN_TOP_SIMILARITY="0.99"
    )
    router = make_router(settings)

    _, by_length = router.route(query="12 characters", chunks=[chunk(1.0)])
    assert by_length["reason"] == "query-length"

    settings2 = make_settings(ROUTING_QUERY_LENGTH_THRESHOLD="500")
    router2 = make_router(settings2)
    _, decision2 = router2.route(query="12 characters", chunks=[chunk(0.9)])
    assert decision2["tier"] == "cheap-fast"


def test_strong_unavailable_degrades_to_cheap_and_records_why():
    router = make_router(make_settings(), with_strong=False)
    long_query = "x" * 500

    client, decision = router.route(query=long_query, chunks=[chunk(0.9)])

    assert client.model == "gpt-4o-mini"
    assert decision["tier"] == "cheap-fast"
    assert decision["reason"] == "strong-unavailable"


def test_decision_records_the_inputs_for_the_trace():
    router = make_router(make_settings())
    _, decision = router.route(query="hello", chunks=[chunk(0.5)])

    assert decision["queryLength"] == 5
    assert decision["topSimilarity"] == 0.5
    assert decision["thresholds"] == {"queryLength": 240, "minTopSimilarity": 0.2}


# --------------------------------------------------------------- assembly
def test_build_router_uses_anthropic_strong_when_key_present():
    settings = make_settings(ANTHROPIC_API_KEY="sk-ant-test")
    router = build_model_router(settings)

    assert isinstance(router.cheap, OpenAIChatClient)
    assert isinstance(router.strong, AnthropicChatClient)
    assert router.strong.model == "claude-opus-4-8"


def test_build_router_without_anthropic_key_has_no_strong_tier():
    router = build_model_router(make_settings())
    assert router.strong is None


def test_build_router_openai_strong_provider():
    settings = make_settings(STRONG_MODEL_PROVIDER="openai", STRONG_MODEL="gpt-4.1")
    router = build_model_router(settings)

    assert isinstance(router.strong, OpenAIChatClient)
    assert router.strong.model == "gpt-4.1"
    assert router.strong.provider == "openai"


# ------------------------------------------------- Anthropic client mapping
class FakeAnthropicMessages:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text="Grounded answer. [1]")],
            usage=SimpleNamespace(input_tokens=120, output_tokens=30),
        )


async def test_anthropic_client_maps_messages_and_usage():
    settings = make_settings(ANTHROPIC_API_KEY="sk-ant-test")
    fake = SimpleNamespace(messages=FakeAnthropicMessages())
    client = AnthropicChatClient(settings, client=fake)

    text, usage = await client.complete(
        [
            {"role": "system", "content": "You are grounded."},
            {"role": "user", "content": "Question?"},
        ]
    )

    assert text == "Grounded answer. [1]"
    # System entries map to the top-level `system` param (Messages API shape).
    call = fake.messages.calls[0]
    assert call["system"] == "You are grounded."
    assert call["messages"] == [{"role": "user", "content": "Question?"}]
    assert call["model"] == "claude-opus-4-8"
    assert call["max_tokens"] == settings.generation_max_tokens
    # input/output tokens map onto the prompt/completion usage shape.
    assert usage == {"promptTokens": 120, "completionTokens": 30, "totalTokens": 150}


async def test_anthropic_client_rejects_empty_completion():
    settings = make_settings(ANTHROPIC_API_KEY="sk-ant-test")

    class EmptyMessages(FakeAnthropicMessages):
        async def create(self, **kwargs):
            return SimpleNamespace(content=[], usage=None)

    client = AnthropicChatClient(settings, client=SimpleNamespace(messages=EmptyMessages()))
    with pytest.raises(RuntimeError, match="empty completion"):
        await client.complete([{"role": "user", "content": "q"}])
