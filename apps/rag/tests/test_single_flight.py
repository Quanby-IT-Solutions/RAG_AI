"""CP-3 single-flight coalescing tests (Flow C)."""

import asyncio

import pytest

from app.single_flight import SingleFlight, SingleFlightError
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


def make_flight(config, redis=None) -> tuple[SingleFlight, FakeRedis]:
    redis = redis or FakeRedis()
    return SingleFlight(config, redis), redis


# ------------------------------------------------------------ exactly once
async def test_n_concurrent_misses_compute_exactly_once(settings):
    config = settings(SINGLE_FLIGHT_POLL_INTERVAL_SECONDS="0.01")
    flight, _ = make_flight(config)
    invocations = 0

    async def compute():
        nonlocal invocations
        invocations += 1
        await asyncio.sleep(0.05)  # keep the lock held while followers arrive
        return {"answer": "42"}

    results = await asyncio.gather(*[flight.run("key-1", compute) for _ in range(8)])

    assert invocations == 1  # AC: exactly one pipeline run
    roles = [role for _, role in results]
    assert roles.count("leader") == 1
    assert roles.count("follower") == 7
    assert all(result == {"answer": "42"} for result, _ in results)


async def test_two_replicas_coalesce_via_shared_redis(settings):
    config = settings(SINGLE_FLIGHT_POLL_INTERVAL_SECONDS="0.01")
    redis = FakeRedis()
    replica_a, _ = make_flight(config, redis)
    replica_b, _ = make_flight(config, redis)
    invocations = 0

    async def compute():
        nonlocal invocations
        invocations += 1
        await asyncio.sleep(0.05)
        return {"answer": "shared"}

    (result_a, _), (result_b, _) = await asyncio.gather(
        replica_a.run("key-1", compute), replica_b.run("key-1", compute)
    )

    assert invocations == 1
    assert result_a == result_b == {"answer": "shared"}


# ------------------------------------------------------------ failure paths
async def test_leader_failure_lets_one_waiter_retry_once(settings):
    config = settings(SINGLE_FLIGHT_POLL_INTERVAL_SECONDS="0.01")
    flight, _ = make_flight(config)
    attempts = 0

    async def compute():
        nonlocal attempts
        attempts += 1
        await asyncio.sleep(0.05)
        if attempts == 1:
            raise RuntimeError("provider down")
        return {"answer": "second try"}

    outcomes = await asyncio.gather(
        *[flight.run("key-1", compute) for _ in range(3)], return_exceptions=True
    )

    # The failing leader re-raises; the waiters retry once and one recomputes.
    errors = [o for o in outcomes if isinstance(o, Exception)]
    successes = [o for o in outcomes if not isinstance(o, Exception)]
    assert len(errors) == 1 and isinstance(errors[0], RuntimeError)
    assert successes and all(result == {"answer": "second try"} for result, _ in successes)
    assert attempts == 2  # first (failed) + exactly one retry


async def test_dead_leader_never_deadlocks_waiters(settings):
    # Simulate a leader that died holding the lock: the lock key is absent
    # by the time the follower polls (TTL expired) and no result exists.
    config = settings(
        SINGLE_FLIGHT_POLL_INTERVAL_SECONDS="0.01",
        SINGLE_FLIGHT_WAIT_TIMEOUT_SECONDS="0.5",
    )
    flight, redis = make_flight(config)

    # Someone else holds the lock, then vanishes without publishing.
    await redis.set("sf:lock:key-1", "dead-owner")

    async def release_later():
        await asyncio.sleep(0.05)
        await redis.delete("sf:lock:key-1")

    async def compute():
        return {"answer": "recovered"}

    (_, (result, role)) = await asyncio.gather(release_later(), flight.run("key-1", compute))

    assert result == {"answer": "recovered"}
    assert role == "leader"  # the waiter took over after the lock vanished


async def test_wait_timeout_raises_instead_of_hanging(settings):
    config = settings(
        SINGLE_FLIGHT_POLL_INTERVAL_SECONDS="0.01",
        SINGLE_FLIGHT_WAIT_TIMEOUT_SECONDS="0.1",
    )
    flight, redis = make_flight(config)
    await redis.set("sf:lock:key-1", "someone-else")  # never releases

    with pytest.raises(SingleFlightError, match="Timed out"):
        await flight.await_result("key-1")


# ----------------------------------------------------- pipeline integration
async def test_concurrent_answers_share_one_llm_call(settings):
    from app.generation import GenerationService
    from tests.test_generation import (
        PRINCIPAL,
        FakeChatClient,
        FakeConversationsRepository,
        FakeRetrievalService,
        make_chunk,
    )
    from tests.test_response_cache import make_cache

    config = settings(SINGLE_FLIGHT_POLL_INTERVAL_SECONDS="0.01")
    redis = FakeRedis()
    cache = make_cache(config, redis)

    class SlowChatClient(FakeChatClient):
        async def complete(self, messages):
            await asyncio.sleep(0.05)
            return await super().complete(messages)

    client = SlowChatClient(answer="Vacation is 20 days [1].")
    repository = FakeConversationsRepository()
    service = GenerationService(
        settings=config,
        retrieval=FakeRetrievalService([make_chunk("Vacation is 20 days.")]),
        chat_client=client,
        repository=repository,
        response_cache=cache,
    )

    results = await asyncio.gather(
        *[
            service.answer(query="vacation days?", k=None, principal=PRINCIPAL)
            for _ in range(5)
        ]
    )

    assert len(client.calls) == 1  # AC: one LLM invocation for 5 concurrent asks
    assert all(r["answer"] == "Vacation is 20 days [1]." for r in results)
    # Every requester still gets their own persisted turn.
    assert len(repository.turns) == 5
