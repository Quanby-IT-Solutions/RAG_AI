"""CP-1 response/semantic cache tests."""

from typing import Any

import pytest

from app.response_cache import ResponseCache, config_fingerprint, normalize_query
from tests.conftest import REQUIRED_ENV


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


class FakeRedis:
    """dict-backed stand-in for redis.asyncio (shared across instances to
    model multi-replica behavior)."""

    def __init__(self):
        self.store: dict[str, Any] = {}
        self.hashes: dict[str, dict[str, str]] = {}
        self.counters: dict[str, int] = {}

    async def get(self, key):
        return self.store.get(key)

    async def set(self, key, value, ex=None):
        self.store[key] = value

    async def hgetall(self, key):
        return dict(self.hashes.get(key, {}))

    async def hset(self, key, field, value):
        self.hashes.setdefault(key, {})[field] = value

    async def expire(self, key, ttl):
        return True

    async def incr(self, key):
        self.counters[key] = self.counters.get(key, 0) + 1

    async def mget(self, keys):
        return [self.counters.get(key) for key in keys]


class DirectionalEmbeddings:
    """'vacation' queries embed along one axis; everything else orthogonal."""

    async def embed_texts(self, texts):
        return [[1.0, 0.0] if "vacation" in t else [0.0, 1.0] for t in texts], 1


PAYLOAD = {
    "answer": "Vacation is 20 days [1].",
    "citations": [{"chunkId": "c1"}],
    "model": "gpt-4o-mini",
    "isIDontKnow": False,
    "degraded": False,
}


def make_cache(config, redis=None):
    return ResponseCache(config, DirectionalEmbeddings(), redis_client=redis or FakeRedis())


# ------------------------------------------------------------------- layers
async def test_exact_hit_returns_payload_and_counts(settings):
    config = settings()
    redis = FakeRedis()
    cache = make_cache(config, redis)

    await cache.store(query="How many vacation days?", filters=None, payload=PAYLOAD)
    hit = await cache.lookup(query="how many  VACATION days?", filters=None)

    assert hit is not None
    assert hit["cacheLayer"] == "exact"
    assert hit["answer"] == PAYLOAD["answer"]
    assert redis.counters["respcache:metrics:hits"] == 1
    assert redis.counters["respcache:metrics:stores"] == 1


async def test_semantic_near_match_hits(settings):
    config = settings(CACHE_SEMANTIC_THRESHOLD="0.9")
    redis = FakeRedis()
    cache = make_cache(config, redis)

    await cache.store(query="How many vacation days do we get?", filters=None, payload=PAYLOAD)
    hit = await cache.lookup(query="what is our vacation allowance", filters=None)

    assert hit is not None
    assert hit["cacheLayer"] == "semantic"
    assert redis.counters["respcache:metrics:semanticHits"] == 1


async def test_miss_is_counted(settings):
    config = settings()
    redis = FakeRedis()
    cache = make_cache(config, redis)

    assert await cache.lookup(query="unrelated question", filters=None) is None
    assert redis.counters["respcache:metrics:misses"] == 1
    metrics = await cache.metrics()
    assert metrics == {"hits": 0, "semanticHits": 0, "misses": 1, "stores": 0}


async def test_never_caches_idk_or_degraded(settings):
    config = settings()
    redis = FakeRedis()
    cache = make_cache(config, redis)

    await cache.store(
        query="q", filters=None, payload={**PAYLOAD, "isIDontKnow": True}
    )
    await cache.store(query="q", filters=None, payload={**PAYLOAD, "degraded": True})

    assert redis.store == {}
    assert "respcache:metrics:stores" not in redis.counters


async def test_filters_are_part_of_the_key(settings):
    config = settings()
    cache = make_cache(config)

    await cache.store(
        query="vacation days?", filters={"docTypes": ["pdf"]}, payload=PAYLOAD
    )
    assert await cache.lookup(query="vacation days?", filters=None) is None
    assert (
        await cache.lookup(query="vacation days?", filters={"docTypes": ["pdf"]})
    ) is not None


async def test_config_change_is_a_clean_namespace(settings, monkeypatch):
    config_a = settings()
    redis = FakeRedis()
    cache_a = make_cache(config_a, redis)
    await cache_a.store(query="vacation days?", filters=None, payload=PAYLOAD)

    config_b = settings(RETRIEVAL_TOP_K="9")
    assert config_fingerprint(config_a) != config_fingerprint(config_b)
    cache_b = make_cache(config_b, redis)
    assert await cache_b.lookup(query="vacation days?", filters=None) is None


async def test_replicas_share_entries_via_the_same_redis(settings):
    config = settings()
    redis = FakeRedis()
    replica_a = make_cache(config, redis)
    replica_b = make_cache(config, redis)

    await replica_a.store(query="vacation days?", filters=None, payload=PAYLOAD)
    hit = await replica_b.lookup(query="vacation days?", filters=None)

    assert hit is not None and hit["answer"] == PAYLOAD["answer"]


def test_normalize_query_collapses_case_and_whitespace():
    assert normalize_query("  How  MANY days? ") == "how many days?"


# ----------------------------------------------------- pipeline integration
async def test_cache_hit_skips_retrieval_and_llm_and_marks_cached(settings):
    from app.generation import GenerationService
    from tests.test_generation import (
        PRINCIPAL,
        FakeChatClient,
        FakeConversationsRepository,
        FakeRetrievalService,
    )

    config = settings()
    redis = FakeRedis()
    cache = make_cache(config, redis)
    await cache.store(query="How many vacation days?", filters=None, payload=PAYLOAD)

    retrieval = FakeRetrievalService([])
    client = FakeChatClient(answer="never used")
    repository = FakeConversationsRepository()
    service = GenerationService(
        settings=config,
        retrieval=retrieval,
        chat_client=client,
        repository=repository,
        response_cache=cache,
    )

    result = await service.answer(
        query="How many vacation days?", k=None, principal=PRINCIPAL
    )

    assert result["cached"] is True
    assert result["answer"] == PAYLOAD["answer"]
    assert retrieval.calls == []  # no retrieval
    assert client.calls == []  # no LLM call (AC)
    # The turn is still persisted and audited with the cache marker.
    assert repository.turns[-1]["answer"] == PAYLOAD["answer"]
    assert repository.audits[-1]["after"]["cacheHit"] is True


async def test_miss_populates_the_cache_for_the_next_request(settings):
    from app.generation import GenerationService
    from tests.test_generation import (
        PRINCIPAL,
        FakeChatClient,
        FakeConversationsRepository,
        FakeRetrievalService,
        make_chunk,
    )

    config = settings()
    redis = FakeRedis()
    cache = make_cache(config, redis)
    retrieval = FakeRetrievalService([make_chunk("Vacation is 20 days.")])
    client = FakeChatClient(answer="Vacation is 20 days [1].")
    service = GenerationService(
        settings=config,
        retrieval=retrieval,
        chat_client=client,
        repository=FakeConversationsRepository(),
        response_cache=cache,
    )

    first = await service.answer(query="vacation days?", k=None, principal=PRINCIPAL)
    assert first["cached"] is False

    second = await service.answer(query="vacation days?", k=None, principal=PRINCIPAL)
    assert second["cached"] is True
    assert len(client.calls) == 1  # only the first request reached the LLM
