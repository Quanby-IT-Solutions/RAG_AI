"""IN-6 embedding-cache tests."""

from typing import Any

from app.embedding_cache import embed_with_cache


class CountingEmbeddings:
    model = "text-embedding-3-large"

    def __init__(self):
        self.calls: list[list[str]] = []

    async def embed_texts(self, texts: list[str]) -> tuple[list[list[float]], int]:
        self.calls.append(texts)
        return [[float(len(text)), 0.0] for text in texts], 1


class FakeCache:
    def __init__(self, preloaded: dict[str, list[float]] | None = None):
        self.rows = dict(preloaded or {})
        self.fetches: list[list[str]] = []
        self.stored: list[tuple[str, list[float]]] = []

    async def fetch(self, *, model: str, hashes: list[str]) -> dict[str, Any]:
        self.fetches.append(hashes)
        return {h: self.rows[h] for h in hashes if h in self.rows}

    async def store(self, *, model: str, entries):
        self.stored.extend(entries)
        self.rows.update(dict(entries))


async def test_fully_cached_input_makes_zero_api_calls():
    embeddings = CountingEmbeddings()
    cache = FakeCache({"h1": [1.0, 0.0], "h2": [2.0, 0.0]})

    vectors, stats = await embed_with_cache(
        embeddings=embeddings, cache=cache, items=[("h1", "one"), ("h2", "two")]
    )

    assert embeddings.calls == []  # AC: no embedding API call for cached hashes
    assert vectors == [[1.0, 0.0], [2.0, 0.0]]
    assert stats == {"cacheHits": 2, "cacheMisses": 0, "embedBatches": 0}


async def test_misses_are_embedded_once_and_written_back():
    embeddings = CountingEmbeddings()
    cache = FakeCache({"h1": [9.0, 0.0]})

    vectors, stats = await embed_with_cache(
        embeddings=embeddings,
        cache=cache,
        items=[("h1", "cached"), ("h2", "new"), ("h2", "new")],
    )

    # Only the unique miss goes out; the duplicate reuses the fresh vector.
    assert embeddings.calls == [["new"]]
    assert vectors == [[9.0, 0.0], [3.0, 0.0], [3.0, 0.0]]
    assert stats["cacheHits"] == 1
    assert stats["cacheMisses"] == 1
    # AC: cache rows written for the miss.
    assert [entry[0] for entry in cache.stored] == ["h2"]


async def test_alignment_is_preserved_across_mixed_hits_and_misses():
    embeddings = CountingEmbeddings()
    cache = FakeCache({"b": [0.5, 0.5]})

    vectors, _ = await embed_with_cache(
        embeddings=embeddings,
        cache=cache,
        items=[("a", "xx"), ("b", "cached"), ("c", "yyyy")],
    )

    assert vectors == [[2.0, 0.0], [0.5, 0.5], [4.0, 0.0]]


async def test_empty_input_short_circuits():
    embeddings = CountingEmbeddings()
    cache = FakeCache()

    vectors, stats = await embed_with_cache(embeddings=embeddings, cache=cache, items=[])

    assert vectors == []
    assert stats == {"cacheHits": 0, "cacheMisses": 0, "embedBatches": 0}
    assert cache.fetches == []
