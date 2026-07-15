# TERMS OF REFERENCE — Production Retrieval-Augmented Generation Platform (RAGP) `[ASSUMED name — verify]`

# I. PROJECT TITLE
Development of a Production Retrieval-Augmented Generation Platform (RAGP) `[ASSUMED name — verify]` — an in-house, general-purpose, client-deployable RAG service with API, web, and mobile interfaces.

# II. BACKGROUND / RATIONALE
Current retrieval-augmented generation implementations built as single-instance prototypes fail predictably when moved to real workloads: in-memory caches and keyword indexes are lost on restart and cannot be shared across replicas; retrieval failures (responsible for roughly 90% of RAG quality problems) cannot be diagnosed because retrieval and generation are not separately observable; naive chunking silently splits answers mid-sentence; uncoordinated replicas exhaust shared LLM provider quotas; and quality regressions ship undetected because there is no measurable evaluation baseline. The result is the well-known "works on 10 documents, breaks on 10,000" cliff — systems that answer correctly once in a demo but cannot handle production concurrency, cost control, or accountability.

The organization proposes to build, in-house, a production-shaped RAG platform from day one — externalized state, asynchronous I/O, full per-request tracing, hybrid retrieval, and evaluation-gated deployment — packaged as a reusable service that can be operated internally and rapidly configured and deployed for future client engagements `[ASSUMED reusable/productized intent — verify]`. Expected outcomes are grounded, cited, streamed answers at production concurrency; measurable and continuously regression-tested retrieval quality; controlled per-request cost; and an inspectable audit trail for every answer produced.

# III. OBJECTIVES
## A. General Objective
To strengthen the organization's capability to deliver accurate, grounded, and scalable AI-powered question answering over arbitrary document corpora, at production load and with measurable quality.

## B. Specific Objectives
- Establish a clean, idempotent, observable ingestion pipeline (load → chunk → embed → store) that supports HTML, DOCX, CSV, and PDF sources with metadata propagated end-to-end (→ Module 5.1).
- Achieve high retrieval precision through hybrid (vector + keyword) search with reciprocal rank fusion, reranking, contextual compression, and metadata filtering (→ Module 5.2).
- Deliver grounded, citation-backed, streamed answers with enforced token budgets, model routing, and automatic failover across two LLM providers (→ Module 5.3).
- Provide end users with chat access via web and mobile interfaces, and integrators with a documented RESTful API (→ Modules 5.4, and Section VIII).
- Make quality measurable and enforceable: a golden evaluation set with retrieval metrics (hit rate, MRR, precision@k, recall@k) and generation metrics (faithfulness, answer relevancy) wired into CI as a deployment regression gate (→ Module 5.7).
- Sustain production load: stateless horizontally-scaled replicas, shared Redis cache with stampede protection, shared provider-rate control, backpressure, and a passing load test at target concurrency (→ Modules 5.5, 5.8).

# IV. SCOPE OF WORK
- **Platform:** Web application (chat UI + administration console) and mobile application for end users (per stated requirement; native vs. cross-platform/PWA to be decided — see Annex B), backed by a Python/FastAPI RAG service exposing a documented RESTful API consumable by external backends (e.g., an existing NestJS backend `[ASSUMED optional consumer — verify]`).
- **Major work areas:** (a) ingestion and indexing pipeline; (b) retrieval and generation engine; (c) API service layer with security, caching, and reliability primitives; (d) web and mobile chat clients; (e) evaluation harness and CI integration; (f) observability stack (tracing, metrics, logging, alerting); (g) containerized deployment with horizontal scaling and load testing; (h) documentation and internal knowledge transfer.
- **Role-based access control and audit logging** are in scope across all components.
- **Out of scope for this version:** regulatory/compliance certifications (explicitly excluded by the source roadmap); Phase-7 advanced capabilities — contextual retrieval, agentic/self-correcting RAG (LangGraph loops), Graph RAG (Neo4j), and multimodal RAG — which are designated as post-stabilization à-la-carte additions; data migration from any legacy system (none identified `[ASSUMED — verify]`); client-specific billing/metering `[ASSUMED — verify]`.

# V. FUNCTIONAL REQUIREMENTS

## 5.1 Document Ingestion & Corpus Management
Purpose: convert heterogeneous source documents into a clean, inspectable, re-runnable index.
- The system shall ingest HTML, DOCX, CSV, and PDF documents and normalize each to a single internal object `{text, source, metadata}`, attaching at load time: source URI, page/section, document type, ingest timestamp, and content hash.
- The system shall chunk documents using a fallback chain — semantic chunking as primary, recursive character splitting as fallback — with 10–20% overlap between adjacent chunks, chunk-size validation, and graceful error handling; fixed-size chunking shall not be the default.
- The system shall provide an inspection utility that renders chunk boundaries so mid-sentence cuts can be visually detected before indexing.
- The system shall embed chunks in batches (never one API call per chunk) using a single pinned embedding model version (`text-embedding-3-large` `[ASSUMED per roadmap recommendation — verify]`) enforced identically for indexing and querying, and shall cache embeddings keyed on content hash so identical content is never re-embedded.
- The system shall persist chunks in Postgres/pgvector (`id, content, embedding, metadata jsonb, source`) with an HNSW vector index and a `tsvector` full-text index, and shall upsert on content hash so re-running ingestion is idempotent and never duplicates records.
- The system shall run ingestion and re-indexing as asynchronous background jobs decoupled from the query path, including a zero-downtime re-embedding path (dual-write/shadow index → cutover) for embedding-model changes.

## 5.2 Retrieval Engine
Purpose: return the right chunks, measurably, before generation is ever blamed.
- The system shall execute vector search and keyword/full-text search in parallel and fuse rankings via Reciprocal Rank Fusion, with configurable weights (default ~50/50 `[ASSUMED starting weights — verify]`), persisted entirely in the database (no in-memory indexes).
- The system shall rerank the top ~20–50 hybrid candidates with a cross-encoder reranker and retain only the top-ranked results for prompt assembly.
- The system shall support multi-query retrieval (LLM-generated query rewordings, union of results) and Maximal Marginal Relevance diversification, both configurable per request or per deployment.
- The system shall support parent-document retrieval (match on small child chunks, return the larger parent for context) and post-retrieval contextual compression (extract only relevant sentences before prompt assembly).
- The system shall filter retrieval by metadata (document type, source, date, section) so queries can target a corpus subset.
- The system shall log retrieved chunks separately from the chunks actually used in the prompt, per request, so retrieval failures are diagnosable independently of generation.

## 5.3 Answer Generation & Grounding
Purpose: produce grounded, cited, cost-controlled, resilient answers.
- The system shall enforce a hard per-request token budget: candidates are ranked first (5.2), then selected up to the budget, with per-request token usage recorded.
- The system shall instruct the model to answer only from the provided context and to state "I don't know" when the context is insufficient, and shall attach citations resolving to chunk sources on every answer.
- The system shall stream tokens to the client so time-to-first-token is near-instant.
- The system shall route queries between at least two LLM providers/models (cheap-fast vs. strong) based on query difficulty/length `[ASSUMED routing heuristic — verify]`, with the second provider doubling as failover.
- The system shall apply retry with exponential backoff, a circuit breaker, model fallback on provider failure, and output validation before returning any response, with a hard timeout on every LLM, embedding, and database call.

## 5.4 End-User Chat Applications (Web & Mobile)
Purpose: give end users a grounded chat experience on web and mobile. `[ASSUMED feature detail — the roadmap is API-centric; UI capabilities below are inferred — verify]`
- The web and mobile applications shall provide a chat interface that renders streamed answers progressively and displays per-answer citations linking to source documents/sections.
- The applications shall maintain per-user conversation history retrievable across sessions `[ASSUMED — verify]`.
- The applications shall surface graceful degradation states (rate-limited, provider unavailable, "I don't know") rather than failing silently.
- The mobile application shall provide functional parity with the web chat for querying, streaming, citations, and history `[ASSUMED parity scope — verify]`.

## 5.5 Caching & Performance Layer
Purpose: keep latency and cost flat as traffic and replicas grow.
- The system shall maintain a Redis-backed response/semantic cache shared across all replicas, checked before LLM invocation, with hits/misses/hit-rate tracked as first-class metrics.
- The system shall apply sane TTLs with staggered expiry and shall invalidate affected cache entries on re-indexing so answers never go stale against the corpus.
- The system shall implement single-flight request coalescing so concurrent misses on the same key trigger exactly one computation (cache-stampede protection).
- The system shall enforce a shared Redis-backed token-bucket/semaphore capping total in-flight provider calls below account TPM/RPM quotas across all replicas, with 429-aware backoff.

## 5.6 Security & Access Control
Purpose: protect the service, the data, and the logs.
- The system shall run security middleware ahead of the cache: prompt-injection checks, input sanitization, and PII detection/masking (for safety and log hygiene).
- The system shall enforce per-client rate limiting (SlowAPI) on all public endpoints.
- The system shall enforce role-based access control per Section VI, authenticate API consumers via API keys or OAuth2-equivalent credentials, and write an audit log entry for every state change and privileged action.
- The system shall encrypt all data in transit with TLS 1.2 or higher and at rest with AES-256.

## 5.7 Evaluation & Quality Assurance
Purpose: make quality a measured, deployment-blocking property.
- The system shall maintain a golden evaluation set of representative questions, the chunk(s) that should be retrieved, and ideal answers.
- The system shall compute retrieval metrics (hit rate, MRR, precision@k, recall@k) and generation metrics (faithfulness/groundedness, answer relevancy via RAGAS or LangSmith evaluators) on every change to chunking, embeddings, retrieval weights, or reranking.
- The system shall run the evaluation suite in CI with a regression gate that blocks deployment when scores drop below the established baseline `[ASSUMED gate thresholds — verify]`.
- The system shall include per-module automated tests (security, cache, retrieval, endpoints) using pytest + httpx.

## 5.8 Observability, Operations & Administration
Purpose: never debug blind; scale on evidence.
- The system shall emit a full trace per request (LangSmith or Langfuse) covering query processing, retrieved vs. used context, model calls, token usage, latency, and cost, wrapped as one traceable per `/chat` call.
- The system shall expose Prometheus metrics (request count, latency percentiles, token usage, cache hit-rate, error rate) visualized in Grafana, and structured JSON logs suitable for aggregation.
- The system shall expose `GET /health` (liveness/readiness checking DB, Redis, and provider reachability) and shall alert on latency, error-rate, throughput, and cache-hit thresholds `[ASSUMED alert thresholds — verify]`.
- The system shall monitor database CPU and query latency as a scaling signal separate from API latency, shed load with fast 503 responses under overload, and drain in-flight requests on shutdown/deploy.
- The system shall provide an administration console for corpus management (trigger ingestion/re-index, view job status), user/role management, and configuration review `[ASSUMED admin UI — the roadmap implies operations but names no console — verify]`.
- The system shall validate all configuration on boot via pydantic-settings and fail fast on missing settings.

## 5.9 Reporting & Exports
Purpose: turn operational and quality data into decision-ready outputs (reports detailed in Section IX).
- The system shall generate the named reports in Section IX on demand and on a schedule.
- The system shall export reports and query/trace data in Excel, CSV, PDF, and JSON.
- The system shall retain and expose the System Audit Trail Report covering all state changes, privileged actions, and integration activity.

# VI. USER ROLES AND ACCESS (RBAC) — `[ASSUMED — the roadmap names no roles; all derived from the workflows — verify]`

| Role | Access Level / Permissions |
| --- | --- |
| System Administrator | Full system configuration, user and role management, environment/secret configuration review, audit log access, deployment and scaling controls (Modules 5.6, 5.8) |
| Knowledge Manager | Manage corpora: upload sources, trigger ingestion/re-indexing, review chunk-boundary inspection output, manage metadata and cache invalidation (Modules 5.1, 5.5, 5.8 console) |
| Quality Analyst | Maintain the golden set, run evaluations, view retrieval-vs-used traces and quality dashboards, approve/block releases at the regression gate (Modules 5.2, 5.7, 5.8) |
| Developer / Integrator | Provision and manage API credentials, access API documentation and sandbox, view own integration logs and usage metrics (Section VIII, Module 5.8) |
| End User | Chat via web/mobile: submit queries, receive streamed cited answers, view own conversation history (Modules 5.3, 5.4) |
| Auditor / Viewer | Read-only access to historical records, audit trails, reports, and dashboards (Modules 5.8, 5.9) |

Every role maps to at least one module and every module (5.1–5.9) has at least one consuming role.

# VII. TECHNICAL SPECIFICATIONS
- **Stack (mandated by the roadmap):** Python 3.11+ with `uv`; FastAPI + uvicorn (tuned worker count); LangChain (LCEL) for linear chains, LangGraph reserved for Phase-7 agentic loops; pydantic-settings for boot-validated configuration; pytest + httpx for tests.
- **Data tier:** Supabase Postgres with `pgvector` (HNSW index; `m`/`ef_construction` tuned at build, `ef_search` at query) plus Postgres FTS (`tsvector`) for the keyword side; connection pooling mandatory; quantization (scalar/product) applied for large corpora; migration path to a Qdrant cluster/sharded index if QPS outgrows pgvector.
- **Embeddings/LLMs:** `text-embedding-3-large`, version pinned and enforced in code for both index and query; two LLM providers (OpenAI + Anthropic) for routing and failover.
- **State & metrics:** Redis for all caching (never in-process); Prometheus + Grafana for metrics (never dict counters); LangSmith (or Langfuse) tracing enabled from project start.
- **Hosting:** containerized (Docker + docker-compose for reproducibility) on any container host (Render/Fly/own infrastructure) `[ASSUMED host undecided — verify]`; multiple stateless replicas behind a load balancer; autoscaling on CPU/latency; secrets bulk-imported into the host's secret store.
- **Security minimums:** RBAC per Section VI; TLS 1.2+ in transit; AES-256 at rest; audit logs on all system activities and state changes.
- **Performance targets:** async I/O end-to-end; hard timeouts on every external call; streaming responses (near-instant TTFT); load test (Locust or k6) at target concurrency of [TARGET CONCURRENCY — to be determined] with p95/p99 latency ≤ [TARGET — to be determined], error rate and cache hit-rate holding, and autoscaling verified before production designation.
- **Data retention:** full request traces and audit trail retained ≥ 1 year; conversation history retained per deployment policy `[ASSUMED retention periods — verify]`.

# VIII. INTEGRATION & INTEROPERABILITY
- **External services consumed:** OpenAI and Anthropic model APIs (chat + embeddings) with shared rate control per 5.5; LangSmith/Langfuse tracing API; Redis; Supabase Postgres.
- **Consumers:** the platform shall expose a secure, documented RESTful JSON API — `POST /chat` (the single heavy path: security → cache → hybrid retrieve → rerank → compress → budget → routed LLM → streamed cited answer), `GET /metrics` (Prometheus), `GET /health` — consumable by web/mobile clients and by external backends such as an existing NestJS service `[ASSUMED — verify]`; the system shall remain API-ready for future client-specific integrations.
- **Formats:** JSON as primary; exports additionally in CSV, Excel, and PDF (Section IX).
- **Authentication for integrations:** API keys or OAuth2-equivalent; all integration activity audit-logged.
- **Deliverable:** API documentation including endpoint reference, data dictionary, field mappings, authentication procedures, and sample request/response structures (including streaming semantics).

# IX. REPORTING REQUIREMENTS
- **Retrieval & Answer Quality Report** — golden-set results over time: hit rate, MRR, precision@k, recall@k, faithfulness, answer relevancy; per-change comparison against baseline; supports the CI regression gate and tuning decisions.
- **Usage & Cost Report** — request volume, token consumption, per-model/per-provider cost, model-routing distribution; supports cost control and routing-policy decisions.
- **Cache & Performance Report** — cache hit-rate, latency percentiles (incl. TTFT), error rates, provider 429/backoff events, DB latency vs. API latency; supports capacity and scaling decisions.
- **Ingestion Status Report** — documents ingested, chunks produced, embedding batches, dedupe/upsert outcomes, failed documents with reasons; supports corpus completeness verification.
- **System Audit Trail Report** — complete log of user activities, privileged actions, configuration changes, and integration events (always included).
- All reports exportable in Excel, CSV, PDF, and JSON; available on demand and on schedule.

# X. DELIVERABLES AND TIMELINE
## A. Deliverables
- Approved implementation plan and target-architecture document.
- Operational ingestion pipeline with chunk-inspection tooling and idempotent re-index jobs.
- Operational RAG service (API) with hybrid retrieval, reranking, grounding, streaming, routing, and failover.
- Web and mobile chat applications and administration console.
- Golden evaluation set, evaluation harness, and CI pipeline with regression gate.
- Observability stack: tracing project, Prometheus/Grafana dashboards, alerts, structured logging.
- Containerized deployment (Dockerfile, compose, start commands), multi-replica production environment, and a passing load-test report at target concurrency.
- API documentation, operations runbook, and internal knowledge-transfer/training session `[ASSUMED training scope — verify]`.

## B. Timeline `[ASSUMED — mapped from the roadmap's suggested build order; calendar dates to be fixed at kickoff — verify]`
| Activity | Target Timeline |
| --- | --- |
| Kickoff, architecture sign-off, project setup (tracing on) | Within 5 working days from start |
| Phase 1 — Ingestion → chunking → batch embeddings → pgvector | Week 1 |
| Phase 2 — DB-resident hybrid search, token budgeting, grounded prompting with citations | Week 2 |
| Phase 3 — Golden set + eval harness; rerank, compression, parent-doc, multi-query/MMR | Week 3 |
| Phase 4 + 6 — Security, rate limiting, caching, reliability, observability, tests, Docker, deploy | Week 4 |
| Phase 5 — Externalized state, async + timeouts, streaming, index tuning, horizontal scale, load test | Week 5 |
| Web + mobile client build and integration | Weeks 4–7 `[ASSUMED — clients not scheduled in the roadmap — verify]` |
| UAT, final acceptance, and turnover of documentation | Within 10 working days after load test passes |
| Phase 7 (contextual retrieval first; agentic/graph/multimodal on concrete need) | Post-acceptance, à la carte |

# XI. DATA GOVERNANCE & SECURITY
- All corpus documents, embeddings, conversation data, traces, and derived indexes are the exclusive property of the organization (and, in client deployments, of the respective client per contract `[ASSUMED client-deployment ownership model — verify]`); no external provider acquires ownership rights.
- The source roadmap explicitly excludes regulatory/compliance scope for this version; nonetheless, as the organization operates in the Philippines, handling of personal data shall follow the Data Privacy Act of 2012 (RA 10173) as a baseline practice, including the PII detection/masking in Module 5.6 `[ASSUMED baseline — formal compliance program out of scope — verify]`.
- Retention: audit trail ≥ 1 year, traces ≥ 1 year, conversation history per deployment policy `[ASSUMED — verify]`; full data portability in open/non-proprietary formats (JSON/CSV) at no additional cost on engagement end; secure deletion after confirmed turnover; no vendor lock-in — the stack permits substituting the vector store (pgvector → Qdrant), tracing provider, and LLM providers.
- Confidentiality: client corpora and conversation data treated as confidential; NDAs applied for client deployments as required.

# XII. WARRANTY AND SUPPORT
- As an in-house build, the internal team shall provide ongoing corrective and preventive maintenance; for client deployments, a minimum one (1) year support commitment covering defects, bug fixes, and updates at no additional cost `[ASSUMED standard commitment — verify]`.
- Support operations shall include the `/health`-driven alerting, runbook-based incident escalation, and response-time tiers of [SLA TIERS — to be determined].
- Post-deployment quality is protected structurally: no release ships past the CI evaluation regression gate, and load-test re-verification is required after scaling-relevant changes.

# ANNEX A. ASSUMPTIONS REGISTER
| # | Item | Assumed value | Why assumed | Impact if wrong |
| --- | --- | --- | --- | --- |
| 1 | System name/acronym | Production Retrieval-Augmented Generation Platform (RAGP) | Notes name no system | Cosmetic; rename throughout |
| 2 | Productized/reusable intent | Built once, redeployable per client | User: "in case we have client that needs this" | Changes configurability, tenancy, and ownership clauses |
| 3 | RBAC roles (all of Section VI) | 6 roles derived from workflows | Roadmap names no roles | Downstream logins, permission keys, and test branches change |
| 4 | Embedding model | text-embedding-3-large, pinned | Roadmap "Recommended" column | Re-index required if changed; retrieval quality shifts |
| 5 | Hybrid fusion starting weights | ~50/50 vector/keyword | Roadmap starting guidance | Retrieval precision tuning restarts |
| 6 | Chat UI feature set (history, parity, degradation states) | Standard chat UX on web + mobile | Roadmap is API-centric; UI unspecified | Client-app scope and tickets change materially |
| 7 | Admin console | Web console for corpus/user/config ops | Operations implied, no UI named | Could be CLI/scripts instead; removes a module's UI tickets |
| 8 | NestJS backend as API consumer | Optional integration | Roadmap mentions it as "if desired" | Integration section shrinks to clients-only |
| 9 | Hosting target | Undecided container host | Roadmap says "any container host" | Deploy scripts, secret store, autoscaler specifics |
| 10 | Retention periods | Audit/traces ≥ 1 yr; history per policy | No regulated retention stated | Storage costs; client contractual terms |
| 11 | Privacy-law baseline | RA 10173 practices despite compliance being out of scope | PH-based organization | If client market differs (e.g., EU), governance section expands |
| 12 | Timeline | 5-week core + weeks 4–7 clients | Roadmap build order; clients unscheduled | Resourcing and acceptance dates |
| 13 | Eval gate thresholds, alert thresholds, SLA tiers, latency targets | Placeholders | No numbers in notes | Acceptance criteria undefined until fixed |
| 14 | Model routing heuristic | Difficulty/length-based | Roadmap says "easy/short vs hard/long" | Cost profile and routing tickets |
| 15 | Support commitment for clients | 1 year included | Industry standard | Commercial terms per engagement |
| 16 | Data migration | None | No legacy system mentioned | Adds an ingestion-migration module |

# ANNEX B. OPEN QUESTIONS
1. **Target concurrency and latency SLOs** — what QPS and p95/p99 must the load test pass? (Blocks Phase-5 acceptance criteria.)
2. **Multi-tenancy** — will one deployment serve multiple clients (tenant isolation in DB, cache, and RBAC), or one deployment per client? Materially changes the data model and Section XI.
3. **Mobile approach** — native (iOS/Android), cross-platform (React Native/Flutter), or PWA? Changes the client-build workstream and timeline.
4. **End-user authentication** — self-registration, SSO, or client-provisioned accounts? Affects Modules 5.4/5.6.
5. **Initial corpus** — which documents/formats/volume seed the first deployment and the golden set? (Golden set cannot be built without it.)
6. **Conversation history retention and privacy policy** — how long, and is per-client configurability required?
7. **Commercials for client deployments** — is usage metering/billing needed in-platform, or handled contractually outside the system?
8. **Which Phase-7 capability is most likely first** (contextual retrieval is the roadmap's best-ROI pick) — worth pre-designing hooks for it?
