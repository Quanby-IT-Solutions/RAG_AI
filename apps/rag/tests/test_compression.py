"""RT-7 contextual compression tests."""

import asyncio
import json
import logging
from typing import Any

import pytest

from app.compression import (
    EmbeddingsFilterCompressor,
    LLMExtractionCompressor,
    build_compressor,
    compress_with_fallback,
    split_sentences,
)
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


def chunk(cid: str, content: str) -> dict[str, Any]:
    return {
        "id": cid,
        "document_id": "d1",
        "content": content,
        "metadata": {"page": 1},
        "content_hash": f"hash-{cid}",
        "similarity": 0.8,
    }


class DirectionalEmbeddings:
    """Embeds 'relevant' sentences along the query axis, others orthogonal."""

    async def embed_texts(self, texts: list[str]) -> tuple[list[list[float]], int]:
        vectors = []
        for text in texts:
            vectors.append([1.0, 0.0] if "vacation" in text.lower() else [0.0, 1.0])
        return vectors, 1


# -------------------------------------------------------- embeddings filter
async def test_embeddings_filter_keeps_relevant_sentences_and_identity(settings):
    config = settings(COMPRESSION_ENABLED="true", COMPRESSION_SIMILARITY_THRESHOLD="0.5")
    compressor = EmbeddingsFilterCompressor(config, DirectionalEmbeddings())
    chunks = [
        chunk("c1", "Vacation is 20 days. The cafeteria opens at nine."),
        chunk("c2", "The cafeteria closes at five. Parking is free."),
    ]

    compressed = await compressor.compress("How much vacation do employees get?", chunks)

    # Chunk c2 has NO relevant sentences -> dropped, never padded.
    assert [c["id"] for c in compressed] == ["c1"]
    assert compressed[0]["content"] == "Vacation is 20 days."
    # Identity preserved for citations (GN-1 contract).
    assert compressed[0]["content_hash"] == "hash-c1"
    assert compressed[0]["metadata"] == {"page": 1}
    assert compressed[0]["compressed"] is True


def test_split_sentences():
    assert split_sentences("One. Two! Three? ") == ["One.", "Two!", "Three?"]
    assert split_sentences("") == []


# ------------------------------------------------------------ LLM extraction
class FakeLLM:
    def __init__(self, response: str):
        self.response = response
        self.calls: list[list[dict[str, str]]] = []

    async def complete(self, messages):
        self.calls.append(messages)
        return self.response, None


async def test_llm_extraction_maps_indexes_and_drops_none(settings):
    config = settings(COMPRESSION_ENABLED="true", COMPRESSION_PROVIDER="llm")
    llm = FakeLLM(json.dumps({"0": "Vacation is 20 days.", "1": "NONE"}))
    compressor = LLMExtractionCompressor(config, llm)
    chunks = [chunk("c1", "Vacation is 20 days. Filler."), chunk("c2", "Irrelevant.")]

    compressed = await compressor.compress("vacation?", chunks)

    assert [c["id"] for c in compressed] == ["c1"]
    assert compressed[0]["content"] == "Vacation is 20 days."
    user = llm.calls[0][1]["content"]
    assert "[0]" in user and "[1]" in user


async def test_llm_extraction_tolerates_fenced_json(settings):
    config = settings(COMPRESSION_ENABLED="true", COMPRESSION_PROVIDER="llm")
    llm = FakeLLM('```json\n{"0": "Kept."}\n```')
    compressor = LLMExtractionCompressor(config, llm)

    compressed = await compressor.compress("q", [chunk("c1", "Kept. Dropped.")])

    assert compressed[0]["content"] == "Kept."


# --------------------------------------------------------------- fallbacks
class ExplodingCompressor:
    provider = "boom"

    async def compress(self, query, chunks):
        raise RuntimeError("compressor down")


class SlowCompressor:
    provider = "slow"

    async def compress(self, query, chunks):
        await asyncio.sleep(0.2)
        return []


async def test_compressor_failure_falls_back_to_uncompressed(caplog):
    chunks = [chunk("c1", "text")]
    with caplog.at_level(logging.WARNING, logger="app.compression"):
        result, applied = await compress_with_fallback(
            ExplodingCompressor(), query="q", chunks=chunks, timeout_seconds=5.0
        )
    assert applied is False
    assert result == chunks
    assert any("fallback_to_uncompressed" in m for m in caplog.messages)


async def test_compressor_timeout_falls_back_to_uncompressed():
    chunks = [chunk("c1", "text")]
    result, applied = await compress_with_fallback(
        SlowCompressor(), query="q", chunks=chunks, timeout_seconds=0.05
    )
    assert applied is False
    assert result == chunks


# ------------------------------------------------------------ build_compressor
def test_build_compressor_off_returns_none(settings):
    config = settings(COMPRESSION_ENABLED="false")
    assert build_compressor(config, embeddings=DirectionalEmbeddings()) is None


def test_build_compressor_selects_provider(settings):
    config = settings(COMPRESSION_ENABLED="true", COMPRESSION_PROVIDER="embeddings")
    assert build_compressor(config, embeddings=DirectionalEmbeddings()).provider == "embeddings"

    config = settings(COMPRESSION_ENABLED="true", COMPRESSION_PROVIDER="llm")
    assert build_compressor(
        config, embeddings=DirectionalEmbeddings(), llm_client=FakeLLM("{}")
    ).provider == "llm"
    with pytest.raises(ValueError, match="chat client"):
        build_compressor(config, embeddings=DirectionalEmbeddings())


# --------------------------------------------- generation pipeline integration
async def test_prepare_compresses_before_budget_and_notes_the_trace(settings, caplog):
    from app.generation import GenerationService
    from tests.test_generation import (
        FakeChatClient,
        FakeConversationsRepository,
        FakeRetrievalService,
        make_chunk,
    )

    config = settings(COMPRESSION_ENABLED="true", COMPRESSION_SIMILARITY_THRESHOLD="0.5")
    retrieval = FakeRetrievalService(
        [
            make_chunk("Vacation is 20 days. Filler sentence here."),
            make_chunk("Parking is free.", similarity=0.7),
        ]
    )
    compressor = EmbeddingsFilterCompressor(config, DirectionalEmbeddings())
    service = GenerationService(
        settings=config,
        retrieval=retrieval,
        chat_client=FakeChatClient(answer="20 days [1]."),
        repository=FakeConversationsRepository(),
        compressor=compressor,
    )

    with caplog.at_level(logging.INFO, logger="app.generation"):
        prep = await service._prepare(query="How much vacation?", k=2)

    # Irrelevant chunk dropped; the kept chunk was compressed pre-budget.
    assert len(prep["used_chunks"]) == 1
    assert prep["used_chunks"][0]["content"] == "Vacation is 20 days."
    record = next(m for m in caplog.messages if "generation.used_chunks" in m)
    payload = json.loads(record.split("generation.used_chunks ")[1])
    assert payload["compression"] == {
        "requested": True,
        "provider": "embeddings",
        "applied": True,
        "dropped": 1,
    }


async def test_prepare_without_compressor_is_unchanged(settings, caplog):
    from app.generation import GenerationService
    from tests.test_generation import (
        FakeChatClient,
        FakeConversationsRepository,
        FakeRetrievalService,
        make_chunk,
    )

    config = settings(COMPRESSION_ENABLED="false")
    retrieval = FakeRetrievalService([make_chunk("Vacation is 20 days. Filler.")])
    service = GenerationService(
        settings=config,
        retrieval=retrieval,
        chat_client=FakeChatClient(answer="20 days [1]."),
        repository=FakeConversationsRepository(),
    )

    with caplog.at_level(logging.INFO, logger="app.generation"):
        prep = await service._prepare(query="q", k=1)

    assert prep["used_chunks"][0]["content"] == "Vacation is 20 days. Filler."
    record = next(m for m in caplog.messages if "generation.used_chunks" in m)
    payload = json.loads(record.split("generation.used_chunks ")[1])
    assert payload["compression"] == {"requested": False}
