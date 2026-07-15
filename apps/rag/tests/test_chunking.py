"""IN-2 interim recursive chunking tests: overlap band, page-metadata
preservation, stable hashes (the chunks upsert key), fallback behavior."""

from app.chunking import CHUNK_OVERLAP, CHUNK_SIZE, build_chunks

PAGE_ONE = "Alpha section of the handbook. " * 60  # > CHUNK_SIZE, forces a split
PAGE_TWO = "Beta remote work policy. " * 10
TEXT = PAGE_ONE + "\n\n" + PAGE_TWO
PAGES = [
    {"page": 0, "chars": len(PAGE_ONE)},
    {"page": 1, "chars": len(PAGE_TWO)},
]
DOC_HASH = "doc-hash-1"


def test_overlap_within_ticket_band():
    assert 0.10 * CHUNK_SIZE <= CHUNK_OVERLAP <= 0.20 * CHUNK_SIZE


def test_chunks_carry_page_metadata_and_sequential_indexes():
    chunks = build_chunks(text=TEXT, pages=PAGES, document_hash=DOC_HASH)

    assert len(chunks) > 1
    assert [c.chunk_index for c in chunks] == list(range(len(chunks)))
    assert all(len(c.content) <= CHUNK_SIZE for c in chunks)
    # Page 0 content chunks first, page 1 content last.
    assert chunks[0].metadata == {"page": 0}
    assert chunks[-1].metadata == {"page": 1}
    assert "Alpha" in chunks[0].content
    assert "Beta" in chunks[-1].content


def test_hashes_stable_across_reruns_and_scoped_to_document():
    first = build_chunks(text=TEXT, pages=PAGES, document_hash=DOC_HASH)
    second = build_chunks(text=TEXT, pages=PAGES, document_hash=DOC_HASH)
    other_doc = build_chunks(text=TEXT, pages=PAGES, document_hash="doc-hash-2")

    # Idempotent re-index: identical inputs reproduce identical upsert keys.
    assert [c.content_hash for c in first] == [c.content_hash for c in second]
    # Same text in a different document never collides on UNIQUE(content_hash).
    assert set(c.content_hash for c in first).isdisjoint(c.content_hash for c in other_doc)
    # Unique within the document too (repeated boilerplate can't collide).
    assert len({c.content_hash for c in first}) == len(first)


def test_falls_back_to_unlabelled_chunks_when_page_counts_mismatch():
    bad_pages = [{"page": 0, "chars": 5}]  # doesn't line up with the text
    chunks = build_chunks(text=TEXT, pages=bad_pages, document_hash=DOC_HASH)

    assert len(chunks) > 0
    assert all(c.metadata == {} for c in chunks)


def test_no_pages_metadata_still_chunks():
    chunks = build_chunks(text=PAGE_ONE, pages=None, document_hash=DOC_HASH)
    assert len(chunks) > 1
    assert all(c.metadata == {} for c in chunks)


def test_empty_text_yields_no_chunks():
    assert build_chunks(text="", pages=[], document_hash=DOC_HASH) == []
    assert build_chunks(text="", pages=[{"page": 0, "chars": 0}], document_hash=DOC_HASH) == []
