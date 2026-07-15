"""IN-2 embeddings client + boot-time dimension guard tests. The OpenAI SDK is
replaced with a fake — real providers are never called."""

from types import SimpleNamespace

import pytest

from app.config import PINNED_EMBEDDING_DIMENSIONS, PINNED_EMBEDDING_MODEL
from app.embeddings import (
    MAX_EMBED_BATCH_SIZE,
    EmbeddingsClient,
    assert_embedding_dimensions,
)


class FakeOpenAI:
    """Mimics AsyncOpenAI().embeddings.create, recording every call."""

    def __init__(self, dimensions: int = PINNED_EMBEDDING_DIMENSIONS) -> None:
        self.calls: list[dict] = []
        self._dimensions = dimensions
        self.embeddings = SimpleNamespace(create=self._create)

    async def _create(self, *, model: str, input: list[str], dimensions: int):
        self.calls.append({"model": model, "input": list(input), "dimensions": dimensions})
        # Return data deliberately out of order to prove index-based reordering.
        data = [
            SimpleNamespace(index=i, embedding=[float(i)] * self._dimensions)
            for i in reversed(range(len(input)))
        ]
        return SimpleNamespace(data=data)


# --------------------------------------------------------------- batch client
async def test_one_provider_call_for_n_texts(settings):
    fake = FakeOpenAI()
    client = EmbeddingsClient(settings, client=fake)

    vectors, batches = await client.embed_texts([f"chunk {i}" for i in range(50)])

    assert len(fake.calls) == 1  # batched: never one call per chunk
    assert batches == 1
    assert len(vectors) == 50


async def test_pinned_model_and_dimensions_forwarded(settings):
    fake = FakeOpenAI()
    client = EmbeddingsClient(settings, client=fake)

    await client.embed_texts(["a", "b"])

    call = fake.calls[0]
    assert call["model"] == PINNED_EMBEDDING_MODEL
    assert call["dimensions"] == PINNED_EMBEDDING_DIMENSIONS
    assert call["input"] == ["a", "b"]


async def test_vectors_returned_in_input_order(settings):
    fake = FakeOpenAI()
    client = EmbeddingsClient(settings, client=fake)

    vectors, _ = await client.embed_texts(["a", "b", "c"])

    # FakeOpenAI responds in reversed order; the client re-sorts by index.
    assert [v[0] for v in vectors] == [0.0, 1.0, 2.0]


async def test_oversized_input_splits_into_minimal_batches(settings):
    fake = FakeOpenAI()
    client = EmbeddingsClient(settings, client=fake)

    texts = [f"t{i}" for i in range(MAX_EMBED_BATCH_SIZE + 1)]
    vectors, batches = await client.embed_texts(texts)

    assert batches == 2
    assert len(fake.calls) == 2
    assert len(fake.calls[0]["input"]) == MAX_EMBED_BATCH_SIZE
    assert len(fake.calls[1]["input"]) == 1
    assert len(vectors) == len(texts)


async def test_wrong_dimension_response_raises(settings):
    fake = FakeOpenAI(dimensions=3072)  # provider ignores the dimensions param
    client = EmbeddingsClient(settings, client=fake)

    with pytest.raises(RuntimeError, match="3072"):
        await client.embed_texts(["a"])


# ---------------------------------------------------- boot-time dimension guard
class FakeDimensionRepo:
    def __init__(self, dimensions: int | None) -> None:
        self._dimensions = dimensions

    async def fetch_embedding_column_dimensions(self) -> int | None:
        return self._dimensions


async def test_dimension_guard_passes_on_match(settings):
    await assert_embedding_dimensions(settings, FakeDimensionRepo(1536))


async def test_dimension_guard_fails_fast_on_mismatch(settings):
    with pytest.raises(RuntimeError, match=r"vector\(3072\).*1536"):
        await assert_embedding_dimensions(settings, FakeDimensionRepo(3072))


async def test_dimension_guard_fails_when_column_missing(settings):
    with pytest.raises(RuntimeError, match="db:migrate"):
        await assert_embedding_dimensions(settings, FakeDimensionRepo(None))
