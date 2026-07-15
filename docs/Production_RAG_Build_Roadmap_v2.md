# Production RAG System — Complete Build Roadmap (v2.1, performance-focused)

Derived from *Production RAG with LangChain & Vector Databases* and re-sequenced into a buildable, ship-it plan. The course teaches concepts in lecture order; this teaches them in **build order**, with observability, caching, evaluation, and scale wired in from the start instead of bolted on at the end.

**Scope of this version:** performance and a complete working production system that handles real load. Regulatory/compliance concerns are intentionally out of scope.

**The two rules that govern everything below:**
1. ~90% of RAG failures are *retrieval* failures, not *generation* failures. Build so you can inspect what was retrieved separately from what was generated, at every step.
2. Build the **production shape** from day one — externalized state, async, observable — even while you only have 10 documents. The "works on 10, breaks on 10k" cliff is caused by building the prototype shape and trying to scale it later.

---

## Phase 0 — Target architecture & stack decisions

**Logical architecture:**

```
Ingestion (async job) → Chunking → Batch embedding → Vector store + keyword index
                                                              │
Client → Rate limiter → Security middleware → Cache (Redis) ──┤
                                                              │
   Query processing → Hybrid retrieval → Rerank → Compress → Token budget
                                                              │
              Prompt assembly → LLM (routed, timeout, retry, fallback) → stream out
                                                              │
                        Grounded answer + citations + full trace
```

Every box is a module you build and test independently, then wire together.

**Stack decisions — make these explicit:**

| Concern | Course default | Recommended | Why |
|---|---|---|---|
| Vector store | Chroma | **Supabase `pgvector`** (Qdrant if you outgrow it) | Already in your stack; hybrid (vector + FTS) inside Postgres; transactional with metadata. |
| Keyword/sparse | in-memory `BM25Retriever` | **Postgres FTS (`tsvector`)** or `pg_search` | In-memory BM25 dies on restart and can't be shared across replicas — a scaling trap. Keep it in the DB next to the vectors. |
| Cache | in-memory dict | **Redis** | In-process cache can't be shared across replicas; the moment you scale out, hit-rate collapses. Externalize from the start. |
| Embeddings | OpenAI `text-embedding-3` | `text-embedding-3-large`, **version pinned** | Embedding quality = RAG quality. Pick one, never mix index/query models. |
| LLM | OpenAI + Anthropic | Same — two providers | Needed for routing (cheap vs. smart) and failover. |
| Orchestration | LangChain (LCEL) | LCEL for linear chains; **LangGraph** for agentic/self-correcting (Phase 7) | Loops want a graph, not a chain. |
| API layer | FastAPI + uvicorn | **FastAPI** (Python-native LangChain), called by your NestJS backend if desired | Cleaner than fighting LangChain in TS; keep the RAG service as a Python sidecar. |
| Metrics | dict counters | **Prometheus + Grafana** | Dict counters don't aggregate across replicas. |
| Observability | LangSmith | LangSmith (or Langfuse) | Non-negotiable. Without tracing you debug blind. |
| Deploy | Render | Any container host (Render/Fly/your infra) | Container + uvicorn start command + env import. |

> The course ships a **single-instance demo** and tells you the scale recipe: swap in-memory cache → Redis, dict counters → Prometheus, add a load balancer. This roadmap builds those in from the start instead of as an afterthought.

---

## Phase 1 — Foundation (ingest → chunk → embed → store)

Goal: a clean, inspectable ingestion pipeline. Don't optimize yet; do make every stage observable.

### 1.1 Project setup
- Lock language versions (Python 3.11+); use `uv` for deps.
- Core deps: `langchain`, `langgraph`, `langsmith`, `anthropic`, `openai`, `fastapi`, `uvicorn`, `slowapi` (rate limiting), `pydantic-settings` (config), `python-dotenv`, `redis`, plus `pytest` + `httpx` for tests.
- `.env`: `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `LANGCHAIN_API_KEY`, `LANGCHAIN_TRACING_V2=true`, `LANGCHAIN_PROJECT`, DB URL, `REDIS_URL`. Load and **validate on boot** via pydantic-settings — fail fast if anything is missing.
- Turn LangSmith tracing on *now*, before you have anything to trace.

### 1.2 Document loading
- Support the formats you ingest (HTML, DOCX, CSV, PDF). Normalize to one internal object: `{ text, source, metadata }`.
- Attach metadata at load time: source URI, page/section, doc type, ingest timestamp, content hash. You need this for citations, filtering, idempotent re-indexing, and debugging.

### 1.3 Chunking — get this right or nothing else matters
Pick per content type; don't use one blindly:
- **Fixed** (every N chars/tokens) — only for uniform structureless text; splits sentences → the #1 failure. Avoid as default.
- **Recursive** (paragraphs → sentences → words → chars) — the sane default for prose. `RecursiveCharacterTextSplitter`.
- **Semantic** (split where embedding similarity drops) — preserves coherence, costs compute; worth it for dense reference/legal/medical docs.
- **Late chunking** (embed full context, then chunk) — for highly cross-referential docs.

Use a **fallback chain**: semantic as primary, recursive as fallback if semantic fails, validate chunk size, handle errors gracefully.

**Overlap matters** — carry ~10–20% so concepts straddling a boundary survive. Test with/without overlap on real queries (the course shows no-overlap dropping the right answer).

Deliverable: a chunker that returns chunks **with metadata propagated**, plus a script that prints boundaries so you can eyeball mid-sentence cuts.

### 1.4 Embeddings — the 3 production rules
1. **Same embedding model + version everywhere.** Index and query must match. Version drift silently destroys retrieval.
2. **Quality > quantity.** Better embeddings on fewer docs beats worse on more.
3. **Test retrieval separately** from generation. Print retrieved chunks before blaming the LLM.

Use **batch embeddings** at ingest — embed in batches, not one call per chunk, for throughput. Cache embeddings keyed on content hash so identical content is never re-embedded.

### 1.5 Vector store
- Enable `pgvector`; table holds `id, content, embedding vector(N), metadata jsonb, source`.
- Build an **HNSW** index (you'll tune it in Phase 5).
- Add a Postgres FTS (`tsvector`) column/index for the keyword side of hybrid.
- Make ingestion **idempotent** — upsert on content hash; re-running never duplicates.

**Exit:** ingest a corpus, retrieve top-k by vector similarity, print retrieved chunks + sources, see the whole run in LangSmith.

---

## Phase 2 — Diagnose and fix the 5 failure modes

For each: reproduce, instrument, fix.

### Mode 1 — Bad chunking
Retrieved chunks contain half a thought → partial/wrong answers. Fix = apply 1.3 properly (recursive/semantic + overlap, validated on real queries). This is a re-index, not a runtime patch.

### Mode 2 — Embedding mismatch (semantic drift)
User asks "how do I cancel," docs say "termination policy" → vector search misses. Fixes: stronger embeddings (1.4), **query translation/expansion** (multi-query, Phase 3), and keyword search (Mode 3).

### Mode 3 — Retrieval noise → Hybrid search (BM25 + vector)
Retriever returns 10, only 2 relevant; pure vector fails on exact identifiers (error codes like `SQ7742X`, SKUs, function names). Fix — the production hybrid pipeline:
1. Run **vector search** (semantic) and **keyword/BM25 search** (exact) in parallel.
2. Fuse rankings with **Reciprocal Rank Fusion**.
3. Tune weights — start ~50/50, bias keyword for identifier-heavy corpora, vector for natural-language. Build it **in the DB** (pgvector + FTS) so it persists and scales — not in-memory.

### Mode 4 — Context overflow → Token budgeting
Too much in the prompt → model ignores the middle ("lost in the middle"), answers truncate. Fix: estimate tokens per candidate, enforce a hard budget, select best chunks up to it (rank first via Phase 3 rerank, then budget), track per-request usage stats.

### Mode 5 — Hallucination
Answer is in context but the model invents something else. Fixes: prompt to answer **only** from context and say "I don't know" otherwise; require **citations** to chunk sources; add a faithfulness check in eval (Phase 3.4).

**Exit:** hybrid retriever, token budgeter, grounded-answer prompting with citations, and a LangSmith trace showing retrieved-vs-used context for any query.

---

## Phase 3 — Optimize for quality (retrieval precision)

Make good answers great. Precision over recall once retrieval basically works.

### 3.1 Reranking
After hybrid returns ~20–50 candidates, run a **cross-encoder reranker** (Cohere Rerank or local) to reorder by true relevance, keep the top few. Cheapest large quality jump; feeds token budgeting (rank → then budget).

### 3.2 Multi-query retrieval
Generate several rewordings of the question, retrieve for each, union results. Directly attacks semantic drift (Mode 2). `MultiQueryRetriever` or a small cheap model.

### 3.3 MMR (Maximal Marginal Relevance)
Diversify retrieved chunks so you're not returning five near-duplicates — better coverage within the same budget.

### 3.4 Parent-document retriever
Retrieve on small precise child chunks but **return the larger parent** for context. Best of both: precise matching, complete context.

### 3.5 Contextual compression
After retrieval, **extract only the relevant sentences** from each chunk before they hit the prompt. Cuts noise and tokens, directly improving Mode 4 budgeting.

### 3.6 Metadata filtering
Filter retrieval by metadata (doc type, source, date, section) so you search the right subset, not the whole corpus.

### 3.7 Evaluation — make quality measurable
You can't optimize what you don't measure. Build before tuning further:
- **Golden set**: representative questions + the chunk(s) that should be retrieved + ideal answers.
- **Retrieval metrics**: hit rate, MRR, precision@k, recall@k — isolates "is the right doc even retrieved."
- **Generation metrics**: faithfulness/groundedness, answer relevancy (RAGAS or LangSmith evaluators).
- Run on every change to chunking/embeddings/weights/reranking; wire into CI with a **regression gate** that blocks deploys on score drops.

**Exit:** measurable retrieval + generation scores, reranker + compression + parent-doc + metadata filtering in the path, multi-query/MMR configurable.

---

## Phase 4 — Production hardening (the application layer)

This is the app the course actually builds. Each is a module — build, test, wire.

### 4.1 Security middleware
Sits before the cache. **Injection check** on user input + **PII detection/masking** + **input sanitization**. (PII masking here is for safety/log-hygiene, not regulatory.)

### 4.2 Rate limiting
**SlowAPI** to throttle hits per client — protects the service from overload and abuse.

### 4.3 Caching
- **Response/semantic cache** in Redis: check cache → on miss call LLM and cache → return. Track hits/misses/hit-rate as first-class metrics.
- Sane **TTL + invalidation** so entries don't go stale after a re-index.

### 4.4 Reliability primitives
**Retry with exponential backoff**, **circuit breaker**, and **model fallback** (primary → fallback on failure). Plus **output validation** before returning a response.

### 4.5 Model routing
Route easy/short queries to a cheap fast model, hard/long to the strong one; add when volume/cost justify it. Second provider doubles as failover.

### 4.6 Structured logging + metrics + health
- **JSON structured logs** for aggregation.
- **Metrics**: request count, latency, token usage, cache hit-rate (Prometheus — see Phase 5).
- **`/health`** endpoint checking DB + provider + Redis reachability.

### 4.7 Config + tests + Docker
- **pydantic-settings** for config; validate on boot.
- **pytest + httpx**: `test_security.py`, `test_cache.py`, retrieval tests, endpoint tests.
- **Dockerfile + docker-compose** for reproducible deploy.

**Exit:** an authenticated, rate-limited, observable, tested, containerized service.

---

## Phase 5 — Scale & performance (handle production load)

The core of "production-level." The course ships single-instance and names the swaps; this phase builds them.

### 5.1 Externalize all state (prerequisite for >1 replica)
- **Redis** for the cache (already wired in 4.3) — shared across all instances.
- **Keyword index in the DB** (Postgres FTS), never in-memory BM25 — survives restarts, shared across replicas.
- **Prometheus** for metrics instead of dict counters — aggregates across replicas.

Once state is external, the API is truly stateless and scales horizontally.

### 5.2 Async I/O + timeouts everywhere (biggest throughput lever)
- **Async endpoints** and **async LLM/embedding/DB calls** so a worker isn't blocked waiting on a 3-second LLM response — multiplies concurrent capacity.
- **Hard timeout on every external call** (LLM, embedding, vector search). Without it, one hung call ties up a worker and cascades to total failure under load. Pair with the retry/backoff/circuit-breaker from 4.4.
- **Connection pooling** for Postgres/pgvector.

### 5.3 Streaming responses
Stream tokens to the client so **time-to-first-token** is near-instant. Without streaming every answer feels as slow as its slowest token — a baseline production expectation for chat-style RAG.

### 5.4 Vector index tuning (where retrieval latency is won or lost)
- **HNSW**: tune `m` and `ef_construction` at build time, `ef_search` at query time — the recall-vs-latency knob.
- **Quantization** (scalar/product) for large corpora to keep vectors in RAM.
- Benchmark **recall@k vs. p95 latency** on *your* data and pick the operating point.

### 5.5 Async ingestion pipeline
Run ingestion/re-embedding as a **background job/queue**, decoupled from the request path, so a re-index never degrades live query latency. This also gives a **zero-downtime re-embed path** when you change embedding models (dual-write/shadow index → cutover).

### 5.6 Horizontal scaling
- **Stateless replicas** behind a **load balancer**.
- Tune **worker/process count** (uvicorn workers / gunicorn).
- Configure autoscaling on CPU/latency.

### 5.7 Load testing
**Locust or k6** to your target concurrency *before* calling it production. Confirm p95/p99 latency, error rate, and cache hit-rate hold; verify autoscaling triggers. "Answered correctly once" is not a capacity guarantee.

### 5.8 The four failure modes that only appear under real load
These bite RAG/LLM systems specifically once you have many replicas and concurrent traffic. None show up on a single dev box, so they're easy to miss until production:

- **Provider rate-limit exhaustion.** N replicas all calling OpenAI/Anthropic share *one* account TPM/RPM quota. Uncoordinated, they'll trip 429s under load. Add a **shared (Redis-backed) token-bucket / semaphore** that caps in-flight provider calls below your quota, plus 429-aware backoff. Per-replica limits aren't enough — the limit is global.
- **Cache stampede (thundering herd).** A hot cache entry expires and 100 concurrent requests all miss and hit the LLM at once — a latency/cost spike. Use **single-flight / request coalescing**: the first miss computes, the rest wait on that result. Also stagger TTLs so entries don't expire in lockstep.
- **The vector store is the bottleneck, not the API.** Stateless replicas scale trivially; the **data tier doesn't**. Under heavy read load `pgvector` saturates first. Plan for it: **read replicas** for query traffic, or move to a **Qdrant cluster / sharded index** when QPS demands. Monitor DB CPU and query latency as a first-class scaling signal, separate from API latency.
- **No backpressure / ungraceful shutdown.** When overloaded, **shed load** (return 503 fast) rather than queue everything and collapse — a slow death is worse than a fast rejection. On deploy/scale-down, **drain in-flight requests** (graceful shutdown) so you don't drop live answers.

**Exit:** multi-replica, async, streaming service with a tuned index, async ingestion, shared provider-rate control, stampede protection, a scaling plan for the data tier, and a passing load test at target concurrency.

---

## Phase 6 — Assemble the complete service & deploy

### 6.1 Endpoints
- **`POST /chat`** — the main path: query → security → cache → hybrid retrieve → rerank → compress → budget → LLM (routed, timeout, retry, fallback) → **streamed** grounded answer + citations. Wrap in one traceable so each call is one clean trace.
- **`GET /metrics`** — Prometheus metrics (cache hit-rate, latency, tokens, request count).
- **`GET /health`** — liveness/readiness (DB + Redis + provider).

Keep `/chat` the only heavy endpoint; the others stay small.

### 6.2 Deploy
- Lockfile committed; everything pinned. Build via Docker.
- **Start command runs uvicorn** (`uvicorn main:app --host 0.0.0.0 --port $PORT`) with tuned workers.
- **Bulk-import env vars** into the host's secret store (don't hand-type 15 keys).
- Deploy **multiple replicas behind a load balancer**, shared Redis + DB + Prometheus.
- `git push` → build → live. Verify `/health`, hit `/chat`, confirm the trace lands in LangSmith from the deployed instance, and confirm cache hit-rate climbs across replicas.

**Exit:** a deployed, horizontally-scaled, observable service whose `/chat` streams grounded, cited answers under load.

---

## Phase 7 — Cutting-edge stack (add once the core is solid)

Don't start here. Add à la carte once Phases 1–6 are stable.

### 7.1 Contextual retrieval (best ROI)
Before embedding each chunk, prepend a short LLM-generated description of how it fits its parent document. Per the cited research, substantially reduces failed retrievals (~50%-class). Pairs especially well with hybrid search. Easiest of the four to add.

### 7.2 Agentic / self-correcting RAG (LangGraph)
Replace the linear chain with a decision loop: grade retrieved docs for relevance; if low, **rewrite the query and retry** instead of answering from garbage; route between tools/sources; regenerate when the answer isn't grounded. Use when single-shot retrieval isn't reliably enough and you can spend the extra latency.

### 7.3 Graph RAG
Extract **entities + relationships** into a knowledge graph (Neo4j), retrieve over the graph. For multi-hop questions that flat chunk retrieval can't answer. Heavier to build/maintain — adopt only when relationship queries are a real requirement.

### 7.4 Multimodal RAG
For corpora where meaning lives in charts/tables/images: convert PDF pages **to images** and answer with a **vision LLM** (GPT-4o-class) rather than extracting text and losing visual structure; use a store supporting image embeddings. ~5× the cost — reserve for documents that genuinely need vision.

The closing thesis: naive chunk-and-pray is dead. A mature system is hybrid + contextual + (where needed) agentic + multimodal.

---

## Performance readiness checklist

State & scale
- [ ] All state external: Redis cache, DB-resident keyword index, Prometheus metrics
- [ ] Stateless replicas behind a load balancer; worker count tuned; autoscaling verified
- [ ] Async I/O end-to-end + hard timeouts on every LLM/embedding/DB call
- [ ] Connection pooling for the DB
- [ ] Ingestion/re-index runs async off the request path
- [ ] Shared (Redis-backed) provider-rate control to stay under OpenAI/Anthropic TPM/RPM; 429 backoff
- [ ] Cache stampede protection (single-flight / coalescing, staggered TTLs)
- [ ] Data-tier scaling plan (pgvector read replicas or Qdrant cluster); DB latency monitored separately
- [ ] Backpressure (fast 503 under overload) + graceful shutdown (drain in-flight)
- [ ] Load tested to target concurrency (p95/p99, error rate, hit-rate hold)

Latency
- [ ] Streaming responses (fast TTFT)
- [ ] HNSW (`m`/`ef_construction`/`ef_search`) + quantization tuned to your recall-vs-latency target on real data
- [ ] Response + embedding cache with live hit-rate and sane TTL/invalidation
- [ ] Token budgeting + model routing enforced

Retrieval quality
- [ ] Same embedding model+version for index and query, enforced in code
- [ ] Chunking validated on real queries (no mid-sentence cuts), overlap tuned, fallback chain
- [ ] Hybrid (vector + keyword) with RRF fusion, persisted in the DB
- [ ] Reranker + contextual compression + parent-doc + metadata filtering in path
- [ ] Retrieval + generation metrics on a golden set, in CI with a regression gate

Reliability
- [ ] Retry w/ backoff + circuit breaker + model fallback + output validation
- [ ] Rate limiting + security middleware (injection check, input sanitization)
- [ ] Structured JSON logging + full per-request tracing (retrieved vs. used, tokens, latency, cost)
- [ ] `/health` + latency/error/throughput/cache-hit alerts wired up
- [ ] Config validated on boot; idempotent re-runnable ingestion; per-module tests

---

## Suggested build order

1. **Week 1 — Phase 1.** Ingestion → chunking → batch embeddings → pgvector, tracing on. Prove top-k prints sane chunks.
2. **Week 2 — Phase 2.** DB-resident hybrid search + token budgeting + grounded prompting with citations. Gets you past the "broke in production" cliff.
3. **Week 3 — Phase 3.** Golden set + eval harness, then rerank + compression + parent-doc + multi-query/MMR, measured.
4. **Week 4 — Phase 4 + 6.** Security middleware, rate limiting, caching, reliability primitives, logging/metrics/health, tests, Docker, FastAPI service, deploy.
5. **Week 5 — Phase 5.** Externalize state (Redis/Prometheus), async + timeouts, streaming, index tuning, async ingestion, horizontal scale, **load test**. This is what makes it handle production load.
6. **Later — Phase 7, à la carte.** Contextual retrieval first (best ROI), then agentic/graph/multimodal only where a concrete requirement demands it.

Build the production shape from day one, externalize state before you scale out, keep retrieval inspectable and separately measurable from generation, async + timeout every external call, and let evaluation drive every optimization. That's the whole game.
