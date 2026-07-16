"""IN-8 zero-downtime re-embedding tests (Flow D)."""

from typing import Any

import pytest

from app.reembed import (
    ALLOWED_COLUMNS,
    get_active_embedding,
    process_reembed_migration,
    run_shadow_pass,
    set_active_embedding,
)
from tests.conftest import REQUIRED_ENV
from tests.test_response_cache import FakeRedis


@pytest.fixture
def settings(monkeypatch):
    def build(**env: str):
        for key, value in REQUIRED_ENV.items():
            monkeypatch.setenv(key, value)
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        from app.config import Settings

        return Settings()

    return build


class FakeReembedRepository:
    def __init__(self, chunks: list[dict[str, Any]], migration: dict[str, Any]):
        self.chunks = list(chunks)
        self.migration = dict(migration)
        self.shadow_writes: list[tuple[str, list[float]]] = []
        self.statuses: list[dict[str, Any]] = []
        self.cleared = False

    async def latest_migration(self):
        return dict(self.migration)

    async def active_migration(self):
        if self.migration["status"] in ("shadowing", "verifying", "ready"):
            return dict(self.migration)
        return None

    async def next_shadow_batch(self, target_model, limit):
        pending = [c for c in self.chunks if c.get("shadow_model") != target_model]
        return [
            {
                "id": c["id"],
                "content": c["content"],
                "content_hash": c.get("content_hash", f"hash-{c['id']}"),
                "document_id": c.get("document_id", "doc-1"),
                "doc_window": c.get("doc_window", "document window text"),
            }
            for c in pending[:limit]
        ]

    async def write_shadow(self, entries, target_model, *, contexts=None):
        self.shadow_writes.extend(entries)
        self.context_writes = getattr(self, "context_writes", {})
        if contexts is not None:
            self.context_writes.update(contexts)
        by_id = {c["id"]: c for c in self.chunks}
        for chunk_id, _vector in entries:
            by_id[chunk_id]["shadow_model"] = target_model

    async def set_status(self, migration_id, *, status, metrics=None, error=None):
        self.migration["status"] = status
        self.statuses.append({"status": status, "metrics": metrics, "error": error})

    async def clear_shadow(self, *, clear_context=False):
        self.cleared = True
        self.context_cleared = clear_context
        for chunk in self.chunks:
            chunk.pop("shadow_model", None)


class FakeTargetEmbeddings:
    model = "text-embedding-3-small"
    dimensions = 1536

    def __init__(self):
        self.calls = 0

    async def embed_texts(self, texts):
        self.calls += 1
        return [[0.5, 0.5] for _ in texts], 1


class PassthroughCache:
    async def fetch(self, *, model, hashes):
        return {}

    async def store(self, *, model, entries):
        return None


MIGRATION = {
    "id": "m1",
    "targetModel": "text-embedding-3-small",
    "status": "shadowing",
    "metrics": None,
    "error": None,
    "triggeredBy": "u1",
}


def chunks(n: int) -> list[dict[str, Any]]:
    return [{"id": f"c{i}", "content": f"chunk content {i}"} for i in range(n)]


# --------------------------------------------------------------- shadow pass
async def test_shadow_pass_embeds_every_chunk_with_the_target_model(settings):
    config = settings()
    repository = FakeReembedRepository(chunks(5), MIGRATION)
    embeddings = FakeTargetEmbeddings()

    written = await run_shadow_pass(
        settings=config,
        repository=repository,
        migration=dict(MIGRATION),
        embeddings=embeddings,
        embedding_cache=PassthroughCache(),
    )

    assert written == 5
    assert len(repository.shadow_writes) == 5
    # Live column untouched by construction: only write_shadow was called.


# ------------------------------------------------------------- verification
async def test_verification_blocks_cutover_below_baseline(settings, monkeypatch):
    config = settings()
    repository = FakeReembedRepository(chunks(1), {**MIGRATION, "status": "verifying"})

    import app.reembed as reembed_module

    async def fake_run_eval(**kwargs):
        return {"metrics": {"hitRate": 0.2, "mrr": 0.2}, "id": "run-x", "gitSha": "x"}

    async def fake_baseline(settings_arg):
        return {"runId": "b", "gitSha": "b", "metrics": {"hitRate": 0.9, "mrr": 0.9}}

    monkeypatch.setattr("app.evaluation.run_retrieval_eval", fake_run_eval)
    monkeypatch.setattr("app.eval_gate.fetch_current_baseline", fake_baseline)

    metrics, allowed, reason = await reembed_module.verify_shadow(
        settings=config,
        repository=repository,
        migration=dict(MIGRATION),
        eval_repository=object(),
        embeddings=FakeTargetEmbeddings(),
    )

    assert allowed is False
    assert "below baseline" in reason
    assert metrics["baselineCompared"] == 1.0


async def test_full_migration_reaches_ready_when_verification_passes(settings, monkeypatch):
    config = settings()
    repository = FakeReembedRepository(chunks(3), dict(MIGRATION))

    async def fake_run_eval(**kwargs):
        return {"metrics": {"hitRate": 1.0, "mrr": 1.0}, "id": "run-x", "gitSha": "x"}

    async def fake_baseline(settings_arg):
        return {"runId": "b", "gitSha": "b", "metrics": {"hitRate": 1.0, "mrr": 1.0}}

    monkeypatch.setattr("app.evaluation.run_retrieval_eval", fake_run_eval)
    monkeypatch.setattr("app.eval_gate.fetch_current_baseline", fake_baseline)

    result = await process_reembed_migration(
        {
            "settings": config,
            "reembed_repository": repository,
            "reembed_embeddings": FakeTargetEmbeddings(),
            "embedding_cache": PassthroughCache(),
            "eval_repository": object(),
        },
        "m1",
    )

    assert result["status"] == "ready"
    assert result["chunksShadowed"] == 3
    assert [s["status"] for s in repository.statuses] == ["verifying", "ready"]


# -------------------------------------------------------------- the pointer
async def test_active_pointer_defaults_to_the_live_column(settings):
    config = settings()
    redis = FakeRedis()
    active = await get_active_embedding(config, redis)
    assert active == {
        "column": "embedding",
        "model": config.embedding_model,
        "contextEnrichment": False,
        "contextPromptVersion": None,
    }


async def test_cutover_pointer_flips_column_and_model_together(settings):
    config = settings()
    redis = FakeRedis()

    await set_active_embedding(redis, column="embedding_shadow", model="text-embedding-3-small")
    active = await get_active_embedding(config, redis)

    # Pinned-model invariant: column and query model flip in ONE write.
    assert active == {
        "column": "embedding_shadow",
        "model": "text-embedding-3-small",
        "contextEnrichment": False,
        "contextPromptVersion": None,
    }


async def test_pointer_rejects_non_whitelisted_columns(settings):
    config = settings()
    redis = FakeRedis()
    await redis.set("embedding:active", '{"column": "evil; DROP TABLE", "model": "x"}')

    active = await get_active_embedding(config, redis)
    assert active["column"] in ALLOWED_COLUMNS
    assert active["column"] == "embedding"  # fell back to the live column


# ------------------------------------------------------- CE-2 enrichment rollout
async def test_enrichment_migration_shadows_enriched_text_and_writes_context(settings):
    from tests.test_context_enrichment import FakeContextCache, FakeContextClient

    config = settings(CONTEXTUAL_CHUNKS_ENABLED="true")
    migration = {
        **MIGRATION,
        "targetModel": config.embedding_model,  # same-model enrichment rollout
        "contextEnrichment": True,
        "contextPromptVersion": "v1",
    }
    repository = FakeReembedRepository(chunks(4), migration)

    class CapturingEmbeddings(FakeTargetEmbeddings):
        def __init__(self):
            super().__init__()
            self.texts: list[str] = []

        async def embed_texts(self, texts):
            self.texts.extend(texts)
            return await super().embed_texts(texts)

    embeddings = CapturingEmbeddings()
    written = await run_shadow_pass(
        settings=config,
        repository=repository,
        migration=migration,
        embeddings=embeddings,
        embedding_cache=PassthroughCache(),
        context_client=FakeContextClient(),
        context_cache=FakeContextCache(),
    )

    assert written == 4
    # Every shadow embedding saw ENRICHED text (context + raw chunk).
    for text in embeddings.texts:
        assert text.startswith("Context for:")
        assert "\n\nchunk content" in text
    # The situating context landed on the rows in the same write.
    assert len(repository.context_writes) == 4
    assert all(c and c.startswith("Context for:") for c in repository.context_writes.values())


async def test_plain_migration_shadow_pass_stays_raw(settings):
    config = settings()
    repository = FakeReembedRepository(chunks(2), MIGRATION)  # no enrichment fields

    written = await run_shadow_pass(
        settings=config,
        repository=repository,
        migration=dict(MIGRATION),
        embeddings=FakeTargetEmbeddings(),
        embedding_cache=PassthroughCache(),
    )

    assert written == 2
    assert getattr(repository, "context_writes", {}) == {}


async def test_enrichment_pointer_roundtrips_config_identity(settings):
    config = settings()
    redis = FakeRedis()

    await set_active_embedding(
        redis,
        column="embedding_shadow",
        model=config.embedding_model,
        context_enrichment=True,
        context_prompt_version="v1",
    )
    active = await get_active_embedding(config, redis)

    assert active["contextEnrichment"] is True
    assert active["contextPromptVersion"] == "v1"


# ------------------------------------------------- retrieval uses the pointer
async def test_retrieval_switches_column_and_model_after_cutover(settings):
    from app.retrieval import RetrievalService
    from tests.test_ingest import FakeEmbeddingsClient
    from tests.test_retrieval import FakeChunksRepository, make_chunk_row

    config = settings()
    rows = [make_chunk_row(0.9, 0)]
    repository = FakeChunksRepository(rows, None)
    pointer = {"column": "embedding", "model": config.embedding_model}

    async def resolver():
        return dict(pointer)

    service = RetrievalService(
        config, FakeEmbeddingsClient(), repository, active_embedding_resolver=resolver
    )

    await service.retrieve("q")
    assert repository.calls[-1]["column"] == "embedding"

    # Cutover flips the pointer; the SAME service instance follows it.
    pointer.update({"column": "embedding_shadow", "model": config.embedding_model})
    await service.retrieve("q")
    assert repository.calls[-1]["column"] == "embedding_shadow"


# ---------------------------------------------------------------- dual-write
async def test_ingestion_during_a_migration_dual_writes_shadow_embeddings(settings):
    from app.indexing import index_document_content
    from tests.test_ingest import FakeEmbeddingsClient
    from tests.test_parent_doc import CapturingRepository, make_document
    from tests.test_worker import NullEmbeddingCache

    config = settings(CHUNKING_STRATEGY="recursive", CHUNK_MIN_CHARS="10", CHUNK_MAX_CHARS="60")
    repository = CapturingRepository()
    reembed_repo = FakeReembedRepository([], dict(MIGRATION))  # active migration

    outcome = await index_document_content(
        document=make_document("some policy text. more policy text here."),
        settings=config,
        repository=repository,
        embeddings=FakeEmbeddingsClient(),
        embedding_cache=NullEmbeddingCache(),
        reembed_repository=reembed_repo,
        shadow_embeddings=FakeTargetEmbeddings(),
    )

    assert outcome["status"] == "indexed"
    rows = repository.indexed["chunks"]
    # AC: BOTH embeddings written during the window.
    for row in rows:
        assert row["embedding"], "live embedding present"
        assert row["shadow_embedding"], "shadow embedding present (dual-write)"
        assert row["shadow_model"] == MIGRATION["targetModel"]


async def test_post_cutover_ingestion_writes_the_active_shadow_column(settings):
    """Flow D gap (surfaced by CE-5): after cutover the query path reads
    embedding_shadow — new chunks must land there too or they are invisible
    to the vector arm."""
    from app.indexing import index_document_content
    from tests.test_ingest import FakeEmbeddingsClient
    from tests.test_parent_doc import CapturingRepository, make_document
    from tests.test_worker import NullEmbeddingCache, NullReembedRepository

    config = settings(CHUNKING_STRATEGY="recursive", CHUNK_MIN_CHARS="10", CHUNK_MAX_CHARS="60")
    repository = CapturingRepository()
    embeddings = FakeEmbeddingsClient()

    outcome = await index_document_content(
        document=make_document("some policy text. more policy text here."),
        settings=config,
        repository=repository,
        embeddings=embeddings,
        embedding_cache=NullEmbeddingCache(),
        reembed_repository=NullReembedRepository(),  # no ACTIVE migration
        active_embedding={
            "column": "embedding_shadow",
            "model": embeddings.model,  # same-model cutover (CE-2 rollout)
        },
    )

    assert outcome["status"] == "indexed"
    for row in repository.indexed["chunks"]:
        # Same model: the identical vector is COPIED, never re-embedded.
        assert row["shadow_embedding"] == row["embedding"]
        assert row["shadow_model"] == embeddings.model


async def test_post_cutover_pointer_on_live_column_writes_no_shadow(settings):
    from app.indexing import index_document_content
    from tests.test_ingest import FakeEmbeddingsClient
    from tests.test_parent_doc import CapturingRepository, make_document
    from tests.test_worker import NullEmbeddingCache, NullReembedRepository

    config = settings(CHUNKING_STRATEGY="recursive", CHUNK_MIN_CHARS="10", CHUNK_MAX_CHARS="60")
    repository = CapturingRepository()

    await index_document_content(
        document=make_document("some policy text. more policy text here."),
        settings=config,
        repository=repository,
        embeddings=FakeEmbeddingsClient(),
        embedding_cache=NullEmbeddingCache(),
        reembed_repository=NullReembedRepository(),
        active_embedding={"column": "embedding", "model": config.embedding_model},
    )

    for row in repository.indexed["chunks"]:
        assert "shadow_embedding" not in row


async def test_ingestion_without_a_migration_writes_no_shadow(settings):
    from app.indexing import index_document_content
    from tests.test_ingest import FakeEmbeddingsClient
    from tests.test_parent_doc import CapturingRepository, make_document
    from tests.test_worker import NullEmbeddingCache, NullReembedRepository

    config = settings(CHUNKING_STRATEGY="recursive", CHUNK_MIN_CHARS="10", CHUNK_MAX_CHARS="60")
    repository = CapturingRepository()

    await index_document_content(
        document=make_document("some policy text. more policy text here."),
        settings=config,
        repository=repository,
        embeddings=FakeEmbeddingsClient(),
        embedding_cache=NullEmbeddingCache(),
        reembed_repository=NullReembedRepository(),
    )

    for row in repository.indexed["chunks"]:
        assert "shadow_embedding" not in row
