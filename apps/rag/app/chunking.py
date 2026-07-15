"""Interim recursive chunking (Flow A step 5, IN-2).

Splits the IN-1 normalized text with LangChain's RecursiveCharacterTextSplitter
at ~15% overlap (semantic chunking replaces this in IN-4). Page metadata from
the normalization payload is preserved per chunk by reconstructing page slices
from the recorded per-page char counts. Each chunk gets a stable sha256
content_hash — the chunks-table upsert key (idempotent re-index).
"""

import hashlib
from dataclasses import dataclass
from typing import Any

CHUNK_SIZE = 1000
# 150/1000 = 15% — inside the ticket's 10-20% overlap band.
CHUNK_OVERLAP = 150
# Must match the page joiner in app.ingest._normalize_pdf.
PAGE_SEPARATOR = "\n\n"


@dataclass(frozen=True)
class Chunk:
    content: str
    metadata: dict[str, Any]
    chunk_index: int
    content_hash: str


def _chunk_hash(document_hash: str, chunk_index: int, content: str) -> str:
    """Chunk upsert key. Scoped to (document content, position, chunk text) so
    identical text in two documents — or repeated within one — never collides
    on the global UNIQUE(content_hash), while re-running the same splitter on
    the same document reproduces the same hashes (idempotent upsert)."""
    payload = f"{document_hash}:{chunk_index}:{content}".encode()
    return hashlib.sha256(payload).hexdigest()


def _page_segments(
    text: str, pages: list[dict[str, Any]] | None
) -> list[tuple[str, dict[str, Any]]]:
    """Rebuild (page_text, {page: n}) slices from IN-1's per-page char counts.

    The normalized payload stores only the joined text plus each page's char
    length; the joiner is PAGE_SEPARATOR, so exact offsets are recoverable.
    Falls back to one unlabelled segment when the counts don't line up."""
    if not pages:
        return [(text, {})]

    segments: list[tuple[str, dict[str, Any]]] = []
    offset = 0
    for position, page in enumerate(pages):
        chars = page.get("chars")
        if not isinstance(chars, int) or chars < 0:
            return [(text, {})]
        if position > 0:
            offset += len(PAGE_SEPARATOR)
        segments.append((text[offset : offset + chars], {"page": page.get("page", position)}))
        offset += chars
    if offset != len(text):
        return [(text, {})]
    return segments


def build_chunks(
    *,
    text: str,
    pages: list[dict[str, Any]] | None,
    document_hash: str,
) -> list[Chunk]:
    """Chunk the normalized document text, carrying page metadata per chunk.

    Splitting runs per page (overlap stays within a page) so every chunk keeps
    an unambiguous page attribution. Empty/whitespace pages yield no chunks."""
    from langchain_text_splitters import RecursiveCharacterTextSplitter

    splitter = RecursiveCharacterTextSplitter(
        chunk_size=CHUNK_SIZE,
        chunk_overlap=CHUNK_OVERLAP,
    )

    chunks: list[Chunk] = []
    for segment, metadata in _page_segments(text, pages):
        for piece in splitter.split_text(segment):
            if not piece.strip():
                continue
            index = len(chunks)
            chunks.append(
                Chunk(
                    content=piece,
                    metadata=metadata,
                    chunk_index=index,
                    content_hash=_chunk_hash(document_hash, index, piece),
                )
            )
    return chunks
