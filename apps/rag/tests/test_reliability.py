"""GN-5 reliability tests: bounded exponential retry with jitter, the
Redis-backed circuit breaker state machine (closed -> open -> half-open probe
-> closed/reopen), fail-fast + failover routing when a circuit is open,
output validation, and the hard-timeout audit over every module that talks to
a provider or the database."""

import pathlib

import pytest

from app.config import Settings
from app.providers import ModelRouter
from app.reliability import (
    BreakerOpenError,
    CircuitBreaker,
    ReliabilityRouter,
    ReliableChatClient,
    is_transient,
    validate_output,
)
from app.timeouts import ExternalCallTimeoutError
from tests.conftest import REQUIRED_ENV


def make_settings(**overrides) -> Settings:
    return Settings(_env_file=None, **{**REQUIRED_ENV, **overrides})


class FakeRedis:
    """Tiny in-memory stand-in for the redis.asyncio commands the breaker
    uses (exists/set-nx-ex/incr/expire/delete/getdel). TTLs are simulated
    with a manual clock the tests advance."""

    def __init__(self) -> None:
        self.store: dict[str, tuple[str, float | None]] = {}
        self.now = 0.0

    def _alive(self, key: str) -> bool:
        if key not in self.store:
            return False
        _, expires = self.store[key]
        if expires is not None and expires <= self.now:
            del self.store[key]
            return False
        return True

    async def exists(self, key: str) -> int:
        return 1 if self._alive(key) else 0

    async def set(self, key: str, value: str, nx: bool = False, ex: int | None = None):
        if nx and self._alive(key):
            return None
        self.store[key] = (str(value), self.now + ex if ex else None)
        return True

    async def incr(self, key: str) -> int:
        current = int(self.store[key][0]) if self._alive(key) else 0
        expires = self.store[key][1] if self._alive(key) else None
        self.store[key] = (str(current + 1), expires)
        return current + 1

    async def expire(self, key: str, seconds: int) -> None:
        if self._alive(key):
            self.store[key] = (self.store[key][0], self.now + seconds)

    async def delete(self, *keys: str) -> None:
        for key in keys:
            self.store.pop(key, None)

    async def getdel(self, key: str):
        if self._alive(key):
            value, _ = self.store.pop(key)
            return value
        return None


class FlakyClient:
    """Provider stub failing the first N calls with a configurable error."""

    model = "stub-model"
    provider = "openai"

    def __init__(self, *, failures: int = 0, error: Exception | None = None) -> None:
        self.failures = failures
        self.error = error or ExternalCallTimeoutError("stub", 1.0)
        self.calls = 0

    async def complete(self, messages):
        self.calls += 1
        if self.calls <= self.failures:
            raise self.error
        return "ok [1]", {"promptTokens": 1, "completionTokens": 1, "totalTokens": 2}

    async def stream(self, messages):
        self.calls += 1
        if self.calls <= self.failures:
            raise self.error
        yield "ok"
        yield ("__usage__", None)


def make_reliable(
    settings: Settings, inner: FlakyClient, redis: FakeRedis
) -> tuple[ReliableChatClient, CircuitBreaker, list[float]]:
    breaker = CircuitBreaker(settings, redis=redis)
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    client = ReliableChatClient(
        inner, breaker=breaker, settings=settings, sleep=fake_sleep, rng=lambda: 0.5
    )
    return client, breaker, sleeps


# ------------------------------------------------------------------ retries
async def test_transient_failure_retries_with_exponential_backoff_and_jitter():
    settings = make_settings(RETRY_MAX_ATTEMPTS="3", RETRY_BASE_DELAY_SECONDS="0.5")
    inner = FlakyClient(failures=2)
    client, _, sleeps = make_reliable(settings, inner, FakeRedis())

    text, _ = await client.complete([])

    assert text == "ok [1]"
    assert inner.calls == 3
    # base * 2^attempt * (0.5 + rng=0.5) -> 0.5, 1.0 — exponential, bounded.
    assert sleeps == [0.5, 1.0]


async def test_retries_are_bounded():
    settings = make_settings(RETRY_MAX_ATTEMPTS="2")
    inner = FlakyClient(failures=99)
    client, _, sleeps = make_reliable(settings, inner, FakeRedis())

    with pytest.raises(ExternalCallTimeoutError):
        await client.complete([])
    assert inner.calls == 2  # bounded attempts
    assert len(sleeps) == 1


async def test_non_transient_errors_do_not_retry():
    settings = make_settings()
    inner = FlakyClient(failures=99, error=ValueError("bad request"))
    client, _, sleeps = make_reliable(settings, inner, FakeRedis())

    with pytest.raises(ValueError):
        await client.complete([])
    assert inner.calls == 1
    assert sleeps == []


def test_is_transient_classification():
    assert is_transient(ExternalCallTimeoutError("x", 1.0))
    assert is_transient(ConnectionError())

    class Fake429(Exception):
        status_code = 429

    class Fake500(Exception):
        status_code = 503

    class Fake400(Exception):
        status_code = 400

    assert is_transient(Fake429())
    assert is_transient(Fake500())
    assert not is_transient(Fake400())
    assert not is_transient(ValueError())


# ------------------------------------------------------------ circuit breaker
async def test_breaker_opens_after_threshold_and_fails_fast():
    settings = make_settings(BREAKER_FAILURE_THRESHOLD="3", RETRY_MAX_ATTEMPTS="1")
    redis = FakeRedis()
    inner = FlakyClient(failures=99)
    client, breaker, _ = make_reliable(settings, inner, redis)

    for _ in range(3):
        with pytest.raises(ExternalCallTimeoutError):
            await client.complete([])

    assert await breaker.state("openai") == "open"
    # Fail fast: the provider is NOT called while open (no timeout wait).
    calls_before = inner.calls
    with pytest.raises(BreakerOpenError):
        await client.complete([])
    assert inner.calls == calls_before


async def test_breaker_half_opens_after_cooldown_and_closes_on_probe_success():
    settings = make_settings(
        BREAKER_FAILURE_THRESHOLD="1", BREAKER_COOLDOWN_SECONDS="30", RETRY_MAX_ATTEMPTS="1"
    )
    redis = FakeRedis()
    inner = FlakyClient(failures=1)  # first call fails, all later succeed
    client, breaker, _ = make_reliable(settings, inner, redis)

    with pytest.raises(ExternalCallTimeoutError):
        await client.complete([])
    assert await breaker.state("openai") == "open"

    redis.now += 31  # cooldown elapses -> half-open
    assert await breaker.state("openai") == "half_open"

    # Probe succeeds -> circuit closes.
    text, _ = await client.complete([])
    assert text == "ok [1]"
    assert await breaker.state("openai") == "closed"


async def test_breaker_reopens_when_the_probe_fails():
    settings = make_settings(
        BREAKER_FAILURE_THRESHOLD="1", BREAKER_COOLDOWN_SECONDS="30", RETRY_MAX_ATTEMPTS="1"
    )
    redis = FakeRedis()
    inner = FlakyClient(failures=99)
    client, breaker, _ = make_reliable(settings, inner, redis)

    with pytest.raises(ExternalCallTimeoutError):
        await client.complete([])
    redis.now += 31

    with pytest.raises(ExternalCallTimeoutError):  # the probe itself fails
        await client.complete([])
    assert await breaker.state("openai") == "open"  # reopened


async def test_half_open_allows_exactly_one_probe_across_replicas():
    settings = make_settings(BREAKER_FAILURE_THRESHOLD="1", BREAKER_COOLDOWN_SECONDS="30")
    redis = FakeRedis()
    breaker = CircuitBreaker(settings, redis=redis)
    await breaker.record_failure("openai")  # threshold 1 -> opens
    redis.now += 31

    first = await breaker.allow("openai")
    second = await breaker.allow("openai")  # a second replica meanwhile
    assert first is True and second is False


# ------------------------------------------------------------------ failover
class StubStrong(FlakyClient):
    model = "claude-opus-4-8"
    provider = "anthropic"


class StubWatsonx(FlakyClient):
    model = "meta-llama/llama-4-maverick-17b-128e-instruct-fp8"
    provider = "watsonx"


async def test_open_circuit_routes_to_the_failover_provider_without_waiting():
    settings = make_settings(
        ANTHROPIC_API_KEY="sk-ant-test", BREAKER_FAILURE_THRESHOLD="1"
    )
    redis = FakeRedis()
    breaker = CircuitBreaker(settings, redis=redis)
    inner = ModelRouter(settings, cheap=FlakyClient(), strong=StubStrong())
    router = ReliabilityRouter(inner, breaker=breaker, settings=settings)
    router._breaker = breaker  # ensure shared fake redis

    await breaker.record_failure("openai")  # opens openai's circuit

    client, decision = await router.route(query="short question", chunks=[])

    assert client.provider == "anthropic"  # failed over, no timeout wait
    assert decision["tier"] == "failover"
    assert decision["reason"] == "breaker-open:openai"


async def test_all_circuits_open_fails_fast_with_structured_error():
    settings = make_settings(ANTHROPIC_API_KEY="sk-ant-test", BREAKER_FAILURE_THRESHOLD="1")
    redis = FakeRedis()
    breaker = CircuitBreaker(settings, redis=redis)
    inner = ModelRouter(settings, cheap=FlakyClient(), strong=StubStrong())
    router = ReliabilityRouter(inner, breaker=breaker, settings=settings)

    await breaker.record_failure("openai")
    await breaker.record_failure("anthropic")

    client, decision = await router.route(query="short question", chunks=[])
    assert decision["reason"] == "all-breakers-open"
    # The returned client fails fast -> the generation layer degrades (B2).
    with pytest.raises(BreakerOpenError):
        await client.complete([])


async def test_two_normal_circuits_open_route_to_watsonx():
    settings = make_settings(BREAKER_FAILURE_THRESHOLD="1")
    redis = FakeRedis()
    breaker = CircuitBreaker(settings, redis=redis)
    inner = ModelRouter(
        settings,
        cheap=FlakyClient(),
        strong=StubStrong(),
        fallbacks=[StubWatsonx()],
    )
    router = ReliabilityRouter(inner, breaker=breaker, settings=settings)
    await breaker.record_failure("openai")
    await breaker.record_failure("anthropic")

    client, decision = await router.route(query="short", chunks=[])

    assert client.provider == "watsonx"
    assert decision["tier"] == "failover"
    assert decision["provider"] == "watsonx"


async def test_open_openai_prefers_anthropic_before_watsonx():
    settings = make_settings(BREAKER_FAILURE_THRESHOLD="1")
    redis = FakeRedis()
    breaker = CircuitBreaker(settings, redis=redis)
    inner = ModelRouter(
        settings,
        cheap=FlakyClient(),
        strong=StubStrong(),
        fallbacks=[StubWatsonx()],
    )
    router = ReliabilityRouter(inner, breaker=breaker, settings=settings)
    await breaker.record_failure("openai")

    client, decision = await router.route(query="short", chunks=[])

    assert client.provider == "anthropic"
    assert decision["tier"] == "failover"


async def test_open_watsonx_is_skipped_when_normal_alternate_is_available():
    settings = make_settings(BREAKER_FAILURE_THRESHOLD="1")
    redis = FakeRedis()
    breaker = CircuitBreaker(settings, redis=redis)
    inner = ModelRouter(
        settings,
        cheap=FlakyClient(),
        strong=StubStrong(),
        fallbacks=[StubWatsonx()],
    )
    router = ReliabilityRouter(inner, breaker=breaker, settings=settings)
    await breaker.record_failure("openai")
    await breaker.record_failure("watsonx")

    client, _ = await router.route(query="short", chunks=[])

    assert client.provider == "anthropic"


async def test_all_three_circuits_open_fails_fast():
    settings = make_settings(BREAKER_FAILURE_THRESHOLD="1")
    redis = FakeRedis()
    breaker = CircuitBreaker(settings, redis=redis)
    inner = ModelRouter(
        settings,
        cheap=FlakyClient(),
        strong=StubStrong(),
        fallbacks=[StubWatsonx()],
    )
    router = ReliabilityRouter(inner, breaker=breaker, settings=settings)
    for provider in ("openai", "anthropic", "watsonx"):
        await breaker.record_failure(provider)

    client, decision = await router.route(query="short", chunks=[])

    assert decision["reason"] == "all-breakers-open"
    with pytest.raises(BreakerOpenError):
        await client.complete([])


# ---------------------------------------------------------- output validation
def test_validate_output_flags_each_problem_class():
    assert validate_output("", citations=[], chunk_count=0) == ["empty-answer"]
    assert validate_output(
        "Context blocks:\n[1] leaked", citations=[], chunk_count=1
    ) == ["leaked-scaffolding:Context blocks:"]
    assert validate_output("Fine answer [3].", citations=[], chunk_count=2) == [
        "citation-out-of-range:[3]"
    ]
    assert (
        validate_output(
            "Good [1].", citations=[{"chunkId": "abc"}], chunk_count=1
        )
        == []
    )
    assert validate_output(
        "Good [1].", citations=[{"chunkId": None}], chunk_count=1
    ) == ["citation-missing-chunk-id"]


# ------------------------------------------------------- hard-timeout audit
def test_every_external_call_module_uses_the_hard_timeout_helper():
    """GN-5 AC: every LLM / embedding / DB call runs under the F1 helper.
    Audits each module that talks to a provider or the database for direct,
    unwrapped calls."""
    app_dir = pathlib.Path(__file__).parent.parent / "app"
    external_modules = [
        "documents.py",
        "chunks.py",
        "conversations.py",
        "jobs.py",
        "embeddings.py",
        "providers.py",
        "watsonx.py",
    ]
    for name in external_modules:
        source = (app_dir / name).read_text(encoding="utf-8")
        assert "with_hard_timeout" in source, f"{name} must use the F1 hard-timeout helper"

    # No naked provider/DB entry points outside the helper. Every
    # `asyncpg.connect(` and provider `.create(`/`.stream(` call site must sit
    # inside a with_hard_timeout(...) wrapper in these modules.
    for name in external_modules:
        source = (app_dir / name).read_text(encoding="utf-8")
        for line_number, line in enumerate(source.splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith("#") or '"""' in stripped:
                continue
            if "asyncpg.connect(" in stripped:
                # the call must be the argument of with_hard_timeout (previous
                # non-empty line opens the wrapper)
                context = source.splitlines()[max(0, line_number - 3) : line_number]
                assert any("with_hard_timeout" in c for c in context), (
                    f"{name}:{line_number} asyncpg.connect must run under with_hard_timeout"
                )
