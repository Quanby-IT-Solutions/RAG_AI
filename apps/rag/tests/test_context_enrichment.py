"""CE-1 contextual chunk enrichment tests (Flow G)."""

from typing import Any

import pytest

from app.context_enrichment import (
    CONTEXT_PROMPT_VERSION,
    enrich_chunks,
    enriched_embed_key,
)
from app.indexing import index_document_content
from tests.conftest import REQUIRED_ENV
from tests.test_ingest import FakeEmbeddingsClient
from tests.test_worker import NullEmbeddingCache


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


class FakeContextClient:
    """Deterministic enrichment model; records calls, can fail per chunk."""

    model = "gpt-4o-mini"

    def __init__(self, *, fail_on: set[str] | None = None) -> None:
        self.calls: list[str] = []
        self.fail_on = fail_on or set()

    async def complete(self, messages: list[dict[str, str]]):
        chunk_text = messages[-1]["content"].split("<chunk>\n")[1].split("\n</chunk>")[0]
        self.calls.append(chunk_text)
        if any(marker in chunk_text for marker in self.fail_on):
            raise RuntimeError("enrichment model down")
        return (
            f"Context for: {chunk_text[:24]}",
            {"promptTokens": 100, "completionTokens": 20, "totalTokens": 120},
        )


class FakeContextCache:
    """In-memory stand-in for ContextCacheRepository."""

    def __init__(self, preloaded: dict[str, str] | None = None) -> None:
        self.entries: dict[tuple[str, str, str], str] = {
            (h, "gpt-4o-mini", CONTEXT_PROMPT_VERSION): c
            for h, c in (preloaded or {}).items()
        }
        self.stored: list[tuple[str, str]] = []

    async def fetch(self, *, model, prompt_version, hashes):
        return {
            h: self.entries[(h, model, prompt_version)]
            for h in hashes
            if (h, model, prompt_version) in self.entries
        }

    async def store(self, *, model, prompt_version, entries):
        self.stored.extend(entries)
        for chunk_hash, context in entries:
            self.entries[(chunk_hash, model, prompt_version)] = context


class Chunk:
    def __init__(self, content: str, content_hash: str) -> None:
        self.content = content
        self.content_hash = content_hash


class CapturingRepository:
    def __init__(self):
        self.indexed: dict[str, Any] | None = None

    async def index_chunks(self, *, document_id, chunks, metadata_patch=None):
        self.indexed = {"documentId": document_id, "chunks": chunks, "patch": metadata_patch}
        return {"id": document_id, "status": "indexed", "error": None}

    async def mark_document_failed(self, document_id, reason):
        raise AssertionError(f"unexpected failure: {reason}")


def make_document(text: str) -> dict[str, Any]:
    return {"id": "doc-1", "content_hash": "doc-hash", "metadata": {"text": text}}


INDEX_ENV = {
    "CHUNKING_STRATEGY": "recursive",
    "CHUNK_MIN_CHARS": "10",
    "CHUNK_MAX_CHARS": "60",
}


# ------------------------------------------------------------ enrich_chunks
async def test_cached_contexts_make_zero_llm_calls(settings):
    config = settings(CONTEXTUAL_CHUNKS_ENABLED="true")
    client = FakeContextClient()
    cache = FakeContextCache(preloaded={"h1": "cached context one", "h2": "cached two"})

    contexts, stats = await enrich_chunks(
        document_text="full document text",
        chunks=[Chunk("alpha", "h1"), Chunk("beta", "h2")],
        client=client,
        cache=cache,
        settings=config,
    )

    assert contexts == ["cached context one", "cached two"]
    assert client.calls == []
    assert stats == {"hits": 2, "misses": 0, "fallbacks": 0, "costUsd": 0.0}


async def test_misses_generate_and_write_back(settings):
    config = settings(CONTEXTUAL_CHUNKS_ENABLED="true")
    client = FakeContextClient()
    cache = FakeContextCache(preloaded={"h1": "cached context one"})

    contexts, stats = await enrich_chunks(
        document_text="full document text",
        chunks=[Chunk("alpha", "h1"), Chunk("beta", "h2")],
        client=client,
        cache=cache,
        settings=config,
    )

    assert contexts[0] == "cached context one"
    assert contexts[1].startswith("Context for: beta")
    assert client.calls == ["beta"]
    assert cache.stored == [("h2", contexts[1])]
    assert stats["hits"] == 1 and stats["misses"] == 1 and stats["fallbacks"] == 0
    # gpt-4o-mini priced: 100 prompt + 20 completion tokens > 0 USD.
    assert stats["costUsd"] > 0


async def test_per_chunk_failure_falls_back_without_blocking(settings):
    config = settings(CONTEXTUAL_CHUNKS_ENABLED="true")
    client = FakeContextClient(fail_on={"beta"})
    cache = FakeContextCache()

    contexts, stats = await enrich_chunks(
        document_text="full document text",
        chunks=[Chunk("alpha", "h1"), Chunk("beta", "h2")],
        client=client,
        cache=cache,
        settings=config,
    )

    assert contexts[0] is not None
    assert contexts[1] is None
    assert stats["fallbacks"] == 1
    # The failed chunk is NOT cached — a re-run retries it.
    assert all(chunk_hash != "h2" for chunk_hash, _ in cache.stored)


# ------------------------------------------------------- indexing pipeline
async def test_flag_on_embeds_enriched_text_and_persists_raw_content(settings):
    config = settings(CONTEXTUAL_CHUNKS_ENABLED="true", **INDEX_ENV)
    repository = CapturingRepository()
    embeddings = FakeEmbeddingsClient()
    text = " ".join(f"sentence number {i} about refund policies." for i in range(12))

    outcome = await index_document_content(
        document=make_document(text),
        settings=config,
        repository=repository,
        embeddings=embeddings,
        embedding_cache=NullEmbeddingCache(),
        context_client=FakeContextClient(),
        context_cache=FakeContextCache(),
    )

    assert outcome["status"] == "indexed"
    rows = repository.indexed["chunks"]
    assert rows
    for row in rows:
        assert row["context_text"].startswith("Context for:")
        # Display/citation surface stays the raw chunk text.
        assert not row["content"].startswith("Context for:")
        assert row["index_text"] == f"{row['context_text']}\n\n{row['content']}"
        assert row["embed_key"] == enriched_embed_key(row["content_hash"], row["context_text"])
    # The embedding provider saw the ENRICHED text, not the raw chunk.
    embedded_texts = [t for call in embeddings.calls for t in call]
    assert embedded_texts == [row["index_text"] for row in rows]
    assert outcome["context"]["misses"] == len(rows)


async def test_flag_off_pipeline_is_unchanged(settings):
    config = settings(CONTEXTUAL_CHUNKS_ENABLED="false", **INDEX_ENV)
    repository = CapturingRepository()
    embeddings = FakeEmbeddingsClient()

    class ExplodingClient:
        model = "never-called"

        async def complete(self, messages):
            raise AssertionError("enrichment must not run when the flag is off")

    text = " ".join(f"sentence number {i} about refund policies." for i in range(12))
    outcome = await index_document_content(
        document=make_document(text),
        settings=config,
        repository=repository,
        embeddings=embeddings,
        embedding_cache=NullEmbeddingCache(),
        context_client=ExplodingClient(),
        context_cache=FakeContextCache(),
    )

    assert outcome["status"] == "indexed"
    assert outcome["context"] is None
    rows = repository.indexed["chunks"]
    for row in rows:
        assert row["context_text"] is None
        # Raw identity: embedding-cache key stays the original chunk hash.
        assert row["embed_key"] == row["content_hash"]
        assert row["index_text"] == row["content"]
    embedded_texts = [t for call in embeddings.calls for t in call]
    assert embedded_texts == [row["content"] for row in rows]


async def test_enrichment_failure_indexes_raw_text_job_succeeds(settings):
    config = settings(CONTEXTUAL_CHUNKS_ENABLED="true", **INDEX_ENV)
    repository = CapturingRepository()

    class AlwaysFailingClient:
        model = "gpt-4o-mini"

        async def complete(self, messages):
            raise RuntimeError("model down")

    text = " ".join(f"sentence number {i} about refund policies." for i in range(12))
    outcome = await index_document_content(
        document=make_document(text),
        settings=config,
        repository=repository,
        embeddings=FakeEmbeddingsClient(),
        embedding_cache=NullEmbeddingCache(),
        context_client=AlwaysFailingClient(),
        context_cache=FakeContextCache(),
    )

    assert outcome["status"] == "indexed"
    rows = repository.indexed["chunks"]
    for row in rows:
        assert row["context_text"] is None
        assert row["embed_key"] == row["content_hash"]
    assert outcome["context"]["fallbacks"] == len(rows)


async def test_reingest_of_identical_content_costs_zero_llm_calls(settings):
    config = settings(CONTEXTUAL_CHUNKS_ENABLED="true", **INDEX_ENV)
    text = " ".join(f"sentence number {i} about refund policies." for i in range(12))
    cache = FakeContextCache()
    first_client = FakeContextClient()

    await index_document_content(
        document=make_document(text),
        settings=config,
        repository=CapturingRepository(),
        embeddings=FakeEmbeddingsClient(),
        embedding_cache=NullEmbeddingCache(),
        context_client=first_client,
        context_cache=cache,
    )
    assert first_client.calls, "first ingest generates contexts"

    second_client = FakeContextClient()
    outcome = await index_document_content(
        document=make_document(text),
        settings=config,
        repository=CapturingRepository(),
        embeddings=FakeEmbeddingsClient(),
        embedding_cache=NullEmbeddingCache(),
        context_client=second_client,
        context_cache=cache,
    )

    assert second_client.calls == []
    assert outcome["context"]["hits"] == outcome["chunkCount"]
    assert outcome["context"]["misses"] == 0
