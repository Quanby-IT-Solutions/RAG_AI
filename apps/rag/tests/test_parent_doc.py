"""RT-6 parent-document retrieval tests."""

from typing import Any

import pytest

from app.indexing import index_document_content
from tests.conftest import REQUIRED_ENV
from tests.test_ingest import FakeEmbeddingsClient
from tests.test_retrieval import FakeChunksRepository, make_chunk_row
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


class CapturingRepository:
    def __init__(self):
        self.indexed: dict[str, Any] | None = None

    async def index_chunks(self, *, document_id, chunks, metadata_patch=None):
        self.indexed = {"documentId": document_id, "chunks": chunks, "patch": metadata_patch}
        return {"id": document_id, "status": "indexed", "error": None}

    async def mark_document_failed(self, document_id, reason):
        raise AssertionError(f"unexpected failure: {reason}")


def make_document(text: str) -> dict[str, Any]:
    return {
        "id": "doc-1",
        "content_hash": "doc-hash",
        "metadata": {"text": text},
    }


# --------------------------------------------------------------- ingestion
async def test_parent_doc_mode_builds_linked_parents(settings):
    config = settings(
        PARENT_DOC_ENABLED="true",
        PARENT_GROUP_SIZE="2",
        CHUNKING_STRATEGY="recursive",
        CHUNK_MIN_CHARS="10",
        CHUNK_MAX_CHARS="60",
    )
    repository = CapturingRepository()
    text = " ".join(f"sentence number {i} about policies." for i in range(12))

    outcome = await index_document_content(
        document=make_document(text),
        settings=config,
        repository=repository,
        embeddings=FakeEmbeddingsClient(),
        embedding_cache=NullEmbeddingCache(),
    )

    assert outcome["status"] == "indexed"
    rows = repository.indexed["chunks"]
    parents = [r for r in rows if (r.get("metadata") or {}).get("role") == "parent"]
    children = [r for r in rows if r not in parents]
    assert parents, "parent rows expected in parent-doc mode"
    # Group size 2: every parent covers at most 2 children.
    for parent in parents:
        assert 1 <= len(parent["metadata"]["childIndexes"]) <= 2
        assert parent["embedding"], "parents are embedded like any chunk"
        assert parent["content_hash"]
    # Every child links to an existing parent hash.
    parent_hashes = {parent["content_hash"] for parent in parents}
    for child in children:
        assert child["parent_hash"] in parent_hashes
    # Parent content is the concatenation of its children's content.
    first = parents[0]
    member_contents = [
        child["content"]
        for child in children
        if child["chunk_index"] in first["metadata"]["childIndexes"]
    ]
    assert first["content"] == "\n\n".join(member_contents)


async def test_parent_doc_off_emits_no_parent_rows(settings):
    config = settings(
        PARENT_DOC_ENABLED="false",
        CHUNKING_STRATEGY="recursive",
        CHUNK_MIN_CHARS="10",
        CHUNK_MAX_CHARS="60",
    )
    repository = CapturingRepository()
    text = " ".join(f"sentence number {i} about policies." for i in range(12))

    await index_document_content(
        document=make_document(text),
        settings=config,
        repository=repository,
        embeddings=FakeEmbeddingsClient(),
        embedding_cache=NullEmbeddingCache(),
    )

    rows = repository.indexed["chunks"]
    assert all((r.get("metadata") or {}).get("role") != "parent" for r in rows)
    assert all("parent_hash" not in r for r in rows)


# ------------------------------------------------------------ SQL exclusion
def test_search_sql_excludes_parent_chunks_in_both_arms():
    from app.chunks import _keyword_sql, _vector_sql

    for sql in (_vector_sql(""), _keyword_sql("")):
        assert "chunks.metadata->>'role' IS DISTINCT FROM 'parent'" in sql
        assert "parent_chunk_id" in sql  # child rows expose the link


# ------------------------------------------------------------- parent swap
def child_row(similarity: float, index: int, parent_id: str | None) -> dict[str, Any]:
    row = make_chunk_row(similarity, index)
    row["parent_chunk_id"] = parent_id
    return row


def parent_payload(pid: str) -> dict[str, Any]:
    return {
        "id": pid,
        "document_id": "d",
        "content": f"parent content {pid}",
        "metadata": {"role": "parent"},
        "chunk_index": 100_000,
        "content_hash": f"parent-hash-{pid}",
        "parent_chunk_id": None,
    }


def make_service(config, rows):
    from app.retrieval import RetrievalService

    repository = FakeChunksRepository(rows, None)
    service = RetrievalService(config, FakeEmbeddingsClient(), repository)
    return service, repository


async def test_children_swap_for_deduped_parents_with_scores_carried(settings):
    config = settings(PARENT_DOC_ENABLED="true")
    rows = [
        child_row(0.9, 0, "p1"),
        child_row(0.8, 1, "p1"),  # same parent -> dedup
        child_row(0.7, 2, "p2"),
    ]
    service, repository = make_service(config, rows)
    repository.by_ids = {"p1": parent_payload("p1"), "p2": parent_payload("p2")}

    chunks = await service.retrieve("q", k=3)

    assert [c["id"] for c in chunks] == ["p1", "p2"]
    # The parent inherits the FIRST matching child's ranking scores.
    assert chunks[0]["similarity"] == 0.9
    assert chunks[0]["rrf_score"] is not None


async def test_chunks_without_parents_pass_through(settings):
    config = settings(PARENT_DOC_ENABLED="true")
    rows = [child_row(0.9, 0, None), child_row(0.8, 1, "p1")]
    service, repository = make_service(config, rows)
    repository.by_ids = {"p1": parent_payload("p1")}

    chunks = await service.retrieve("q", k=2)

    assert [c["id"] for c in chunks] == [rows[0]["id"], "p1"]


async def test_parent_doc_off_returns_children_unchanged(settings):
    config = settings(PARENT_DOC_ENABLED="false")
    rows = [child_row(0.9, 0, "p1")]
    service, repository = make_service(config, rows)
    repository.by_ids = {"p1": parent_payload("p1")}

    chunks = await service.retrieve("q", k=1)

    assert chunks[0]["id"] == rows[0]["id"]
    assert repository.by_ids_calls == []


async def test_matched_children_are_recorded_in_the_trace(settings, caplog):
    import json
    import logging

    config = settings(PARENT_DOC_ENABLED="true")
    rows = [child_row(0.9, 0, "p1")]
    service, repository = make_service(config, rows)
    repository.by_ids = {"p1": parent_payload("p1")}

    with caplog.at_level(logging.INFO, logger="app.retrieval"):
        await service.retrieve("q", k=1)

    record = next(m for m in caplog.messages if "retrieval.retrieved_chunks" in m)
    payload = json.loads(record.split("retrieval.retrieved_chunks ")[1])
    assert payload["parentDoc"]["requested"] is True
    assert payload["parentDoc"]["matchedChildren"] == [
        {"childId": rows[0]["id"], "parentId": "p1"}
    ]
