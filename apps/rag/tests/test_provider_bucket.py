"""CP-4 shared provider-bucket tests (Flow C)."""

import asyncio

import pytest

from app.provider_bucket import (
    ProviderBucket,
    ProviderBucketExhausted,
    ThrottledChatClient,
    ThrottledEmbeddingsClient,
    build_provider_bucket,
)
from tests.conftest import REQUIRED_ENV
from tests.test_response_cache import FakeRedis


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


def make_bucket(config, redis=None) -> tuple[ProviderBucket, FakeRedis]:
    redis = redis or FakeRedis()
    return ProviderBucket(config, redis), redis


# ------------------------------------------------------------------ capping
async def test_calls_beyond_the_window_budget_wait_or_shed(settings):
    # 6 RPM over 10s windows -> 1 request per window.
    config = settings(
        PROVIDER_RPM_OPENAI="6",
        BUCKET_WINDOW_SECONDS="10",
        BUCKET_WAIT_TIMEOUT_SECONDS="0.05",
    )
    bucket, redis = make_bucket(config)

    await bucket.acquire("openai")  # fits the window
    with pytest.raises(ProviderBucketExhausted):
        await bucket.acquire("openai")  # window full, wait budget too small

    assert redis.counters["bucket:metrics:acquired:openai"] == 1
    assert redis.counters["bucket:metrics:shed:openai"] == 1


async def test_replicas_share_one_budget(settings):
    config = settings(
        PROVIDER_RPM_OPENAI="6",
        BUCKET_WINDOW_SECONDS="10",
        BUCKET_WAIT_TIMEOUT_SECONDS="0.05",
    )
    redis = FakeRedis()
    replica_a, _ = make_bucket(config, redis)
    replica_b, _ = make_bucket(config, redis)

    await replica_a.acquire("openai")
    # The OTHER replica sees the same exhausted window (shared Redis INCR).
    with pytest.raises(ProviderBucketExhausted):
        await replica_b.acquire("openai")


async def test_providers_have_separate_budgets(settings):
    config = settings(
        PROVIDER_RPM_OPENAI="6",
        PROVIDER_RPM_ANTHROPIC="6",
        BUCKET_WINDOW_SECONDS="10",
        BUCKET_WAIT_TIMEOUT_SECONDS="0.05",
    )
    bucket, _ = make_bucket(config)

    await bucket.acquire("openai")
    await bucket.acquire("anthropic")  # unaffected by openai's window


# ------------------------------------------------------------ 429 adaptation
async def test_429_shrinks_the_bucket_temporarily(settings):
    # 12 RPM over 10s windows -> 2/window normally, 1/window at factor 0.5.
    config = settings(
        PROVIDER_RPM_OPENAI="12",
        BUCKET_WINDOW_SECONDS="10",
        BUCKET_WAIT_TIMEOUT_SECONDS="0.05",
        BUCKET_SHRINK_FACTOR="0.5",
    )
    bucket, redis = make_bucket(config)

    await bucket.on_429("openai")
    assert redis.counters["bucket:metrics:429:openai"] == 1
    assert redis.store["bucket:shrink:openai"] == "0.5"

    await bucket.acquire("openai")
    with pytest.raises(ProviderBucketExhausted):
        await bucket.acquire("openai")  # shrunk window admits only one


# ----------------------------------------------------------------- wrappers
class FakeChat:
    model = "gpt-4o-mini"
    provider = "openai"

    def __init__(self, error: Exception | None = None):
        self.error = error
        self.calls = 0

    async def complete(self, messages):
        self.calls += 1
        if self.error:
            raise self.error
        return "answer", None

    async def stream(self, messages):
        self.calls += 1
        if self.error:
            raise self.error
        yield "tok"


class Fake429(Exception):
    status_code = 429


async def test_chat_wrapper_acquires_before_the_call_and_reports_429(settings):
    config = settings(
        PROVIDER_RPM_OPENAI="6",
        BUCKET_WINDOW_SECONDS="10",
        BUCKET_WAIT_TIMEOUT_SECONDS="0.05",
    )
    bucket, redis = make_bucket(config)

    ok = ThrottledChatClient(FakeChat(), bucket)
    assert await ok.complete([]) == ("answer", None)
    assert redis.counters["bucket:metrics:acquired:openai"] == 1

    redis2 = FakeRedis()
    bucket2 = ProviderBucket(config, redis2)
    failing = ThrottledChatClient(FakeChat(error=Fake429("too many")), bucket2)
    with pytest.raises(Fake429):
        await failing.complete([])
    assert redis2.counters["bucket:metrics:429:openai"] == 1


async def test_stream_wrapper_takes_one_token_per_stream(settings):
    config = settings(
        PROVIDER_RPM_OPENAI="6",
        BUCKET_WINDOW_SECONDS="10",
        BUCKET_WAIT_TIMEOUT_SECONDS="0.05",
    )
    bucket, redis = make_bucket(config)
    client = ThrottledChatClient(FakeChat(), bucket)

    deltas = [item async for item in client.stream([])]

    assert deltas == ["tok"]
    assert redis.counters["bucket:metrics:acquired:openai"] == 1


async def test_embedding_batches_draw_from_the_same_budget(settings):
    config = settings(
        PROVIDER_RPM_OPENAI="6",
        BUCKET_WINDOW_SECONDS="10",
        BUCKET_WAIT_TIMEOUT_SECONDS="0.05",
    )
    bucket, _ = make_bucket(config)

    class FakeEmbeddings:
        model = "text-embedding-3-large"
        dimensions = 1536

        async def embed_texts(self, texts):
            return [[0.0] * 4 for _ in texts], 1

    embeddings = ThrottledEmbeddingsClient(FakeEmbeddings(), bucket)
    chat = ThrottledChatClient(FakeChat(), bucket)

    await embeddings.embed_texts(["a"])  # consumes the single window slot
    with pytest.raises(ProviderBucketExhausted):
        await chat.complete([])  # SAME budget as chat (AC)


def test_build_provider_bucket_respects_the_toggle(settings):
    config = settings(PROVIDER_BUCKET_ENABLED="false")
    assert build_provider_bucket(config, FakeRedis()) is None
    config = settings(PROVIDER_BUCKET_ENABLED="true")
    assert build_provider_bucket(config, FakeRedis()) is not None


def test_bucket_uses_watsonx_specific_rpm(settings):
    config = settings(PROVIDER_RPM_WATSONX="17")
    assert ProviderBucket(config, FakeRedis())._rpm("watsonx") == 17


def test_bucket_rejects_unknown_provider(settings):
    with pytest.raises(ValueError, match="unknown provider"):
        ProviderBucket(settings(), FakeRedis())._rpm("mystery")


async def test_waiting_caller_proceeds_in_the_next_window(settings):
    # 1-second windows so the waiter crosses a boundary quickly.
    config = settings(
        PROVIDER_RPM_OPENAI="60",
        BUCKET_WINDOW_SECONDS="1",
        BUCKET_WAIT_TIMEOUT_SECONDS="3",
    )
    bucket, redis = make_bucket(config)

    await bucket.acquire("openai")  # fills the 1/window budget
    started = asyncio.get_event_loop().time()
    await bucket.acquire("openai")  # waits into the next window
    waited = asyncio.get_event_loop().time() - started

    assert waited >= 0.005
    assert redis.counters["bucket:metrics:waited:openai"] == 1
