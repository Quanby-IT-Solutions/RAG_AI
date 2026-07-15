"""Source-format loaders (IN-3) — the Flow A loader stage behind one interface.

Every supported format normalizes to the same `{text, source, metadata}` shape
IN-1 established for PDF: a metadata dict carrying `text`, `source`,
`originalFilename`, `ingestedAt`, and optional `segments` (per-segment char
counts + labels) that chunking uses to attribute page/section metadata per
chunk. Loaders are pure functions from raw bytes to that dict; corrupt or
unreadable input raises, and the caller records the document as `failed`
(Flow A failure branch).

Adding a format = adding a LoaderSpec here. The gateway (NestJS Multer filter)
enforces the same MIME allow-list before anything reaches this service; this
module is the single source of truth on the Python side.
"""

import csv
import io
import os
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

# Must match the segment joiner assumed by app.chunking._page_segments.
SEGMENT_SEPARATOR = "\n\n"

# CSV rows are grouped into sections of this many data rows; each group is one
# segment so chunk metadata records which row-range a chunk came from.
CSV_ROW_GROUP_SIZE = 50

Normalizer = Callable[[bytes, str, str], dict[str, Any]]


@dataclass(frozen=True)
class LoaderSpec:
    doc_type: str
    mime_types: tuple[str, ...]
    normalize: Normalizer


def _base_metadata(filename: str, source_uri: str) -> dict[str, Any]:
    return {
        # Normalized document (DOMAIN-MODEL): {text, source, metadata}.
        "source": source_uri,
        "originalFilename": filename,
        "ingestedAt": datetime.now(UTC).isoformat(),
    }


def _decode_text(raw: bytes) -> str:
    """Decode text-based uploads: UTF-8 (BOM-tolerant) first, latin-1 last."""
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("latin-1")


def normalize_pdf(raw: bytes, filename: str, source_uri: str) -> dict[str, Any]:
    """IN-1's PDF normalization (sync — run in a thread).

    PyPDFLoader reads from a path, so the bytes are staged in a temp dir for
    the duration of the parse. Raises on corrupt/unreadable input.
    """
    from langchain_community.document_loaders import PyPDFLoader

    with tempfile.TemporaryDirectory() as tmp_dir:
        pdf_path = os.path.join(tmp_dir, "upload.pdf")
        with open(pdf_path, "wb") as handle:
            handle.write(raw)
        pages = PyPDFLoader(pdf_path).load()

    text = SEGMENT_SEPARATOR.join(page.page_content for page in pages)
    return {
        **_base_metadata(filename, source_uri),
        "pageCount": len(pages),
        "pages": [
            {
                "page": page.metadata.get("page", index),
                "chars": len(page.page_content),
            }
            for index, page in enumerate(pages)
        ],
        "text": text,
    }


def normalize_html(raw: bytes, filename: str, source_uri: str) -> dict[str, Any]:
    """HTML → visible text (scripts/styles stripped), title kept as metadata."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(_decode_text(raw), "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    text = "\n".join(line for line in soup.get_text("\n").splitlines() if line.strip())
    metadata = _base_metadata(filename, source_uri)
    if soup.title and soup.title.string and soup.title.string.strip():
        metadata["title"] = soup.title.string.strip()
    metadata["text"] = text
    return metadata


def normalize_docx(raw: bytes, filename: str, source_uri: str) -> dict[str, Any]:
    """DOCX → plain text via docx2txt (sync — run in a thread).

    Raises on non-DOCX/corrupt input (docx2txt opens it as a zip)."""
    import docx2txt

    with tempfile.TemporaryDirectory() as tmp_dir:
        docx_path = os.path.join(tmp_dir, "upload.docx")
        with open(docx_path, "wb") as handle:
            handle.write(raw)
        text = docx2txt.process(docx_path) or ""

    return {**_base_metadata(filename, source_uri), "text": text.strip()}


def _render_csv_row(header: list[str], row: list[str]) -> str:
    """One data row as `column: value` pairs — structure preserved in text."""
    cells = []
    for index, value in enumerate(row):
        label = header[index] if index < len(header) and header[index] else f"column_{index + 1}"
        cells.append(f"{label}: {value}")
    return " | ".join(cells)


def normalize_csv(raw: bytes, filename: str, source_uri: str) -> dict[str, Any]:
    """CSV → row-group segments; each group carries `section` metadata.

    Row/column structure is preserved in text form (`col: value | ...` per
    row); groups of CSV_ROW_GROUP_SIZE data rows become segments so chunks
    record the row-range they cover (ticket AC: section metadata per
    row-group)."""
    rows = list(csv.reader(io.StringIO(_decode_text(raw))))
    if not rows:
        raise ValueError("CSV contains no rows")
    header, data_rows = rows[0], rows[1:]

    segments: list[dict[str, Any]] = []
    segment_texts: list[str] = []
    for start in range(0, len(data_rows), CSV_ROW_GROUP_SIZE):
        group = data_rows[start : start + CSV_ROW_GROUP_SIZE]
        # 1-based data-row numbers (header is row 0 conceptually excluded).
        section = f"rows {start + 1}-{start + len(group)}"
        body = "\n".join(_render_csv_row(header, row) for row in group)
        segment_texts.append(body)
        segments.append({"section": section, "chars": len(body)})

    if not segments:  # header-only file: index the header as one segment
        body = " | ".join(cell for cell in header)
        segment_texts.append(body)
        segments.append({"section": "header", "chars": len(body)})

    return {
        **_base_metadata(filename, source_uri),
        "columns": header,
        "rowCount": len(data_rows),
        "pages": segments,
        "text": SEGMENT_SEPARATOR.join(segment_texts),
    }


def _lazy(name: str) -> Normalizer:
    """Late-bind the normalizer to the module attribute so tests can
    monkeypatch e.g. app.loaders.normalize_pdf and the dispatch sees it."""

    def call(raw: bytes, filename: str, source_uri: str) -> dict[str, Any]:
        import sys

        return getattr(sys.modules[__name__], name)(raw, filename, source_uri)

    return call


_SPECS: tuple[LoaderSpec, ...] = (
    LoaderSpec(doc_type="pdf", mime_types=("application/pdf",), normalize=_lazy("normalize_pdf")),
    LoaderSpec(doc_type="html", mime_types=("text/html",), normalize=_lazy("normalize_html")),
    LoaderSpec(
        doc_type="docx",
        mime_types=(
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ),
        normalize=_lazy("normalize_docx"),
    ),
    LoaderSpec(
        doc_type="csv",
        mime_types=("text/csv", "application/csv"),
        normalize=_lazy("normalize_csv"),
    ),
)

_BY_MIME: dict[str, LoaderSpec] = {
    mime: spec for spec in _SPECS for mime in spec.mime_types
}


def resolve_loader(content_type: str | None) -> LoaderSpec | None:
    """Match a request content type (parameters stripped) to a loader."""
    if not content_type:
        return None
    return _BY_MIME.get(content_type.split(";")[0].strip().lower())


def supported_mime_types() -> tuple[str, ...]:
    return tuple(_BY_MIME)
