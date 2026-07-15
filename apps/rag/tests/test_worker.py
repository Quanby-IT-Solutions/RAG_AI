"""arq worker tests (IN-7): the queued job runs the full Flow A pipeline
(chunk -> ONE batched embed call -> transactional index), records per-document
outcomes, accumulates job stats, continues past per-document failures, marks
the job failed only when everything failed, audits the run, and re-runs
idempotently (upserts — chunk counts stay stable, no duplicates)."""

import hashlib
from datetime import UTC, datetime
from typing import Any

import pytest

from app.config import PINNED_EMBEDDING_DIMENSIONS, PINNED_EMBEDDING_MODEL, Settings
from app.loaders import normalize_csv
from app.worker import process_ingestion_job
from tests.conftest import REQUIRED_ENV
from tests.test_ingest import FakeEmbeddingsClient, FakeJobsRepository, FakeRepository

TWO_PAGE_TEXTS = [
    "Employee handbook. " * 120,
    "Remote work policy. " * 40,
]


@pytest.fixture
def settings() -> Settings:
    return Settings(_env_file=None, **REQUIRED_ENV)


@pytest.fixture
def repository() -> FakeRepository:
    return FakeRepository()


@pytest.fixture
def jobs(repository) -> FakeJobsRepository:
    return FakeJobsRepository(docs=repository)


@pytest.fixture
def embeddings() -> FakeEmbeddingsClient:
    return FakeEmbeddingsClient()


def make_ctx(settings, repository, jobs, embeddings) -> dict[str, Any]:
    return {"settings": settings, "repository": repository, "jobs": jobs, "embeddings": embeddings}


def seed_document(repository: FakeRepository, *, text: str, pages=None) -> dict[str, Any]:
    content_hash = hashlib.sha256(text.encode()).hexdigest()
    return repository.seed(
        content_hash,
        "processing",
        metadata={
            "originalFilename": "doc.pdf",
            "ingestedAt": datetime.now(UTC).isoformat(),
            "text": text,
            **({"pages": pages} if pages else {}),
        },
    )


async def run_job(settings, repository, jobs, embeddings, document_ids) -> tuple[str, dict]:
    job = await jobs.create_job(kind="ingest", triggered_by="user-1", document_ids=document_ids)
    result = await process_ingestion_job(
        make_ctx(settings, repository, jobs, embeddings), job["id"]
    )
    return job["id"], result


# ------------------------------------------------------------------ happy path
async def test_job_indexes_document_and_completes(settings, repository, jobs, embeddings):
    text = "\n\n".join(TWO_PAGE_TEXTS)
    pages = [{"page": i, "chars": len(t)} for i, t in enumerate(TWO_PAGE_TEXTS)]
    document = seed_document(repository, text=text, pages=pages)

    job_id, result = await run_job(settings, repository, jobs, embeddings, [document["id"]])

    assert result["status"] == "completed"
    assert jobs.jobs[job_id]["status"] == "completed"
    stats = jobs.jobs[job_id]["stats"]
    assert stats["documents"] == 1
    assert stats["chunks"] == len(repository.chunks) > 0
    assert stats["embedBatches"] >= 1
    assert stats["failures"] == []

    # Document flipped to indexed; chunks carry the pinned model + dimensions.
    row = repository._row_by_id(document["id"])
    assert row["status"] == "indexed"
    for chunk in repository.chunks.values():
        assert chunk["embedding_model"] == PINNED_EMBEDDING_MODEL
        assert len(chunk["embedding"]) == PINNED_EMBEDDING_DIMENSIONS

    # Per-document outcome recorded for the console.
    outcome = jobs.job_documents[job_id][0]
    assert outcome["status"] == "indexed"
    assert outcome["chunk_count"] == stats["chunks"]

    # The run is audited (Flow E) with the job stats.
    audit = repository.audits[-1]
    assert audit["action"] == "corpus.ingest.run"
    assert audit["resource_type"] == "ingestion_job"
    assert audit["resource_id"] == job_id
    assert audit["after"]["status"] == "completed"


async def test_embedding_calls_are_batched(settings, repository, jobs, embeddings):
    document = seed_document(repository, text="Employee handbook. " * 200)

    job_id, _ = await run_job(settings, repository, jobs, embeddings, [document["id"]])

    chunk_count = jobs.jobs[job_id]["stats"]["chunks"]
    assert chunk_count > 1
    # One sentence pass (IN-4 boundary detection) + ONE call for all N chunks.
    assert len(embeddings.calls) == 2
    assert len(embeddings.calls[-1]) == chunk_count


async def test_csv_sections_flow_into_chunks(settings, repository, jobs, embeddings):
    rows = "\n".join(f"item {i},{'description ' * 30}{i}" for i in range(1, 120))
    normalized = normalize_csv(
        f"item,description\n{rows}\n".encode(), "items.csv", "upload://items.csv"
    )
    content_hash = hashlib.sha256(b"items-csv").hexdigest()
    document = repository.seed(content_hash, "processing", doc_type="csv", metadata=normalized)

    await run_job(settings, repository, jobs, embeddings, [document["id"]])

    sections = {chunk["metadata"].get("section") for chunk in repository.chunks.values()}
    assert "rows 1-50" in sections
    assert "rows 51-100" in sections


# ------------------------------------------------------------- failure branch
async def test_embed_failure_marks_document_and_job_failed(
    settings, repository, jobs, embeddings
):
    embeddings.fail = True
    document = seed_document(repository, text="Employee handbook. " * 100)

    job_id, result = await run_job(settings, repository, jobs, embeddings, [document["id"]])

    # Single document, and it failed -> the job reports failed.
    assert result["status"] == "failed"
    assert jobs.jobs[job_id]["status"] == "failed"
    assert "embedding provider down" in jobs.jobs[job_id]["error"]

    row = repository._row_by_id(document["id"])
    assert row["status"] == "failed"  # never left stuck in processing
    assert repository.chunks == {}
    assert jobs.job_documents[job_id][0]["status"] == "failed"


async def test_one_failure_does_not_stop_remaining_documents(
    settings, repository, jobs, embeddings
):
    """Flow A failure branch: a document failing mid-pipeline is recorded and
    the job CONTINUES with the remaining documents."""
    good = seed_document(repository, text="Employee handbook. " * 100)
    # A job entry whose document row no longer exists -> per-document failure.
    job = await jobs.create_job(
        kind="ingest", triggered_by="user-1", document_ids=["missing-doc", good["id"]]
    )

    result = await process_ingestion_job(
        make_ctx(settings, repository, jobs, embeddings), job["id"]
    )

    assert result["status"] == "completed"  # partial failure != failed job
    stats = jobs.jobs[job["id"]]["stats"]
    assert len(stats["failures"]) == 1
    assert stats["failures"][0]["documentId"] == "missing-doc"
    assert repository._row_by_id(good["id"])["status"] == "indexed"

    outcomes = {e["document_id"]: e["status"] for e in jobs.job_documents[job["id"]]}
    assert outcomes == {"missing-doc": "failed", good["id"]: "indexed"}


async def test_chunk_write_failure_marks_document_failed(settings, repository, jobs, embeddings):
    repository.fail_index = True
    document = seed_document(repository, text="Employee handbook. " * 100)

    job_id, result = await run_job(settings, repository, jobs, embeddings, [document["id"]])

    assert result["status"] == "failed"
    assert "chunks write failed" in jobs.job_documents[job_id][0]["error"]
    assert repository._row_by_id(document["id"])["status"] == "failed"


# ----------------------------------------------------------------- idempotency
async def test_rerun_keeps_chunk_count_stable(settings, repository, jobs, embeddings):
    document = seed_document(repository, text="Employee handbook. " * 200)

    job_id, _ = await run_job(settings, repository, jobs, embeddings, [document["id"]])
    first_count = len(repository.chunks)
    assert first_count > 1

    # Re-run the finished job (console button path): reset + process again.
    assert await jobs.requeue(job_id)
    repository.rows[document["content_hash"]]["status"] = "processing"
    result = await process_ingestion_job(
        make_ctx(settings, repository, jobs, embeddings), job_id
    )

    assert result["status"] == "completed"
    # Upsert on chunk content_hash: same rows, no duplicates.
    assert len(repository.chunks) == first_count
    assert repository.index_calls == 2  # the pipeline really ran twice
