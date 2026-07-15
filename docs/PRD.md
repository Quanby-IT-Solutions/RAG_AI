# PRD — Production Retrieval-Augmented Generation Platform (RAGP)

Source: *Terms of Reference — Production RAG Platform (RAGP)*. Stack per TOR §VII: Python 3.11+/uv · FastAPI + uvicorn · LangChain (LCEL) · Supabase Postgres (pgvector HNSW + tsvector FTS) · Redis · OpenAI + Anthropic · LangSmith/Langfuse tracing · Prometheus/Grafana · Docker. Web/admin client: React + Vite, packaged as an installable PWA (decision #2 below).

## Problem
Prototype RAG systems fail at production load: in-memory state is lost across replicas, retrieval failures are undiagnosable because retrieval and generation aren't separately observable, naive chunking corrupts answers, uncoordinated replicas blow provider quotas, and quality regressions ship because nothing measures them (TOR §II).

## Goal
An in-house, redeployable, production-shaped RAG service: idempotent observable ingestion, hybrid DB-resident retrieval, grounded/cited/streamed answers with cost control and provider failover, web + mobile (PWA) chat, evaluation-gated CI, full per-request tracing, and horizontally scaled deployment that passes a load test at target concurrency (TOR §III).

## Primary users (RBAC roles per TOR §VI — roles are admin-configurable; permissions are what the system enforces)
System Administrator · Knowledge Manager · Quality Analyst · Developer/Integrator · End User · Auditor/Viewer.

## Non-goals (v1, per TOR §IV)
- Regulatory/compliance certification programs (RA 10173 practices applied as baseline only).
- Phase-7 capabilities: contextual retrieval, agentic/self-correcting RAG, Graph RAG, multimodal RAG.
- Data migration from legacy systems (none exist).
- In-platform billing/metering for client deployments (handled contractually).
- Native iOS/Android apps (PWA delivers mobile; native is a later à-la-carte addition).
- Multi-tenancy within one deployment (see decision #1).

## Key constraints
- **No in-process state.** All cache/coordination in Redis; all indexes in Postgres. Replicas are stateless and interchangeable (§II, §5.5, §VII).
- **Idempotent ingestion.** Upsert on content hash; re-running ingestion never duplicates; identical content is never re-embedded or re-charged (§5.1).
- **One pinned embedding model** (`text-embedding-3-large`) enforced in code for both indexing and querying; changing it requires the zero-downtime shadow-reindex path (§5.1, §VII).
- **Retrieval observable separately from generation.** Retrieved chunks and used-in-prompt chunks logged per request (§5.2).
- **Hard budgets and timeouts everywhere:** per-request token budget, hard timeout on every LLM/embedding/DB call (§5.3, §VII).
- **No release past the CI evaluation regression gate** (§5.7, §XII).
- **Security middleware runs ahead of the cache** (§5.6).
- **RBAC + audit log across all components** (§IV, §5.6); every state change and privileged action audited.

## Non-functional requirements
- **Load-test acceptance (decision #4):** 50 concurrent users / ~20 QPS sustained · TTFT p95 ≤ 2 s · full-answer p95 ≤ 15 s · error rate < 1% · cache hit-rate ≥ 30% · autoscaling verified (fills TOR §VII placeholders).
- **Security:** TLS 1.2+ in transit; AES-256 at rest (Supabase-managed disk encryption + encrypted secrets in host secret store); prompt-injection checks, input sanitization, PII masking pre-cache; per-client rate limiting (SlowAPI).
- **Observability:** one trace per `/chat` call from project start; Prometheus metrics; structured JSON logs; `/health` liveness/readiness (DB, Redis, providers); alerting on latency/error/throughput/cache thresholds.
- **Retention:** audit trail ≥ 1 year; traces ≥ 1 year; conversation history configurable per deployment (default 1 year). Data portability in JSON/CSV at engagement end (§XI).
- **Locale:** organization operates in the Philippines; timestamps stored UTC, rendered Asia/Manila by default; RA 10173 baseline practices.

## Decisions made
**Business-fact calls (confirmed at the gate):**
1. **Single-tenant per deployment.** One instance per client engagement, config-driven; no `tenant_id` plumbing. Matches the "redeploy per client" intent with far less complexity (Annex B Q2).
2. **Mobile = PWA.** The React web chat client is responsive and installable; parity (streaming, citations, history) comes free (Annex B Q3).
3. **Auth = admin-provisioned accounts,** email+password via Supabase Auth; no self-registration; SSO per-engagement later (Annex B Q4).
4. **Load-test targets** as in NFRs above (Annex B Q1).

**Best-practice calls (auto-resolved, per TOR assumption register):**
- Embedding model `text-embedding-3-large`, pinned (Annex A#4 — roadmap recommendation).
- Hybrid fusion starts 50/50 vector/keyword, configurable (Annex A#5); tuning is a Quality-Analyst activity driven by eval metrics, not a ticket.
- Model routing heuristic: query length + retrieval-confidence based, cheap-fast default, strong model for long/hard queries; second provider doubles as failover (Annex A#14).
- Admin console is a web UI, grown screen-by-screen inside the features that need it (corpus mgmt in ingestion tickets, user/role in foundation, config review in observability) — not a separate build (Annex A#7).
- Embedding cache is a Postgres table keyed `(content_hash, model)` — persistent and shared, unlike Redis, and survives cache flushes; Redis reserved for response/semantic cache and coordination.
- Background jobs: FastAPI + a lightweight worker (arq on Redis) — async, replica-safe, no new infra beyond Redis.
- Golden-set seeding requires the initial corpus (Annex B Q5) — flagged as an operational prerequisite on ticket EV-1, not a blocker for the build.
- Phase-7 hooks: chunk `metadata jsonb` and the LCEL chain composition already leave room for contextual retrieval; no speculative code.
- NestJS consumer: nothing special to build — the documented REST API + API keys (SC-3) serves it (Annex A#8).
- Retention defaults per §XI assumed values; report scheduling via the same arq worker.

## Definition of Ready
A ticket may start when it has Given/When/Then acceptance criteria, its dependencies are identified, and any risky flow it touches is referenced (Flows A–F in TICKETS.md).

## Definition of Done
Code + tests (pytest + httpx) merged · acceptance criteria pass · audit/permission checks verified · trace/metrics emitted where applicable · eval regression gate green (once EV-4 ships) · PR reviewed · docs updated.
