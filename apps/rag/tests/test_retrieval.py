"""Retrieval service tests (RT-1): query embedded with the pinned model/dims,
top-k rows returned in similarity order with configurable k, empty corpus ->
empty list, retrieved chunk ids/scores logged per request, and the DB call
wrapped in the hard-timeout helper. No real OpenAI, no real DB."""

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
    """Records search args; returns preset rows (already similarity-ordered,
    as the SQL ORDER BY guarantees)."""

    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.rows = rows or []
        self.calls: list[dict[str, Any]] = []

    async def search_similar(
        self, *, embedding: list[float], k: int, ef_search: int
    ) -> list[dict[str, Any]]:
        self.calls.append({"embedding": embedding, "k": k, "ef_search": ef_search})
        return [dict(row) for row in self.rows[:k]]


def make_service(
    settings: Settings,
    *,
    rows: list[dict[str, Any]] | None = None,
) -> tuple[RetrievalService, FakeEmbeddingsClient, FakeChunksRepository]:
    embeddings = FakeEmbeddingsClient()
    repository = FakeChunksRepository(rows)
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
        }
    # Config default k and ef_search reach the repository.
    assert repository.calls[0]["k"] == settings.retrieval_top_k == 5
    assert repository.calls[0]["ef_search"] == settings.hnsw_ef_search == 40


async def test_per_request_k_override(settings):
    rows = [make_chunk_row(0.9 - i * 0.1, i) for i in range(6)]
    service, _, repository = make_service(settings, rows=rows)

    chunks = await service.retrieve("query", k=2)

    assert len(chunks) == 2
    assert repository.calls[0]["k"] == 2


async def test_config_default_k_is_used_when_no_override():
    custom = Settings(_env_file=None, RETRIEVAL_TOP_K=3, HNSW_EF_SEARCH=64, **REQUIRED_ENV)
    service, _, repository = make_service(custom)

    await service.retrieve("query")

    assert repository.calls[0]["k"] == 3
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
    assert payload["chunks"] == [
        {"id": rows[0]["id"], "similarity": 0.91},
        {"id": rows[1]["id"], "similarity": 0.77},
    ]


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
