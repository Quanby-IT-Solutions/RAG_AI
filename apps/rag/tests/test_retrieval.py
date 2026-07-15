"""Retrieval service tests (RT-1 + RT-2): query embedded with the pinned
model/dims, parallel vector+keyword arms fused by weighted RRF, configurable
k/weights, empty corpus -> empty list, retrieved chunk ids/scores logged per
request, and the DB call wrapped in the hard-timeout helper. No real OpenAI,
no real DB."""

import asyncio
import json
import logging
import uuid
from typing import Any

import pytest

import app.chunks
from app.chunks import ChunksRepository
from app.config import (
    PINNED_EMBEDDING_DIMENSIONS,
    PINNED_EMBEDDING_MODEL,
    Settings,
)
from app.retrieval import RetrievalService
from app.timeouts import ExternalCallTimeoutError
from tests.conftest import REQUIRED_ENV
from tests.test_ingest import FakeEmbeddingsClient


def make_chunk_row(similarity: float, chunk_index: int = 0) -> dict[str, Any]:
    return {
        "id": str(uuid.uuid4()),
        "document_id": str(uuid.uuid4()),
        "content": f"chunk at similarity {similarity}",
        "metadata": {"page": chunk_index},
        "chunk_index": chunk_index,
        "similarity": similarity,
    }


class FakeChunksRepository:
    """Records search args; returns preset rows (already rank-ordered, as the
    SQL ORDER BY guarantees) for each arm."""

    def __init__(
        self,
        rows: list[dict[str, Any]] | None = None,
        keyword_rows: list[dict[str, Any]] | None = None,
    ) -> None:
        self.rows = rows or []
        self.keyword_rows = keyword_rows or []
        self.calls: list[dict[str, Any]] = []
        self.keyword_calls: list[dict[str, Any]] = []
        self.concurrency_probe: dict[str, Any] | None = None

    async def search_similar(
        self, *, embedding: list[float], k: int, ef_search: int
    ) -> list[dict[str, Any]]:
        self.calls.append({"embedding": embedding, "k": k, "ef_search": ef_search})
        if self.concurrency_probe is not None:
            self.concurrency_probe["vector_started"].set()
            await self.concurrency_probe["keyword_started"].wait()
        return [dict(row) for row in self.rows[:k]]

    async def search_keyword(self, *, query: str, k: int) -> list[dict[str, Any]]:
        self.keyword_calls.append({"query": query, "k": k})
        if self.concurrency_probe is not None:
            self.concurrency_probe["keyword_started"].set()
            await self.concurrency_probe["vector_started"].wait()
        return [dict(row) for row in self.keyword_rows[:k]]


def make_service(
    settings: Settings,
    *,
    rows: list[dict[str, Any]] | None = None,
    keyword_rows: list[dict[str, Any]] | None = None,
) -> tuple[RetrievalService, FakeEmbeddingsClient, FakeChunksRepository]:
    embeddings = FakeEmbeddingsClient()
    repository = FakeChunksRepository(rows, keyword_rows)
    return RetrievalService(settings, embeddings, repository), embeddings, repository


# ------------------------------------------------ pinned query embedding (D1)
async def test_query_embedded_with_pinned_model_and_dimensions(settings):
    service, embeddings, repository = make_service(settings)

    await service.retrieve("what is the remote work policy?")

    # The SAME shared client as indexing embeds the query — pinned params.
    assert embeddings.calls == [["what is the remote work policy?"]]
    assert embeddings.model == PINNED_EMBEDDING_MODEL
    assert embeddings.dimensions == PINNED_EMBEDDING_DIMENSIONS
    # The query vector produced by that client is what hits the DB search.
    assert len(repository.calls) == 1
    assert len(repository.calls[0]["embedding"]) == PINNED_EMBEDDING_DIMENSIONS


# ------------------------------------------------------- top-k + configurable k
async def test_top_k_rows_returned_in_similarity_order_with_scores(settings):
    rows = [make_chunk_row(0.95, 0), make_chunk_row(0.80, 1), make_chunk_row(0.60, 2)]
    service, _, repository = make_service(settings, rows=rows)

    chunks = await service.retrieve("query")

    # Keyword arm empty -> fused order preserves the vector arm's ranking.
    assert [c["similarity"] for c in chunks] == [0.95, 0.80, 0.60]
    assert [c["id"] for c in chunks] == [r["id"] for r in rows]
    for chunk in chunks:
        assert set(chunk) == {
            "id",
            "document_id",
            "content",
            "metadata",
            "chunk_index",
            "similarity",
            "rrf_score",
            "arm_ranks",
        }
    # Both arms draw a candidate pool at least the configured size (RT-2/RT-3
    # window); ef_search reaches the vector arm.
    assert repository.calls[0]["k"] == settings.retrieval_candidate_pool == 20
    assert repository.keyword_calls[0]["k"] == 20
    assert repository.calls[0]["ef_search"] == settings.hnsw_ef_search == 40


async def test_per_request_k_override(settings):
    rows = [make_chunk_row(0.9 - i * 0.1, i) for i in range(6)]
    service, _, repository = make_service(settings, rows=rows)

    chunks = await service.retrieve("query", k=2)

    assert len(chunks) == 2
    # Arms still fetch the candidate pool; the FUSED result is cut to k.
    assert repository.calls[0]["k"] == settings.retrieval_candidate_pool


async def test_config_default_k_is_used_when_no_override():
    custom = Settings(_env_file=None, RETRIEVAL_TOP_K=3, HNSW_EF_SEARCH=64, **REQUIRED_ENV)
    service, _, repository = make_service(custom)

    chunks = await service.retrieve("query")

    assert len(chunks) <= 3  # fused list cut to the configured k
    assert repository.calls[0]["ef_search"] == 64


async def test_invalid_k_rejected(settings):
    service, _, _ = make_service(settings)
    with pytest.raises(ValueError):
        await service.retrieve("query", k=0)


# --------------------------------------------------------------- empty corpus
async def test_empty_corpus_returns_empty_list_cleanly(settings):
    service, _, _ = make_service(settings, rows=[])

    chunks = await service.retrieve("query")

    assert chunks == []


# ------------------------------------------- retrieved-chunk logging (RT-1 obs)
async def test_retrieved_chunk_ids_and_scores_logged_per_request(settings, caplog):
    rows = [make_chunk_row(0.91, 0), make_chunk_row(0.77, 1)]
    service, _, _ = make_service(settings, rows=rows)

    with caplog.at_level(logging.INFO, logger="app.retrieval"):
        await service.retrieve("query")

    records = [r for r in caplog.records if "retrieval.retrieved_chunks" in r.getMessage()]
    assert len(records) == 1
    payload = json.loads(records[0].getMessage().split(" ", 1)[1])
    # Clearly labeled as retrieved — NOT the GN-2 "used in prompt" record.
    assert payload["stage"] == "retrieved"
    assert payload["k"] == 5
    assert [c["id"] for c in payload["chunks"]] == [rows[0]["id"], rows[1]["id"]]
    assert payload["weights"] == {"vector": 0.5, "keyword": 0.5}
    assert [c["id"] for c in payload["vector_arm"]] == [rows[0]["id"], rows[1]["id"]]
    assert payload["keyword_arm"] == []


async def test_empty_retrieval_still_logs_the_request(settings, caplog):
    service, _, _ = make_service(settings, rows=[])

    with caplog.at_level(logging.INFO, logger="app.retrieval"):
        await service.retrieve("query")

    records = [r for r in caplog.records if "retrieval.retrieved_chunks" in r.getMessage()]
    assert len(records) == 1
    assert json.loads(records[0].getMessage().split(" ", 1)[1])["chunks"] == []


# --------------------------------------- ChunksRepository (SQL layer, fake conn)
class RecordingConnection:
    """asyncpg.Connection stand-in: records the SET LOCAL + fetch sequence and
    whether each ran inside the transaction. Doubles as its own transaction
    context manager."""

    def __init__(self, rows: list[dict[str, Any]] | None = None, hang: bool = False) -> None:
        self.rows = rows or []
        self.hang = hang
        self.events: list[tuple[str, Any]] = []
        self.in_transaction = False
        self.closed = False

    def transaction(self) -> "RecordingConnection":
        return self

    async def __aenter__(self) -> "RecordingConnection":
        self.in_transaction = True
        self.events.append(("begin", None))
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        self.in_transaction = False
        self.events.append(("commit", None))
        return False

    async def execute(self, sql: str, *args: Any) -> None:
        self.events.append(("execute", sql, self.in_transaction))

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        if self.hang:
            await asyncio.sleep(60)
        self.events.append(("fetch", sql, args, self.in_transaction))
        return self.rows

    async def close(self) -> None:
        self.closed = True


def patch_connect(monkeypatch, connection: RecordingConnection) -> None:
    async def fake_connect(dsn: str) -> RecordingConnection:
        return connection

    monkeypatch.setattr(app.chunks.asyncpg, "connect", fake_connect)


async def test_repository_sets_ef_search_locally_inside_the_query_transaction(
    settings, monkeypatch
):
    row = {**make_chunk_row(0.9), "metadata": json.dumps({"page": 0})}
    connection = RecordingConnection(rows=[row])
    patch_connect(monkeypatch, connection)

    result = await ChunksRepository(settings).search_similar(
        embedding=[0.1, 0.2], k=5, ef_search=40
    )

    execute = next(e for e in connection.events if e[0] == "execute")
    assert execute[1] == "SET LOCAL hnsw.ef_search = 40"
    assert execute[2] is True  # inside the transaction (SET LOCAL scope)

    fetch = next(e for e in connection.events if e[0] == "fetch")
    assert "embedding <=>" in fetch[1]  # cosine distance, matches the HNSW opclass
    assert fetch[2] == ("[0.1,0.2]", 5)  # vector literal + LIMIT bind
    assert fetch[3] is True

    assert connection.closed is True
    # jsonb string metadata is decoded; similarity is a float.
    assert result[0]["metadata"] == {"page": 0}
    assert result[0]["similarity"] == 0.9


async def test_repository_rejects_invalid_ef_search(settings, monkeypatch):
    patch_connect(monkeypatch, RecordingConnection())
    with pytest.raises(ValueError):
        await ChunksRepository(settings).search_similar(embedding=[0.1], k=5, ef_search=0)


async def test_repository_empty_corpus_returns_empty_list(settings, monkeypatch):
    patch_connect(monkeypatch, RecordingConnection(rows=[]))

    result = await ChunksRepository(settings).search_similar(embedding=[0.1], k=5, ef_search=40)

    assert result == []


# --------------------------------------------------- hard DB timeout (F1 helper)
async def test_search_runs_under_the_hard_db_timeout(monkeypatch):
    fast_timeout = Settings(_env_file=None, DB_TIMEOUT_SECONDS=0.05, **REQUIRED_ENV)
    connection = RecordingConnection(hang=True)
    patch_connect(monkeypatch, connection)

    with pytest.raises(ExternalCallTimeoutError) as excinfo:
        await ChunksRepository(fast_timeout).search_similar(embedding=[0.1], k=5, ef_search=40)

    assert excinfo.value.operation == "chunks.search_similar"
    assert connection.closed is True  # connection not leaked on timeout


# ------------------------------------------------------ hybrid + RRF (RT-2)
async def test_vector_and_keyword_arms_run_concurrently(settings):
    """Each arm blocks until the OTHER has started — the test only completes
    if both really run in parallel (asyncio.gather), else it deadlocks and
    the hard 5s timeout fails it."""
    rows = [make_chunk_row(0.9, 0)]
    service, _, repository = make_service(settings, rows=rows, keyword_rows=rows)
    repository.concurrency_probe = {
        "vector_started": asyncio.Event(),
        "keyword_started": asyncio.Event(),
    }

    chunks = await asyncio.wait_for(service.retrieve("query"), timeout=5)

    assert len(chunks) == 1
    assert len(repository.calls) == 1 and len(repository.keyword_calls) == 1


async def test_rrf_fuses_both_arms_by_weighted_reciprocal_rank(settings):
    shared = make_chunk_row(0.9, 0)  # rank 1 in vector AND rank 2 in keyword
    vector_only = make_chunk_row(0.8, 1)
    keyword_only = make_chunk_row(0.0, 2)
    service, _, _ = make_service(
        settings,
        rows=[shared, vector_only],
        keyword_rows=[keyword_only, shared],
    )

    chunks = await service.retrieve("query")

    scores = {c["id"]: c["rrf_score"] for c in chunks}
    rrf_k = settings.rrf_k
    assert scores[shared["id"]] == pytest.approx(0.5 / (rrf_k + 1) + 0.5 / (rrf_k + 2))
    assert scores[vector_only["id"]] == pytest.approx(0.5 / (rrf_k + 2))
    assert scores[keyword_only["id"]] == pytest.approx(0.5 / (rrf_k + 1))
    # Present-in-both ranks first; the two single-arm rank-1/rank-2 entries follow.
    assert chunks[0]["id"] == shared["id"]
    assert chunks[0]["arm_ranks"] == {"0": 1, "1": 2} or chunks[0]["arm_ranks"] == {0: 1, 1: 2}


async def test_exact_rare_term_found_by_keyword_arm_even_when_vector_misses(settings):
    """RT-2 regression AC: a chunk containing an exact rare term (e.g. an
    error code) missed by vector similarity still appears in fused results."""
    vector_hits = [make_chunk_row(0.9 - i * 0.1, i) for i in range(3)]
    rare = {**make_chunk_row(0.99, 9), "content": "error code QX-1441 remediation steps"}
    service, _, _ = make_service(settings, rows=vector_hits, keyword_rows=[rare])

    chunks = await service.retrieve("what is QX-1441?")

    assert rare["id"] in {c["id"] for c in chunks}


async def test_weights_shift_the_ranking(settings):
    vector_first = make_chunk_row(0.9, 0)
    keyword_first = make_chunk_row(0.0, 1)
    service, _, _ = make_service(
        settings, rows=[vector_first], keyword_rows=[keyword_first]
    )

    balanced = await service.retrieve("query")
    keyword_heavy = await service.retrieve("query", vector_weight=0.1, keyword_weight=0.9)
    vector_heavy = await service.retrieve("query", vector_weight=0.9, keyword_weight=0.1)

    # 50/50 with equal ranks: tie broken by insertion, but the heavy overrides
    # MUST order deterministically toward the weighted arm.
    assert keyword_heavy[0]["id"] == keyword_first["id"]
    assert vector_heavy[0]["id"] == vector_first["id"]
    assert {c["id"] for c in balanced} == {vector_first["id"], keyword_first["id"]}


async def test_zero_weight_skips_an_arm_entirely(settings):
    rows = [make_chunk_row(0.9, 0)]
    service, embeddings, repository = make_service(settings, rows=rows, keyword_rows=rows)

    chunks = await service.retrieve("query", vector_weight=0, keyword_weight=1)

    # Vector arm skipped: no query embedding call, no similarity search.
    assert embeddings.calls == []
    assert repository.calls == []
    assert len(repository.keyword_calls) == 1
    assert len(chunks) == 1


async def test_repository_keyword_search_uses_websearch_tsquery(settings, monkeypatch):
    row = {
        **make_chunk_row(0.0),
        "metadata": json.dumps({"section": "rows 1-2"}),
        "rank": 0.42,
    }
    connection = RecordingConnection(rows=[row])
    patch_connect(monkeypatch, connection)

    result = await ChunksRepository(settings).search_keyword(query="exact term", k=7)

    fetch = next(e for e in connection.events if e[0] == "fetch")
    assert "websearch_to_tsquery" in fetch[1]
    assert "tsv @@" in fetch[1]
    assert fetch[2] == ("exact term", 7)
    assert result[0]["metadata"] == {"section": "rows 1-2"}
    assert result[0]["similarity"] == pytest.approx(0.42)
