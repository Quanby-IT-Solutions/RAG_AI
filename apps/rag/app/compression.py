"""Post-retrieval contextual compression (RT-7, Flow B compress stage).

Reduces each retrieved chunk to its QUERY-RELEVANT sentences BEFORE budget
selection (order contractual: retrieve → compress → budget), so the GN-2
token budget buys more distinct evidence. Original chunk ids/metadata are
preserved — citations must resolve to the ORIGINAL chunk (GN-1 contract).
A chunk with no relevant sentences is DROPPED, never padded.

Two implementations behind one interface (§XI no-lock-in):
- "embeddings": sentence-split + cosine filter against the query embedding
  (no extra model dependency; uses the pinned EmbeddingsClient)
- "llm": cheap-fast extraction (one call covering every chunk)

Failure policy (AC): the compressor runs under a hard timeout and ANY
failure falls back to the uncompressed chunks, noted in the trace.
"""

import json
import logging
import re
from typing import Any, Protocol

from app.config import Settings
from app.expansion import SupportsComplete, _cosine
from app.timeouts import with_hard_timeout

logger = logging.getLogger(__name__)

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")

_EXTRACTION_PROMPT = (
    "You compress retrieved passages for a question-answering system. For "
    "each numbered passage, extract ONLY the sentences relevant to the "
    "user's question, verbatim. Respond with a JSON object mapping each "
    "passage number (as a string) to its extracted text, or to \"NONE\" when "
    "nothing in the passage is relevant. No other output."
)


def split_sentences(text: str) -> list[str]:
    return [sentence.strip() for sentence in _SENTENCE_SPLIT.split(text) if sentence.strip()]


class Compressor(Protocol):
    provider: str

    async def compress(
        self, query: str, chunks: list[dict[str, Any]]
    ) -> list[dict[str, Any]]: ...


class SupportsEmbedTexts(Protocol):
    async def embed_texts(self, texts: list[str]) -> tuple[list[list[float]], int]: ...


def _compressed(chunk: dict[str, Any], content: str) -> dict[str, Any]:
    """Same chunk identity (id/document_id/metadata/content_hash), new text."""
    return {**chunk, "content": content, "compressed": True}


class EmbeddingsFilterCompressor:
    """Keep sentences whose embedding is close enough to the query's."""

    provider = "embeddings"

    def __init__(self, settings: Settings, embeddings: SupportsEmbedTexts) -> None:
        self._settings = settings
        self._embeddings = embeddings

    async def compress(
        self, query: str, chunks: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        sentences_per_chunk = [split_sentences(chunk["content"]) for chunk in chunks]
        flat = [sentence for sentences in sentences_per_chunk for sentence in sentences]
        if not flat:
            return []
        vectors, _ = await self._embeddings.embed_texts([query, *flat])
        query_vector = vectors[0]
        sentence_vectors = vectors[1:]

        threshold = self._settings.compression_similarity_threshold
        result: list[dict[str, Any]] = []
        cursor = 0
        for chunk, sentences in zip(chunks, sentences_per_chunk, strict=True):
            kept = [
                sentence
                for sentence, vector in zip(
                    sentences, sentence_vectors[cursor : cursor + len(sentences)], strict=True
                )
                if _cosine(query_vector, vector) >= threshold
            ]
            cursor += len(sentences)
            if kept:
                result.append(_compressed(chunk, " ".join(kept)))
        return result


class LLMExtractionCompressor:
    """Cheap-fast model extracts the relevant sentences (one call, all chunks)."""

    provider = "llm"

    def __init__(self, settings: Settings, client: SupportsComplete) -> None:
        self._settings = settings
        self._client = client

    async def compress(
        self, query: str, chunks: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        numbered = "\n\n".join(
            f"[{index}] {chunk['content']}" for index, chunk in enumerate(chunks)
        )
        text, _ = await self._client.complete(
            [
                {"role": "system", "content": _EXTRACTION_PROMPT},
                {"role": "user", "content": f"Question: {query}\n\nPassages:\n{numbered}"},
            ]
        )
        cleaned = text.strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned)
        extracted = json.loads(cleaned)
        result = []
        for index, chunk in enumerate(chunks):
            content = extracted.get(str(index))
            if isinstance(content, str) and content.strip() and content.strip() != "NONE":
                result.append(_compressed(chunk, content.strip()))
        return result


def build_compressor(
    settings: Settings,
    *,
    embeddings: SupportsEmbedTexts,
    llm_client: SupportsComplete | None = None,
) -> Compressor | None:
    """Config-selected compressor; None when COMPRESSION_ENABLED is off."""
    if not settings.compression_enabled:
        return None
    if settings.compression_provider == "llm":
        if llm_client is None:
            raise ValueError("COMPRESSION_PROVIDER=llm requires a chat client")
        return LLMExtractionCompressor(settings, llm_client)
    return EmbeddingsFilterCompressor(settings, embeddings)


async def compress_with_fallback(
    compressor: Compressor,
    *,
    query: str,
    chunks: list[dict[str, Any]],
    timeout_seconds: float,
) -> tuple[list[dict[str, Any]], bool]:
    """Compress under a hard timeout; ANY failure returns the chunks
    uncompressed with applied=False (the trace notes the fallback)."""
    if not chunks:
        return chunks, False
    try:
        compressed = await with_hard_timeout(
            compressor.compress(query, chunks),
            seconds=timeout_seconds,
            operation=f"compress.{compressor.provider}",
        )
        return compressed, True
    except Exception:
        logger.warning(
            "compression.fallback_to_uncompressed",
            extra={"provider": compressor.provider},
            exc_info=True,
        )
        return chunks, False
