"""Chunking chain (IN-4): semantic primary, recursive character fallback.

`chunk_document` is the single entry point (Flow A step 5). The semantic pass
splits each loader segment into sentences, embeds them in ONE batched call
(pinned model), and breaks where adjacent-sentence cosine distance spikes past
the configured percentile — meaning boundaries, not character counts. Any
failure or timeout in that pass falls back to recursive character splitting
for the document, and the fallback is recorded in the returned chunking
metadata (persisted on the document row and in the audit entry).

Every produced chunk then passes size validation: oversized chunks are
re-split, undersized chunks are merged into a neighbor, and anything that
still can't fit the bounds (e.g. a document smaller than CHUNK_MIN_CHARS) is
flagged in metadata — never indexed silently out of bounds. Adjacent semantic
chunks overlap ~15% (inside the ticket's 10-20% band); the recursive splitter
carries the same band via CHUNK_OVERLAP/CHUNK_SIZE.

Fixed-size chunking is not selectable: CHUNKING_STRATEGY only admits
"semantic" and "recursive" (config Literal), defaulting to semantic.

Each chunk gets a stable sha256 content_hash — the chunks-table upsert key —
so re-running the same chain on the same document reproduces the same rows
(idempotent re-index), and a strategy change naturally replaces the old rows
(the repository deletes stale hashes in the same transaction).
"""

import hashlib
import math
import re
from dataclasses import dataclass
from typing import Any, Protocol

CHUNK_SIZE = 1000
# 150/1000 = 15% — inside the ticket's 10-20% overlap band.
CHUNK_OVERLAP = 150
# Semantic-chunk overlap target as a fraction of the previous chunk (15%,
# word-snapped, so realized overlap stays inside the 10-20% band for prose).
SEMANTIC_OVERLAP_FRACTION = 0.15
# Must match the segment joiner in app.loaders (SEGMENT_SEPARATOR).
PAGE_SEPARATOR = "\n\n"

_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?])\s+|\n+")


class SupportsEmbedTexts(Protocol):
    async def embed_texts(self, texts: list[str]) -> tuple[list[list[float]], int]: ...


@dataclass(frozen=True)
class Chunk:
    content: str
    metadata: dict[str, Any]
    chunk_index: int
    content_hash: str


def _chunk_hash(document_hash: str, chunk_index: int, content: str) -> str:
    """Chunk upsert key. Scoped to (document content, position, chunk text) so
    identical text in two documents — or repeated within one — never collides
    on the global UNIQUE(content_hash), while re-running the same chain on
    the same document reproduces the same hashes (idempotent upsert)."""
    payload = f"{document_hash}:{chunk_index}:{content}".encode()
    return hashlib.sha256(payload).hexdigest()


def _page_segments(
    text: str, pages: list[dict[str, Any]] | None
) -> list[tuple[str, dict[str, Any]]]:
    """Rebuild (segment_text, metadata) slices from the loader's char counts.

    The normalized payload stores only the joined text plus each segment's
    char length; the joiner is PAGE_SEPARATOR, so exact offsets are
    recoverable. Every descriptor key except `chars` becomes chunk metadata —
    PDF segments carry {page: n} (IN-1 shape, unchanged), CSV row-groups carry
    {section: "rows a-b"} (IN-3). Falls back to one unlabelled segment when
    the counts don't line up."""
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
        metadata = {key: value for key, value in page.items() if key != "chars"}
        if not metadata:
            metadata = {"page": position}
        segments.append((text[offset : offset + chars], metadata))
        offset += chars
    if offset != len(text):
        return [(text, {})]
    return segments


def split_sentences(text: str) -> list[str]:
    """Sentence-ish units: sentence-ending punctuation or line breaks."""
    return [part.strip() for part in _SENTENCE_BOUNDARY.split(text) if part and part.strip()]


def _cosine_distance(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 1.0
    return 1.0 - dot / (norm_a * norm_b)


def _interpolated_percentile(ordered: list[float], percentile: int) -> float:
    """Linear-interpolation percentile (numpy default) — a discrete pick would
    return the max for small samples, and no spike can exceed the max."""
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * percentile / 100
    low = math.floor(rank)
    high = math.ceil(rank)
    if low == high:
        return ordered[low]
    fraction = rank - low
    return ordered[low] * (1 - fraction) + ordered[high] * fraction


def _breakpoint_indexes(distances: list[float], percentile: int) -> set[int]:
    """Indexes i where the distance between sentence i and i+1 exceeds the
    percentile threshold — a meaning boundary. Uniform distances (no spike)
    yield no breakpoints (strict >)."""
    if not distances:
        return set()
    threshold = _interpolated_percentile(sorted(distances), percentile)
    return {index for index, distance in enumerate(distances) if distance > threshold}


def _group_sentences(sentences: list[str], breakpoints: set[int]) -> list[str]:
    bodies: list[str] = []
    current: list[str] = []
    for index, sentence in enumerate(sentences):
        current.append(sentence)
        if index in breakpoints:
            bodies.append(" ".join(current))
            current = []
    if current:
        bodies.append(" ".join(current))
    return bodies


def _word_snapped_tail(text: str, target_chars: int) -> str:
    """The last ~target_chars of text, snapped forward to a word boundary.

    A tail that would start mid-word and contains no later boundary (e.g. the
    "ok" of "Handbook" when the previous chunk is tiny) is dropped — a
    fragment is worse than no overlap."""
    if target_chars <= 0 or target_chars >= len(text):
        return ""
    tail = text[-target_chars:]
    if text[-target_chars - 1].isspace():
        return tail.strip()
    space = tail.find(" ")
    if space < 0:
        return ""
    return tail[space + 1 :].strip()


def _apply_overlap(bodies: list[str]) -> list[str]:
    """Prefix each chunk (after the first) with ~15% of the PREVIOUS chunk's
    original text so adjacent chunks share context (ticket: 10-20% band)."""
    if len(bodies) < 2:
        return bodies
    out = [bodies[0]]
    for previous, body in zip(bodies, bodies[1:], strict=False):
        tail = _word_snapped_tail(previous, int(len(previous) * SEMANTIC_OVERLAP_FRACTION))
        out.append(f"{tail} {body}" if tail else body)
    return out


def _recursive_split(text: str, *, max_chars: int) -> list[str]:
    from langchain_text_splitters import RecursiveCharacterTextSplitter

    size = min(CHUNK_SIZE, max_chars)
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=size,
        chunk_overlap=int(size * SEMANTIC_OVERLAP_FRACTION),
    )
    return [piece for piece in splitter.split_text(text) if piece.strip()]


def _normalize_sizes(bodies: list[str], *, min_chars: int, max_chars: int) -> list[str]:
    """Size validation: re-split oversized bodies, merge undersized ones into
    a neighbor. A body that still can't reach min_chars (nothing to merge
    with, or merging would break max_chars) survives and is flagged by the
    caller — never silently dropped or silently indexed out of bounds."""
    resplit: list[str] = []
    for body in bodies:
        if len(body) > max_chars:
            resplit.extend(_recursive_split(body, max_chars=max_chars))
        else:
            resplit.append(body)

    merged: list[str] = []
    for body in resplit:
        if merged and len(body) < min_chars and len(merged[-1]) + 1 + len(body) <= max_chars:
            merged[-1] = f"{merged[-1]} {body}"
        else:
            merged.append(body)
    # A leading runt merges forward when possible.
    if (
        len(merged) >= 2
        and len(merged[0]) < min_chars
        and len(merged[0]) + 1 + len(merged[1]) <= max_chars
    ):
        merged[1] = f"{merged[0]} {merged[1]}"
        merged.pop(0)
    return merged


async def _semantic_segment_bodies(
    segments: list[tuple[str, dict[str, Any]]],
    *,
    embeddings: SupportsEmbedTexts,
    percentile: int,
) -> list[list[str]]:
    """Sentence-split every segment, embed ALL sentences in one batched call,
    and group each segment at its distance spikes. Raises on any provider
    failure — the caller falls back to recursive splitting."""
    per_segment_sentences = [split_sentences(segment) for segment, _ in segments]

    embeddable = [
        sentences for sentences in per_segment_sentences if len(sentences) >= 2
    ]
    flat = [sentence for sentences in embeddable for sentence in sentences]
    vectors: list[list[float]] = []
    if flat:
        vectors, _ = await embeddings.embed_texts(flat)

    bodies: list[list[str]] = []
    cursor = 0
    for sentences in per_segment_sentences:
        if len(sentences) < 2:
            bodies.append([" ".join(sentences)] if sentences else [])
            continue
        segment_vectors = vectors[cursor : cursor + len(sentences)]
        cursor += len(sentences)
        distances = [
            _cosine_distance(a, b)
            for a, b in zip(segment_vectors, segment_vectors[1:], strict=False)
        ]
        breakpoints = _breakpoint_indexes(distances, percentile)
        bodies.append(_group_sentences(sentences, breakpoints))
    return bodies


def build_chunks(
    *,
    text: str,
    pages: list[dict[str, Any]] | None,
    document_hash: str,
    max_chars: int = CHUNK_SIZE,
) -> list[Chunk]:
    """Recursive character splitting per segment (IN-2 behavior; IN-4's
    fallback path). Kept as a pure sync function — no provider calls."""
    chunks: list[Chunk] = []
    for segment, metadata in _page_segments(text, pages):
        for piece in _recursive_split(segment, max_chars=max_chars):
            index = len(chunks)
            chunks.append(
                Chunk(
                    content=piece,
                    metadata={**metadata, "strategy": "recursive"},
                    chunk_index=index,
                    content_hash=_chunk_hash(document_hash, index, piece),
                )
            )
    return chunks


async def chunk_document(
    *,
    text: str,
    pages: list[dict[str, Any]] | None,
    document_hash: str,
    embeddings: SupportsEmbedTexts,
    strategy: str = "semantic",
    min_chars: int = 200,
    max_chars: int = 2000,
    breakpoint_percentile: int = 90,
) -> tuple[list[Chunk], dict[str, Any]]:
    """The IN-4 chain: semantic (with recursive fallback) -> overlap -> size
    validation -> hashed chunks. Returns (chunks, chunking metadata); the
    metadata records the strategy used, whether the fallback fired and why,
    and how many chunks remain undersized after normalization (flagged, not
    silent)."""
    segments = _page_segments(text, pages)

    used_strategy = strategy
    fallback_reason: str | None = None
    segment_bodies: list[list[str]] | None = None

    if strategy == "semantic":
        try:
            segment_bodies = await _semantic_segment_bodies(
                segments, embeddings=embeddings, percentile=breakpoint_percentile
            )
            segment_bodies = [_apply_overlap(bodies) for bodies in segment_bodies]
        except Exception as exc:
            used_strategy = "recursive"
            fallback_reason = str(exc) or type(exc).__name__

    if segment_bodies is None:  # recursive by config, or semantic fallback
        segment_bodies = [
            _recursive_split(segment, max_chars=max_chars) for segment, _ in segments
        ]

    chunks: list[Chunk] = []
    undersized = 0
    for (_segment, metadata), bodies in zip(segments, segment_bodies, strict=True):
        normalized = _normalize_sizes(
            [body for body in bodies if body.strip()],
            min_chars=min_chars,
            max_chars=max_chars,
        )
        for body in normalized:
            flags: dict[str, Any] = {}
            if len(body) < min_chars:
                undersized += 1
                flags["undersized"] = True
            index = len(chunks)
            chunks.append(
                Chunk(
                    content=body,
                    metadata={**metadata, "strategy": used_strategy, **flags},
                    chunk_index=index,
                    content_hash=_chunk_hash(document_hash, index, body),
                )
            )

    chunking_meta: dict[str, Any] = {
        "strategy": used_strategy,
        "fallback": used_strategy != strategy,
    }
    if fallback_reason is not None:
        chunking_meta["fallbackReason"] = fallback_reason
    if undersized:
        chunking_meta["undersizedChunks"] = undersized
    return chunks, chunking_meta
