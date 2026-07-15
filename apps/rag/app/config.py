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
    # GN-2: hard per-request context token budget (rank-then-select cap).
    # Read per request (env override wins) so changes apply without restart.
    context_token_budget: int = Field(alias="CONTEXT_TOKEN_BUDGET", default=2000, ge=1)

    # --- Model routing (GN-4) — cheap-fast vs strong across two providers.
    # Strong tier defaults to Anthropic; without ANTHROPIC_API_KEY the router
    # degrades to cheap-only (decision records "strong-unavailable").
    anthropic_api_key: str | None = Field(alias="ANTHROPIC_API_KEY", default=None)
    strong_model: str = Field(alias="STRONG_MODEL", default="claude-opus-4-8", min_length=1)
    strong_model_provider: Literal["openai", "anthropic"] = Field(
        alias="STRONG_MODEL_PROVIDER", default="anthropic"
    )
    # Anthropic's Messages API requires max_tokens; also caps answer length.
    generation_max_tokens: int = Field(alias="GENERATION_MAX_TOKENS", default=2048, ge=1)
    # Route strong when the query is at least this many characters...
    routing_query_length_threshold: int = Field(
        alias="ROUTING_QUERY_LENGTH_THRESHOLD", default=240, ge=1
    )
    # ...or when the best fused similarity is below this floor (hard question).
    routing_min_top_similarity: float = Field(
        alias="ROUTING_MIN_TOP_SIMILARITY", default=0.2, ge=0, le=1
    )

    # --- Chat history (CH-3) — retention window for the purge job ---
    chat_retention_days: int = Field(alias="CHAT_RETENTION_DAYS", default=365, ge=1)

    # --- Evaluation (EV-2) ---
    # Code-version stamp recorded on every eval_runs row. Set by the deploy
    # environment (docker build arg / CI); "unknown" flags an unstamped run.
    git_sha: str = Field(alias="GIT_SHA", default="unknown")

    # --- Reliability (GN-5) — retry/backoff + shared circuit breaker ---
    retry_max_attempts: int = Field(alias="RETRY_MAX_ATTEMPTS", default=3, ge=1)
    retry_base_delay_seconds: float = Field(alias="RETRY_BASE_DELAY_SECONDS", default=0.5, gt=0)
    breaker_failure_threshold: int = Field(alias="BREAKER_FAILURE_THRESHOLD", default=5, ge=1)
    breaker_cooldown_seconds: int = Field(alias="BREAKER_COOLDOWN_SECONDS", default=30, ge=1)

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

    # --- Hybrid search (RT-2) — parallel vector + keyword arms fused by RRF.
    # Weights default 50/50 (PRD decision); candidates per arm feed the fusion
    # (and later RT-3 rerank window).
    retrieval_vector_weight: float = Field(alias="RETRIEVAL_VECTOR_WEIGHT", default=0.5, ge=0)
    retrieval_keyword_weight: float = Field(alias="RETRIEVAL_KEYWORD_WEIGHT", default=0.5, ge=0)
    retrieval_candidate_pool: int = Field(alias="RETRIEVAL_CANDIDATE_POOL", default=20, ge=1)
    # Reciprocal Rank Fusion constant: score = w / (RRF_K + rank).
    rrf_k: int = Field(alias="RRF_K", default=60, ge=1)

    # --- Reranking (RT-3) — cross-encoder over the fused candidate window.
    # Off by default so eval A/B (rerank on vs off) stays producible and the
    # base install needs no rerank dependency/key. Provider is swappable per
    # §XI no-lock-in: "cohere" (hosted) or "local" (sentence-transformers
    # cross-encoder; requires the optional `rerank` dependency group).
    rerank_enabled: bool = Field(alias="RERANK_ENABLED", default=False)
    rerank_provider: Literal["cohere", "local"] = Field(alias="RERANK_PROVIDER", default="cohere")
    # Top ~20-50 fused candidates are re-scored jointly with the query (§5.2).
    rerank_window: int = Field(alias="RERANK_WINDOW", default=20, ge=1, le=50)
    rerank_model: str = Field(alias="RERANK_MODEL", default="rerank-english-v3.0")
    rerank_timeout_seconds: float = Field(alias="RERANK_TIMEOUT_SECONDS", default=10.0, gt=0)
    cohere_api_key: str | None = Field(alias="COHERE_API_KEY", default=None)

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
    def _require_some_fusion_weight(self) -> Self:
        if self.retrieval_vector_weight == 0 and self.retrieval_keyword_weight == 0:
            raise ValueError(
                "RETRIEVAL_VECTOR_WEIGHT and RETRIEVAL_KEYWORD_WEIGHT cannot both be 0"
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
