"""Batch embeddings with the pinned OpenAI model (D1) + boot-time dimension guard.

Every embed call uses the single source of truth in app.config
(PINNED_EMBEDDING_MODEL / PINNED_EMBEDDING_DIMENSIONS via Settings) and passes
OpenAI's `dimensions` param, in BATCHES — one API call for N chunks, never one
call per chunk (Flow A step 6). Calls go through the shared hard-timeout
helper. RT-1's query path reuses this client so index and query embeddings can
never diverge.
"""

from typing import TYPE_CHECKING, Protocol

from app.config import Settings
from app.timeouts import with_hard_timeout

if TYPE_CHECKING:
    from openai import AsyncOpenAI

# OpenAI's embeddings API accepts up to 2048 inputs per request; larger chunk
# sets are split into ceil(N / 2048) calls — still batched, never per-chunk.
MAX_EMBED_BATCH_SIZE = 2048


class EmbeddingsClient:
    """Thin batching wrapper over the OpenAI embeddings endpoint."""

    def __init__(self, settings: Settings, client: "AsyncOpenAI | None" = None) -> None:
        self._settings = settings
        self._client = client
        self.model = settings.embedding_model
        self.dimensions = settings.embedding_dimensions

    def _openai(self) -> "AsyncOpenAI":
        if self._client is None:
            from openai import AsyncOpenAI

            self._client = AsyncOpenAI(api_key=self._settings.openai_api_key.get_secret_value())
        return self._client

    async def embed_texts(self, texts: list[str]) -> tuple[list[list[float]], int]:
        """Embed all texts with the pinned (model, dimensions); returns
        (vectors in input order, number of provider calls made)."""
        vectors: list[list[float]] = []
        batches = 0
        for start in range(0, len(texts), MAX_EMBED_BATCH_SIZE):
            batch = texts[start : start + MAX_EMBED_BATCH_SIZE]
            response = await with_hard_timeout(
                self._openai().embeddings.create(
                    model=self.model,
                    input=batch,
                    dimensions=self.dimensions,
                ),
                seconds=self._settings.external_call_timeout_seconds,
                operation="openai.embeddings.create",
            )
            batches += 1
            ordered = sorted(response.data, key=lambda item: item.index)
            for item in ordered:
                if len(item.embedding) != self.dimensions:
                    raise RuntimeError(
                        f"OpenAI returned a {len(item.embedding)}-dim embedding; "
                        f"expected the pinned {self.dimensions} (model={self.model})"
                    )
                vectors.append(item.embedding)
        return vectors, batches


class _DimensionSource(Protocol):
    async def fetch_embedding_column_dimensions(self) -> int | None: ...


async def assert_embedding_dimensions(settings: Settings, repository: _DimensionSource) -> None:
    """Boot-time dimension guard (Tech Plan §4): the configured
    EMBEDDING_DIMENSIONS and the chunks.embedding column dimension are two
    encodings of the same truth — fail fast (abort boot) on any mismatch."""
    column_dimensions = await repository.fetch_embedding_column_dimensions()
    if column_dimensions is None:
        raise RuntimeError(
            "Dimension guard: chunks.embedding column not found — run the "
            "Drizzle migrations (pnpm db:migrate) before starting the RAG service"
        )
    if column_dimensions != settings.embedding_dimensions:
        raise RuntimeError(
            f"Dimension guard: chunks.embedding is vector({column_dimensions}) "
            f"but EMBEDDING_DIMENSIONS={settings.embedding_dimensions} "
            f"(pinned model {settings.embedding_model}) — refusing to boot"
        )
