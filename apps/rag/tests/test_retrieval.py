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
        "content_hash": f"hash-{similarity}-{chunk_index}",
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
        # RT-5: MMR reads stored embeddings through this seam.
        self.embeddings: dict[str, list[float]] = {}
        # RT-6: parent-swap reads full rows through this seam.
        self.by_ids: dict[str, dict[str, Any]] = {}
        self.by_ids_calls: list[list[str]] = []

    async def search_similar(
        self, *, embedding: list[float], k: int, ef_search: int, filters=None, column="embedding"
    ) -> list[dict[str, Any]]:
        self.calls.append(
            {
                "embedding": embedding,
                "k": k,
                "ef_search": ef_search,
                "filters": filters,
                "column": column,
            }
        )
        if self.concurrency_probe is not None:
            self.concurrency_probe["vector_started"].set()
            await self.concurrency_probe["keyword_started"].wait()
        return [dict(row) for row in self.rows[:k]]

    async def fetch_embeddings(self, chunk_ids: list[str]) -> dict[str, list[float]]:
        return {cid: self.embeddings[cid] for cid in chunk_ids if cid in self.embeddings}

    async def fetch_by_ids(self, chunk_ids: list[str]) -> dict[str, dict[str, Any]]:
        self.by_ids_calls.append(chunk_ids)
        return {cid: self.by_ids[cid] for cid in chunk_ids if cid in self.by_ids}

    async def search_keyword(
        self, *, query: str, k: int, filters=None
    ) -> list[dict[str, Any]]:
        self.keyword_calls.append({"query": query, "k": k, "filters": filters})
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
            "content_hash",
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


# ------------------------------------------------- /internal/retrieve (EV-1)
@pytest.fixture
def retrieve_app(settings):
    from app.main import create_app
    from app.retrieval import get_retrieval_service

    application = create_app(settings)

    class FakeRetrieval:
        def __init__(self):
            self.calls = []

        async def retrieve(self, query, *, k=None, filters=None):
            self.calls.append({"query": query, "k": k, "filters": filters})
            return [
                {
                    "id": "c1",
                    "document_id": "d1",
                    "content": "Vacation is 20 days.",
                    "metadata": {},
                    "chunk_index": 0,
                    "content_hash": "hash-1",
                    "similarity": 0.9,
                    "rrf_score": 0.03,
                    "arm_ranks": {0: 1},
                }
            ]

    fake = FakeRetrieval()
    application.dependency_overrides[get_retrieval_service] = lambda: fake
    return application, fake


async def _post_retrieve(app, *, token, json):
    import httpx

    headers = {"authorization": f"Bearer {token}"} if token else {}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://rag") as client:
        return await client.post("/internal/retrieve", headers=headers, json=json)


async def test_retrieve_endpoint_requires_service_jwt(retrieve_app):
    app, _ = retrieve_app
    response = await _post_retrieve(app, token=None, json={"query": "vacation"})
    assert response.status_code == 401


async def test_retrieve_endpoint_returns_chunks_with_content_hash(retrieve_app):
    from tests.test_ingest import make_token

    app, fake = retrieve_app
    response = await _post_retrieve(
        app, token=make_token(), json={"query": "vacation", "k": 5}
    )

    assert response.status_code == 200
    chunk = response.json()["chunks"][0]
    # content_hash is the golden-set binding key (stable across re-index).
    assert chunk == {
        "id": "c1",
        "documentId": "d1",
        "contentHash": "hash-1",
        "content": "Vacation is 20 days.",
        "score": 0.03,
    }
    assert fake.calls == [{"query": "vacation", "k": 5, "filters": None}]


# ------------------------------------------------------- RT-3 rerank stage
class StubReranker:
    model = "stub-model"
    provider = "stub"

    def __init__(self, scores: dict[str, float] | None = None, error: Exception | None = None):
        self.scores = scores or {}
        self.error = error
        self.calls: list[dict[str, Any]] = []

    async def rerank(self, query, chunks):
        self.calls.append({"query": query, "count": len(chunks)})
        if self.error:
            raise self.error
        reordered = [
            {**c, "rerank_score": self.scores.get(c["id"], 0.0)} for c in chunks
        ]
        reordered.sort(key=lambda c: c["rerank_score"], reverse=True)
        return reordered


def make_reranked_service(settings, *, rows, reranker):
    embeddings = FakeEmbeddingsClient()
    repository = FakeChunksRepository(rows, None)
    service = RetrievalService(settings, embeddings, repository, reranker=reranker)
    return service, reranker


async def test_rerank_reorders_the_fused_window_before_top_k(settings):
    rows = [make_chunk_row(0.9, 0), make_chunk_row(0.8, 1), make_chunk_row(0.7, 2)]
    # The cross-encoder disagrees with fusion: last fused chunk is best.
    reranker = StubReranker(scores={rows[2]["id"]: 0.9, rows[1]["id"]: 0.5, rows[0]["id"]: 0.1})
    service, _ = make_reranked_service(settings, rows=rows, reranker=reranker)

    chunks = await service.retrieve("query", k=2)

    assert [c["id"] for c in chunks] == [rows[2]["id"], rows[1]["id"]]
    assert chunks[0]["rerank_score"] == 0.9
    # The whole fused window (all 3 candidates) was scored, not just top-k.
    assert reranker.calls[0]["count"] == 3


async def test_rerank_false_forces_fused_order_for_ab_comparison(settings):
    rows = [make_chunk_row(0.9, 0), make_chunk_row(0.8, 1)]
    reranker = StubReranker(scores={rows[1]["id"]: 1.0})
    service, _ = make_reranked_service(settings, rows=rows, reranker=reranker)

    chunks = await service.retrieve("query", k=2, rerank=False)

    assert [c["id"] for c in chunks] == [rows[0]["id"], rows[1]["id"]]
    assert reranker.calls == []


async def test_rerank_failure_degrades_to_fused_order(settings):
    rows = [make_chunk_row(0.9, 0), make_chunk_row(0.8, 1)]
    reranker = StubReranker(error=RuntimeError("cross-encoder down"))
    service, _ = make_reranked_service(settings, rows=rows, reranker=reranker)

    chunks = await service.retrieve("query", k=2)

    assert [c["id"] for c in chunks] == [rows[0]["id"], rows[1]["id"]]
    assert "rerank_score" not in chunks[0]


async def test_no_reranker_configured_keeps_fused_order(settings):
    rows = [make_chunk_row(0.9, 0), make_chunk_row(0.8, 1)]
    service, _, _ = make_service(settings, rows=rows)

    chunks = await service.retrieve("query", k=2)

    assert [c["id"] for c in chunks] == [rows[0]["id"], rows[1]["id"]]


# ------------------------------------------------------- RT-4 metadata filters
def test_build_metadata_filter_emits_predicates_for_every_supported_key():
    from app.chunks import build_metadata_filter

    clause, params = build_metadata_filter(
        {
            "docTypes": ["pdf", "html"],
            "source": "handbook",
            "dateFrom": "2026-01-01T00:00:00Z",
            "dateTo": "2026-12-31T00:00:00Z",
            "section": "rows 1-50",
        },
        start_index=3,
    )

    assert "d.doc_type = ANY($3::text[])" in clause
    assert "d.source_uri ILIKE $4" in clause
    assert "d.ingested_at >= $5::timestamptz" in clause
    assert "d.ingested_at <= $6::timestamptz" in clause
    assert "chunks.metadata->>'section' ILIKE $7" in clause
    assert params == [
        ["pdf", "html"],
        "%handbook%",
        "2026-01-01T00:00:00Z",
        "2026-12-31T00:00:00Z",
        "%rows 1-50%",
    ]


def test_build_metadata_filter_empty_returns_no_clause():
    from app.chunks import build_metadata_filter

    assert build_metadata_filter(None, start_index=3) == ("", [])
    assert build_metadata_filter({}, start_index=3) == ("", [])
    assert build_metadata_filter({"source": ""}, start_index=3) == ("", [])


def test_filtered_sql_joins_documents_in_both_arms():
    from app.chunks import _keyword_sql, _vector_sql, build_metadata_filter

    clause, _ = build_metadata_filter({"docTypes": ["csv"]}, start_index=3)
    vector = _vector_sql(clause)
    keyword = _keyword_sql(clause)

    for sql in (vector, keyword):
        assert "JOIN documents d ON d.id = chunks.document_id" in sql
        assert "d.doc_type = ANY($3::text[])" in sql
    # Unfiltered SQL has no join at all (identical to the RT-2 base path).
    assert "JOIN" not in _vector_sql("")
    assert "JOIN" not in _keyword_sql("")


async def test_filters_reach_both_arms_pre_fusion(settings):
    rows = [make_chunk_row(0.9, 0)]
    service, _, repository = make_service(settings, rows=rows, keyword_rows=rows)
    filters = {"docTypes": ["pdf"], "source": "manual"}

    await service.retrieve("query", filters=filters)

    assert repository.calls[0]["filters"] == filters
    assert repository.keyword_calls[0]["filters"] == filters


async def test_filter_matching_nothing_returns_empty_not_unfiltered(settings):
    # Repository honoring the filter returns no rows -> retrieve() yields [].
    service, _, _ = make_service(settings, rows=[], keyword_rows=[])

    chunks = await service.retrieve("query", filters={"docTypes": ["pdf"]})

    assert chunks == []


async def test_repository_applies_filter_params_in_vector_arm(settings, monkeypatch):
    row = {**make_chunk_row(0.9), "metadata": json.dumps({"page": 0})}
    connection = RecordingConnection(rows=[row])
    patch_connect(monkeypatch, connection)

    await ChunksRepository(settings).search_similar(
        embedding=[0.1, 0.2],
        k=5,
        ef_search=40,
        filters={"docTypes": ["pdf"], "section": "intro"},
    )

    fetch = next(e for e in connection.events if e[0] == "fetch")
    assert "JOIN documents d ON d.id = chunks.document_id" in fetch[1]
    assert "d.doc_type = ANY($3::text[])" in fetch[1]
    assert "chunks.metadata->>'section' ILIKE $4" in fetch[1]
    assert fetch[2] == ("[0.1,0.2]", 5, ["pdf"], "%intro%")


async def test_filters_are_recorded_in_the_retrieval_trace(settings, caplog):
    rows = [make_chunk_row(0.9, 0)]
    service, _, _ = make_service(settings, rows=rows, keyword_rows=rows)

    with caplog.at_level(logging.INFO, logger="app.retrieval"):
        await service.retrieve("query", filters={"docTypes": ["csv"]})

    record = next(m for m in caplog.messages if "retrieval.retrieved_chunks" in m)
    payload = json.loads(record.split("retrieval.retrieved_chunks ")[1])
    assert payload["filters"] == {"docTypes": ["csv"]}
