"""Ingest endpoint tests (IN-1 + IN-3 + IN-7): service-JWT auth, defensive
re-checks, Flow A idempotency (A1), corrupt-file failure (A2), and the IN-7
queue path — upload returns immediately with a queued job id; the pipeline
itself is exercised in tests/test_worker.py. DB is mocked with an in-memory
repository mirroring the ON CONFLICT upsert semantics; OpenAI is a fake
embeddings client — real providers are never called."""

import hashlib
import io
import time
import uuid
from datetime import UTC, datetime
from typing import Any

import httpx
import jwt
import pytest

from app.config import PINNED_EMBEDDING_DIMENSIONS, PINNED_EMBEDDING_MODEL
from app.ingest import (
    INGESTION_TASK_NAME,
    get_documents_repository,
    get_embeddings_client,
    get_job_queue,
    get_jobs_repository,
)
from app.main import create_app
from tests.conftest import REQUIRED_ENV

SECRET = REQUIRED_ENV["RAG_SERVICE_TOKEN_SECRET"]
PERMISSIONS = ["corpus.documents.upload", "corpus.ingest.run"]


def make_token(
    *,
    secret: str = SECRET,
    sub: str = "user-1",
    expires_in: int = 60,
    actor_type: str = "user",
) -> str:
    now = int(time.time())
    return jwt.encode(
        {
            "sub": sub,
            "permissions": PERMISSIONS,
            "requestId": "req-1",
            "traceId": "trace-1",
            "actorType": actor_type,
            "iat": now,
            "exp": now + expires_in,
        },
        secret,
        algorithm="HS256",
    )


def make_pdf_bytes() -> bytes:
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


class FakeRepository:
    """In-memory stand-in mirroring the SQL upsert semantics (see documents.py):
    keyed on content_hash, never updating an `indexed` row."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        self.chunks: dict[str, dict[str, Any]] = {}
        self.audits: list[dict[str, Any]] = []
        self.upsert_calls = 0
        self.index_calls = 0
        self.fail_index = False

    def seed(self, content_hash: str, status: str, **overrides: Any) -> dict[str, Any]:
        row = {
            "id": str(uuid.uuid4()),
            "source_uri": "upload://seeded.pdf",
            "doc_type": "pdf",
            "content_hash": content_hash,
            "status": status,
            "metadata": {"originalFilename": "seeded.pdf", "pageCount": 1},
            "error": overrides.pop("error", None),
            "ingested_at": datetime.now(UTC).isoformat(),
            **overrides,
        }
        self.rows[content_hash] = row
        return row

    async def fetch_by_content_hash(self, content_hash: str) -> dict[str, Any] | None:
        row = self.rows.get(content_hash)
        return dict(row) if row else None

    async def fetch_by_id(self, document_id: str) -> dict[str, Any] | None:
        row = self._row_by_id(document_id)
        return dict(row) if row else None

    async def upsert_document(
        self,
        *,
        source_uri: str,
        doc_type: str,
        content_hash: str,
        status: str,
        metadata: dict[str, Any],
        error: str | None,
    ) -> dict[str, Any] | None:
        self.upsert_calls += 1
        existing = self.rows.get(content_hash)
        if existing is not None and existing["status"] == "indexed":
            return None
        row = {
            "id": existing["id"] if existing else str(uuid.uuid4()),
            "source_uri": source_uri,
            "doc_type": doc_type,
            "content_hash": content_hash,
            "status": status,
            "metadata": metadata,
            "error": error,
            "ingested_at": datetime.now(UTC).isoformat(),
        }
        self.rows[content_hash] = row
        return dict(row)

    def _row_by_id(self, document_id: str) -> dict[str, Any] | None:
        for row in self.rows.values():
            if row["id"] == document_id:
                return row
        return None

    async def index_chunks(
        self,
        *,
        document_id: str,
        chunks: list[dict[str, Any]],
        metadata_patch: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Mirrors documents.py: stale-hash delete + chunk upsert ON CONFLICT
        (content_hash) + status flip to indexed + metadata merge, atomically."""
        self.index_calls += 1
        if self.fail_index:
            raise RuntimeError("chunks write failed")
        new_hashes = {chunk["content_hash"] for chunk in chunks}
        self.chunks = {
            content_hash: chunk
            for content_hash, chunk in self.chunks.items()
            if chunk["document_id"] != document_id or content_hash in new_hashes
        }
        for chunk in chunks:
            self.chunks[chunk["content_hash"]] = {**chunk, "document_id": document_id}
        row = self._row_by_id(document_id)
        if row is None:
            return None
        row["status"] = "indexed"
        row["error"] = None
        row["metadata"] = {**(row["metadata"] or {}), **(metadata_patch or {})}
        return dict(row)

    async def mark_document_failed(self, document_id: str, reason: str) -> dict[str, Any] | None:
        row = self._row_by_id(document_id)
        if row is None:
            return None
        row["status"] = "failed"
        row["error"] = reason
        return dict(row)

    async def write_audit(self, **entry: Any) -> None:
        self.audits.append(entry)


class FakeJobsRepository:
    """In-memory ingestion_jobs/job_documents mirror (IN-7). `docs` links to
    the FakeRepository so fetch_job_documents can join document columns the
    way the SQL does."""

    def __init__(self, docs: "FakeRepository | None" = None) -> None:
        self.docs = docs
        self.jobs: dict[str, dict[str, Any]] = {}
        self.job_documents: dict[str, list[dict[str, Any]]] = {}

    async def create_job(
        self, *, kind: str, triggered_by: str, document_ids: list[str]
    ) -> dict[str, Any]:
        job_id = str(uuid.uuid4())
        job = {
            "id": job_id,
            "kind": kind,
            "status": "queued",
            "triggered_by": triggered_by,
            "stats": None,
            "error": None,
            "created_at": datetime.now(UTC).isoformat(),
            "started_at": None,
            "finished_at": None,
        }
        self.jobs[job_id] = job
        self.job_documents[job_id] = [
            {"document_id": document_id, "status": "pending", "chunk_count": None, "error": None}
            for document_id in document_ids
        ]
        return dict(job)

    async def mark_running(self, job_id: str) -> None:
        self.jobs[job_id]["status"] = "running"

    async def finish(
        self, job_id: str, *, status: str, stats: dict[str, Any], error: str | None
    ) -> None:
        self.jobs[job_id].update(
            status=status,
            stats=stats,
            error=error,
            finished_at=datetime.now(UTC).isoformat(),
        )

    async def requeue(self, job_id: str) -> bool:
        job = self.jobs.get(job_id)
        if job is None or job["status"] not in ("completed", "failed"):
            return False
        job.update(status="queued", error=None, started_at=None, finished_at=None)
        for entry in self.job_documents[job_id]:
            entry.update(status="pending", error=None)
        return True

    async def set_document_outcome(
        self,
        *,
        job_id: str,
        document_id: str,
        status: str,
        chunk_count: int | None,
        error: str | None,
    ) -> None:
        for entry in self.job_documents[job_id]:
            if entry["document_id"] == document_id:
                entry.update(status=status, chunk_count=chunk_count, error=error)

    async def fetch_job(self, job_id: str) -> dict[str, Any] | None:
        job = self.jobs.get(job_id)
        return dict(job) if job else None

    async def fetch_job_documents(self, job_id: str) -> list[dict[str, Any]]:
        entries = []
        for entry in self.job_documents.get(job_id, []):
            row = None
            if self.docs is not None:
                row = self.docs._row_by_id(entry["document_id"])
            entries.append(
                {
                    **entry,
                    "updated_at": datetime.now(UTC).isoformat(),
                    "source_uri": row["source_uri"] if row else "upload://missing",
                    "doc_type": row["doc_type"] if row else "pdf",
                    "metadata": row["metadata"] if row else None,
                }
            )
        return entries


class FakeJobQueue:
    def __init__(self) -> None:
        self.enqueued: list[tuple[str, ...]] = []

    async def enqueue_job(self, function: str, *args: Any) -> None:
        self.enqueued.append((function, *args))


class FakeEmbeddingsClient:
    """Deterministic stand-in for the OpenAI batch client; records every
    provider call so tests can assert batching (one call for N chunks)."""

    def __init__(self, *, fail: bool = False) -> None:
        self.model = PINNED_EMBEDDING_MODEL
        self.dimensions = PINNED_EMBEDDING_DIMENSIONS
        self.calls: list[list[str]] = []
        self.fail = fail

    async def embed_texts(self, texts: list[str]) -> tuple[list[list[float]], int]:
        self.calls.append(list(texts))
        if self.fail:
            raise RuntimeError("embedding provider down")
        return [[float(i)] * self.dimensions for i in range(len(texts))], 1


@pytest.fixture
def repository() -> FakeRepository:
    return FakeRepository()


@pytest.fixture
def embeddings() -> FakeEmbeddingsClient:
    return FakeEmbeddingsClient()


@pytest.fixture
def jobs(repository) -> FakeJobsRepository:
    return FakeJobsRepository(docs=repository)


@pytest.fixture
def queue() -> FakeJobQueue:
    return FakeJobQueue()


@pytest.fixture
def app(settings, repository, embeddings, jobs, queue):
    application = create_app(settings)
    application.dependency_overrides[get_documents_repository] = lambda: repository
    application.dependency_overrides[get_embeddings_client] = lambda: embeddings
    application.dependency_overrides[get_jobs_repository] = lambda: jobs
    application.dependency_overrides[get_job_queue] = lambda: queue
    return application


TWO_PAGE_TEXTS = [
    "Employee handbook. " * 120,  # ~2280 chars -> multiple chunks
    "Remote work policy. " * 40,
]


def fake_two_page_normalize(raw: bytes, filename: str, source_uri: str) -> dict[str, Any]:
    """Deterministic stand-in for _normalize_pdf: same payload shape (IN-1),
    real multi-page text so chunking has something to split."""
    return {
        "source": source_uri,
        "originalFilename": filename,
        "ingestedAt": datetime.now(UTC).isoformat(),
        "pageCount": len(TWO_PAGE_TEXTS),
        "pages": [{"page": index, "chars": len(page)} for index, page in enumerate(TWO_PAGE_TEXTS)],
        "text": "\n\n".join(TWO_PAGE_TEXTS),
    }


async def post_ingest(
    app,
    *,
    token: str | None,
    content: bytes,
    content_type: str = "application/pdf",
    filename: str = "handbook.pdf",
) -> httpx.Response:
    headers = {"authorization": f"Bearer {token}"} if token else {}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://rag") as client:
        return await client.post(
            "/internal/ingest",
            headers=headers,
            files={"file": (filename, content, content_type)},
        )


# ---------------------------------------------------------------- service JWT
async def test_rejects_missing_token(app):
    response = await post_ingest(app, token=None, content=b"%PDF-1.4")
    assert response.status_code == 401


async def test_rejects_invalid_signature(app):
    token = make_token(secret="wrong-secret")
    response = await post_ingest(app, token=token, content=b"%PDF-1.4")
    assert response.status_code == 401
    assert "Invalid service token" in response.json()["detail"]


async def test_rejects_expired_token(app):
    token = make_token(expires_in=-30)
    response = await post_ingest(app, token=token, content=b"%PDF-1.4")
    assert response.status_code == 401
    assert "expired" in response.json()["detail"].lower()


# ------------------------------------------------------- defensive re-checks
async def test_rejects_non_pdf_content_type(app, repository):
    response = await post_ingest(
        app, token=make_token(), content=b"hello", content_type="text/plain"
    )
    assert response.status_code == 415
    assert repository.rows == {}


async def test_rejects_oversized_pdf(app, repository):
    oversized = b"0" * (25 * 1024 * 1024 + 1)
    response = await post_ingest(app, token=make_token(), content=oversized)
    assert response.status_code == 413
    assert repository.rows == {}


# ------------------------------------------------- happy path (A, IN-7 queue)
async def test_valid_pdf_upload_queues_job_and_returns_immediately(
    app, repository, embeddings, jobs, queue
):
    """IN-7: upload normalizes + registers the document, creates a queued
    ingestion job, enqueues the arq task, and returns the job id — no
    chunk/embed/index work happens on the request path."""
    pdf = make_pdf_bytes()
    response = await post_ingest(app, token=make_token(), content=pdf)

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "processing"
    assert body["docType"] == "pdf"
    assert body["sourceUri"] == "upload://handbook.pdf"
    assert body["contentHash"] == hashlib.sha256(pdf).hexdigest()
    assert body["reused"] is False
    assert body["error"] is None
    assert body["chunkCount"] is None
    assert body["jobId"]

    assert len(repository.rows) == 1
    row = repository.rows[body["contentHash"]]
    metadata = row["metadata"]
    assert metadata["originalFilename"] == "handbook.pdf"
    assert metadata["pageCount"] == 1
    assert "text" in metadata
    assert metadata["ingestedAt"]

    # Job row queued with this document; the arq task was enqueued.
    job = jobs.jobs[body["jobId"]]
    assert job["status"] == "queued"
    assert job["kind"] == "ingest"
    assert job["triggered_by"] == "user-1"
    assert jobs.job_documents[body["jobId"]][0]["document_id"] == row["id"]
    assert queue.enqueued == [(INGESTION_TASK_NAME, body["jobId"])]

    # NO provider or chunk work on the request path (worker's job).
    assert embeddings.calls == []
    assert repository.chunks == {}

    # Upload audit attributes the actor and records the queued job.
    assert len(repository.audits) == 1
    audit = repository.audits[0]
    assert audit["actor_id"] == "user-1"
    assert audit["actor_type"] == "user"
    assert audit["action"] == "corpus.document.upload"
    assert audit["resource_type"] == "document"
    assert audit["resource_id"] == row["id"]
    assert audit["after"]["status"] == "processing"
    assert audit["after"]["jobId"] == body["jobId"]
    assert audit["after"]["queued"] is True


# ------------------------------------------------------- idempotency (A1)
async def test_indexed_document_reupload_is_noop(app, repository, embeddings):
    pdf = make_pdf_bytes()
    content_hash = hashlib.sha256(pdf).hexdigest()
    seeded = repository.seed(content_hash, "indexed")

    response = await post_ingest(app, token=make_token(), content=pdf)

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "indexed"  # status untouched
    assert body["reused"] is True
    assert body["jobId"] is None  # no-op -> no job, nothing enqueued
    assert body["id"] == seeded["id"]
    assert len(repository.rows) == 1  # no duplicate row
    assert repository.upsert_calls == 0  # short-circuited before any write
    assert embeddings.calls == []  # no chunking/embedding work either


async def test_failed_document_retry_requeues_a_job(app, repository, jobs, queue):
    pdf = make_pdf_bytes()
    content_hash = hashlib.sha256(pdf).hexdigest()
    seeded = repository.seed(content_hash, "failed", error="PDF parse failed: boom")

    response = await post_ingest(app, token=make_token(), content=pdf)

    assert response.status_code == 200
    body = response.json()
    # Retry: failed -> processing + a fresh queued job (worker re-indexes).
    assert body["status"] == "processing"
    assert body["error"] is None
    assert body["id"] == seeded["id"]  # same row, no duplicate
    assert body["jobId"] in jobs.jobs
    assert len(repository.rows) == 1
    assert len(queue.enqueued) == 1
    assert repository.audits[0]["after"]["retried"] is True


# ---------------------------------------------------- corrupt PDF (A2)
async def test_corrupt_pdf_marked_failed_with_reason(app, repository):
    corrupt = b"%PDF-1.4 this is not really a pdf"
    response = await post_ingest(app, token=make_token(), content=corrupt)

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "failed"
    assert body["error"]
    assert "PDF parse failed" in body["error"]

    assert len(repository.rows) == 1
    row = repository.rows[body["contentHash"]]
    assert row["status"] == "failed"
    assert row["error"] == body["error"]
    # No partial normalized payload on a failed parse.
    assert "text" not in row["metadata"]

    audit = repository.audits[0]
    assert audit["after"]["status"] == "failed"
    assert audit["after"]["error"] == body["error"]


# -------------------------------------------------------- IN-3 loader formats
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


async def test_html_upload_normalizes_and_queues(app, repository, embeddings, jobs):
    html = (
        b"<html><head><title>Handbook</title></head><body>"
        + b"<p>" + b"Vacation policy details. " * 80 + b"</p>"
        + b"<script>ignore()</script></body></html>"
    )
    response = await post_ingest(
        app, token=make_token(), content=html, content_type="text/html", filename="handbook.html"
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "processing"
    assert body["docType"] == "html"
    assert body["sourceUri"] == "upload://handbook.html"
    assert body["jobId"] in jobs.jobs

    row = repository.rows[body["contentHash"]]
    assert row["metadata"]["title"] == "Handbook"
    assert "ignore()" not in row["metadata"]["text"]
    # Same {text, source, metadata} shape as PDF -> same worker pipeline;
    # nothing embeds on the request path.
    assert embeddings.calls == []


async def test_docx_upload_normalizes_and_queues(app, repository, embeddings):
    from tests.test_loaders import make_docx_bytes

    docx = make_docx_bytes("Remote work policy. " * 100)
    response = await post_ingest(
        app, token=make_token(), content=docx, content_type=DOCX_MIME, filename="policy.docx"
    )

    body = response.json()
    assert response.status_code == 200
    assert body["status"] == "processing"
    assert body["docType"] == "docx"
    assert body["jobId"]


async def test_csv_upload_normalizes_structure_and_queues(app, repository, embeddings):
    rows = "\n".join(f"item {i},{'description ' * 30}{i}" for i in range(1, 120))
    csv_bytes = f"item,description\n{rows}\n".encode()
    response = await post_ingest(
        app, token=make_token(), content=csv_bytes, content_type="text/csv", filename="items.csv"
    )

    body = response.json()
    assert response.status_code == 200
    assert body["status"] == "processing"
    assert body["docType"] == "csv"
    assert body["jobId"]

    row = repository.rows[body["contentHash"]]
    assert row["metadata"]["columns"] == ["item", "description"]
    assert row["metadata"]["rowCount"] == 119
    # Row/column structure preserved in text form (chunk-level section
    # metadata is asserted in tests/test_worker.py where indexing runs).
    assert "item: item 1 |" in row["metadata"]["text"]


async def test_corrupt_docx_marked_failed_with_reason(app, repository):
    response = await post_ingest(
        app, token=make_token(), content=b"not a zip", content_type=DOCX_MIME, filename="bad.docx"
    )

    body = response.json()
    assert response.status_code == 200
    assert body["status"] == "failed"
    assert "DOCX parse failed" in body["error"]
    assert repository.rows[body["contentHash"]]["status"] == "failed"


async def test_unsupported_type_rejected_with_supported_list(app, repository):
    response = await post_ingest(
        app, token=make_token(), content=b"plain", content_type="text/plain", filename="notes.txt"
    )

    assert response.status_code == 415
    detail = response.json()["detail"]
    assert "text/plain" in detail
    assert "text/csv" in detail and "text/html" in detail  # supported list shown
    assert repository.rows == {}  # rejected at the API, no partial rows


async def test_identical_content_dedups_across_formats(app, repository, jobs, queue):
    """Flow A dedup is format-agnostic: bytes whose hash is already indexed
    no-op on content_hash regardless of the upload's declared format."""
    csv_bytes = b"name,role\nAda,Engineer\n"
    repository.seed(hashlib.sha256(csv_bytes).hexdigest(), "indexed")

    response = await post_ingest(
        app, token=make_token(), content=csv_bytes, content_type="text/csv", filename="copy.csv"
    )
    assert response.json()["reused"] is True
    assert response.json()["jobId"] is None
    assert len(repository.rows) == 1  # no duplicate row
    assert jobs.jobs == {} and queue.enqueued == []  # nothing queued


# ----------------------------------------------------------- job rerun (IN-7)
async def rerun(app, *, token: str | None, job_id: str) -> httpx.Response:
    headers = {"authorization": f"Bearer {token}"} if token else {}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://rag") as client:
        return await client.post(f"/internal/jobs/{job_id}/rerun", headers=headers)


async def test_rerun_requeues_a_finished_job(app, repository, jobs, queue):
    job = await jobs.create_job(kind="ingest", triggered_by="user-1", document_ids=["doc-1"])
    await jobs.finish(job["id"], status="failed", stats={}, error="boom")

    response = await rerun(app, token=make_token(), job_id=job["id"])

    assert response.status_code == 200
    assert response.json() == {"id": job["id"], "status": "queued"}
    assert jobs.jobs[job["id"]]["status"] == "queued"
    assert jobs.job_documents[job["id"]][0]["status"] == "pending"
    assert queue.enqueued == [(INGESTION_TASK_NAME, job["id"])]
    # The rerun itself is audited.
    assert repository.audits[-1]["action"] == "corpus.ingest.rerun"
    assert repository.audits[-1]["resource_id"] == job["id"]


async def test_rerun_rejects_running_or_unknown_jobs(app, jobs, queue):
    running = await jobs.create_job(kind="ingest", triggered_by="user-1", document_ids=["d"])
    await jobs.mark_running(running["id"])

    still_running = await rerun(app, token=make_token(), job_id=running["id"])
    unknown = await rerun(app, token=make_token(), job_id=str(uuid.uuid4()))

    assert still_running.status_code == 409
    assert unknown.status_code == 409
    assert queue.enqueued == []


async def test_rerun_requires_service_jwt(app):
    response = await rerun(app, token=None, job_id="any")
    assert response.status_code == 401
