"""Graph RAG store + entity/relationship extraction (CE-5, Flow I).

The knowledge graph is DERIVED data: Postgres stays the source of truth and
the graph is rebuildable from it (ingestion re-run + extraction cache). Every
edge carries provenance (document id, chunk hash, prompt version) so
re-ingestion/deletion keeps both stores consistent, and the graph is an
optional accelerator — Neo4j being down never fails a document (the job
records the extraction failure; a rerun repairs the graph only).

Extraction economics mirror the context cache (CE-1): content-addressed on
(chunk hash, model, prompt version) in `graph_extraction_cache`, so unchanged
content never re-extracts and a prompt/model change is a clean namespace.
"""

import asyncio
import json
import logging
from typing import Any, Protocol

import asyncpg
from prometheus_client import Counter

from app.config import Settings
from app.costs import compute_cost_usd
from app.timeouts import with_hard_timeout

logger = logging.getLogger(__name__)

# Part of the extraction-cache key: bump when the prompt changes so stale
# extractions are never mixed with new ones.
GRAPH_PROMPT_VERSION = "v1"

_EXTRACTION_SYSTEM_PROMPT = (
    "You extract a knowledge graph from a document chunk. Reply with ONLY a "
    "JSON object: {\"entities\": [{\"name\": \"...\", \"type\": \"...\"}], "
    "\"relations\": [{\"source\": \"...\", \"type\": \"...\", \"target\": "
    "\"...\"}]}. Entity names are short canonical noun phrases; relation "
    "types are UPPER_SNAKE_CASE verbs. Extract only what the chunk states. "
    "Empty lists are valid."
)

_MAX_CONCURRENT_CALLS = 5

GRAPH_EVENTS = Counter(
    "rag_graph_events_total",
    "Graph RAG events (CE-5)",
    ["event"],  # extract_hit | extract_miss | extract_fallback | upsert_failed
)

_CACHE_LOOKUP_SQL = """
SELECT chunk_hash, extraction FROM graph_extraction_cache
WHERE model = $1 AND prompt_version = $2 AND chunk_hash = ANY($3::text[])
"""

_CACHE_UPSERT_SQL = """
INSERT INTO graph_extraction_cache (chunk_hash, model, prompt_version, extraction)
VALUES ($1, $2, $3, $4::jsonb)
ON CONFLICT (chunk_hash, model, prompt_version) DO NOTHING
"""


class SupportsComplete(Protocol):
    model: str

    async def complete(
        self, messages: list[dict[str, str]]
    ) -> tuple[str, dict[str, int] | None]: ...


class GraphExtractionCacheRepository:
    """asyncpg persistence over the Drizzle-owned `graph_extraction_cache`."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def _connect(self) -> asyncpg.Connection:
        return await with_hard_timeout(
            asyncpg.connect(dsn=self._settings.database_url),
            seconds=self._settings.db_timeout_seconds,
            operation="postgres.connect",
        )

    async def fetch(
        self, *, model: str, prompt_version: str, hashes: list[str]
    ) -> dict[str, dict[str, Any]]:
        if not hashes:
            return {}
        conn = await self._connect()
        try:
            rows = await with_hard_timeout(
                conn.fetch(_CACHE_LOOKUP_SQL, model, prompt_version, hashes),
                seconds=self._settings.db_timeout_seconds,
                operation="graph_extraction_cache.fetch",
            )
        finally:
            await conn.close()
        return {
            row["chunk_hash"]: (
                json.loads(row["extraction"])
                if isinstance(row["extraction"], str)
                else row["extraction"]
            )
            for row in rows
        }

    async def store(
        self,
        *,
        model: str,
        prompt_version: str,
        entries: list[tuple[str, dict[str, Any]]],
    ) -> None:
        if not entries:
            return
        conn = await self._connect()
        try:
            for chunk_hash, extraction in entries:
                await with_hard_timeout(
                    conn.execute(
                        _CACHE_UPSERT_SQL,
                        chunk_hash,
                        model,
                        prompt_version,
                        json.dumps(extraction),
                    ),
                    seconds=self._settings.db_timeout_seconds,
                    operation="graph_extraction_cache.store",
                )
        finally:
            await conn.close()


def _parse_extraction(text: str) -> dict[str, Any] | None:
    """Validate the extractor's JSON; None = unusable (fail-soft per chunk)."""
    try:
        start = text.index("{")
        end = text.rindex("}") + 1
        payload = json.loads(text[start:end])
    except (ValueError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    entities = payload.get("entities")
    relations = payload.get("relations")
    if not isinstance(entities, list) or not isinstance(relations, list):
        return None
    clean_entities = [
        {"name": str(e["name"]).strip(), "type": str(e.get("type", "Entity")).strip()}
        for e in entities
        if isinstance(e, dict) and str(e.get("name", "")).strip()
    ]
    clean_relations = [
        {
            "source": str(r["source"]).strip(),
            "type": str(r.get("type", "RELATES")).strip() or "RELATES",
            "target": str(r["target"]).strip(),
        }
        for r in relations
        if isinstance(r, dict)
        and str(r.get("source", "")).strip()
        and str(r.get("target", "")).strip()
    ]
    return {"entities": clean_entities, "relations": clean_relations}


async def extract_graph(
    *,
    chunks: list[Any],
    client: SupportsComplete,
    cache: GraphExtractionCacheRepository,
    settings: Settings,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Per-chunk {entities, relations}, cache-first and fail-soft (Flow I).

    Returns ({chunk_hash: extraction}, stats). A failed extraction simply
    omits that chunk (counted) — the document itself never fails here."""
    if not chunks:
        return {}, {"hits": 0, "misses": 0, "fallbacks": 0, "costUsd": 0.0}

    hashes = [chunk["content_hash"] for chunk in chunks]
    cached = await cache.fetch(
        model=client.model,
        prompt_version=GRAPH_PROMPT_VERSION,
        hashes=list(dict.fromkeys(hashes)),
    )
    semaphore = asyncio.Semaphore(_MAX_CONCURRENT_CALLS)
    cost_total = 0.0
    cost_lock = asyncio.Lock()

    async def extract(chunk: dict[str, Any]) -> dict[str, Any] | None:
        nonlocal cost_total
        async with semaphore:
            try:
                text, usage = await client.complete(
                    [
                        {"role": "system", "content": _EXTRACTION_SYSTEM_PROMPT},
                        {"role": "user", "content": chunk["content"]},
                    ]
                )
            except Exception:
                logger.warning(
                    "graph.extraction_failed chunk=%s", chunk["content_hash"], exc_info=True
                )
                return None
            cost = compute_cost_usd(settings, model=client.model, token_usage=usage)
            if cost is not None:
                async with cost_lock:
                    cost_total += cost
            return _parse_extraction(text)

    miss_chunks: dict[str, dict[str, Any]] = {}
    for chunk in chunks:
        if chunk["content_hash"] not in cached and chunk["content_hash"] not in miss_chunks:
            miss_chunks[chunk["content_hash"]] = chunk

    extracted = await asyncio.gather(*(extract(c) for c in miss_chunks.values()))
    fresh = [
        (chunk_hash, extraction)
        for chunk_hash, extraction in zip(miss_chunks, extracted, strict=True)
        if extraction is not None
    ]
    await cache.store(
        model=client.model, prompt_version=GRAPH_PROMPT_VERSION, entries=fresh
    )
    resolved = {**cached, **dict(fresh)}

    stats = {
        "hits": len([h for h in dict.fromkeys(hashes) if h in cached]),
        "misses": len(miss_chunks),
        "fallbacks": len(miss_chunks) - len(fresh),
        "costUsd": round(cost_total, 8),
    }
    GRAPH_EVENTS.labels(event="extract_hit").inc(stats["hits"])
    GRAPH_EVENTS.labels(event="extract_miss").inc(stats["misses"])
    GRAPH_EVENTS.labels(event="extract_fallback").inc(stats["fallbacks"])
    logger.info("graph.extraction_stats %s", json.dumps(stats))
    return resolved, stats


# ---------------------------------------------------------------- Neo4j store
class GraphRepository:
    """Provenance-keyed graph persistence (Flow I consistency contract)."""

    def __init__(self, settings: Settings, driver: Any = None) -> None:
        self._settings = settings
        self._driver = driver

    def _get_driver(self) -> Any:
        if self._driver is None:
            from neo4j import AsyncGraphDatabase

            assert self._settings.neo4j_password is not None
            self._driver = AsyncGraphDatabase.driver(
                self._settings.neo4j_uri,
                auth=(
                    self._settings.neo4j_user,
                    self._settings.neo4j_password.get_secret_value(),
                ),
            )
        return self._driver

    async def verify_connectivity(self) -> None:
        await with_hard_timeout(
            self._get_driver().verify_connectivity(),
            seconds=self._settings.db_timeout_seconds,
            operation="neo4j.verify_connectivity",
        )

    async def upsert_document_graph(
        self,
        *,
        document_id: str,
        extractions: dict[str, dict[str, Any]],
        current_hashes: list[str],
    ) -> dict[str, int]:
        """MERGE the document's entities/relations and remove elements whose
        chunk hash left the current set — one call keeps both effects together
        so a re-ingest converges to exactly the current chunks (Flow I)."""
        driver = self._get_driver()
        nodes = 0
        edges = 0
        async with driver.session() as session:
            for chunk_hash, extraction in extractions.items():
                for entity in extraction.get("entities", []):
                    await session.run(
                        "MERGE (e:Entity {name: $name}) "
                        "SET e.type = coalesce($type, e.type)",
                        name=entity["name"],
                        type=entity.get("type"),
                    )
                    nodes += 1
                for relation in extraction.get("relations", []):
                    await session.run(
                        "MERGE (s:Entity {name: $source}) "
                        "MERGE (t:Entity {name: $target}) "
                        "MERGE (s)-[r:RELATES {type: $type, documentId: $doc, "
                        "chunkHash: $hash}]->(t) "
                        "SET r.promptVersion = $version",
                        source=relation["source"],
                        target=relation["target"],
                        type=relation["type"],
                        doc=document_id,
                        hash=chunk_hash,
                        version=GRAPH_PROMPT_VERSION,
                    )
                    edges += 1
            # Stale cleanup: relations from chunks no longer in the document,
            # then entities left with no relationships at all.
            await session.run(
                "MATCH ()-[r:RELATES {documentId: $doc}]->() "
                "WHERE NOT r.chunkHash IN $hashes DELETE r",
                doc=document_id,
                hashes=current_hashes,
            )
            await session.run("MATCH (e:Entity) WHERE NOT (e)--() DELETE e")
        return {"nodes": nodes, "edges": edges}

    async def delete_document_graph(self, document_id: str) -> None:
        driver = self._get_driver()
        async with driver.session() as session:
            await session.run(
                "MATCH ()-[r:RELATES {documentId: $doc}]->() DELETE r", doc=document_id
            )
            await session.run("MATCH (e:Entity) WHERE NOT (e)--() DELETE e")

    async def close(self) -> None:
        if self._driver is not None:
            await self._driver.close()


async def index_document_graph(
    *,
    document: dict[str, Any],
    chunks: list[dict[str, Any]],
    settings: Settings,
    client: SupportsComplete,
    cache: GraphExtractionCacheRepository,
    repository: GraphRepository,
) -> dict[str, Any]:
    """Flow I ingest hook: extract + upsert AFTER the Postgres transaction.

    Returns stats on success; raises on store failure — the CALLER records
    the graph failure and lets the document succeed (never a document
    failure, never a silent skip). Parent chunks (RT-6) are excluded: they
    aggregate children whose content is already extracted."""
    children = [
        chunk
        for chunk in chunks
        if (chunk.get("metadata") or {}).get("role") != "parent"
    ]
    extractions, stats = await extract_graph(
        chunks=children, client=client, cache=cache, settings=settings
    )
    counts = await repository.upsert_document_graph(
        document_id=document["id"],
        extractions=extractions,
        current_hashes=[chunk["content_hash"] for chunk in children],
    )
    return {**stats, **counts}
