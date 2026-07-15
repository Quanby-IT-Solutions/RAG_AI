"""RT-5 multi-query expansion + MMR tests."""

from typing import Any

import pytest

from app.expansion import generate_rewordings, mmr_select
from tests.conftest import REQUIRED_ENV
from tests.test_ingest import FakeEmbeddingsClient
from tests.test_retrieval import FakeChunksRepository, make_chunk_row


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


class FakeExpansionClient:
    def __init__(self, text: str = "", error: Exception | None = None):
        self.text = text
        self.error = error
        self.calls: list[list[dict[str, str]]] = []

    async def complete(self, messages):
        self.calls.append(messages)
        if self.error:
            raise self.error
        return self.text, None


# ------------------------------------------------------- generate_rewordings
async def test_rewordings_parse_lines_and_strip_numbering():
    client = FakeExpansionClient("1. How much PTO?\n- Vacation allowance?\n\nDays off policy?")

    rewordings = await generate_rewordings("vacation days?", client=client, count=3)

    assert rewordings == ["How much PTO?", "Vacation allowance?", "Days off policy?"]
    system = client.calls[0][0]["content"]
    assert "3" in system


async def test_rewordings_drop_duplicates_of_the_original_and_cap_at_count():
    client = FakeExpansionClient("vacation days?\na\nb\nc\nd")

    rewordings = await generate_rewordings("Vacation days?", client=client, count=2)

    assert rewordings == ["a", "b"]


async def test_expansion_failure_degrades_to_no_rewordings():
    client = FakeExpansionClient(error=RuntimeError("model down"))
    assert await generate_rewordings("q", client=client, count=3) == []


# ----------------------------------------------------------------- mmr_select
def cand(cid: str) -> dict[str, Any]:
    return {"id": cid, "content": cid, "rrf_score": 0.1}


def test_mmr_penalizes_near_duplicates():
    # b duplicates a (identical embedding); c is orthogonal. Relevance order
    # a > b > c, but MMR at λ=0.5 should pick c over the duplicate b.
    embeddings = {"a": [1.0, 0.0], "b": [1.0, 0.0], "c": [0.0, 1.0]}

    selected = mmr_select(
        [cand("a"), cand("b"), cand("c")],
        embeddings_by_id=embeddings,
        k=2,
        lambda_=0.5,
    )

    assert [c["id"] for c in selected] == ["a", "c"]


def test_mmr_lambda_one_keeps_pure_relevance_order():
    embeddings = {"a": [1.0, 0.0], "b": [1.0, 0.0], "c": [0.0, 1.0]}

    selected = mmr_select(
        [cand("a"), cand("b"), cand("c")],
        embeddings_by_id=embeddings,
        k=2,
        lambda_=1.0,
    )

    assert [c["id"] for c in selected] == ["a", "b"]


def test_mmr_handles_missing_embeddings_and_small_pools():
    # Pool <= k short-circuits; unknown embeddings contribute no redundancy.
    pool = [cand("a"), cand("b")]
    assert mmr_select(pool, embeddings_by_id={}, k=5, lambda_=0.5) == pool
    selected = mmr_select(
        [cand("a"), cand("b"), cand("c")], embeddings_by_id={}, k=2, lambda_=0.5
    )
    assert len(selected) == 2


# ------------------------------------------------- RetrievalService integration
def make_service(settings, *, rows, keyword_rows=None, expansion_client=None):
    from app.retrieval import RetrievalService

    repository = FakeChunksRepository(rows, keyword_rows)
    service = RetrievalService(
        settings,
        FakeEmbeddingsClient(),
        repository,
        expansion_client=expansion_client,
    )
    return service, repository


async def test_multi_query_retrieves_every_variant_and_unions(settings):
    config = settings(MULTI_QUERY_ENABLED="true", MULTI_QUERY_COUNT="2")
    rows = [make_chunk_row(0.9, 0)]
    client = FakeExpansionClient("variant one\nvariant two")
    service, repository = make_service(
        config, rows=rows, keyword_rows=rows, expansion_client=client
    )

    chunks = await service.retrieve("original question")

    # 3 variants x both arms; keyword arm receives each variant's text.
    assert len(repository.calls) == 3
    assert [call["query"] for call in repository.keyword_calls] == [
        "original question",
        "variant one",
        "variant two",
    ]
    # Union dedups by chunk id: the same row from 6 rankings appears once.
    assert len(chunks) == 1


async def test_multi_query_off_keeps_the_single_query_pipeline(settings):
    config = settings(MULTI_QUERY_ENABLED="false")
    rows = [make_chunk_row(0.9, 0)]
    client = FakeExpansionClient("variant")
    service, repository = make_service(
        config, rows=rows, keyword_rows=rows, expansion_client=client
    )

    await service.retrieve("q")

    assert client.calls == []
    assert len(repository.calls) == 1
    assert len(repository.keyword_calls) == 1


async def test_multi_query_expansion_failure_degrades_to_single_query(settings):
    config = settings(MULTI_QUERY_ENABLED="true")
    rows = [make_chunk_row(0.9, 0)]
    client = FakeExpansionClient(error=RuntimeError("down"))
    service, repository = make_service(
        config, rows=rows, keyword_rows=rows, expansion_client=client
    )

    chunks = await service.retrieve("q")

    assert len(repository.keyword_calls) == 1
    assert len(chunks) == 1


async def test_mmr_reselects_final_top_k(settings):
    config = settings(MMR_ENABLED="true", MMR_LAMBDA="0.5", RETRIEVAL_CANDIDATE_POOL="3")
    rows = [make_chunk_row(0.9, 0), make_chunk_row(0.8, 1), make_chunk_row(0.7, 2)]
    service, repository = make_service(config, rows=rows)
    # First two rows are duplicates in embedding space; third is orthogonal.
    repository.embeddings = {
        rows[0]["id"]: [1.0, 0.0],
        rows[1]["id"]: [1.0, 0.0],
        rows[2]["id"]: [0.0, 1.0],
    }

    chunks = await service.retrieve("q", k=2)

    assert [c["id"] for c in chunks] == [rows[0]["id"], rows[2]["id"]]


async def test_mmr_off_keeps_fused_order(settings):
    config = settings(MMR_ENABLED="false")
    rows = [make_chunk_row(0.9, 0), make_chunk_row(0.8, 1), make_chunk_row(0.7, 2)]
    service, _ = make_service(config, rows=rows)

    chunks = await service.retrieve("q", k=2)

    assert [c["id"] for c in chunks] == [rows[0]["id"], rows[1]["id"]]
