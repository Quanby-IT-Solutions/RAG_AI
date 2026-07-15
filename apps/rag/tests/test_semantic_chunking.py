"""IN-4 chunking-chain tests: semantic boundaries from embedding distance
spikes, the 10-20% overlap band, min/max size validation (re-split / merge /
flag — never silent), recursive fallback on provider failure with the reason
recorded, and the fixed-size-not-selectable config rule."""

import pytest

from app.chunking import (
    SEMANTIC_OVERLAP_FRACTION,
    chunk_document,
    split_sentences,
)
from app.config import Settings
from tests.conftest import REQUIRED_ENV

# Two clearly separated topics; every sentence ends with a period so the
# sentence splitter produces uniform units.
FRUIT = "Apples grow best in temperate orchard climates with cold winters and mild summers. "
SPACE = "Rocket engines burn cryogenic propellant at extremely high combustion pressure. "


class TopicEmbeddings:
    """Deterministic embeddings: fruit sentences -> [1, 0], space -> [0, 1].
    Adjacent same-topic distance is 0; the topic switch spikes to 1."""

    model = "text-embedding-3-large"
    dimensions = 1536

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.fail = False

    async def embed_texts(self, texts: list[str]) -> tuple[list[list[float]], int]:
        self.calls.append(list(texts))
        if self.fail:
            raise RuntimeError("embedding provider down")
        vectors = []
        for text in texts:
            vectors.append([0.0, 1.0] if "Rocket" in text or "propellant" in text else [1.0, 0.0])
        return vectors, 1


def make_text(fruit_sentences: int, space_sentences: int) -> str:
    return (FRUIT * fruit_sentences + SPACE * space_sentences).strip()


async def run_chain(
    text: str,
    *,
    embeddings: TopicEmbeddings | None = None,
    strategy: str = "semantic",
    min_chars: int = 60,
    max_chars: int = 2000,
) -> tuple[list, dict]:
    return await chunk_document(
        text=text,
        pages=None,
        document_hash="doc-hash-semantic",
        embeddings=embeddings or TopicEmbeddings(),
        strategy=strategy,
        min_chars=min_chars,
        max_chars=max_chars,
        breakpoint_percentile=90,
    )


# ------------------------------------------------------------ semantic split
async def test_splits_at_the_meaning_boundary_not_at_character_counts():
    chunks, meta = await run_chain(make_text(5, 5))

    assert meta == {"strategy": "semantic", "fallback": False}
    assert len(chunks) == 2
    # All fruit content in the first chunk, all space content in the second.
    assert "Apples" in chunks[0].content and "Rocket" not in chunks[0].content
    assert "Rocket" in chunks[1].content
    assert all(chunk.metadata["strategy"] == "semantic" for chunk in chunks)


async def test_uniform_topic_yields_single_chunk():
    chunks, meta = await run_chain(make_text(6, 0), max_chars=4000)
    assert meta["strategy"] == "semantic"
    assert len(chunks) == 1


async def test_sentence_embeddings_are_one_batched_call():
    embeddings = TopicEmbeddings()
    await run_chain(make_text(4, 4), embeddings=embeddings)
    # ONE batched sentence pass for the whole document — never per-sentence.
    # (Chunk embedding is a separate ingest-stage call, not chunk_document's.)
    assert len(embeddings.calls) == 1
    assert embeddings.calls[0] == split_sentences(make_text(4, 4))


# ------------------------------------------------------------------- overlap
async def test_adjacent_semantic_chunks_overlap_10_to_20_percent():
    chunks, _ = await run_chain(make_text(6, 6))
    assert len(chunks) == 2

    first, second = chunks[0].content, chunks[1].content
    # The second chunk starts with the tail of the first (shared context).
    overlap_len = 0
    for size in range(min(len(first), len(second)), 0, -1):
        if second.startswith(first[-size:]):
            overlap_len = size
            break
    assert overlap_len > 0, "adjacent semantic chunks must share an overlap"
    ratio = overlap_len / len(first)
    assert 0.10 <= ratio <= 0.20, f"overlap {ratio:.2%} outside the 10-20% band"
    # And the target constant itself sits inside the band.
    assert 0.10 <= SEMANTIC_OVERLAP_FRACTION <= 0.20


# ----------------------------------------------------------- size validation
async def test_oversized_chunks_are_resplit_to_max():
    chunks, meta = await run_chain(make_text(40, 0), max_chars=500, min_chars=50)
    assert meta["strategy"] == "semantic"
    assert len(chunks) > 1
    assert all(len(chunk.content) <= 500 for chunk in chunks)


async def test_undersized_chunk_merges_into_neighbor():
    # One space sentence after many fruit sentences: the semantic split makes
    # a runt chunk that must merge back within max_chars.
    chunks, meta = await run_chain(make_text(5, 1), min_chars=120, max_chars=2000)
    assert all(len(chunk.content) >= 120 for chunk in chunks)
    assert "undersizedChunks" not in meta


async def test_document_smaller_than_min_is_flagged_not_silent():
    chunks, meta = await run_chain("One tiny sentence.", min_chars=200)
    assert len(chunks) == 1
    assert chunks[0].metadata.get("undersized") is True
    assert meta["undersizedChunks"] == 1


# ------------------------------------------------------------------ fallback
async def test_provider_failure_falls_back_to_recursive_and_records_reason():
    embeddings = TopicEmbeddings()
    embeddings.fail = True

    chunks, meta = await run_chain(make_text(6, 6), embeddings=embeddings)

    assert meta["strategy"] == "recursive"
    assert meta["fallback"] is True
    assert "embedding provider down" in meta["fallbackReason"]
    assert len(chunks) >= 1
    assert all(chunk.metadata["strategy"] == "recursive" for chunk in chunks)


async def test_recursive_strategy_by_config_never_calls_embeddings_for_sentences():
    embeddings = TopicEmbeddings()
    chunks, meta = await run_chain(make_text(6, 6), embeddings=embeddings, strategy="recursive")

    assert meta == {"strategy": "recursive", "fallback": False}
    assert embeddings.calls == []  # chunk embedding happens later, in ingest
    assert len(chunks) >= 1


# ----------------------------------------------------------- hash idempotency
async def test_semantic_hashes_stable_across_reruns():
    first, _ = await run_chain(make_text(5, 5))
    second, _ = await run_chain(make_text(5, 5))
    assert [c.content_hash for c in first] == [c.content_hash for c in second]


# -------------------------------------------------------------------- config
def make_settings(**overrides) -> Settings:
    return Settings(**{**REQUIRED_ENV, **overrides})


def test_default_strategy_is_semantic_not_fixed():
    settings = make_settings()
    assert settings.chunking_strategy == "semantic"


def test_fixed_size_chunking_is_not_selectable():
    with pytest.raises(Exception) as excinfo:
        make_settings(CHUNKING_STRATEGY="fixed")
    assert "CHUNKING_STRATEGY" in str(excinfo.value) or "chunking_strategy" in str(excinfo.value)


def test_chunk_bounds_must_be_ordered():
    with pytest.raises(Exception) as excinfo:
        make_settings(CHUNK_MIN_CHARS="2000", CHUNK_MAX_CHARS="1000")
    assert "CHUNK_MIN_CHARS" in str(excinfo.value)


async def test_overlap_never_emits_mid_word_fragments():
    """A tiny previous chunk (e.g. a title) must not leak word fragments like
    the 'ok' of 'Handbook' into the next chunk's overlap."""
    text = "Mixed Handbook.\n" + make_text(0, 6)
    chunks, _ = await run_chain(text, min_chars=60)

    for chunk in chunks:
        first_word = chunk.content.split(" ", 1)[0]
        assert first_word not in {"ok", "book"}, chunk.content[:60]
        # Every chunk starts with a capitalized full word from the source.
        assert first_word[0].isupper() or first_word[0].isdigit(), chunk.content[:60]
