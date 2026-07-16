"""CE-5 Graph RAG extraction + dual-store consistency tests (Flow I)."""

import json
from typing import Any

import pytest

from app.graph import (
    GRAPH_PROMPT_VERSION,
    _parse_extraction,
    extract_graph,
    index_document_graph,
)
from tests.conftest import REQUIRED_ENV


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


EXTRACTION = {
    "entities": [
        {"name": "Radiology Staff", "type": "Role"},
        {"name": "PACS", "type": "System"},
    ],
    "relations": [{"source": "Radiology Staff", "type": "LINKS_TO", "target": "PACS"}],
}


class FakeExtractorClient:
    model = "gpt-4o-mini"
    provider = "openai"

    def __init__(self, *, fail: bool = False, reply: Any = None) -> None:
        self.calls: list[str] = []
        self.fail = fail
        self.reply = reply if reply is not None else json.dumps(EXTRACTION)

    async def complete(self, messages):
        self.calls.append(messages[-1]["content"])
        if self.fail:
            raise RuntimeError("extractor down")
        return self.reply, {"promptTokens": 80, "completionTokens": 30, "totalTokens": 110}


class FakeExtractionCache:
    def __init__(self, preloaded: dict[str, dict] | None = None) -> None:
        self.entries = {
            (h, "gpt-4o-mini", GRAPH_PROMPT_VERSION): e for h, e in (preloaded or {}).items()
        }
        self.stored: list[tuple[str, dict]] = []

    async def fetch(self, *, model, prompt_version, hashes):
        return {
            h: self.entries[(h, model, prompt_version)]
            for h in hashes
            if (h, model, prompt_version) in self.entries
        }

    async def store(self, *, model, prompt_version, entries):
        self.stored.extend(entries)
        for chunk_hash, extraction in entries:
            self.entries[(chunk_hash, model, prompt_version)] = extraction


class FakeGraphRepository:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.upserts: list[dict[str, Any]] = []
        self.deletes: list[str] = []

    async def upsert_document_graph(self, *, document_id, extractions, current_hashes):
        if self.fail:
            raise RuntimeError("neo4j down")
        self.upserts.append(
            {
                "documentId": document_id,
                "extractions": extractions,
                "currentHashes": current_hashes,
            }
        )
        edges = sum(len(e.get("relations", [])) for e in extractions.values())
        return {"nodes": 2, "edges": edges}

    async def delete_document_graph(self, document_id):
        self.deletes.append(document_id)


def chunk(content: str, content_hash: str, *, role: str | None = None) -> dict[str, Any]:
    return {
        "content": content,
        "content_hash": content_hash,
        "metadata": {"role": role} if role else {},
    }


# ---------------------------------------------------------------- extraction
def test_parse_extraction_validates_and_cleans():
    assert _parse_extraction(json.dumps(EXTRACTION))["relations"][0]["type"] == "LINKS_TO"
    assert _parse_extraction("not json") is None
    assert _parse_extraction('{"entities": "nope", "relations": []}') is None
    cleaned = _parse_extraction(
        '{"entities": [{"name": "  X "}, {"nope": 1}], '
        '"relations": [{"source": "X", "target": ""}]}'
    )
    assert cleaned == {"entities": [{"name": "X", "type": "Entity"}], "relations": []}


async def test_cached_extractions_make_zero_llm_calls(settings):
    config = settings(GRAPH_RAG_ENABLED="true", NEO4J_PASSWORD="pw")
    client = FakeExtractorClient()
    cache = FakeExtractionCache(preloaded={"h1": EXTRACTION})

    resolved, stats = await extract_graph(
        chunks=[chunk("radiology text", "h1")], client=client, cache=cache, settings=config
    )

    assert client.calls == []
    assert resolved["h1"] == EXTRACTION
    assert stats == {"hits": 1, "misses": 0, "fallbacks": 0, "costUsd": 0.0}


async def test_misses_extract_and_write_back(settings):
    config = settings(GRAPH_RAG_ENABLED="true", NEO4J_PASSWORD="pw")
    client = FakeExtractorClient()
    cache = FakeExtractionCache()

    resolved, stats = await extract_graph(
        chunks=[chunk("radiology text", "h1")], client=client, cache=cache, settings=config
    )

    assert len(client.calls) == 1
    assert cache.stored and cache.stored[0][0] == "h1"
    assert stats["misses"] == 1 and stats["fallbacks"] == 0
    assert stats["costUsd"] > 0
    assert resolved["h1"]["entities"]


async def test_extraction_failure_is_fail_soft(settings):
    config = settings(GRAPH_RAG_ENABLED="true", NEO4J_PASSWORD="pw")
    client = FakeExtractorClient(fail=True)
    cache = FakeExtractionCache()

    resolved, stats = await extract_graph(
        chunks=[chunk("radiology text", "h1")], client=client, cache=cache, settings=config
    )

    assert resolved == {}
    assert stats["fallbacks"] == 1
    assert cache.stored == []  # failed chunks are NOT cached — a rerun retries


# ------------------------------------------------------------ document hook
async def test_index_document_graph_upserts_with_provenance_hashes(settings):
    config = settings(GRAPH_RAG_ENABLED="true", NEO4J_PASSWORD="pw")
    repository = FakeGraphRepository()

    stats = await index_document_graph(
        document={"id": "doc-1"},
        chunks=[
            chunk("radiology text", "h1"),
            chunk("parent aggregate", "p1", role="parent"),  # RT-6: excluded
        ],
        settings=config,
        client=FakeExtractorClient(),
        cache=FakeExtractionCache(),
        repository=repository,
    )

    assert stats["edges"] == 1
    upsert = repository.upserts[0]
    assert upsert["documentId"] == "doc-1"
    # Parent chunks never reach the graph; current-hash set drives cleanup.
    assert upsert["currentHashes"] == ["h1"]
    assert "p1" not in upsert["extractions"]


async def test_store_failure_raises_for_the_caller_to_record(settings):
    config = settings(GRAPH_RAG_ENABLED="true", NEO4J_PASSWORD="pw")

    with pytest.raises(RuntimeError, match="neo4j down"):
        await index_document_graph(
            document={"id": "doc-1"},
            chunks=[chunk("radiology text", "h1")],
            settings=config,
            client=FakeExtractorClient(),
            cache=FakeExtractionCache(),
            repository=FakeGraphRepository(fail=True),
        )


# ------------------------------------------------------------------- config
def test_graph_flag_requires_neo4j_password(settings):
    from pydantic import ValidationError

    with pytest.raises(ValidationError) as excinfo:
        settings(GRAPH_RAG_ENABLED="true")
    assert "NEO4J_PASSWORD" in str(excinfo.value)


# ------------------------------------------------------------ worker wiring
async def test_worker_job_populates_graph_and_document_survives_neo4j_outage(monkeypatch):
    from app.config import Settings
    from app.worker import process_ingestion_job
    from tests.test_worker import (
        FakeCounterRedis,
        FakeEmbeddingsClient,
        FakeJobsRepository,
        FakeRepository,
        NullEmbeddingCache,
        NullReembedRepository,
        seed_document,
    )

    config = Settings(
        _env_file=None, GRAPH_RAG_ENABLED="true", NEO4J_PASSWORD="pw", **REQUIRED_ENV
    )
    repository = FakeRepository()
    jobs = FakeJobsRepository(docs=repository)
    document = seed_document(repository, text="Radiology staff link reports to PACS. " * 30)
    ok_graph = FakeGraphRepository()
    job = await jobs.create_job(kind="ingest", triggered_by="u1", document_ids=[document["id"]])
    ctx = {
        "settings": config,
        "repository": repository,
        "jobs": jobs,
        "embeddings": FakeEmbeddingsClient(),
        "embedding_cache": NullEmbeddingCache(),
        "respcache_redis": FakeCounterRedis(),
        "reembed_repository": NullReembedRepository(),
        "graph_client": FakeExtractorClient(),
        "graph_cache": FakeExtractionCache(),
        "graph_repository": ok_graph,
    }
    result = await process_ingestion_job(ctx, job["id"])

    assert result["status"] == "completed"
    stats = jobs.jobs[job["id"]]["stats"]
    assert stats["graph"]["edges"] >= 1
    assert ok_graph.upserts

    # ── Neo4j down: the document STILL indexes; the failure is recorded.
    document2 = seed_document(repository, text="Scheduler manages clinic slots. " * 30)
    job2 = await jobs.create_job(
        kind="ingest", triggered_by="u1", document_ids=[document2["id"]]
    )
    ctx["graph_repository"] = FakeGraphRepository(fail=True)
    result2 = await process_ingestion_job(ctx, job2["id"])

    assert result2["status"] == "completed"
    stats2 = jobs.jobs[job2["id"]]["stats"]
    assert stats2["failures"] == []  # document-level failures: none
    assert stats2["graphFailures"][0]["documentId"] == document2["id"]
    assert repository._row_by_id(document2["id"])["status"] == "indexed"


async def test_worker_baseline_job_never_touches_graph(monkeypatch):
    from app.config import Settings
    from app.worker import process_ingestion_job
    from tests.test_worker import (
        FakeCounterRedis,
        FakeEmbeddingsClient,
        FakeJobsRepository,
        FakeRepository,
        NullEmbeddingCache,
        NullReembedRepository,
        seed_document,
    )

    config = Settings(_env_file=None, **REQUIRED_ENV)  # baseline: graph off

    class ExplodingGraphRepository:
        async def upsert_document_graph(self, **kwargs):
            raise AssertionError("graph must not be touched when the flag is off")

    repository = FakeRepository()
    jobs = FakeJobsRepository(docs=repository)
    document = seed_document(repository, text="Plain document text. " * 30)
    job = await jobs.create_job(kind="ingest", triggered_by="u1", document_ids=[document["id"]])
    result = await process_ingestion_job(
        {
            "settings": config,
            "repository": repository,
            "jobs": jobs,
            "embeddings": FakeEmbeddingsClient(),
            "embedding_cache": NullEmbeddingCache(),
            "respcache_redis": FakeCounterRedis(),
            "reembed_repository": NullReembedRepository(),
            "graph_repository": ExplodingGraphRepository(),
        },
        job["id"],
    )

    assert result["status"] == "completed"
    assert "graph" not in jobs.jobs[job["id"]]["stats"]
    assert "graphFailures" not in jobs.jobs[job["id"]]["stats"]
