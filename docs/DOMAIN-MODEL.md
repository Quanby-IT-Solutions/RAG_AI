# DOMAIN MODEL — RAGP

One page of shared vocabulary. Every ticket uses these names; every story names one of these permission keys.

## Glossary (ubiquitous language)

- **Document** — one ingested source file (HTML/DOCX/CSV/PDF), normalized to `{text, source, metadata}`, identified by its **content hash**.
- **Chunk** — a retrievable slice of a document, with embedding, full-text vector, and metadata; may reference a **parent chunk** (parent-document retrieval).
- **Ingestion job** — an asynchronous run of load → chunk → embed → store for one or more documents; idempotent (upsert on content hash).
- **Embedding cache** — Postgres table keyed `(content_hash, embedding_model)`; identical content is never re-embedded.
- **Retrieval** — hybrid (vector + keyword) candidate selection, fused by RRF, then optionally reranked/compressed/diversified. **Retrieved chunks** ≠ **used chunks** (those that survived budgeting into the prompt) — logged separately.
- **Answer** — a grounded, cited, streamed generation over used chunks, produced by a **routed** provider (cheap-fast vs strong; second provider = failover).
- **Conversation / Message** — an End User's chat session and its turns; messages store citations, token usage, and trace id.
- **Golden item / Eval run** — a benchmark question with expected chunks and ideal answer; a scored execution of the golden set (retrieval + generation metrics) compared to the **baseline**.
- **Regression gate** — the CI check that blocks deployment when an eval run scores below baseline.
- **Trace** — the per-request LangSmith/Langfuse record covering query processing, retrieved vs used context, model calls, tokens, latency, cost.
- **Audit entry** — immutable record of a state change or privileged action (who, what, before/after, when).
- **API key** — credential for external integrators (e.g. a NestJS backend), scoped to permissions, audited.

## Permission-key convention (RBAC in scope)

Format: `module.submodule.action`. Stories, acceptance criteria, and middleware all use the exact key. v1 keys:

| Key | Meaning (typical role) |
|---|---|
| `users.accounts.manage` | Create/disable users (System Administrator) |
| `users.roles.manage` | Define roles, assign permissions (System Administrator) |
| `audit.log.view` | Read audit trail (Auditor/Viewer, SysAdmin) |
| `corpus.documents.upload` / `corpus.documents.read` | Add/view sources (Knowledge Manager) |
| `corpus.ingest.run` | Trigger ingestion/re-index jobs (Knowledge Manager) |
| `corpus.chunks.inspect` | View chunk-boundary inspection (Knowledge Manager) |
| `corpus.reembed.run` | Run shadow re-embedding cutover (SysAdmin/Knowledge Manager) |
| `chat.query.submit` | Ask questions via /chat (End User) |
| `chat.history.read` | Read **own** conversation history (End User) |
| `eval.golden.manage` | Maintain golden set (Quality Analyst) |
| `eval.runs.execute` | Run evaluations, view results (Quality Analyst) |
| `eval.gate.approve` | Accept a new baseline / override gate (Quality Analyst) |
| `apikeys.keys.manage` | Provision/revoke own API credentials (Developer/Integrator) |
| `ops.dashboards.view` | Quality/ops dashboards (Quality Analyst, Auditor) |
| `ops.config.view` | Read-only config review, secrets masked (SysAdmin) |
| `reports.reports.view` / `reports.reports.export` | View/export §IX reports (Auditor/Viewer+) |
| `reports.schedules.manage` | Schedule report generation (SysAdmin) |

Roles are rows in the DB mapping to sets of these keys — never hard-coded job titles.

## Entities and relationships

```
role >──< permission          (role_permissions)
role >──< user                (user_roles)
user ──< audit_entry
user ──< api_key
user ──< conversation ──< message ──(cites)──> chunk

document ──< chunk ──(child of)──> chunk (parent_chunk_id, nullable)
ingestion_job ──< job_document (per-document outcome within a job)
embedding_cache: (content_hash, model) → vector      [standalone]

golden_item ──< eval_result >── eval_run ──(compared to)──> eval_run (baseline)
report_schedule ── report_kind
```

## Core tables (lifecycle notes)

- **`users`, `roles`, `permissions`, `role_permissions`, `user_roles`, `audit_log`** — RBAC + audit spine (F2/F3). `audit_log` is append-only.
- **`api_keys`** — hashed key, permission scopes, owner, revoked_at (SC-3).
- **`documents`** — `id, source_uri, doc_type, content_hash UNIQUE, status, metadata jsonb, ingested_at`. Status lifecycle below.
- **`chunks`** — `id, document_id, content, embedding vector(3072), tsv tsvector, metadata jsonb, content_hash, parent_chunk_id, embedding_model, chunk_index`. HNSW index on `embedding`, GIN on `tsv`. Upsert key: `content_hash`.
- **`embedding_cache`** — `(content_hash, model) PK, embedding, created_at`.
- **`ingestion_jobs`** — `id, kind (ingest|reindex|reembed), status, counts jsonb, error, started_at, finished_at`.
- **`conversations`, `messages`** — messages carry `role (user|assistant), content, citations jsonb, token_usage jsonb, trace_id, model_used`.
- **`golden_items`** — `question, expected_chunk_hashes, ideal_answer, tags`.
- **`eval_runs`** — `git_sha, config_snapshot jsonb, retrieval_metrics jsonb, generation_metrics jsonb, is_baseline, passed`.
- **`report_schedules`** — `report_kind, cron, formats, recipients`.

### Cross-epic ownership risks (and the rule that resolves each)
- **`chunks`** is written by `ingestion`, read by `retrieval`, extended by parent-doc retrieval (RT-6: `parent_chunk_id`) and re-embedding (IN-8: `embedding_model`). **Rule: the `ingestion` epic owns the schema.** Other epics add columns only additively, reviewed against ingestion's idempotency invariant (upsert on `content_hash` must survive every change).
- **`messages`** is written by `generation`, read by `reporting` and `observability`. **Rule: `generation` owns writes; readers never mutate; new read needs = new columns proposed to generation.**
- **`eval_runs`** written by `evaluation`, read by `reporting` (RP-3). Same owner-writes rule.

## State machines

### Document lifecycle (the heart of ingestion — Flow A)
```
registered ──> processing ──> indexed
                  │
                  └────────> failed  ──(re-run job, same hash)──> processing
re-ingest of identical content (same content_hash) ──> no-op (idempotent upsert)
```

### Ingestion job lifecycle
```
queued ──> running ──> succeeded
              └─────> failed        (jobs are safe to re-run; per-document outcomes in job_documents)
```

### Re-embedding cutover (Flow D)
```
idle ──> shadow_writing ──> verifying ──> cutover ──> complete
            │                  │
            └──── abort ◄──────┘        (abort drops shadow column/index; serving never interrupted)
```

### Provider circuit breaker (per provider — Flow B)
```
closed ──(failure threshold)──> open ──(cooldown)──> half-open ──(probe ok)──> closed
                                                        └─(probe fails)──> open
```
