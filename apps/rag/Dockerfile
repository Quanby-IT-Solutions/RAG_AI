# ========================================
# Python RAG service — internal-only (no Nginx route)
# ========================================
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS base
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1

# Install dependencies first (change less often than app code)
COPY apps/rag/pyproject.toml apps/rag/uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

COPY apps/rag/app ./app

# Don't run as root
RUN groupadd --system --gid 1001 rag && \
    useradd --system --uid 1001 --gid rag rag && \
    chown -R rag:rag /app
USER rag

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
    CMD ["python3", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=5).status == 200 else 1)"]
# Exec uvicorn straight from the synced venv (no uv cache needed at runtime)
CMD ["/app/.venv/bin/uvicorn", "--factory", "app.main:create_app", "--host", "0.0.0.0", "--port", "8000"]
