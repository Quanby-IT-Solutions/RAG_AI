# TICKETS — RAGP Developer Hand-off (primary build guide)

Vertical slices only (DB → API → UI where a UI exists). Modules are epics. Walking skeleton first — for the product and inside each epic. Shared services grow through their first consumer. Entity names and permission keys per DOMAIN-MODEL.md; constraints per PRD.md. Each ticket ≈ 1–2 days → one branch → one small PR.

---

## Risky-path flows (referenced by ticket technical notes; everything not listed here — standard create-and-view screens, list pages, report views — needs no flow, its acceptance criteria suffice)

### Flow A — Idempotent ingestion (hash dedup + upsert + embedding cache)
```
Upload/ingest a source file
  → compute sha256 of raw content; normalize to {text, source, metadata}
  → documents: upsert on content_hash (identical content = no-op, status untouched)
  → chunk → for each chunk compute chunk content_hash
  → embedding_cache lookup (content_hash, model)
        HIT  → reuse vector, zero API cost
        MISS → add to batch; ONE batched embedding call; write cache rows
  → chunks: upsert on content_hash (re-run never duplicates rows)
FAILURE: any document fails mid-pipeline → document.status = failed with reason;
  job continues with remaining documents; re-running the job is safe (upserts).
```
Risky because: idempotency + the cache invariant ("identical content never re-embedded") span three tables and must survive every later schema change.

### Flow B — /chat heavy path (ordering + provider failure)
```
POST /chat (auth: chat.query.submit)
  → SC middleware FIRST: sanitize → prompt-injection check → PII mask   [ahead of cache]
  → semantic/response cache lookup (CP-1) ── HIT → stream cached answer, mark trace cache_hit
  → hybrid retrieve (vector ∥ keyword → RRF) → rerank → [compress] → metadata filter
  → token budget: rank-ordered selection up to budget; log retrieved vs USED chunks separately
  → route: cheap-fast vs strong model (length/difficulty heuristic)
  → call provider under shared token-bucket (CP-4), hard timeout
  → stream tokens (SSE) with citations; validate output before final flush; record usage/cost
FAILURE: provider error/timeout → retry w/ exponential backoff (bounded)
  → circuit breaker opens after threshold → route to failover provider
  → both down → graceful degradation response (no silent failure), trace records the path
```
Risky because: the pipeline ORDER is a contract (security before cache; budget after ranking; validation before flush) and the failure branch spans four tickets.

### Flow C — Cache stampede + shared provider throttle
```
N concurrent identical misses on one cache key
  → Redis single-flight lock (SET NX + wait/subscribe): exactly ONE computes; N−1 await the result
Every provider call (any replica)
  → acquire shared Redis token-bucket/semaphore capped below account TPM/RPM
  → 429 from provider → back off with jitter AND shrink bucket temporarily
```
Risky because: correctness only shows under concurrency; naive per-replica limits or per-process locks pass all single-instance tests and fail in production.

### Flow D — Zero-downtime re-embedding (shadow index → cutover)
```
Admin triggers re-embed to new model (corpus.reembed.run)
  → add shadow embedding column/index; job embeds all chunks into shadow (Flow A cache rules, new model key)
  → dual-write: any ingestion during the window writes BOTH embeddings
  → verify: golden-set retrieval metrics on shadow ≥ baseline
  → cutover: atomic switch of the query path to shadow column; old column dropped after soak
ABORT at any step → drop shadow artifacts; serving path never touched.
```
Risky because: the pinned-model invariant (index model == query model) must hold at every instant; a partial cutover silently corrupts retrieval.

### Flow E — RBAC enforcement + audit
```
Any protected request
  → middleware resolves user/API-key → roles → permission keys (per-request cache)
  → has required key? proceed : 403 AND audit the denial
  → on success, any state change/privileged action → append audit_log (actor, action, before→after, ts)
Role editing takes effect on next request (no stale grants).
```
Risky because: security-critical, cross-cutting, and the prerequisite spine for every other module.

### Flow F — CI evaluation regression gate
```
CI on every merge touching chunking/embedding/retrieval/rerank/prompting
  → run golden set → retrieval metrics (hit rate, MRR, P@k, R@k) + generation metrics (faithfulness, relevancy)
  → compare vs stored baseline eval_run
        all metrics ≥ baseline − tolerance → gate PASSES, deploy proceeds, run stored
        any metric below → gate FAILS, deploy BLOCKED, diff report attached
  → intentional improvements: user with eval.gate.approve promotes the new run to baseline (audited)
Baseline only ever changes by explicit approval — never silently ratchets down.
```
Risky because: a gate that silently re-baselines is worse than no gate; the approval rule is the whole point.

---

## 1. Epic map

| Epic key | Label | TOR/PRD source | Note |
|---|---|---|---|
| `foundation` | Service, Auth, RBAC & Audit Spine | §5.6, §VI, §VII | Walking skeleton; tracing on from day one; cross-cutting spine built just-in-time. |
| `ingestion` | Ingestion & Corpus Management | §5.1 | Owns `documents`/`chunks`/`embedding_cache` schema (see DOMAIN-MODEL ownership rule). |
| `retrieval` | Retrieval Engine | §5.2 | Hybrid, rerank, compression, filters — measurable before generation is blamed. |
| `generation` | Answer Generation & Grounding | §5.3 | Budget, citations, streaming, routing, failover. |
| `chat-clients` | Web/Mobile Chat (PWA) | §5.4 | React+Vite PWA; admin console screens live in their feature epics, not here. |
| `caching-performance` | Caching & Performance Layer | §5.5 | Redis response cache, stampede protection, shared provider throttle. |
| `security` | Security Middleware & API Credentials | §5.6, §VIII | Pre-cache middleware, rate limiting, integrator API keys. |
| `evaluation` | Evaluation & Quality Gate | §5.7 | Golden set → metrics → CI regression gate. |
| `observability-ops` | Observability & Operations | §5.8 | Trace, metrics, health/alerts, load shed, config review. |
| `reporting` | Reporting & Exports | §5.9, §IX | Five reports, four formats, on demand + scheduled. |
| `deployment-scale` | Deployment, Scale & Load Test | §VII, §X | Multi-replica, autoscale, load test at agreed targets, docs/runbook. |

## 2. Build order

**Release 1 — product walking skeleton** (upload a PDF → ask → grounded cited answer; authed, audited, traced):
1. **F1** — Walking skeleton: FastAPI service, boot-validated config, tracing on, /health, Docker
2. **F2** — Admin-provisioned auth (Supabase Auth) + authenticated shell
3. **F3** — RBAC model + one enforced permission + audit spine
4. **IN-1** — Upload & normalize a PDF; document record + corpus list screen
5. **IN-2** — Chunk → batch-embed → index to pgvector, idempotent upsert
6. **RT-1** — Vector top-k retrieval with per-request retrieved-chunk logging
7. **GN-1** — Grounded, cited, "I don't know" answer (single provider)
8. **CH-1** — Web chat: ask a question, see answer + citations

**Release 2 — complete grounded pipeline:**
9. **IN-3** — HTML, DOCX, CSV loaders
10. **IN-4** — Semantic chunking fallback chain + overlap/size validation
11. **IN-7** — Async ingestion jobs + job-status console
12. **RT-2** — Hybrid search (parallel keyword FTS) + RRF with configurable weights
13. **GN-2** — Hard per-request token budget (rank-then-select) + usage recording
14. **GN-3** — Token streaming (SSE) end to end
15. **CH-2** — Progressive stream rendering + graceful degradation states
16. **GN-4** — Model routing (cheap-fast vs strong) across two providers
17. **GN-5** — Retry/backoff, circuit breaker, failover, output validation, hard timeouts
18. **CH-3** — Per-user conversation history across sessions
19. **F4** — User & role management console
20. **SC-3** — Integrator API keys + documented API access

**Release 3 — measurable quality + advanced retrieval:**
21. **EV-1** — Golden evaluation set storage & management
22. **EV-2** — Retrieval metrics harness (hit rate, MRR, P@k, R@k)
23. **RT-3** — Cross-encoder reranking of hybrid candidates
24. **RT-4** — Metadata filtering (type, source, date, section)
25. **RT-5** — Multi-query retrieval + MMR diversification (configurable)
26. **RT-6** — Parent-document retrieval
27. **RT-7** — Contextual compression
28. **IN-5** — Chunk-boundary inspection utility
29. **IN-6** — Embedding cache keyed on content hash
30. **EV-3** — Generation metrics (faithfulness, answer relevancy)
31. **EV-4** — CI regression gate with baseline approval

**Release 4 — harden, scale, operate:**
32. **SC-1** — Security middleware ahead of cache (sanitize, prompt-injection, PII mask)
33. **SC-2** — Per-client rate limiting (SlowAPI)
34. **CP-1** — Redis semantic/response cache with hit/miss metrics
35. **CP-2** — TTL staggering + invalidation on re-index
36. **CP-3** — Single-flight request coalescing
37. **CP-4** — Shared provider rate control (token bucket, 429-aware)
38. **OB-1** — Full per-request /chat trace (retrieved vs used, tokens, cost)
39. **OB-2** — Prometheus metrics, Grafana dashboards, structured JSON logs
40. **OB-3** — /health with provider reachability + threshold alerting
41. **OB-4** — Load shedding, graceful drain, DB scaling signal
42. **OB-5** — Admin configuration review (read-only, secrets masked)
43. **IN-8** — Zero-downtime re-embedding (shadow index → cutover)
44. **CH-4** — PWA packaging: installable, responsive, mobile parity
45. **RP-1** — Report framework + System Audit Trail Report
46. **RP-2** — Usage & Cost Report
47. **RP-3** — Retrieval & Answer Quality Report
48. **RP-4** — Cache & Performance Report
49. **RP-5** — Ingestion Status Report
50. **RP-6** — Excel/PDF exports + scheduled report generation
51. **DP-1** — Multi-replica production deployment (LB, secrets, TLS)
52. **DP-2** — Autoscaling on CPU/latency
53. **DP-3** — Load test at agreed targets (50 users / 20 QPS)
54. **DP-4** — API documentation, operations runbook, knowledge transfer

## 3. Full tickets

### Epic: `foundation`

---

**Title:** [F1] Walking skeleton — FastAPI service with validated config, tracing, /health, Docker
**Epic:** `foundation`
**Problem / context:** §VII mandates pydantic-settings fail-fast config, tracing from project start, and containerized reproducibility; §5.8 requires `/health`. Thinnest slice proving the whole runtime: app boots, talks to Supabase Postgres and Redis, emits a trace, runs in Docker.
**User story:** As the internal team, we want a running, traceable, containerized service shell so that every later slice lands on a production-shaped foundation.
**Acceptance criteria:**
- *Given* a missing/invalid required setting, *when* the app boots, *then* it fails fast with a named-setting error (pydantic-settings).
- *Given* the app is up, *when* `GET /health` is called, *then* it returns liveness + readiness reflecting real DB and Redis connectivity checks.
- *Given* any request to a sample endpoint, *when* it executes, *then* a trace appears in LangSmith/Langfuse for that request.
- *Given* the repo, *when* `docker compose up` runs, *then* API + Postgres + Redis start and `/health` passes.
**In scope:** FastAPI + uvicorn scaffold (async), pydantic-settings, LangSmith/Langfuse wiring, `/health` (DB+Redis), Dockerfile + compose, pytest + httpx harness with one passing test, default hard timeout helper for external calls.
**Out of scope:** Auth (F2), RBAC/audit (F3), provider reachability in health (OB-3), metrics (OB-2).
**Technical notes:** Async engine (asyncpg) + connection pooling per §VII. Timeout helper is the seed reused by GN-5/Flow B. Trace-from-start is non-negotiable (PRD constraint).
**Definition of done:** Code + tests merged · criteria pass · PR reviewed · README run instructions updated.
**Dependencies:** none.

---

**Title:** [F2] Admin-provisioned authentication with authenticated shell
**Epic:** `foundation`
**Problem / context:** PRD decision #3: admin-provisioned email+password accounts via Supabase Auth, no self-registration (§5.6, §VI). Establishes login and the authenticated web shell the chat and console screens hang off.
**User story:** As a provisioned user, I want to log in and reach an authenticated shell so that all later features are gated behind real identity.
**Acceptance criteria:**
- *Given* valid provisioned credentials, *when* the user logs in, *then* a session (JWT) is established and the shell shows their identity.
- *Given* invalid credentials, *when* they log in, *then* login fails with a generic error and no session exists.
- *Given* no session, *when* any protected API route is called, *then* it returns 401.
- *Given* the app, *when* someone seeks a registration page, *then* none exists — accounts are created only by an admin (seeded admin for bootstrap).
**In scope:** Supabase Auth integration, JWT verification middleware in FastAPI, `users` table (profile mirror), React shell with login/logout, seed script for first admin.
**Out of scope:** Roles/permissions (F3), user-management UI (F4), API keys for integrators (SC-3).
**Technical notes:** Tables: `users`. FastAPI dependency `current_user` reused by every route. TLS enforced at deploy (DP-1).
**Definition of done:** as standard.
**Dependencies:** F1.

---

**Title:** [F3] RBAC model + one enforced permission + audit spine
**Epic:** `foundation`
**Problem / context:** §IV/§5.6: RBAC and audit logging across all components. Establishes the permission model (keys per DOMAIN-MODEL.md) and proves enforcement on one real route, plus the append-only audit helper every ticket reuses. **Flow E.**
**User story:** As a user with `users.roles.manage` permission, I want roles mapped to permission keys and enforced on protected actions so that access control and accountability are real from the first feature.
**Acceptance criteria:**
- *Given* the seeded six roles (§VI), *when* a role is assigned to a user, *then* the user's effective permission keys reflect the role's grants on their next request.
- *Given* a user lacking the required key, *when* they call the protected route, *then* 403 is returned and the denial is written to `audit_log`.
- *Given* a user holding the key, *when* they call it, *then* it succeeds and the state change is audited (actor, action, before→after, timestamp).
- *Given* the audit table, *when* any writer attempts UPDATE/DELETE, *then* it is rejected (append-only).
**In scope:** `roles`, `permissions`, `role_permissions`, `user_roles`, `audit_log`; permission middleware (per-request cache); `write_audit()` helper; seed of §VI roles and DOMAIN-MODEL keys; enforcement on one route.
**Out of scope:** Role-management UI (F4), audit viewer/report (RP-1), API-key scopes (SC-3).
**Technical notes:** **Flow E.** Permission keys exactly as DOMAIN-MODEL.md. Middleware resolves user → roles → keys; API-key principal added later by SC-3 through the same interface.
**Definition of done:** as standard.
**Dependencies:** F1, F2.

---

**Title:** [F4] User & role management console
**Epic:** `foundation`
**Problem / context:** §5.8 names user/role management as an admin-console operation; F3 built the model, this adds the operator UI (vertical slice: same tables, new API + screens).
**User story:** As a user with `users.accounts.manage` permission, I want to create/disable users and assign roles in the console so that access is administered without touching the database.
**Acceptance criteria:**
- *Given* the console, *when* an admin creates a user (email + temp password via Supabase Auth), *then* the user can log in and appears in the user list.
- *Given* a user, *when* an admin assigns/removes roles, *then* effective permissions change on the user's next request and the change is audited.
- *Given* a user with `users.roles.manage`, *when* they define a custom role from the permission-key catalog, *then* it is assignable like the seeded roles.
- *Given* a user without these keys, *when* they open the console routes, *then* 403 + audited denial.
**In scope:** User list/create/disable, role CRUD, role assignment UI + endpoints; audit on all changes.
**Out of scope:** SSO (per-engagement later, PRD), API keys (SC-3), config review (OB-5).
**Technical notes:** Enforces `users.accounts.manage`, `users.roles.manage`. Reuses F3 tables/middleware and Flow E. First screens of the admin console shell.
**Definition of done:** as standard.
**Dependencies:** F2, F3.

---

### Epic: `ingestion`

---

**Title:** [IN-1] Upload & normalize a PDF into a document record with corpus list
**Epic:** `ingestion`
**Problem / context:** §5.1: normalize every source to `{text, source, metadata}` with source URI, page/section, type, timestamp, content hash. Module walking skeleton: one format (PDF), document registered and visible. **Flow A** begins here.
**User story:** As a user with `corpus.documents.upload` permission, I want to upload a PDF and see it registered in the corpus so that sources enter the system through one inspectable door.
**Acceptance criteria:**
- *Given* a PDF upload, *when* ingestion runs, *then* a `documents` row exists with normalized text, source URI, doc type, ingest timestamp, and sha256 content hash, status `indexed` (or `processing` until IN-2).
- *Given* the same file uploaded again, *when* ingestion runs, *then* no duplicate row is created (upsert on `content_hash` — no-op).
- *Given* a corrupt/unreadable PDF, *when* ingestion runs, *then* the document is marked `failed` with a stored reason and the upload UI shows it.
- *Given* a user without the key, *when* they hit the upload endpoint, *then* 403 + audited denial.
**In scope:** PDF loader → normalized object; `documents` table per DOMAIN-MODEL; upload endpoint + corpus list screen (name, type, status, hash, timestamp); audit on upload.
**Out of scope:** Chunking/embedding (IN-2), other formats (IN-3), async jobs (IN-7).
**Technical notes:** **Flow A** (document-level upsert + failure branch). Enforces `corpus.documents.upload` / `corpus.documents.read`. LangChain document loader; metadata jsonb carries page/section.
**Definition of done:** as standard.
**Dependencies:** F3.

---

**Title:** [IN-2] Chunk, batch-embed, and index to pgvector with idempotent upsert
**Epic:** `ingestion`
**Problem / context:** §5.1: chunks persisted in Postgres/pgvector with HNSW + tsvector, embedded in batches with the pinned model, upsert on content hash. Completes the module walking skeleton: an uploaded document becomes retrievable rows. **Flow A** core.
**User story:** As a user with `corpus.ingest.run` permission, I want an uploaded document chunked, embedded, and indexed so that retrieval has something real to search.
**Acceptance criteria:**
- *Given* a registered document, *when* indexing runs, *then* `chunks` rows exist with content, `embedding` (pinned `text-embedding-3-large`), `tsv`, metadata, and chunk content hash, and the document status becomes `indexed`.
- *Given* N chunks, *when* embedding runs, *then* provider calls are batched (never one call per chunk), verified by call-count assertion in tests.
- *Given* re-running indexing on the same document, *when* it completes, *then* the chunk count is unchanged (upsert on chunk `content_hash`).
- *Given* the database, *when* inspected, *then* an HNSW index exists on `embedding` and a GIN index on `tsv`.
**In scope:** Recursive character splitter with 10–20% overlap (semantic chain arrives in IN-4); batch embedding client with pinned model constant; `chunks` table + indexes; upsert logic; synchronous run for now.
**Out of scope:** Semantic chunking + validation (IN-4), embedding cache (IN-6), async jobs (IN-7), keyword search usage (RT-2).
**Technical notes:** **Flow A.** Enforces `corpus.ingest.run`. Pinned model enforced as a single code constant used by index AND query paths (PRD constraint). `chunks` schema owned by this epic (DOMAIN-MODEL ownership rule). HNSW `m`/`ef_construction` per §VII.
**Definition of done:** as standard.
**Dependencies:** IN-1.

---

**Title:** [IN-3] HTML, DOCX, and CSV loaders
**Epic:** `ingestion`
**Problem / context:** §5.1 requires four source formats; IN-1 shipped PDF. Extends the loader stage of the same pipeline (split by business rule, not by layer).
**User story:** As a user with `corpus.documents.upload` permission, I want to ingest HTML, DOCX, and CSV sources so that the whole heterogeneous corpus flows through one pipeline.
**Acceptance criteria:**
- *Given* an HTML, DOCX, or CSV upload, *when* ingestion runs, *then* it normalizes to the same `{text, source, metadata}` object and indexes end to end like PDF.
- *Given* a CSV, *when* normalized, *then* row/column structure is preserved in text form with section metadata per row-group.
- *Given* an unsupported file type, *when* uploaded, *then* it is rejected at the API with a clear message (no partial rows).
- *Given* identical content in any format, *when* re-ingested, *then* dedup by content hash still holds.
**In scope:** Three loaders behind the IN-1 loader interface; format detection/validation; tests per format.
**Out of scope:** New pipeline stages; chunking changes (IN-4).
**Technical notes:** Flow A unchanged — loaders only. Reuses `corpus.documents.upload` enforcement.
**Definition of done:** as standard.
**Dependencies:** IN-1, IN-2.

---

**Title:** [IN-4] Semantic chunking fallback chain with overlap and size validation
**Epic:** `ingestion`
**Problem / context:** §5.1: semantic chunking primary, recursive splitting fallback, 10–20% overlap, chunk-size validation, graceful errors; fixed-size must not be the default. Replaces IN-2's interim splitter.
**User story:** As a user with `corpus.ingest.run` permission, I want chunking that follows meaning boundaries with a safe fallback so that answers are never silently split mid-thought.
**Acceptance criteria:**
- *Given* a normal document, *when* chunking runs, *then* semantic chunking is used and adjacent chunks overlap 10–20% (asserted in tests).
- *Given* semantic chunking fails or times out on a document, *when* the pipeline continues, *then* recursive character splitting is used for that document and the fallback is recorded in job/document metadata.
- *Given* a produced chunk outside configured min/max size, *when* validation runs, *then* it is re-split/merged or the document is flagged — never indexed silently out of bounds.
- *Given* the config, *when* reviewed, *then* fixed-size chunking is not selectable as default.
**In scope:** Semantic chunker + fallback chain, overlap enforcement, size validation, per-document strategy recorded.
**Out of scope:** Visual inspection utility (IN-5), parent/child chunk linking (RT-6 adds `parent_chunk_id` additively).
**Technical notes:** Chunk hashes change ⇒ upsert naturally replaces old chunks per Flow A; verify no orphans remain for re-chunked documents.
**Definition of done:** as standard.
**Dependencies:** IN-2.

---

**Title:** [IN-5] Chunk-boundary inspection utility
**Epic:** `ingestion`
**Problem / context:** §5.1: an inspection utility rendering chunk boundaries so mid-sentence cuts are visually detectable before indexing.
**User story:** As a user with `corpus.chunks.inspect` permission, I want to see a document's chunk boundaries rendered so that I can catch bad splits before they poison retrieval.
**Acceptance criteria:**
- *Given* a chunked document, *when* I open its inspection view, *then* the full text renders with visible chunk boundaries, overlap regions, and per-chunk index/size/strategy.
- *Given* a chunk boundary mid-sentence, *when* rendered, *then* it is visually flagged (heuristic: boundary not at sentence-ending punctuation).
- *Given* a user without the key, *when* they open the view, *then* 403 + audited denial.
**In scope:** Inspection endpoint + console screen; boundary/overlap rendering; mid-sentence heuristic flag.
**Out of scope:** Editing chunks (re-run ingestion instead); chunking changes (IN-4).
**Technical notes:** Enforces `corpus.chunks.inspect`. Read-only over `chunks`; joins on `chunk_index`.
**Definition of done:** as standard.
**Dependencies:** IN-4.

---

**Title:** [IN-6] Embedding cache keyed on content hash
**Epic:** `ingestion`
**Problem / context:** §5.1: cache embeddings keyed on content hash so identical content is never re-embedded (cost + idempotency). Completes Flow A's cache branch.
**User story:** As a user with `corpus.ingest.run` permission, I want unchanged content to skip the embedding API so that re-indexing a mostly-unchanged corpus is fast and near-free.
**Acceptance criteria:**
- *Given* a chunk hash already in `embedding_cache` for the pinned model, *when* indexing runs, *then* no embedding API call is made for it (asserted via call counting) and the cached vector is used.
- *Given* a cache miss, *when* the batch call returns, *then* cache rows `(content_hash, model, embedding)` are written.
- *Given* a full corpus re-index with no content changes, *when* it completes, *then* zero embedding API calls occur and the Ingestion Status data records 100% cache hits.
**In scope:** `embedding_cache` table (PRD decision: Postgres, not Redis); lookup-before-batch logic; cache-hit counters surfaced to job stats.
**Out of scope:** Response/semantic cache (CP-1); re-embedding to a new model (IN-8 — different model key = clean cache namespace).
**Technical notes:** **Flow A** cache branch. Composite PK `(content_hash, model)` makes IN-8's new-model pass safe by construction.
**Definition of done:** as standard.
**Dependencies:** IN-2.

---

**Title:** [IN-7] Asynchronous ingestion jobs with status console
**Epic:** `ingestion`
**Problem / context:** §5.1: ingestion/re-indexing run as background jobs decoupled from the query path; §5.8: console shows job status. Moves IN-2's synchronous run onto a worker with a tracked lifecycle.
**User story:** As a user with `corpus.ingest.run` permission, I want ingestion and re-index to run as background jobs I can monitor so that heavy corpus work never blocks or degrades /chat.
**Acceptance criteria:**
- *Given* an upload or re-index trigger, *when* accepted, *then* an `ingestion_jobs` row is created `queued` and the API returns immediately with the job id.
- *Given* a running job, *when* I open the jobs console, *then* I see live status, per-document outcomes, and counts (documents, chunks, embed batches, cache hits, failures with reasons).
- *Given* a failed job, *when* I re-run it, *then* upserts make the re-run safe and duplicates never appear (Flow A).
- *Given* a job in flight, *when* /chat is exercised, *then* query latency is unaffected (worker process, not request workers).
**In scope:** arq worker on Redis; `ingestion_jobs` + `job_documents` tables; enqueue on upload/re-index; jobs console screen; job lifecycle per DOMAIN-MODEL state machine.
**Out of scope:** Re-embedding cutover (IN-8); report exports (RP-5).
**Technical notes:** Enforces `corpus.ingest.run`. arq chosen in PRD decisions (Redis already present, replica-safe). Audit job triggers.
**Definition of done:** as standard.
**Dependencies:** IN-2 (IN-6 counters if present).

---

**Title:** [IN-8] Zero-downtime re-embedding — shadow index and cutover
**Epic:** `ingestion`
**Problem / context:** §5.1: a zero-downtime re-embedding path (dual-write/shadow index → cutover) for embedding-model changes, protecting the pinned-model invariant. **Flow D.**
**User story:** As a user with `corpus.reembed.run` permission, I want to re-embed the corpus to a new model without interrupting serving so that model upgrades are safe, verifiable, and reversible.
**Acceptance criteria:**
- *Given* a re-embed trigger with a new model id, *when* the job runs, *then* shadow embeddings are written for all chunks while the live query path continues on the old column uninterrupted.
- *Given* ingestion occurring during the window, *when* documents index, *then* both old and shadow embeddings are written (dual-write).
- *Given* shadow completion, *when* verification runs, *then* golden-set retrieval metrics on the shadow index are reported and cutover requires an explicit confirm; below-baseline results block cutover.
- *Given* cutover confirmed, *when* it executes, *then* the query path switches atomically and an abort at any earlier step removes shadow artifacts with serving untouched.
**In scope:** Shadow column/index management; dual-write toggle; verification step reusing EV-2 harness; cutover + abort; audit of every transition.
**Out of scope:** Vector-store migration to Qdrant (§VII names it a future path); routine re-index (IN-7).
**Technical notes:** **Flow D.** Enforces `corpus.reembed.run`. State machine per DOMAIN-MODEL. Embedding cache namespacing by model (IN-6) makes the shadow pass cache-correct.
**Definition of done:** as standard.
**Dependencies:** IN-6, IN-7, EV-2.

---

### Epic: `retrieval`

---

**Title:** [RT-1] Vector top-k retrieval with retrieved-chunk logging
**Epic:** `retrieval`
**Problem / context:** §5.2: DB-resident retrieval, and retrieved chunks logged per request so failures are diagnosable. Module walking skeleton: embed query with the pinned model, HNSW top-k, log what came back. Serves GN-1 in Release 1.
**User story:** As a user with `chat.query.submit` permission, I want my question to retrieve the most similar indexed chunks so that answers are grounded in the corpus.
**Acceptance criteria:**
- *Given* an indexed corpus, *when* a query runs, *then* the query is embedded with the same pinned model as indexing and the top-k chunks by HNSW similarity are returned.
- *Given* any retrieval, *when* it completes, *then* the retrieved chunk ids/scores are logged per request (distinct from used-in-prompt logging, which GN-2 adds).
- *Given* an empty corpus, *when* a query runs, *then* retrieval returns an empty set cleanly (GN-1 will answer "I don't know").
- *Given* the retrieval call, *when* it executes, *then* it runs under a hard DB timeout (F1 helper).
**In scope:** Retrieval service (LCEL), pgvector HNSW query with `ef_search` config, per-request retrieved-chunk log, k configurable.
**Out of scope:** Keyword/RRF (RT-2), rerank (RT-3), filters (RT-4).
**Technical notes:** Part of **Flow B** pipeline. Reads `chunks` (ingestion owns schema). Pinned-model constant shared with IN-2 — one source of truth.
**Definition of done:** as standard.
**Dependencies:** IN-2.

---

**Title:** [RT-2] Hybrid search — parallel keyword FTS + Reciprocal Rank Fusion
**Epic:** `retrieval`
**Problem / context:** §5.2: vector and keyword search in parallel, fused by RRF with configurable weights (default 50/50, PRD decision), fully DB-resident — no in-memory indexes.
**User story:** As a user with `chat.query.submit` permission, I want retrieval to combine semantic and exact-term matching so that both paraphrases and precise terms (names, codes) are found.
**Acceptance criteria:**
- *Given* a query, *when* retrieval runs, *then* vector search and `tsvector` full-text search execute concurrently (async gather) and results fuse via RRF.
- *Given* fusion weights in config, *when* changed per deployment (or per request override), *then* ranking shifts accordingly; default is 50/50.
- *Given* a query containing an exact rare term present in one chunk, *when* retrieval runs, *then* that chunk appears in fused results even if vector similarity alone missed it (regression test).
- *Given* a replica restart, *when* queries resume, *then* results are identical — no in-memory index exists to lose.
**In scope:** FTS query over `tsv` (GIN), RRF fusion with weights, parallel execution, config plumbing.
**Out of scope:** Rerank (RT-3), multi-query/MMR (RT-5).
**Technical notes:** **Flow B** retrieve stage. Both searches are SQL against `chunks` — DB-resident per PRD constraint.
**Definition of done:** as standard.
**Dependencies:** RT-1.

---

**Title:** [RT-3] Cross-encoder reranking of hybrid candidates
**Epic:** `retrieval`
**Problem / context:** §5.2: rerank the top ~20–50 hybrid candidates with a cross-encoder; only top-ranked results proceed to prompt assembly.
**User story:** As a user with `chat.query.submit` permission, I want candidates re-scored jointly against my query so that the chunks entering the prompt are the truly relevant ones, not just the roughly similar ones.
**Acceptance criteria:**
- *Given* fused hybrid results, *when* reranking runs, *then* the top 20–50 candidates are scored by a cross-encoder and re-ordered before selection.
- *Given* rerank is toggled off in config, *when* retrieval runs, *then* the pipeline degrades gracefully to fused order (needed for eval A/B comparison).
- *Given* the golden set, *when* EV-2 metrics run with rerank on vs off, *then* the comparison is producible (supports the gate baseline).
- *Given* the reranker call, *when* it executes, *then* it runs under a hard timeout with fallback to fused order on failure.
**In scope:** Cross-encoder integration (local model or hosted reranker behind an interface), configurable candidate window, timeout + fallback.
**Out of scope:** Compression (RT-7), budget selection (GN-2).
**Technical notes:** **Flow B** rerank stage. Interface allows swapping reranker per §XI no-lock-in.
**Definition of done:** as standard.
**Dependencies:** RT-2.

---

**Title:** [RT-4] Metadata filtering (document type, source, date, section)
**Epic:** `retrieval`
**Problem / context:** §5.2: filter retrieval by metadata so queries can target a corpus subset.
**User story:** As a user with `chat.query.submit` permission, I want to restrict a question to part of the corpus so that answers come from the sources I trust for that question.
**Acceptance criteria:**
- *Given* filter parameters (doc type, source, date range, section) on a /chat request, *when* retrieval runs, *then* both vector and keyword arms apply the filter in SQL and only matching chunks return.
- *Given* a filter matching nothing, *when* the query runs, *then* retrieval returns empty and the answer path yields a grounded "I don't know" (never an unfiltered fallback).
- *Given* the web chat, *when* a user sets a source/type filter in the UI, *then* it is passed through and reflected in the trace.
**In scope:** Filter schema on the API, SQL predicates on `metadata jsonb` (indexed paths as needed), chat-UI filter control, trace annotation.
**Out of scope:** Saved filter presets; per-role corpus partitioning (single-tenant per PRD decision #1).
**Technical notes:** **Flow B** filter stage; applied pre-fusion in both arms so RRF ranks only eligible chunks.
**Definition of done:** as standard.
**Dependencies:** RT-2, CH-1 (for the UI control).

---

**Title:** [RT-5] Multi-query retrieval and MMR diversification (configurable)
**Epic:** `retrieval`
**Problem / context:** §5.2: LLM-generated query rewordings with union of results, and Maximal Marginal Relevance diversification — each configurable per request or per deployment.
**User story:** As a user with `chat.query.submit` permission, I want poorly-phrased questions expanded and redundant results diversified so that recall improves without drowning the prompt in near-duplicates.
**Acceptance criteria:**
- *Given* multi-query enabled, *when* a query runs, *then* the cheap-fast model generates N rewordings, each retrieves, and the union (deduped by chunk id) feeds fusion/rerank.
- *Given* MMR enabled, *when* selection runs, *then* results balance relevance and diversity per the configured lambda.
- *Given* both disabled (default off pending eval evidence), *when* a query runs, *then* the pipeline is unchanged — toggles are independent.
- *Given* multi-query enabled, *when* tracing is inspected, *then* generated rewordings and per-reword hits are visible.
**In scope:** Multi-query generation + union; MMR selector; per-request/per-deployment config; trace annotations.
**Out of scope:** Agentic/self-correcting loops (Phase 7, out of scope per PRD).
**Technical notes:** **Flow B** optional stages. Rewording calls go through the shared provider throttle once CP-4 lands; until then, standard timeout/retry.
**Definition of done:** as standard.
**Dependencies:** RT-2 (RT-3 ordering respected when both on).

---

**Title:** [RT-6] Parent-document retrieval
**Epic:** `retrieval`
**Problem / context:** §5.2: match on small child chunks, return the larger parent for context.
**User story:** As a user with `chat.query.submit` permission, I want precise matching but generous context so that answers aren't starved by tiny matched fragments.
**Acceptance criteria:**
- *Given* parent-doc mode enabled, *when* ingestion chunks a document, *then* child chunks store `parent_chunk_id` linking to larger parent chunks (additive column per DOMAIN-MODEL ownership rule).
- *Given* a query in parent-doc mode, *when* children match, *then* their parents (deduped) are returned for prompt assembly, with the matching children recorded in the trace.
- *Given* parent-doc mode off, *when* retrieval runs, *then* behavior is unchanged.
**In scope:** Parent/child chunking option in the IN-4 chain; `parent_chunk_id` column; parent-swap step in retrieval; config toggle.
**Out of scope:** Graph relationships (Phase 7).
**Technical notes:** Schema change reviewed with `ingestion` (owner) — must not break Flow A upsert (parents hashed and upserted like any chunk). **Flow B** stage.
**Definition of done:** as standard.
**Dependencies:** RT-2, IN-4.

---

**Title:** [RT-7] Post-retrieval contextual compression
**Epic:** `retrieval`
**Problem / context:** §5.2: extract only relevant sentences from candidates before prompt assembly, so budget (GN-2) buys more distinct evidence.
**User story:** As a user with `chat.query.submit` permission, I want retrieved chunks compressed to their relevant sentences so that the token budget is spent on signal, not filler.
**Acceptance criteria:**
- *Given* compression enabled, *when* candidates pass rerank, *then* each is reduced to query-relevant sentences before budget selection, with original chunk ids preserved for citation.
- *Given* a chunk with no relevant sentences, *when* compression runs, *then* it is dropped rather than padded.
- *Given* compression on vs off on the golden set, *when* EV metrics run, *then* faithfulness/relevancy comparison is producible.
- *Given* the compressor call fails/times out, *when* the pipeline continues, *then* uncompressed chunks proceed (graceful fallback) and the trace notes it.
**In scope:** Compression step (LLM-extraction via cheap-fast model or embeddings-filter behind one interface), citation-id preservation, toggle, fallback.
**Out of scope:** Budget enforcement itself (GN-2).
**Technical notes:** **Flow B** compress stage — runs before budgeting, order is contractual. Citations must resolve to the ORIGINAL chunk (GN-1 contract).
**Definition of done:** as standard.
**Dependencies:** RT-3.

---

### Epic: `generation`

---

**Title:** [GN-1] Grounded, cited answer with "I don't know" (single provider)
**Epic:** `generation`
**Problem / context:** §5.3: answer only from provided context, say "I don't know" when insufficient, citations resolving to chunk sources on every answer. Release-1 slice: one provider, non-streamed.
**User story:** As a user with `chat.query.submit` permission, I want an answer built only from retrieved sources with citations so that every claim is checkable and hallucination is structurally discouraged.
**Acceptance criteria:**
- *Given* retrieved chunks, *when* generation runs, *then* the response contains an answer plus citations resolving to chunk ids → document source/section.
- *Given* retrieval returns nothing relevant (or empty), *when* generation runs, *then* the answer states it doesn't know rather than fabricating, and no citations are invented.
- *Given* a question answerable from the corpus, *when* asked, *then* the cited chunks actually contain the supporting content (golden-set spot test).
- *Given* the LLM call, *when* it executes, *then* it runs under a hard timeout and the request is traced (model, tokens, latency).
**In scope:** Grounded prompt template (LCEL), citation assembly + resolution, `POST /chat` (non-streaming v1), `conversations`/`messages` rows written, single provider (cheap-fast default).
**Out of scope:** Token budgeting (GN-2), streaming (GN-3), routing/failover (GN-4/5), history UI (CH-3).
**Technical notes:** **Flow B** generate stage. Enforces `chat.query.submit` (Flow E middleware). Messages store citations jsonb + trace_id per DOMAIN-MODEL.
**Definition of done:** as standard.
**Dependencies:** RT-1, F3.

---

**Title:** [GN-2] Hard per-request token budget with rank-then-select and usage recording
**Epic:** `generation`
**Problem / context:** §5.3: enforce a hard per-request token budget — candidates ranked first, then selected up to budget — with per-request usage recorded. Also completes §5.2's retrieved-vs-USED logging.
**User story:** As a user with `chat.query.submit` permission, I want context selection capped by a hard budget after ranking so that cost per request is bounded and the best evidence wins the budget.
**Acceptance criteria:**
- *Given* ranked candidates, *when* selection runs, *then* chunks are added in rank order until the configured token budget is reached — never truncated mid-chunk, never exceeding budget.
- *Given* any /chat request, *when* it completes, *then* the chunks USED in the prompt are logged separately from all RETRIEVED chunks (trace + message record).
- *Given* a completed request, *when* inspected, *then* prompt/completion token usage is recorded on the message row.
- *Given* a budget config change, *when* the next request runs, *then* the new budget applies (no restart).
**In scope:** Token counting, budget selector, used-vs-retrieved logging, usage persistence.
**Out of scope:** Cost dashboards (RP-2), compression (RT-7).
**Technical notes:** **Flow B** budget stage — order (rank → select) is contractual per §5.3. Used-chunk log is what makes retrieval failures diagnosable independent of generation (PRD constraint).
**Definition of done:** as standard.
**Dependencies:** GN-1.

---

**Title:** [GN-3] Token streaming end to end (SSE)
**Epic:** `generation`
**Problem / context:** §5.3/§VII: stream tokens so time-to-first-token is near-instant; §VIII documents streaming semantics on `POST /chat`.
**User story:** As a user with `chat.query.submit` permission, I want the answer to start appearing immediately so that the system feels responsive even on long answers.
**Acceptance criteria:**
- *Given* a /chat request with streaming, *when* generation begins, *then* tokens stream via SSE and TTFT is measured and traced.
- *Given* a stream in progress, *when* the client disconnects, *then* the server cancels the provider call (no orphaned spend).
- *Given* stream completion, *then* the final event carries citations and usage, and the persisted message matches the streamed content.
- *Given* a non-streaming client (integrator), *when* it requests non-stream mode, *then* the JSON response mode still works.
**In scope:** SSE on `POST /chat`, provider streaming through LCEL, cancellation propagation, final-frame citations/usage, both modes documented.
**Out of scope:** Client rendering (CH-2), output validation on stream (GN-5).
**Technical notes:** **Flow B** stream stage. Async end-to-end per §VII; uvicorn workers tuned.
**Definition of done:** as standard.
**Dependencies:** GN-1.

---

**Title:** [GN-4] Model routing across two providers (cheap-fast vs strong)
**Epic:** `generation`
**Problem / context:** §5.3/§VII: route between OpenAI and Anthropic by query difficulty/length (PRD auto-resolved heuristic); the second provider doubles as failover (wired fully in GN-5).
**User story:** As a user with `chat.query.submit` permission, I want easy queries on the cheap-fast model and hard ones on the strong model so that quality holds while cost stays controlled.
**Acceptance criteria:**
- *Given* a short/simple query, *when* routing runs, *then* the cheap-fast model is selected; *given* a long/complex query (length + retrieval-confidence heuristic), *then* the strong model is selected — decision recorded in the trace and message row.
- *Given* routing config (thresholds, model ids), *when* changed per deployment, *then* behavior follows without code change.
- *Given* both providers configured, *when* either is called, *then* calls go through one provider-client interface (the seam GN-5 uses for failover).
**In scope:** Routing heuristic + config, second provider client, `model_used` persisted, routing distribution visible in trace/metrics.
**Out of scope:** Failover/circuit breaker (GN-5), per-model cost reporting (RP-2).
**Technical notes:** **Flow B** route stage. Heuristic per PRD decisions (Annex A#14 resolved).
**Definition of done:** as standard.
**Dependencies:** GN-1.

---

**Title:** [GN-5] Reliability: retry/backoff, circuit breaker, failover, output validation, hard timeouts
**Epic:** `generation`
**Problem / context:** §5.3: retry with exponential backoff, circuit breaker, model fallback on provider failure, output validation before returning, hard timeout on every LLM/embedding/DB call. **Flow B failure branch.**
**User story:** As a user with `chat.query.submit` permission, I want provider failures absorbed by retries and failover so that one provider's outage degrades cost/latency, not availability.
**Acceptance criteria:**
- *Given* a transient provider error, *when* the call retries, *then* backoff is exponential with jitter and bounded attempts.
- *Given* failures crossing the threshold, *when* the circuit opens, *then* subsequent calls route to the failover provider without waiting for timeouts; the circuit half-opens after cooldown per the DOMAIN-MODEL state machine.
- *Given* both providers failing, *when* a request runs, *then* the client receives a structured degradation response (CH-2 renders it) — never a silent hang or stack trace.
- *Given* any response, *when* about to return/flush, *then* output validation runs (non-empty, citation ids resolve, no leaked prompt scaffolding); failures are replaced by a safe error response and traced.
- *Given* every LLM, embedding, and DB call in the codebase, *when* audited, *then* each has an explicit hard timeout (test enforces via the F1 helper).
**In scope:** Retry/backoff wrapper, per-provider circuit breaker (state in Redis, shared across replicas), failover routing, output validator, timeout audit.
**Out of scope:** Shared rate throttle (CP-4), alerting on breaker state (OB-3).
**Technical notes:** **Flow B failure branch** + circuit-breaker state machine (DOMAIN-MODEL). Breaker state in Redis so replicas share provider health.
**Definition of done:** as standard.
**Dependencies:** GN-4.

---

### Epic: `chat-clients`

---

**Title:** [CH-1] Web chat — ask a question, see the cited answer
**Epic:** `chat-clients`
**Problem / context:** §5.4: chat interface with per-answer citations linking to source documents/sections. Release-1 client slice on the F2 authenticated shell (non-streamed until CH-2).
**User story:** As a user with `chat.query.submit` permission, I want to ask a question in the browser and read a cited answer so that the platform is usable by non-technical end users from day one.
**Acceptance criteria:**
- *Given* a logged-in end user, *when* they submit a question, *then* the answer renders with citation markers, and each citation reveals source document, section/page, and the cited snippet.
- *Given* an "I don't know" answer, *when* rendered, *then* it displays as a clear state, not an error.
- *Given* a user without `chat.query.submit`, *when* they open chat, *then* the UI blocks it and the API returns 403 (audited).
- *Given* a request failure, *when* it occurs, *then* an honest error state renders (no silent failure per §5.4).
**In scope:** React chat screen in the F2 shell, /chat client call, citation rendering + source popover, basic error states.
**Out of scope:** Streaming render + full degradation states (CH-2), history (CH-3), PWA (CH-4), metadata filter UI (RT-4 adds it).
**Technical notes:** Consumes GN-1's endpoint. Citations resolve via chunk id → document metadata.
**Definition of done:** as standard.
**Dependencies:** F2, GN-1.

---

**Title:** [CH-2] Progressive streaming render with graceful degradation states
**Epic:** `chat-clients`
**Problem / context:** §5.4: render streamed answers progressively and surface degradation states (rate-limited, provider unavailable, "I don't know") rather than failing silently.
**User story:** As a user with `chat.query.submit` permission, I want the answer to appear as it's generated and honest status when the system is limited so that the experience is fast and trustworthy.
**Acceptance criteria:**
- *Given* a streamed response, *when* tokens arrive, *then* the answer renders progressively with citations attaching on the final frame.
- *Given* a 429 (rate-limited), *when* received, *then* a "you're rate-limited, retry in X" state renders with retry affordance.
- *Given* a provider-unavailable degradation response (GN-5), *when* received, *then* a clear service-status message renders.
- *Given* a dropped connection mid-stream, *when* it happens, *then* the UI shows partial-answer state with a retry, never a frozen spinner.
**In scope:** SSE client, progressive render, the three degradation states + disconnect handling.
**Out of scope:** Offline queueing; native push.
**Technical notes:** Consumes GN-3 SSE contract; degradation payload shapes agreed with GN-5/SC-2.
**Definition of done:** as standard.
**Dependencies:** CH-1, GN-3 (GN-5, SC-2 payloads when they land).

---

**Title:** [CH-3] Per-user conversation history across sessions
**Epic:** `chat-clients`
**Problem / context:** §5.4: conversation history retrievable across sessions (confirmed in-scope by PRD). GN-1 already persists messages; this slices the API + UI and the ownership guard.
**User story:** As a user with `chat.history.read` permission, I want my past conversations available when I return so that I can pick up threads and re-check cited answers.
**Acceptance criteria:**
- *Given* prior conversations, *when* the user opens history, *then* their conversations list with titles/timestamps and open with full messages + citations intact.
- *Given* user A's session, *when* A requests user B's conversation, *then* 403 + audited (own-data scope on `chat.history.read`).
- *Given* an open past conversation, *when* the user continues it, *then* new turns append to the same conversation record.
- *Given* the deployment retention policy, *when* history exceeds it, *then* expired conversations are purged by a scheduled job (default 1 year per PRD).
**In scope:** History endpoints (list/get/continue), ownership enforcement, history UI, retention purge job (arq).
**Out of scope:** Sharing/export of conversations; admin browsing of user chats (privacy — not in TOR).
**Technical notes:** Enforces `chat.history.read` with own-scope rule. Reuses `conversations`/`messages` (generation owns writes per DOMAIN-MODEL rule).
**Definition of done:** as standard.
**Dependencies:** CH-1.

---

**Title:** [CH-4] PWA packaging — installable, responsive, mobile parity
**Epic:** `chat-clients`
**Problem / context:** §5.4 mobile requirement resolved as PWA (PRD decision #2): functional parity for querying, streaming, citations, and history with zero extra codebase.
**User story:** As a user with `chat.query.submit` permission on a phone, I want to install the chat as an app with full parity so that mobile access needs no separate build.
**Acceptance criteria:**
- *Given* a phone browser, *when* the user visits, *then* the app is installable (manifest + service worker) and passes PWA installability checks.
- *Given* a phone viewport, *when* chat/history/citations are used, *then* layouts are fully responsive and streaming renders correctly on iOS Safari and Android Chrome.
- *Given* offline, *when* the app opens, *then* an honest offline state renders (no cached stale answers presented as live).
- *Given* the parity checklist (query, stream, citations, history, degradation states), *when* tested on mobile, *then* all pass.
**In scope:** Manifest, service worker (app-shell caching only), responsive pass, mobile QA checklist.
**Out of scope:** Native apps, push notifications (Phase-7-style additions per PRD).
**Technical notes:** Never cache /chat responses in the service worker (correctness + privacy); cache static shell only.
**Definition of done:** as standard.
**Dependencies:** CH-2, CH-3.

---

### Epic: `caching-performance`

---

**Title:** [CP-1] Redis semantic/response cache with hit/miss metrics
**Epic:** `caching-performance`
**Problem / context:** §5.5: a Redis-backed response/semantic cache shared across all replicas, checked before LLM invocation, with hits/misses/hit-rate as first-class metrics. Load-test acceptance needs ≥ 30% hit-rate (PRD NFR).
**User story:** As a user with `chat.query.submit` permission, I want repeated/near-identical questions answered from cache so that latency and provider cost stay flat as traffic grows.
**Acceptance criteria:**
- *Given* a /chat request whose normalized query (and filter set) matches a cached entry, *when* the pipeline runs, *then* the cached answer + citations return without any LLM call, marked `cache_hit` in the trace.
- *Given* a semantic near-match above the similarity threshold, *when* checked, *then* it also hits (semantic layer keyed on query embedding).
- *Given* any lookup, *when* it resolves, *then* hit/miss counters increment (consumed by OB-2/RP-4).
- *Given* two replicas, *when* one caches an answer, *then* the other serves it (shared Redis, nothing in-process).
**In scope:** Redis exact + semantic cache (query-embedding similarity), lookup positioned AFTER security middleware and BEFORE retrieval/LLM (Flow B order), counters, cache-key design (query hash + filters + model config).
**Out of scope:** TTL/invalidation policy (CP-2), single-flight (CP-3).
**Technical notes:** **Flow B** cache stage — position after SC-1 is contractual (§5.6). Never cache "I don't know" or degradation responses. Keys include routing/config fingerprint so config changes don't serve stale-config answers.
**Definition of done:** as standard.
**Dependencies:** GN-1 (SC-1 ordering enforced once SC-1 lands).

---

**Title:** [CP-2] TTL staggering and cache invalidation on re-index
**Epic:** `caching-performance`
**Problem / context:** §5.5: sane TTLs with staggered expiry; invalidate affected entries on re-indexing so answers never go stale against the corpus.
**User story:** As a user with `corpus.ingest.run` permission, I want re-indexing to invalidate affected cached answers so that users never receive answers contradicting the updated corpus.
**Acceptance criteria:**
- *Given* cached entries, *when* written, *then* TTLs carry random jitter so expiry never synchronizes (no thundering-herd refill).
- *Given* an ingestion/re-index job completes, *when* invalidation runs, *then* response-cache entries are flushed (v1 rule: corpus-generation key bump = full response-cache invalidation — simple and provably correct) while the embedding cache is untouched.
- *Given* a query after invalidation, *when* it runs, *then* the answer reflects the new corpus (integration test: ingest change → ask → updated answer).
**In scope:** TTL + jitter policy, corpus-generation key in cache keys, invalidation hook on IN-7 job completion.
**Out of scope:** Fine-grained per-document invalidation (later optimization once hit-rate data exists).
**Technical notes:** Generation-key bump chosen over selective invalidation: correctness first, per PRD "answers never go stale". Embedding cache (IN-6) is content-addressed and never invalidated.
**Definition of done:** as standard.
**Dependencies:** CP-1, IN-7.

---

**Title:** [CP-3] Single-flight request coalescing (stampede protection)
**Epic:** `caching-performance`
**Problem / context:** §5.5: concurrent misses on the same key must trigger exactly one computation. **Flow C.**
**User story:** As a user with `chat.query.submit` permission, I want simultaneous identical questions to share one computation so that a popular question can't multiply provider cost or exhaust quota.
**Acceptance criteria:**
- *Given* N concurrent requests missing on the same cache key, *when* they execute, *then* exactly one runs the pipeline and N−1 receive its result (asserted with a concurrency test counting LLM invocations).
- *Given* the computing request fails, *when* waiters are notified, *then* they fail fast or retry-once — never deadlock on a lock held by a dead worker (lock TTL).
- *Given* two replicas, *when* the same key misses on both, *then* coalescing still holds (Redis lock, not in-process).
**In scope:** Redis SET-NX lock + result publish/subscribe (or polling with backoff), lock TTL + owner check, concurrency tests.
**Out of scope:** Provider throttle (CP-4).
**Technical notes:** **Flow C.** Streaming interaction: waiters receive the completed cached answer (v1); do not attempt fan-out streaming.
**Definition of done:** as standard.
**Dependencies:** CP-1.

---

**Title:** [CP-4] Shared provider rate control — token bucket with 429-aware backoff
**Epic:** `caching-performance`
**Problem / context:** §5.5: a shared Redis-backed token-bucket/semaphore capping total in-flight provider calls below account TPM/RPM across ALL replicas, with 429-aware backoff. The uncoordinated-replicas failure named in §II. **Flow C.**
**User story:** As the internal team, we want every replica to draw provider capacity from one shared budget so that scaling out never trips account quotas or cascades 429s.
**Acceptance criteria:**
- *Given* configured TPM/RPM caps per provider, *when* calls exceed available tokens, *then* callers wait (bounded) or shed per backpressure policy — provider-observed rate stays below cap under load test.
- *Given* a 429 from a provider, *when* received, *then* backoff with jitter applies AND the bucket temporarily shrinks (adaptive), traced and counted.
- *Given* multiple replicas, *when* all call providers, *then* the combined rate respects the single shared budget (multi-process test).
- *Given* embedding batch calls (ingestion), *when* they run, *then* they draw from the same bucket as chat calls.
**In scope:** Redis token bucket per provider (Lua for atomicity), acquisition wrapper on the provider-client interface (GN-4 seam), 429 adaptive shrink, metrics for RP-4.
**Out of scope:** Load shedding at the API edge (OB-4), per-client rate limiting (SC-2).
**Technical notes:** **Flow C.** Wraps the same interface the circuit breaker (GN-5) wraps — order: bucket → breaker → call.
**Definition of done:** as standard.
**Dependencies:** GN-4 (GN-5 recommended first).

---

### Epic: `security`

---

**Title:** [SC-1] Security middleware ahead of cache — sanitization, prompt-injection checks, PII masking
**Epic:** `security`
**Problem / context:** §5.6: security middleware runs ahead of the cache: prompt-injection checks, input sanitization, and PII detection/masking for safety and log hygiene. **Flow B** first stage.
**User story:** As the internal team, we want every inbound query screened and cleaned before caching, retrieval, or logging so that injected instructions and personal data never propagate into prompts, caches, traces, or logs.
**Acceptance criteria:**
- *Given* any /chat request, *when* processed, *then* sanitization + injection check + PII masking run BEFORE cache lookup (Flow B order asserted in an integration test).
- *Given* a query matching injection patterns (e.g. "ignore previous instructions"), *when* screened, *then* it is blocked or neutralized per config, and the event is audited + traced.
- *Given* a query containing PII (emails, phone numbers, PH national-ID formats), *when* processed, *then* masked values are what appear in cache keys, traces, and logs (RA 10173 baseline per PRD).
- *Given* a clean query, *when* screened, *then* added latency stays within budget (screening is local/regex+model-light, measured).
**In scope:** ASGI middleware stage, injection heuristics, PII detectors + masking, config toggles per deployment, audit/trace hooks.
**Out of scope:** Per-client rate limiting (SC-2), output-side validation (GN-5).
**Technical notes:** **Flow B** stage 1 — position is contractual. Masking must run before CP-1 key computation (cache keys must never embed raw PII).
**Definition of done:** as standard.
**Dependencies:** GN-1 (CP-1 order test once CP-1 lands).

---

**Title:** [SC-2] Per-client rate limiting on public endpoints (SlowAPI)
**Epic:** `security`
**Problem / context:** §5.6: per-client rate limiting (SlowAPI) on all public endpoints — abuse control at the edge, distinct from the provider bucket (CP-4).
**User story:** As the internal team, we want per-user/per-key request limits so that one hot client cannot starve everyone else or run up provider cost.
**Acceptance criteria:**
- *Given* a client exceeding its configured limit, *when* the next request arrives, *then* 429 returns with Retry-After, the event is counted, and CH-2 renders the rate-limited state.
- *Given* limits keyed by authenticated principal (user id or API key), *when* enforced, *then* one client's throttling never affects another.
- *Given* multiple replicas, *when* limits are enforced, *then* counts are shared via Redis storage (not per-process).
- *Given* /health and /metrics, *when* called by infra, *then* they are exempt.
**In scope:** SlowAPI with Redis backend, per-principal keys, per-route configs, 429 payload shape shared with CH-2.
**Out of scope:** Provider-side throttle (CP-4), load shedding (OB-4).
**Technical notes:** Principal from F3/SC-3 auth context. Limits config per deployment (single-tenant, PRD decision #1).
**Definition of done:** as standard.
**Dependencies:** F3 (SC-3 for API-key principals).

---

**Title:** [SC-3] Integrator API keys — provision, scope, revoke, audit
**Epic:** `security`
**Problem / context:** §VIII/§5.6: authenticate API consumers (e.g. the NestJS backend) via API keys or OAuth2-equivalent; all integration activity audit-logged; Developer/Integrator role manages own credentials.
**User story:** As a user with `apikeys.keys.manage` permission, I want to provision and revoke scoped API keys so that external backends can consume the REST API securely and accountably.
**Acceptance criteria:**
- *Given* the developer console, *when* a key is created with selected permission scopes (subset of the holder's own), *then* the secret displays once and only a hash is stored.
- *Given* a request with a valid key, *when* it calls an endpoint within scope, *then* it succeeds and the call is audited with the key identity; out-of-scope → 403 + audited.
- *Given* a revoked key, *when* used, *then* 401 immediately (revocation checked per request).
- *Given* a key holder, *when* they view their keys, *then* they see only their own, with last-used timestamps.
**In scope:** `api_keys` table, key auth middleware plugged into the F3 permission resolver (API key = principal with scoped keys), console screens (create/list/revoke), audit of all integration activity.
**Out of scope:** OAuth2 flows (keys satisfy §VIII "API keys or OAuth2-equivalent"; OAuth per engagement later), usage dashboards (Developer sees own logs via OB/RP views).
**Technical notes:** Enforces `apikeys.keys.manage`. Extends Flow E's principal resolution — one middleware path for users and keys.
**Definition of done:** as standard.
**Dependencies:** F3, F4.

---

### Epic: `evaluation`

---

**Title:** [EV-1] Golden evaluation set — storage and management
**Epic:** `evaluation`
**Problem / context:** §5.7: maintain a golden set of representative questions, the chunk(s) that should be retrieved, and ideal answers. Everything downstream (metrics, gate, IN-8 verification) reads this.
**User story:** As a user with `eval.golden.manage` permission, I want to curate golden questions with expected chunks and ideal answers so that quality has a concrete, versioned definition.
**Acceptance criteria:**
- *Given* the golden-set screen, *when* an analyst adds an item (question, expected chunk references, ideal answer, tags), *then* it persists and lists; edits/retires are audited.
- *Given* expected chunks, *when* referenced, *then* they bind by chunk content hash (stable across re-index) and the UI assists by running live retrieval to pick chunks.
- *Given* a re-chunking that orphans an expected hash, *when* the set is validated, *then* the item is flagged stale rather than silently passing/failing.
- *Given* a user without the key, *when* they modify the set, *then* 403 + audited.
**In scope:** `golden_items` table, CRUD endpoints + console screen, hash-binding + staleness validation, import/export JSON (portability per §XI).
**Out of scope:** Metric computation (EV-2/3), CI wiring (EV-4).
**Technical notes:** Enforces `eval.golden.manage`. **Operational prerequisite flagged in PRD:** seeding needs the initial corpus (Annex B Q5) — build is unblocked, seeding is a client-side task.
**Definition of done:** as standard.
**Dependencies:** IN-2, F3.

---

**Title:** [EV-2] Retrieval metrics harness — hit rate, MRR, precision@k, recall@k
**Epic:** `evaluation`
**Problem / context:** §5.7: compute retrieval metrics on every change to chunking, embeddings, retrieval weights, or reranking. This is how retrieval gets blamed or cleared before generation (§5.2 purpose).
**User story:** As a user with `eval.runs.execute` permission, I want one command/run that scores retrieval against the golden set so that any retrieval change is judged by numbers, not vibes.
**Acceptance criteria:**
- *Given* the golden set, *when* an eval run executes, *then* hit rate, MRR, precision@k, and recall@k compute per item and aggregate, stored as an `eval_runs` row with git sha + config snapshot.
- *Given* two runs, *when* compared in the console, *then* per-metric deltas display (the diff the gate and IN-8 verification consume).
- *Given* pipeline toggles (rerank on/off, weights, multi-query), *when* a run is parameterized, *then* the config snapshot records exactly what was evaluated.
- *Given* an empty/stale golden set, *when* a run starts, *then* it fails loudly rather than reporting perfect scores on nothing.
**In scope:** Eval runner (CLI + endpoint + console trigger), metric implementations, `eval_runs` persistence, comparison view.
**Out of scope:** Generation metrics (EV-3), CI gate (EV-4).
**Technical notes:** Enforces `eval.runs.execute`. Runs against the real retrieval service (Flow B stages), not a reimplementation — else the numbers lie.
**Definition of done:** as standard.
**Dependencies:** EV-1, RT-2.

---

**Title:** [EV-3] Generation metrics — faithfulness and answer relevancy
**Epic:** `evaluation`
**Problem / context:** §5.7: generation metrics (faithfulness/groundedness, answer relevancy) via RAGAS or LangSmith evaluators, alongside EV-2's retrieval metrics in the same run record.
**User story:** As a user with `eval.runs.execute` permission, I want answers scored for groundedness and relevancy so that generation quality is measured separately from retrieval quality.
**Acceptance criteria:**
- *Given* an eval run with generation enabled, *when* it executes, *then* faithfulness and answer relevancy compute per item (RAGAS) and aggregate into the same `eval_runs` row.
- *Given* an answer containing claims absent from its used chunks, *when* scored, *then* faithfulness drops (verified with a seeded negative case).
- *Given* evaluator LLM calls, *when* they run, *then* they respect the shared provider throttle and are cost-tracked as eval spend, not user spend.
**In scope:** RAGAS integration, per-item + aggregate persistence, negative-case tests, eval-spend tagging.
**Out of scope:** CI gate thresholds (EV-4).
**Technical notes:** Evaluator calls go through the provider-client interface (CP-4/GN-5 wrapped). Trace eval runs distinctly (OB-1 tags).
**Definition of done:** as standard.
**Dependencies:** EV-2, GN-2.

---

**Title:** [EV-4] CI regression gate with explicit baseline approval
**Epic:** `evaluation`
**Problem / context:** §5.7/§XII: the evaluation suite runs in CI and blocks deployment when scores drop below baseline; no release ships past a failing gate. **Flow F.**
**User story:** As a user with `eval.gate.approve` permission, I want CI to block quality regressions and require my explicit approval to move the baseline so that quality only changes on purpose.
**Acceptance criteria:**
- *Given* a merge touching chunking/embedding/retrieval/rerank/prompting paths, *when* CI runs, *then* the eval suite executes and compares against the stored baseline run.
- *Given* any metric below baseline − tolerance, *when* the gate evaluates, *then* the pipeline fails with a per-metric diff report and deployment is blocked.
- *Given* an intentional improvement, *when* a holder of `eval.gate.approve` promotes the run to baseline, *then* the promotion is audited and subsequent gates compare against it — the baseline NEVER updates implicitly (Flow F rule, negative test).
- *Given* metrics at/above baseline, *when* the gate evaluates, *then* it passes and the run is stored for RP-3 trending.
**In scope:** CI job (invokes EV-2/EV-3 runner against a seeded test corpus), baseline storage + comparison + tolerance config, promotion endpoint/console action, diff report artifact.
**Out of scope:** Deployment automation itself (DP-1); per-module pytest suites (standing DoD item, not a ticket).
**Technical notes:** **Flow F.** Enforces `eval.gate.approve` on promotion. Initial thresholds = first approved baseline − small tolerance (PRD: Annex A#13 resolved as baseline-relative, not absolute numbers).
**Definition of done:** as standard.
**Dependencies:** EV-3.

---

### Epic: `observability-ops`

---

**Title:** [OB-1] Full per-request /chat trace — retrieved vs used, tokens, latency, cost
**Epic:** `observability-ops`
**Problem / context:** §5.8: one traceable per /chat call covering query processing, retrieved vs used context, model calls, token usage, latency, and cost. F1 turned tracing on; this makes the trace complete and diagnostic (fulfills §5.2's separate-observability purpose end to end).
**User story:** As a user with `ops.dashboards.view` permission, I want every answer's full pipeline visible in one trace so that a bad answer is diagnosable to its exact stage — retrieval, selection, or generation — in minutes.
**Acceptance criteria:**
- *Given* any /chat request, *when* traced, *then* one root trace contains child spans for security screen, cache lookup, each retrieval stage (vector, keyword, fusion, rerank, compression), budget selection, routing decision, and the model call.
- *Given* the trace, *when* inspected, *then* retrieved chunks and used chunks are separately recorded with ids/scores, and token usage + computed cost appear on the model span.
- *Given* a degraded request (retry, breaker, failover, cache hit), *when* traced, *then* the path taken is explicit in span attributes.
- *Given* PII masking (SC-1), *when* traces are written, *then* only masked content appears.
**In scope:** Span instrumentation across Flow B stages, cost computation per model pricing config, trace-id propagation to `messages.trace_id`, eval-run tagging.
**Out of scope:** Metrics/dashboards (OB-2), alerting (OB-3).
**Technical notes:** Wraps **Flow B** — each contractual stage is a named span, so order violations are visible in traces. LangSmith/Langfuse per §VII (provider-swappable per §XI).
**Definition of done:** as standard.
**Dependencies:** GN-2 (richer with GN-5, CP-1, SC-1 landed).

---

**Title:** [OB-2] Prometheus metrics, Grafana dashboards, structured JSON logs
**Epic:** `observability-ops`
**Problem / context:** §5.8/§VII: Prometheus metrics (request count, latency percentiles, token usage, cache hit-rate, error rate) visualized in Grafana; structured JSON logs suitable for aggregation; never dict counters.
**User story:** As a user with `ops.dashboards.view` permission, I want live service metrics on dashboards so that capacity, cost, and error trends are visible before users feel them.
**Acceptance criteria:**
- *Given* the service under traffic, *when* `GET /metrics` is scraped, *then* it exposes request counts, latency histograms (incl. TTFT), token usage, cache hit-rate, provider 429/backoff counts, and error rates — all via prometheus-client, none in-process-only.
- *Given* Grafana, *when* opened, *then* provisioned dashboards render the above from Prometheus (dashboards as code in the repo).
- *Given* any log line, *when* emitted, *then* it is structured JSON with request id, principal (masked), route, status, duration.
- *Given* multiple replicas, *when* scraped, *then* per-replica metrics aggregate correctly (no shared-state counters).
**In scope:** prometheus-client instrumentation, /metrics endpoint, Grafana provisioning (compose), structlog JSON config, request-id middleware.
**Out of scope:** Alert rules (OB-3), report exports (RP-4).
**Technical notes:** Counters from CP-1/CP-4/GN-5 register here. /metrics exempt from SC-2 limits.
**Definition of done:** as standard.
**Dependencies:** F1 (richer with CP-1/CP-4/GN-5).

---

**Title:** [OB-3] /health with provider reachability + threshold alerting
**Epic:** `observability-ops`
**Problem / context:** §5.8: `/health` checking DB, Redis, AND provider reachability; alerts on latency, error-rate, throughput, and cache-hit thresholds; §XII names /health-driven alerting as a support operation.
**User story:** As the internal team, we want health checks and alerts that page us on real degradation so that incidents are caught by machines, not by users.
**Acceptance criteria:**
- *Given* `GET /health`, *when* called, *then* readiness reflects DB, Redis, and cached provider reachability (probed on interval, never per-request), with per-dependency status in the body.
- *Given* alert rules for p95 latency, error rate, throughput collapse, and cache hit-rate drop, *when* a threshold breaches for its sustained window, *then* an alert fires to the configured channel and resolves on recovery.
- *Given* a provider circuit opening (GN-5), *when* it happens, *then* an alert fires.
- *Given* alert thresholds in config, *when* tuned per deployment, *then* no code change is needed (initial values derived from the DP-3 load-test baseline — PRD Annex A#13 resolution).
**In scope:** Provider probe cache, extended /health, Prometheus alert rules + Alertmanager wiring, breaker-state alert.
**Out of scope:** SLA response tiers (§XII per-engagement), on-call rota.
**Technical notes:** Load balancer readiness consumes /health (DP-1). Rules as code alongside OB-2 dashboards.
**Definition of done:** as standard.
**Dependencies:** OB-2, GN-5.

---

**Title:** [OB-4] Load shedding, graceful drain, and DB scaling signal
**Epic:** `observability-ops`
**Problem / context:** §5.8: shed load with fast 503s under overload, drain in-flight requests on shutdown/deploy, and monitor DB CPU/query latency as a scaling signal separate from API latency.
**User story:** As the internal team, we want the service to fail fast under overload and exit clean on deploys so that saturation degrades throughput predictably instead of timing everything out.
**Acceptance criteria:**
- *Given* in-flight requests above the backpressure threshold, *when* new requests arrive, *then* they receive fast 503 + Retry-After (no queue buildup), counted and visible in OB-2.
- *Given* SIGTERM on deploy/scale-in, *when* received, *then* the replica stops accepting new work, finishes or cleanly cancels in-flight streams within the drain window, then exits (zero dropped requests in a rolling-deploy test).
- *Given* DB CPU/query-latency metrics, *when* dashboards render, *then* DB pressure is distinguishable from API pressure (separate panels + alert rule).
- *Given* a shed event, *when* it occurs, *then* CH-2's degradation state renders on the client.
**In scope:** Concurrency-limit middleware, uvicorn lifespan drain handling, DB stat collection (pg_stat views) into Prometheus, panels + rules.
**Out of scope:** Autoscaler config (DP-2) — this provides its signals.
**Technical notes:** Sheds BEFORE expensive stages (position at Flow B entry). Drain must cancel provider streams (GN-3 cancellation reused).
**Definition of done:** as standard.
**Dependencies:** OB-2, GN-3.

---

**Title:** [OB-5] Admin configuration review (read-only, secrets masked)
**Epic:** `observability-ops`
**Problem / context:** §5.8: the admin console includes configuration review; §VII: pydantic-settings validated on boot. Read-only visibility of effective config.
**User story:** As a user with `ops.config.view` permission, I want to see the running deployment's effective configuration with secrets masked so that misconfiguration is diagnosable without shell access.
**Acceptance criteria:**
- *Given* the config screen, *when* opened by a holder of the key, *then* effective settings render grouped by module (retrieval weights, budgets, cache TTLs, routing thresholds, limits), with secret-typed fields masked.
- *Given* a user without the key, *when* they open it, *then* 403 + audited.
- *Given* any secret value, *when* the endpoint responds, *then* no unmasked secret ever leaves the server (response-shape test).
**In scope:** Config-introspection endpoint over pydantic-settings with SecretStr masking, console screen, access audit.
**Out of scope:** Config editing (deploy-time per twelve-factor; single-tenant redeploys per PRD decision #1).
**Technical notes:** Enforces `ops.config.view`. Mask by type, not by field-name convention.
**Definition of done:** as standard.
**Dependencies:** F3, F4.

---

### Epic: `reporting`

---

**Title:** [RP-1] Report framework + System Audit Trail Report
**Epic:** `reporting`
**Problem / context:** §5.9/§IX: named reports on demand, exportable; the System Audit Trail Report (all state changes, privileged actions, integration activity) is always included. Walking skeleton: framework + the report every other one reuses.
**User story:** As a user with `reports.reports.view` permission, I want to run and filter the audit trail report so that accountability questions are answerable from records, not memory.
**Acceptance criteria:**
- *Given* the reports screen, *when* the audit report runs with filters (actor, action type, date range, integration/API-key activity), *then* matching entries render paginated.
- *Given* `reports.reports.export`, *when* the user exports, *then* JSON and CSV downloads match the filtered view (Excel/PDF in RP-6).
- *Given* a user with only `audit.log.view`-less roles, *when* they open the report, *then* 403 + audited.
- *Given* ≥ 1-year retention (PRD), *when* old entries are queried, *then* they remain available within the retention window.
**In scope:** Report framework (definition interface: query + filters + columns), reports console screen, audit report implementation, JSON/CSV export, permission gates.
**Out of scope:** Other four reports (RP-2..5), Excel/PDF + scheduling (RP-6).
**Technical notes:** Enforces `reports.reports.view` / `reports.reports.export`. Reads `audit_log` (append-only, F3). Framework interface is what RP-2..5 implement — grow through consumers.
**Definition of done:** as standard.
**Dependencies:** F3, F4.

---

**Title:** [RP-2] Usage & Cost Report
**Epic:** `reporting`
**Problem / context:** §IX: request volume, token consumption, per-model/per-provider cost, model-routing distribution — the inputs to cost control and routing-policy decisions.
**User story:** As a user with `reports.reports.view` permission, I want usage and cost broken down by model and provider over time so that routing policy and budgets are decided on real spend.
**Acceptance criteria:**
- *Given* a date range, *when* the report runs, *then* it shows request counts, prompt/completion token totals, computed cost per model/provider, and routing distribution (cheap-fast vs strong share).
- *Given* eval-tagged spend (EV-3), *when* reported, *then* it is separable from user-facing spend.
- *Given* export, *when* requested, *then* JSON/CSV match the view.
**In scope:** Aggregations over `messages` usage + pricing config, report definition on the RP-1 framework, charts on the screen.
**Out of scope:** Client billing/metering (PRD non-goal), forecasting.
**Technical notes:** Reads `messages` (generation owns writes — read-only per DOMAIN-MODEL rule). Pricing table shared with OB-1 cost spans — one source.
**Definition of done:** as standard.
**Dependencies:** RP-1, GN-4.

---

**Title:** [RP-3] Retrieval & Answer Quality Report
**Epic:** `reporting`
**Problem / context:** §IX: golden-set results over time — hit rate, MRR, precision@k, recall@k, faithfulness, answer relevancy — with per-change comparison against baseline; supports the gate and tuning.
**User story:** As a user with `reports.reports.view` permission, I want quality metrics trended across eval runs so that the effect of every tuning change is visible against the baseline.
**Acceptance criteria:**
- *Given* stored eval runs, *when* the report runs, *then* each metric trends over time with the active baseline marked and per-run config snapshots accessible.
- *Given* two selected runs, *when* compared, *then* per-metric deltas display (reusing EV-2's comparison).
- *Given* a gate failure (EV-4), *when* the run is inspected in the report, *then* the failing metrics are highlighted.
**In scope:** Report over `eval_runs` on the RP-1 framework; trend + comparison views; export JSON/CSV.
**Out of scope:** Running evals from the report (EV-2's job).
**Technical notes:** Reads `eval_runs` (evaluation owns writes). Enforces `reports.reports.view`.
**Definition of done:** as standard.
**Dependencies:** RP-1, EV-3.

---

**Title:** [RP-4] Cache & Performance Report
**Epic:** `reporting`
**Problem / context:** §IX: cache hit-rate, latency percentiles incl. TTFT, error rates, provider 429/backoff events, DB latency vs API latency — the capacity and scaling decision inputs.
**User story:** As a user with `reports.reports.view` permission, I want performance and cache behavior summarized over time so that scaling and TTL decisions are made on evidence.
**Acceptance criteria:**
- *Given* a date range, *when* the report runs, *then* it shows cache hit-rate, p50/p95/p99 latency and TTFT, error rates, 429/backoff counts, and DB-vs-API latency series.
- *Given* the load-test acceptance thresholds (PRD NFR), *when* rendered, *then* threshold lines are overlaid so breaches are visually obvious.
- *Given* export, *when* requested, *then* JSON/CSV match the view.
**In scope:** Report definition querying Prometheus (range API) on the RP-1 framework; threshold overlays; export.
**Out of scope:** Alerting (OB-3), dashboard duplication (Grafana remains the live view; this is the periodic decision report).
**Technical notes:** Prometheus as source keeps one truth for metrics. Enforces `reports.reports.view`.
**Definition of done:** as standard.
**Dependencies:** RP-1, OB-2.

---

**Title:** [RP-5] Ingestion Status Report
**Epic:** `reporting`
**Problem / context:** §IX: documents ingested, chunks produced, embedding batches, dedupe/upsert outcomes, failed documents with reasons — corpus completeness verification.
**User story:** As a user with `reports.reports.view` permission, I want ingestion outcomes summarized per period and per job so that corpus completeness is verifiable before anyone trusts answers over it.
**Acceptance criteria:**
- *Given* a date range, *when* the report runs, *then* it shows documents ingested by type/status, chunks produced, embedding batches and cache-hit ratio, dedupe/upsert outcomes, and failed documents with reasons.
- *Given* a failed document, *when* listed, *then* it links to the job detail (IN-7 console) for re-run.
- *Given* export, *when* requested, *then* JSON/CSV match the view.
**In scope:** Report over `ingestion_jobs`/`job_documents`/`documents` on the RP-1 framework; export.
**Out of scope:** Re-running jobs (IN-7).
**Technical notes:** Reads ingestion tables (ingestion owns writes). Enforces `reports.reports.view`.
**Definition of done:** as standard.
**Dependencies:** RP-1, IN-7.

---

**Title:** [RP-6] Excel and PDF exports + scheduled report generation
**Epic:** `reporting`
**Problem / context:** §5.9/§IX: all reports exportable in Excel, CSV, PDF, and JSON; available on demand AND on schedule. RP-1 shipped JSON/CSV; this completes formats and adds scheduling.
**User story:** As a user with `reports.schedules.manage` permission, I want reports generated on a schedule in any of the four formats so that recurring oversight needs are met without manual runs.
**Acceptance criteria:**
- *Given* any RP report, *when* exported as Excel or PDF, *then* the file opens correctly with the same data as the on-screen view (openpyxl/report-layout PDF).
- *Given* a schedule (report, cron, formats, recipients), *when* its time arrives, *then* the arq worker generates the files and delivers/stores them per config, audited.
- *Given* a schedule created/edited/deleted, *when* saved, *then* the change is audited and takes effect without redeploy.
- *Given* a user without `reports.schedules.manage`, *when* they modify schedules, *then* 403 + audited.
**In scope:** Excel + PDF renderers on the RP-1 framework, `report_schedules` table + console screen, arq cron scheduling, delivery to configured store/email.
**Out of scope:** New report kinds.
**Technical notes:** Enforces `reports.schedules.manage` (view/export keys for on-demand). Reuses IN-7's worker — no new infra.
**Definition of done:** as standard.
**Dependencies:** RP-1 (covers all report kinds as they exist).

---

### Epic: `deployment-scale`

---

**Title:** [DP-1] Multi-replica production deployment — load balancer, secrets, TLS
**Epic:** `deployment-scale`
**Problem / context:** §VII: multiple stateless replicas behind a load balancer on a container host, secrets bulk-imported into the host secret store, TLS 1.2+ in transit, AES-256 at rest (PRD NFR). Host undecided (Annex A#9) — target Render/Fly-compatible container contract so the choice stays open.
**User story:** As the internal team, we want the service running as N stateless replicas behind a load balancer with managed secrets and TLS so that production traffic is served the way the architecture promises.
**Acceptance criteria:**
- *Given* ≥ 2 replicas deployed, *when* traffic flows, *then* requests distribute across replicas and any single replica can be killed with zero user-visible errors (statelessness proven; OB-4 drain exercised).
- *Given* the LB, *when* routing, *then* it uses /health readiness and terminates TLS 1.2+ (verified by scan).
- *Given* secrets, *when* the app boots, *then* they come only from the host secret store — none in images, repo, or compose files (repo scan in CI).
- *Given* Postgres/Redis, *when* reviewed, *then* at-rest encryption is confirmed (Supabase AES-256 managed; Redis per host).
**In scope:** Production deploy config (host-agnostic container contract), LB + TLS, secret-store integration, replica-kill test, deployment doc.
**Out of scope:** Autoscaling (DP-2), load test (DP-3).
**Technical notes:** Worker process (arq) deploys alongside API replicas. EV-4 gate wired as the deploy precondition (Flow F).
**Definition of done:** as standard.
**Dependencies:** F1, OB-3, OB-4, EV-4.

---

**Title:** [DP-2] Autoscaling on CPU/latency
**Epic:** `deployment-scale`
**Problem / context:** §VII: autoscaling on CPU/latency, verified before production designation; DB pressure monitored as a separate signal (OB-4) so the autoscaler doesn't scale the wrong tier.
**User story:** As the internal team, we want replica count to follow load automatically so that capacity matches demand without manual intervention.
**Acceptance criteria:**
- *Given* sustained CPU/latency above scale-out thresholds, *when* the autoscaler evaluates, *then* replicas increase within configured min/max; on sustained low load they scale in via graceful drain (OB-4) with zero dropped requests.
- *Given* a scale event, *when* it occurs, *then* it is visible in OB-2 dashboards (replica count as a metric).
- *Given* DB-pressure alerts firing while API metrics are healthy, *when* reviewed, *then* documentation/runbook directs to DB scaling, not replica scaling (signal separation honored).
**In scope:** Host autoscaler config, thresholds + min/max, scale-in drain verification, replica-count metric, runbook section.
**Out of scope:** DB autoscaling (managed Supabase; runbook procedure only).
**Technical notes:** Thresholds finalized against DP-3 results; the two are iterated together.
**Definition of done:** as standard.
**Dependencies:** DP-1.

---

**Title:** [DP-3] Load test at agreed targets — 50 users / 20 QPS
**Epic:** `deployment-scale`
**Problem / context:** §VII/§X: a passing load test at target concurrency gates production designation. Targets fixed by PRD decision #4: 50 concurrent users / ~20 QPS sustained, TTFT p95 ≤ 2 s, full-answer p95 ≤ 15 s, error rate < 1%, cache hit-rate ≥ 30%, autoscaling verified.
**User story:** As the internal team, we want a repeatable load test proving the acceptance targets so that "production-ready" is a measured fact, not a claim.
**Acceptance criteria:**
- *Given* the load scenario (realistic query mix incl. cache-hitting repeats and filtered queries), *when* run at 50 users / 20 QPS sustained, *then* TTFT p95 ≤ 2 s, full-answer p95 ≤ 15 s, error rate < 1%, cache hit-rate ≥ 30%.
- *Given* the run, *when* observed, *then* autoscaling triggers and recovers correctly, provider-observed rate stays under quota (CP-4), and no 5xx bursts occur during scale events.
- *Given* completion, *then* a versioned load-test report (scenario, environment, results vs targets) is produced — a named §X deliverable.
- *Given* a scaling-relevant change later, *when* merged, *then* the runbook requires re-running this test (§XII).
**In scope:** Locust or k6 scenario as code, seeded test corpus + accounts, streaming-aware TTFT measurement, report generation.
**Out of scope:** Chaos testing; soak tests beyond the sustained window (runbook follow-ups).
**Technical notes:** Exercises Flows B and C under real concurrency — the only place stampede/throttle correctness is truly proven.
**Definition of done:** as standard.
**Dependencies:** DP-2, CP-3, CP-4.

---

**Title:** [DP-4] API documentation, operations runbook, knowledge transfer
**Epic:** `deployment-scale`
**Problem / context:** §VIII/§X: API documentation (endpoint reference, data dictionary, field mappings, auth procedures, sample requests/responses incl. streaming semantics), operations runbook, and internal knowledge-transfer session are named deliverables.
**User story:** As a user with `apikeys.keys.manage` permission (an integrator) and as the operating team, we want complete API docs and a runbook so that integration and incident response need no tribal knowledge.
**Acceptance criteria:**
- *Given* the API docs, *when* an integrator follows them cold (auth via API key, POST /chat streaming and non-streaming, error/degradation shapes, /health, /metrics), *then* a working integration results without asking the team (doc-driven test).
- *Given* the runbook, *when* an on-call follows it for the top scenarios (provider outage/breaker open, cache/Redis failure, DB pressure, gate failure, re-embed cutover, scale events), *then* each has detection, diagnosis (trace/dashboards), and action steps.
- *Given* the OpenAPI schema, *when* compared to the live service, *then* they match (CI schema check).
- *Given* the knowledge-transfer session, *when* delivered, *then* materials are stored in the repo and attendance recorded (§X deliverable).
**In scope:** OpenAPI-derived reference + written guides (data dictionary, streaming semantics), runbook, KT session materials, CI doc-drift check.
**Out of scope:** End-user help content (in-app states cover it), SLA tier definitions (§XII per-engagement).
**Technical notes:** Docs live in-repo; doc updates are already in every ticket's DoD — this ticket assembles and verifies the whole.
**Definition of done:** as standard.
**Dependencies:** SC-3, GN-3, DP-3 (final numbers referenced in docs).

---

*End of ticket set. Standard: vertical slices as tickets, modules as epics, walking skeleton first (product-wide and per epic), shared services grown through first consumers (audit helper in F3, worker in IN-7, report framework in RP-1, provider-client seam in GN-4) and reused rather than rebuilt, every story naming the exact permission key its acceptance criteria enforce.*

---

## How a developer uses this package
1. Open your assigned ticket in TICKETS.md; create a branch named after its KEY; build to its acceptance criteria (they are the test list).
2. When unsure of a name, lifecycle, or permission key, consult DOMAIN-MODEL.md; when unsure of a boundary or constraint, consult PRD.md; when your ticket references a Flow (A–F), re-read that flow before coding.
3. One ticket → one branch → one small PR; the Definition of Done on the ticket is the merge checklist.
