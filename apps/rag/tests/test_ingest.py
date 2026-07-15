"""Ingest endpoint tests (IN-1 + IN-2): service-JWT auth, defensive re-checks,
Flow A idempotency (A1), corrupt-PDF failure (A2), and the synchronous
chunk -> batch-embed -> index pipeline (steps 5-7). DB is mocked with an
in-memory repository mirroring the ON CONFLICT upsert semantics (documents on
content_hash, chunks on chunk content_hash); OpenAI is a fake embeddings
client — real providers are never called."""

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
from app.ingest import get_documents_repository, get_embeddings_client
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
        self, *, document_id: str, chunks: list[dict[str, Any]]
    ) -> dict[str, Any] | None:
        """Mirrors documents.py: chunk upsert ON CONFLICT (content_hash) plus
        the status flip to indexed, atomically."""
        self.index_calls += 1
        if self.fail_index:
            raise RuntimeError("chunks write failed")
        for chunk in chunks:
            self.chunks[chunk["content_hash"]] = {**chunk, "document_id": document_id}
        row = self._row_by_id(document_id)
        if row is None:
            return None
        row["status"] = "indexed"
        row["error"] = None
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
def app(settings, repository, embeddings):
    application = create_app(settings)
    application.dependency_overrides[get_documents_repository] = lambda: repository
    application.dependency_overrides[get_embeddings_client] = lambda: embeddings
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


# ------------------------------------------------------------ happy path (A)
async def test_valid_pdf_is_indexed_synchronously(app, repository, embeddings):
    """R1 fuses upload + index: a blank (no-text) PDF normalizes to zero
    chunks and still flips straight to indexed — never left in processing."""
    pdf = make_pdf_bytes()
    response = await post_ingest(app, token=make_token(), content=pdf)

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "indexed"
    assert body["docType"] == "pdf"
    assert body["sourceUri"] == "upload://handbook.pdf"
    assert body["contentHash"] == hashlib.sha256(pdf).hexdigest()
    assert body["reused"] is False
    assert body["error"] is None
    assert body["chunkCount"] == 0

    assert len(repository.rows) == 1
    row = repository.rows[body["contentHash"]]
    metadata = row["metadata"]
    assert metadata["originalFilename"] == "handbook.pdf"
    assert metadata["pageCount"] == 1
    assert "text" in metadata
    assert metadata["pages"][0]["page"] == 0
    assert metadata["ingestedAt"]

    # No text -> no chunks -> zero provider calls.
    assert embeddings.calls == []
    assert repository.chunks == {}

    # ONE RAG-action audit entry for the fused upload+ingest, attributed to
    # the JWT principal, recording chunk count / embed batch info.
    assert len(repository.audits) == 1
    audit = repository.audits[0]
    assert audit["actor_id"] == "user-1"
    assert audit["actor_type"] == "user"
    assert audit["action"] == "corpus.document.upload"
    assert audit["resource_type"] == "document"
    assert audit["resource_id"] == row["id"]
    assert audit["after"]["status"] == "indexed"
    assert audit["after"]["chunkCount"] == 0
    assert audit["after"]["embedBatches"] == 0


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
    assert body["id"] == seeded["id"]
    assert len(repository.rows) == 1  # no duplicate row
    assert repository.upsert_calls == 0  # short-circuited before any write
    assert embeddings.calls == []  # no chunking/embedding work either


async def test_failed_document_retries_through_to_indexed(app, repository):
    pdf = make_pdf_bytes()
    content_hash = hashlib.sha256(pdf).hexdigest()
    seeded = repository.seed(content_hash, "failed", error="PDF parse failed: boom")

    response = await post_ingest(app, token=make_token(), content=pdf)

    assert response.status_code == 200
    body = response.json()
    # R1 retry: failed -> processing -> (synchronous pipeline) -> indexed.
    assert body["status"] == "indexed"
    assert body["error"] is None
    assert body["id"] == seeded["id"]  # same row, no duplicate
    assert len(repository.rows) == 1
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


# ------------------------------------- chunk + embed + index pipeline (IN-2)
async def test_chunks_written_with_pinned_model_and_document_indexed(
    app, repository, embeddings, monkeypatch
):
    monkeypatch.setattr("app.loaders.normalize_pdf", fake_two_page_normalize)
    pdf = make_pdf_bytes()

    response = await post_ingest(app, token=make_token(), content=pdf)

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "indexed"
    assert body["chunkCount"] == len(repository.chunks) > 1

    chunks = sorted(repository.chunks.values(), key=lambda c: c["chunk_index"])
    document_id = repository.rows[body["contentHash"]]["id"]
    for position, chunk in enumerate(chunks):
        assert chunk["document_id"] == document_id
        assert chunk["chunk_index"] == position
        assert chunk["content"].strip()
        assert len(chunk["embedding"]) == PINNED_EMBEDDING_DIMENSIONS
        assert chunk["embedding_model"] == PINNED_EMBEDDING_MODEL
        assert chunk["content_hash"]
        assert chunk["metadata"]["page"] in (0, 1)

    # Page metadata survived onto chunks: page 0 text chunks first, page 1 last.
    assert chunks[0]["metadata"]["page"] == 0
    assert chunks[-1]["metadata"]["page"] == 1

    audit = repository.audits[0]
    assert audit["after"]["status"] == "indexed"
    assert audit["after"]["chunkCount"] == body["chunkCount"]
    assert audit["after"]["embedBatches"] == 1
    assert audit["after"]["embeddingModel"] == PINNED_EMBEDDING_MODEL


async def test_embedding_calls_are_batched_one_call_for_n_chunks(
    app, repository, embeddings, monkeypatch
):
    monkeypatch.setattr("app.loaders.normalize_pdf", fake_two_page_normalize)

    response = await post_ingest(app, token=make_token(), content=make_pdf_bytes())

    chunk_count = response.json()["chunkCount"]
    assert chunk_count > 1
    # ONE provider call carrying all N chunk texts — never one call per chunk.
    assert len(embeddings.calls) == 1
    assert len(embeddings.calls[0]) == chunk_count


async def test_reindexing_same_document_keeps_chunk_count_stable(
    app, repository, embeddings, monkeypatch
):
    monkeypatch.setattr("app.loaders.normalize_pdf", fake_two_page_normalize)
    pdf = make_pdf_bytes()

    first = await post_ingest(app, token=make_token(), content=pdf)
    first_count = first.json()["chunkCount"]
    assert first_count > 1

    # Force the pipeline to re-run (an indexed doc would no-op): simulate a
    # failed document so the R1 retry path re-chunks, re-embeds, re-upserts.
    content_hash = first.json()["contentHash"]
    repository.rows[content_hash]["status"] = "failed"

    second = await post_ingest(app, token=make_token(), content=pdf)

    assert second.json()["status"] == "indexed"
    assert second.json()["chunkCount"] == first_count
    # Upsert on chunk content_hash: same rows, no duplicates.
    assert len(repository.chunks) == first_count
    assert repository.index_calls == 2  # the pipeline really ran twice


async def test_embed_failure_marks_document_failed_not_stuck_processing(
    app, repository, embeddings, monkeypatch
):
    monkeypatch.setattr("app.loaders.normalize_pdf", fake_two_page_normalize)
    embeddings.fail = True

    response = await post_ingest(app, token=make_token(), content=make_pdf_bytes())

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "failed"
    assert "embedding provider down" in body["error"]

    row = repository.rows[body["contentHash"]]
    assert row["status"] == "failed"  # not left in processing
    assert "embedding provider down" in row["error"]
    assert repository.chunks == {}  # nothing indexed

    audit = repository.audits[0]
    assert audit["after"]["status"] == "failed"
    assert "embedding provider down" in audit["after"]["error"]


async def test_chunk_write_failure_marks_document_failed(app, repository, embeddings, monkeypatch):
    monkeypatch.setattr("app.loaders.normalize_pdf", fake_two_page_normalize)
    repository.fail_index = True

    response = await post_ingest(app, token=make_token(), content=make_pdf_bytes())

    body = response.json()
    assert body["status"] == "failed"
    assert "chunks write failed" in body["error"]
    assert repository.rows[body["contentHash"]]["status"] == "failed"


# -------------------------------------------------------- IN-3 loader formats
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


async def test_html_upload_indexes_end_to_end(app, repository, embeddings):
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
    assert body["status"] == "indexed"
    assert body["docType"] == "html"
    assert body["sourceUri"] == "upload://handbook.html"
    assert body["chunkCount"] > 0

    row = repository.rows[body["contentHash"]]
    assert row["metadata"]["title"] == "Handbook"
    assert "ignore()" not in row["metadata"]["text"]
    # Same {text, source, metadata} shape as PDF -> same downstream pipeline.
    assert len(embeddings.calls) == 1  # one batched call, never per-chunk


async def test_docx_upload_indexes_end_to_end(app, repository, embeddings):
    from tests.test_loaders import make_docx_bytes

    docx = make_docx_bytes("Remote work policy. " * 100)
    response = await post_ingest(
        app, token=make_token(), content=docx, content_type=DOCX_MIME, filename="policy.docx"
    )

    body = response.json()
    assert response.status_code == 200
    assert body["status"] == "indexed"
    assert body["docType"] == "docx"
    assert body["chunkCount"] > 0
    assert len(embeddings.calls) == 1


async def test_csv_upload_preserves_sections_into_chunks(app, repository, embeddings):
    rows = "\n".join(f"item {i},{'description ' * 30}{i}" for i in range(1, 120))
    csv_bytes = f"item,description\n{rows}\n".encode()
    response = await post_ingest(
        app, token=make_token(), content=csv_bytes, content_type="text/csv", filename="items.csv"
    )

    body = response.json()
    assert response.status_code == 200
    assert body["status"] == "indexed"
    assert body["docType"] == "csv"

    row = repository.rows[body["contentHash"]]
    assert row["metadata"]["columns"] == ["item", "description"]
    assert row["metadata"]["rowCount"] == 119
    # Row/column structure preserved in text form.
    assert "item: item 1 |" in row["metadata"]["text"]
    # Section metadata per row-group flows through to indexed chunks.
    sections = {chunk["metadata"].get("section") for chunk in repository.chunks.values()}
    assert "rows 1-50" in sections
    assert "rows 51-100" in sections


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


async def test_identical_content_dedups_across_formats(app, repository):
    """Flow A dedup is format-agnostic: the same bytes re-uploaded under any
    supported type no-op on content_hash."""
    csv_bytes = b"name,role\nAda,Engineer\n"
    first = await post_ingest(
        app, token=make_token(), content=csv_bytes, content_type="text/csv", filename="a.csv"
    )
    assert first.json()["reused"] is False

    second = await post_ingest(
        app, token=make_token(), content=csv_bytes, content_type="text/csv", filename="copy.csv"
    )
    assert second.json()["reused"] is True
    assert len(repository.rows) == 1  # no duplicate row
