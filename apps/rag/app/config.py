"""Service configuration (pydantic-settings).

Fail-fast contract (Tech Plan §8): every required setting missing or invalid at
boot aborts the process with an error that NAMES the offending setting. Mirrors
the fail-fast intent of apps/backend/src/config/env.config.ts.
"""

from functools import lru_cache
from typing import Literal, Self

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# D1 — pinned embedding constants. Single source of truth for both index (IN-2)
# and query (RT-1) calls. The DB column dimension (vector(1536)) must match.
PINNED_EMBEDDING_MODEL = "text-embedding-3-large"
PINNED_EMBEDDING_DIMENSIONS = 1536


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Infrastructure (required) ---
    database_url: str = Field(alias="DATABASE_URL", min_length=1)
    redis_url: str = Field(alias="REDIS_URL", min_length=1)

    # --- Providers / secrets (required) ---
    openai_api_key: str = Field(alias="OPENAI_API_KEY", min_length=1)
    rag_service_token_secret: str = Field(alias="RAG_SERVICE_TOKEN_SECRET", min_length=1)

    # --- Tracing (LangSmith default, Langfuse optional) ---
    tracing_provider: Literal["langsmith", "langfuse"] = Field(
        alias="TRACING_PROVIDER", default="langsmith"
    )
    langsmith_api_key: str | None = Field(alias="LANGSMITH_API_KEY", default=None)
    langsmith_project: str = Field(alias="LANGSMITH_PROJECT", default="ragp")
    # Region-specific API host: US (default), EU https://eu.api.smith.langchain.com,
    # APAC https://apac.api.smith.langchain.com
    langsmith_endpoint: str = Field(
        alias="LANGSMITH_ENDPOINT", default="https://api.smith.langchain.com"
    )
    langfuse_public_key: str | None = Field(alias="LANGFUSE_PUBLIC_KEY", default=None)
    langfuse_secret_key: str | None = Field(alias="LANGFUSE_SECRET_KEY", default=None)
    langfuse_host: str = Field(alias="LANGFUSE_HOST", default="https://cloud.langfuse.com")

    # --- Embedding constants (D1) — pinned; overriding to another value is invalid ---
    embedding_model: Literal["text-embedding-3-large"] = Field(
        alias="EMBEDDING_MODEL", default=PINNED_EMBEDDING_MODEL
    )
    embedding_dimensions: int = Field(
        alias="EMBEDDING_DIMENSIONS", default=PINNED_EMBEDDING_DIMENSIONS
    )

    # --- Generation (GN-1) — single provider (OpenAI), non-streamed. Cheap-fast
    # default; model routing / second provider arrive with GN-4/GN-5.
    generation_model: str = Field(alias="GENERATION_MODEL", default="gpt-4o-mini", min_length=1)

    # --- Chunking (IN-4) — semantic primary, recursive fallback. Fixed-size
    # chunking is intentionally NOT selectable (ticket AC): the Literal only
    # admits meaning-based strategies.
    chunking_strategy: Literal["semantic", "recursive"] = Field(
        alias="CHUNKING_STRATEGY", default="semantic"
    )
    chunk_min_chars: int = Field(alias="CHUNK_MIN_CHARS", default=200, ge=1)
    chunk_max_chars: int = Field(alias="CHUNK_MAX_CHARS", default=2000, ge=1)
    # Adjacent-sentence cosine-distance percentile above which a semantic
    # boundary is declared (LangChain SemanticChunker convention).
    semantic_breakpoint_percentile: int = Field(
        alias="SEMANTIC_BREAKPOINT_PERCENTILE", default=90, ge=50, le=99
    )

    # --- Retrieval (RT-1) — vector top-k over chunks.embedding ---
    retrieval_top_k: int = Field(alias="RETRIEVAL_TOP_K", default=5, ge=1)
    # pgvector HNSW search breadth, applied per query via SET LOCAL hnsw.ef_search.
    hnsw_ef_search: int = Field(alias="HNSW_EF_SEARCH", default=40, ge=1)

    # --- Timeouts (seconds) for the shared hard-timeout helper ---
    db_timeout_seconds: float = Field(alias="DB_TIMEOUT_SECONDS", default=5.0, gt=0)
    redis_timeout_seconds: float = Field(alias="REDIS_TIMEOUT_SECONDS", default=5.0, gt=0)
    external_call_timeout_seconds: float = Field(
        alias="EXTERNAL_CALL_TIMEOUT_SECONDS", default=30.0, gt=0
    )

    @model_validator(mode="after")
    def _require_pinned_embedding_dimensions(self) -> Self:
        """EMBEDDING_DIMENSIONS is pinned (D1) — any other value is invalid."""
        if self.embedding_dimensions != PINNED_EMBEDDING_DIMENSIONS:
            raise ValueError(
                f"EMBEDDING_DIMENSIONS must be {PINNED_EMBEDDING_DIMENSIONS} "
                f"(pinned, D1); got {self.embedding_dimensions}"
            )
        return self

    @model_validator(mode="after")
    def _require_valid_chunk_bounds(self) -> Self:
        if self.chunk_min_chars >= self.chunk_max_chars:
            raise ValueError(
                f"CHUNK_MIN_CHARS ({self.chunk_min_chars}) must be smaller than "
                f"CHUNK_MAX_CHARS ({self.chunk_max_chars})"
            )
        return self

    @model_validator(mode="after")
    def _require_tracing_keys(self) -> Self:
        """Tracing keys are required for the configured provider (fail-fast, named)."""
        if self.tracing_provider == "langsmith" and not self.langsmith_api_key:
            raise ValueError("LANGSMITH_API_KEY is required when TRACING_PROVIDER=langsmith")
        if self.tracing_provider == "langfuse" and not (
            self.langfuse_public_key and self.langfuse_secret_key
        ):
            raise ValueError(
                "LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY are required "
                "when TRACING_PROVIDER=langfuse"
            )
        return self


def load_settings() -> Settings:
    """Load settings, aborting with the offending setting names on failure."""
    try:
        return Settings()
    except Exception as exc:  # pydantic ValidationError names each bad field/alias
        raise SystemExit(f"Invalid RAG service configuration:\n{exc}") from exc


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return load_settings()
