import { defineRelations, sql } from "drizzle-orm"
import { customType, index, primaryKey, type AnyPgColumn } from "drizzle-orm/pg-core"

import { createTable } from "./utils/table.js"

/**
 * pgvector column (Tech Plan §3, D5). Dimension is pinned to 1536 — the same
 * truth as the Python PINNED_EMBEDDING_DIMENSIONS constant; the RAG service
 * asserts the two match at boot (dimension guard, Tech Plan §4).
 * R-DDL spike (packages/db/README.md): drizzle-kit beta emits this DDL cleanly.
 */
const vector1536 = customType<{ data: number[]; driverData: string }>({
	dataType: () => "vector(1536)",
	toDriver: v => `[${v.join(",")}]`,
})

/** Nullable variant for the IN-8 shadow column. */
const vector1536Nullable = customType<{ data: number[] | null; driverData: string }>({
	dataType: () => "vector(1536)",
	toDriver: v => (v === null ? (null as unknown as string) : `[${v.join(",")}]`),
})

/** Full-text search vector — DB-maintained via GENERATED ALWAYS AS (§3). */
const tsvector = customType<{ data: string }>({
	dataType: () => "tsvector",
})

// ============================================================================
// BETTER AUTH TABLES
// ============================================================================

export const users = createTable("users", t => ({
	id: t.text("id").primaryKey(),
	name: t.text("name").notNull(),
	email: t.text("email").notNull().unique(),
	emailVerified: t.boolean("email_verified").default(false).notNull(),
	image: t.text("image"),
	// F4: admin-disabled accounts cannot sign in (enforced by the Better Auth
	// session-create hook in packages/auth) and their sessions are revoked.
	disabled: t.boolean("disabled").default(false).notNull(),
	createdAt: t.timestamp("created_at").notNull().defaultNow(),
	updatedAt: t.timestamp("updated_at").notNull().defaultNow(),
}))

export const sessions = createTable("sessions", t => ({
	id: t.text("id").primaryKey(),
	token: t.text("token").notNull().unique(),
	userId: t
		.text("user_id")
		.notNull()
		.references(() => users.id, { onDelete: "cascade" }),
	expiresAt: t.timestamp("expires_at").notNull(),
	ipAddress: t.text("ip_address"),
	userAgent: t.text("user_agent"),
	createdAt: t.timestamp("created_at").notNull().defaultNow(),
	updatedAt: t.timestamp("updated_at").notNull().defaultNow(),
}))

export const accounts = createTable(
	"accounts",
	t => ({
		id: t.text("id"),
		accountId: t.text("account_id").notNull(),
		providerId: t.text("provider_id").notNull(),
		userId: t
			.text("user_id")
			.notNull()
			.references(() => users.id, { onDelete: "cascade" }),
		accessToken: t.text("access_token"),
		refreshToken: t.text("refresh_token"),
		idToken: t.text("id_token"),
		accessTokenExpiresAt: t.timestamp("access_token_expires_at"),
		refreshTokenExpiresAt: t.timestamp("refresh_token_expires_at"),
		scope: t.text("scope"),
		password: t.text("password"), // For email/password auth
		createdAt: t.timestamp("created_at").notNull().defaultNow(),
		updatedAt: t.timestamp("updated_at").notNull().defaultNow(),
	}),
	t => [
		// Composite primary key on provider and account
		primaryKey({ columns: [t.providerId, t.accountId] }),
		index("account_user_id_idx").on(t.userId),
	]
)

export const verifications = createTable(
	"verifications",
	t => ({
		id: t.text("id"),
		identifier: t.text("identifier").notNull(),
		value: t.text("value").notNull(),
		expiresAt: t.timestamp("expires_at").notNull(),
		createdAt: t.timestamp("created_at").defaultNow(),
		updatedAt: t.timestamp("updated_at").defaultNow(),
	}),
	t => [
		// Composite primary key on identifier and value
		primaryKey({ columns: [t.identifier, t.value] }),
	]
)

// ============================================================================
// TODOs
// ============================================================================

export const todos = createTable("todos", t => ({
	id: t.serial("id").primaryKey(),
	title: t.text("title").notNull(),
	completed: t.boolean("completed").notNull().default(false),
	authorId: t
		.text("author_id")
		.notNull()
		.references(() => users.id, { onDelete: "cascade" }),
	createdAt: t.timestamp("created_at").notNull().defaultNow(),
	updatedAt: t.timestamp("updated_at").notNull().defaultNow(),
}))

// ============================================================================
// TICKETS
// ============================================================================

export const tickets = createTable("tickets", t => ({
	id: t.serial("id").primaryKey(),
	name: t.text("name").notNull(),
	email: t.text("email").notNull(),
	subject: t.text("subject").notNull(),
	priority: t
		.text("priority")
		.notNull()
		.default("medium")
		.$type<"low" | "medium" | "high" | "urgent">(),
	concern: t.text("concern").notNull(),
	status: t
		.text("status")
		.notNull()
		.default("received")
		.$type<"received" | "in_progress" | "resolved" | "closed">(),
	authorId: t.text("author_id").references(() => users.id, { onDelete: "set null" }),
	createdAt: t.timestamp("created_at").notNull().defaultNow(),
	updatedAt: t.timestamp("updated_at").notNull().defaultNow(),
}))

// ============================================================================
// IDEMPOTENCY KEYS
// ============================================================================

export const idempotencyKeys = createTable("idempotency_keys", t => ({
	key: t.text("key").primaryKey(),
	authorId: t.text("author_id").notNull(),
	response: t.jsonb("response").notNull().$type<unknown>(),
	createdAt: t.timestamp("created_at").notNull().defaultNow(),
}))

// ============================================================================
// RBAC
// ============================================================================

export const roles = createTable("roles", t => ({
	id: t.serial("id").primaryKey(),
	name: t.text("name").notNull().unique(),
	description: t.text("description"),
	createdAt: t.timestamp("created_at").notNull().defaultNow(),
	updatedAt: t.timestamp("updated_at").notNull().defaultNow(),
}))

export const permissions = createTable("permissions", t => ({
	id: t.serial("id").primaryKey(),
	name: t.text("name").notNull().unique(),
	description: t.text("description"),
	createdAt: t.timestamp("created_at").notNull().defaultNow(),
	updatedAt: t.timestamp("updated_at").notNull().defaultNow(),
}))

export const userRoles = createTable(
	"user_roles",
	t => ({
		userId: t
			.text("user_id")
			.notNull()
			.references(() => users.id, { onDelete: "cascade" }),
		roleId: t
			.integer("role_id")
			.notNull()
			.references(() => roles.id, { onDelete: "cascade" }),
	}),
	t => [primaryKey({ columns: [t.userId, t.roleId] }), index("user_roles_user_id_idx").on(t.userId)]
)

export const rolePermissions = createTable(
	"role_permissions",
	t => ({
		roleId: t
			.integer("role_id")
			.notNull()
			.references(() => roles.id, { onDelete: "cascade" }),
		permissionId: t
			.integer("permission_id")
			.notNull()
			.references(() => permissions.id, { onDelete: "cascade" }),
	}),
	t => [primaryKey({ columns: [t.roleId, t.permissionId] })]
)

// ============================================================================
// AUDIT LOG (append-only)
// ============================================================================

/**
 * Append-only audit spine (Tech Plan §6). INSERT-only for every writer
 * (NestJS now, Python later): a hand-augmented migration installs a
 * BEFORE UPDATE OR DELETE trigger that raises, enforcing immutability at the
 * DB. No `updatedAt`, no FKs — actors may be users, services, or the system.
 */
export const auditLog = createTable("audit_log", t => ({
	id: t.bigserial("id", { mode: "number" }).primaryKey(),
	actorId: t.text("actor_id"),
	actorType: t
		.text("actor_type")
		.notNull()
		.$type<"user" | "service" | "system">(),
	action: t.text("action").notNull(),
	resourceType: t.text("resource_type").notNull(),
	resourceId: t.text("resource_id"),
	before: t.jsonb("before").$type<unknown>(),
	after: t.jsonb("after").$type<unknown>(),
	createdAt: t.timestamp("created_at", { withTimezone: true }).notNull().defaultNow(),
}))

// ============================================================================
// API KEYS (security — SC-3)
// ============================================================================

/**
 * Integrator API keys (SC-3). Only a sha256 HASH of the secret is stored —
 * the plaintext is shown once at creation. `scopes` is the permission-key
 * subset granted to the key (validated to be within the creator's own
 * permissions). Revocation is a timestamp so the row remains for audit;
 * a revoked key 401s immediately (checked per request).
 */
export const apiKeys = createTable(
	"api_keys",
	t => ({
		id: t.uuid("id").primaryKey().defaultRandom(),
		name: t.text("name").notNull(),
		keyHash: t.text("key_hash").notNull().unique(),
		// First characters of the secret for display ("ragp_ab12…").
		keyPrefix: t.text("key_prefix").notNull(),
		ownerUserId: t
			.text("owner_user_id")
			.notNull()
			.references(() => users.id, { onDelete: "cascade" }),
		scopes: t.jsonb("scopes").$type<string[]>().notNull(),
		lastUsedAt: t.timestamp("last_used_at", { withTimezone: true }),
		revokedAt: t.timestamp("revoked_at", { withTimezone: true }),
		createdAt: t.timestamp("created_at", { withTimezone: true }).notNull().defaultNow(),
	}),
	t => [index("api_keys_owner_idx").on(t.ownerUserId)]
)

// ============================================================================
// DOCUMENTS (ingestion — Flow A)
// ============================================================================

/**
 * One ingested source file, normalized to {text, source, metadata} and
 * identified by its sha256 `contentHash` (idempotent upsert key, Flow A/A1).
 * Schema is OWNED by the ingestion epic (DOMAIN-MODEL cross-epic rule).
 * Written by the Python RAG service (no Python migrations — Drizzle owns DDL);
 * read by the NestJS gateway for the corpus list.
 *
 * Status follows the R1 document state machine:
 * registered -> processing -> indexed | failed (failed re-enters processing on
 * retry; identical content re-ingest is a no-op). IN-1 sets `processing` on a
 * successful normalize and `failed` (+ `error` reason) on parse failure;
 * `indexed` arrives with IN-2.
 */
export const documents = createTable("documents", t => ({
	// gen_random_uuid() — pgcrypto; the migration installs the extension defensively.
	id: t.uuid("id").primaryKey().defaultRandom(),
	sourceUri: t.text("source_uri").notNull(),
	docType: t.text("doc_type").notNull(),
	contentHash: t.text("content_hash").notNull().unique(),
	status: t
		.text("status")
		.notNull()
		.default("registered")
		.$type<"registered" | "processing" | "indexed" | "failed">(),
	// Normalization payload: page/section info, ingest timestamp, original filename.
	metadata: t.jsonb("metadata").$type<unknown>(),
	error: t.text("error"),
	ingestedAt: t.timestamp("ingested_at", { withTimezone: true }).notNull().defaultNow(),
}))

// ============================================================================
// CHUNKS (ingestion — Flow A, IN-2)
// ============================================================================

/**
 * One retrievable slice of a document (DOMAIN-MODEL `chunks`). Written by the
 * Python RAG service (chunk -> batch-embed -> upsert), read by retrieval
 * (RT-1/RT-2). The `ingestion` epic owns this schema; other epics extend it
 * additively only (RT-6 adds parent_chunk_id, deliberately absent here).
 *
 * Idempotency invariant: upsert ON CONFLICT (content_hash) — re-running
 * indexing on the same document never duplicates rows. `embedding_model`
 * stamps the pinned model per row so a future model change (IN-8) is
 * detectable. `tsv` is a DB-maintained generated column; the HNSW
 * (embedding) and GIN (tsv) indexes are hand-augmented in the migration
 * (drizzle can't emit them — Tech Plan §3 / Risk R-DDL).
 */
export const chunks = createTable("chunks", t => ({
	id: t.uuid("id").primaryKey().defaultRandom(),
	documentId: t
		.uuid("document_id")
		.notNull()
		.references(() => documents.id, { onDelete: "cascade" }),
	content: t.text("content").notNull(),
	embedding: vector1536("embedding").notNull(),
	// CE-1 contextual retrieval: the FTS vector indexes the LLM-generated
	// situating context TOGETHER with the raw chunk text (Flow G — both
	// retrieval arms see the enriched text); `content` stays the raw
	// citation/display surface. NULL context (baseline) yields a tsv
	// identical to plain to_tsvector(content).
	tsv: tsvector("tsv").generatedAlwaysAs(
		sql`to_tsvector('english', coalesce("context_text" || ' ', '') || "content")`
	),
	// Page/section carried over from the document normalization payload.
	metadata: t.jsonb("metadata").$type<unknown>(),
	contentHash: t.text("content_hash").notNull().unique(),
	chunkIndex: t.integer("chunk_index").notNull(),
	embeddingModel: t.text("embedding_model").notNull(),
	// RT-6 parent-document retrieval (additive per the cross-epic rule):
	// small child chunks link to their larger parent chunk; parents carry
	// metadata.role = 'parent' and are excluded from the search arms.
	parentChunkId: t.uuid("parent_chunk_id").references((): AnyPgColumn => chunks.id, {
		onDelete: "set null",
	}),
	// IN-8 (Flow D): shadow embeddings written by a re-embed migration while
	// the live query path stays on `embedding`; the active-column pointer in
	// Redis flips atomically at cutover. Nullable — populated only during a
	// migration window; abort NULLs it back.
	embeddingShadow: vector1536Nullable("embedding_shadow"),
	shadowEmbeddingModel: t.text("shadow_embedding_model"),
	// CE-1 (Flow G): LLM-generated situating context, prepended to the chunk
	// for BOTH retrieval indexes (embedding + tsv above). Nullable — baseline
	// deployments and enrichment fallbacks leave it NULL.
	contextText: t.text("context_text"),
}))

// ============================================================================
// INGESTION JOBS (ingestion — IN-7)
// ============================================================================

/**
 * One background ingestion/re-index run (DOMAIN-MODEL `ingestion_jobs`).
 * OWNED by the ingestion epic; written by the Python RAG service + arq worker
 * (no Python migrations — Drizzle owns DDL), read by the gateway for the jobs
 * console and by reporting (RP-5, read-only).
 *
 * Lifecycle: queued -> running -> completed | failed. Re-running a failed job
 * is always safe: the pipeline is upsert-idempotent end to end (Flow A).
 * `stats` accumulates the counts the console shows (documents, chunks, embed
 * batches, cache hits once IN-6 lands, failures).
 */
export const ingestionJobs = createTable("ingestion_jobs", t => ({
	id: t.uuid("id").primaryKey().defaultRandom(),
	kind: t
		.text("kind")
		.notNull()
		.default("ingest")
		.$type<"ingest" | "reindex">(),
	status: t
		.text("status")
		.notNull()
		.default("queued")
		.$type<"queued" | "running" | "completed" | "failed">(),
	// Actor who triggered the job (audit joins on audit_log; this is display).
	triggeredBy: t.text("triggered_by"),
	stats: t.jsonb("stats").$type<unknown>(),
	error: t.text("error"),
	createdAt: t.timestamp("created_at", { withTimezone: true }).notNull().defaultNow(),
	startedAt: t.timestamp("started_at", { withTimezone: true }),
	finishedAt: t.timestamp("finished_at", { withTimezone: true }),
}))

/**
 * Per-document outcome inside a job (DOMAIN-MODEL `job_documents`): the jobs
 * console lists each document's status, chunk count, and failure reason.
 */
export const jobDocuments = createTable(
	"job_documents",
	t => ({
		id: t.uuid("id").primaryKey().defaultRandom(),
		jobId: t
			.uuid("job_id")
			.notNull()
			.references(() => ingestionJobs.id, { onDelete: "cascade" }),
		documentId: t
			.uuid("document_id")
			.notNull()
			.references(() => documents.id, { onDelete: "cascade" }),
		status: t
			.text("status")
			.notNull()
			.default("pending")
			.$type<"pending" | "indexed" | "failed">(),
		chunkCount: t.integer("chunk_count"),
		error: t.text("error"),
		updatedAt: t.timestamp("updated_at", { withTimezone: true }).notNull().defaultNow(),
	}),
	t => [index("job_documents_job_id_idx").on(t.jobId)]
)

// ============================================================================
// CONVERSATIONS + MESSAGES (generation — Flow B, GN-1)
// ============================================================================

/**
 * One End User chat session (DOMAIN-MODEL `conversations`). R1 is single-turn:
 * every /chat request creates a NEW conversation (Core Flows, Flow B). Schema
 * is OWNED by the generation epic — `generation` owns writes; `reporting` and
 * `observability` read only (cross-epic rule). Rows are written by the Python
 * RAG service (no Python migrations — Drizzle owns DDL).
 */
export const conversations = createTable("conversations", t => ({
	id: t.uuid("id").primaryKey().defaultRandom(),
	userId: t
		.text("user_id")
		.notNull()
		.references(() => users.id, { onDelete: "cascade" }),
	createdAt: t.timestamp("created_at", { withTimezone: true }).notNull().defaultNow(),
}))

/**
 * One turn in a conversation (DOMAIN-MODEL `messages`). The assistant row
 * carries the grounded-answer envelope: `citations` (resolved chunk ->
 * document source/section), `retrievedChunks` (the RT-1 retrieved record for
 * the turn — distinct from GN-2's "used in prompt"), `tokenUsage`, `traceId`,
 * and `modelUsed`. `turnIndex` is the stable ordering key within a
 * conversation (created_at alone can tie).
 */
export const messages = createTable(
	"messages",
	t => ({
		id: t.uuid("id").primaryKey().defaultRandom(),
		conversationId: t
			.uuid("conversation_id")
			.notNull()
			.references(() => conversations.id, { onDelete: "cascade" }),
		role: t
			.text("role")
			.notNull()
			.$type<"user" | "assistant">(),
		content: t.text("content").notNull(),
		citations: t.jsonb("citations").$type<unknown>(),
		retrievedChunks: t.jsonb("retrieved_chunks").$type<unknown>(),
		// GN-2: the chunks actually USED in the prompt (rank-then-select under
		// the hard token budget) — recorded separately from retrievedChunks so
		// retrieval failures are diagnosable independent of generation.
		usedChunks: t.jsonb("used_chunks").$type<unknown>(),
		tokenUsage: t.jsonb("token_usage").$type<unknown>(),
		traceId: t.text("trace_id"),
		modelUsed: t.text("model_used"),
		turnIndex: t.integer("turn_index").notNull(),
		createdAt: t.timestamp("created_at", { withTimezone: true }).notNull().defaultNow(),
	}),
	t => [index("messages_conversation_id_idx").on(t.conversationId)]
)

/**
 * Embedding cache (IN-6, Flow A cache branch — PRD decision: Postgres, not
 * Redis). Keyed on (content_hash, model): identical content is never
 * re-embedded, and IN-8's new-model pass is safe by construction (a new
 * model key is a clean namespace). Written/read by the Python RAG service.
 */
export const embeddingCache = createTable(
	"embedding_cache",
	t => ({
		contentHash: t.text("content_hash").notNull(),
		model: t.text("model").notNull(),
		embedding: vector1536("embedding").notNull(),
		createdAt: t.timestamp("created_at", { withTimezone: true }).notNull().defaultNow(),
	}),
	t => [primaryKey({ columns: [t.contentHash, t.model] })]
)

/**
 * Chunk-context cache (CE-1, Flow G — mirrors `embedding_cache`). Keyed on
 * (chunk content_hash, context model, prompt version): identical content is
 * never re-described by the LLM, and a prompt/model change is a clean
 * namespace by construction. Written/read by the Python RAG service.
 */
export const contextCache = createTable(
	"context_cache",
	t => ({
		chunkHash: t.text("chunk_hash").notNull(),
		model: t.text("model").notNull(),
		promptVersion: t.text("prompt_version").notNull(),
		context: t.text("context").notNull(),
		createdAt: t.timestamp("created_at", { withTimezone: true }).notNull().defaultNow(),
	}),
	t => [primaryKey({ columns: [t.chunkHash, t.model, t.promptVersion] })]
)

// ============================================================================
// GOLDEN EVALUATION SET (evaluation — EV-1)
// ============================================================================

/**
 * One golden evaluation item (DOMAIN-MODEL `golden_items`, §5.7): a curated
 * question, the chunk(s) that SHOULD be retrieved, and the ideal answer.
 * Everything downstream (EV-2/3 metrics, EV-4 gate, IN-8 verification) reads
 * this. OWNED by the evaluation epic.
 *
 * Expected chunks bind by chunk CONTENT HASH (chunks.content_hash), which is
 * stable across re-indexing of unchanged content. A re-chunking that orphans
 * a hash makes the item STALE (validated on read, never silently pass/fail).
 * Retirement is a status flip, not a delete — the row remains for audit.
 */
export const goldenItems = createTable(
	"golden_items",
	t => ({
		id: t.uuid("id").primaryKey().defaultRandom(),
		question: t.text("question").notNull(),
		idealAnswer: t.text("ideal_answer").notNull(),
		// chunks.content_hash values expected in retrieval results.
		expectedChunkHashes: t.jsonb("expected_chunk_hashes").$type<string[]>().notNull(),
		tags: t.jsonb("tags").$type<string[]>().notNull().default([]),
		status: t.text("status", { enum: ["active", "retired"] }).notNull().default("active"),
		createdBy: t
			.text("created_by")
			.notNull()
			.references(() => users.id, { onDelete: "restrict" }),
		createdAt: t.timestamp("created_at", { withTimezone: true }).notNull().defaultNow(),
		updatedAt: t.timestamp("updated_at", { withTimezone: true }).notNull().defaultNow(),
	}),
	t => [index("golden_items_status_idx").on(t.status)]
)

/**
 * One evaluation run (DOMAIN-MODEL `eval_runs`, EV-2). Written by the Python
 * RAG service (the runner executes against the REAL retrieval pipeline —
 * Flow B stages, not a reimplementation); read by the gateway for the runs
 * console, comparisons, the EV-4 gate, and RP-3 trending.
 *
 * `config` snapshots exactly what was evaluated (k, weights, toggles) and
 * `gitSha` pins the code version. `metrics` holds the aggregates; `perItem`
 * the per-golden-item scores (stale items are SKIPPED and counted, never
 * silently perfect).
 */
export const evalRuns = createTable(
	"eval_runs",
	t => ({
		id: t.uuid("id").primaryKey().defaultRandom(),
		kind: t.text("kind", { enum: ["retrieval", "generation"] }).notNull(),
		status: t.text("status", { enum: ["running", "completed", "failed"] }).notNull(),
		gitSha: t.text("git_sha").notNull(),
		config: t.jsonb("config").$type<Record<string, unknown>>().notNull(),
		metrics: t.jsonb("metrics").$type<Record<string, number>>(),
		perItem: t.jsonb("per_item").$type<unknown>(),
		error: t.text("error"),
		triggeredBy: t.text("triggered_by").notNull(),
		createdAt: t.timestamp("created_at", { withTimezone: true }).notNull().defaultNow(),
		finishedAt: t.timestamp("finished_at", { withTimezone: true }),
	}),
	t => [index("eval_runs_created_at_idx").on(t.createdAt)]
)

/**
 * Approved eval baselines (EV-4, Flow F). The CURRENT baseline is the newest
 * row; the gate compares candidate runs against it and the baseline ONLY
 * ever changes through an explicit, audited promotion by a holder of
 * `eval.gate.approve` — never implicitly.
 */
export const evalBaselines = createTable(
	"eval_baselines",
	t => ({
		id: t.uuid("id").primaryKey().defaultRandom(),
		runId: t
			.uuid("run_id")
			.notNull()
			.references(() => evalRuns.id, { onDelete: "restrict" }),
		promotedBy: t.text("promoted_by").notNull(),
		note: t.text("note"),
		createdAt: t.timestamp("created_at", { withTimezone: true }).notNull().defaultNow(),
	}),
	t => [index("eval_baselines_created_at_idx").on(t.createdAt)]
)

/**
 * Re-embed migration state machine (IN-8, Flow D). One non-terminal row at a
 * time; every transition is audited. Statuses: shadowing -> verifying ->
 * ready -> cutover | aborted | failed. The pinned-model invariant (index
 * model == query model) is preserved by the Redis active-column pointer.
 */
export const reembedMigrations = createTable(
	"reembed_migrations",
	t => ({
		id: t.uuid("id").primaryKey().defaultRandom(),
		targetModel: t.text("target_model").notNull(),
		status: t
			.text("status", {
				enum: ["shadowing", "verifying", "ready", "cutover", "aborted", "failed"],
			})
			.notNull()
			.default("shadowing"),
		/** EV-2 verification metrics on the shadow index. */
		metrics: t.jsonb("metrics").$type<Record<string, number>>(),
		error: t.text("error"),
		triggeredBy: t.text("triggered_by").notNull(),
		createdAt: t.timestamp("created_at", { withTimezone: true }).notNull().defaultNow(),
		updatedAt: t.timestamp("updated_at", { withTimezone: true }).notNull().defaultNow(),
	}),
	t => [index("reembed_migrations_created_at_idx").on(t.createdAt)]
)

// ============================================================================
// RELATIONS
// ============================================================================

/**
 * RP-6: scheduled report generation. Schedules are managed through the
 * backend console (reports.schedules.manage, audited); the arq worker polls
 * for due schedules every minute and generates the files.
 */
export const reportSchedules = createTable(
	"report_schedules",
	t => ({
		id: t.uuid("id").primaryKey().defaultRandom(),
		reportKey: t.text("report_key").notNull(),
		/** Standard 5-field cron expression. */
		cron: t.text("cron").notNull(),
		filters: t.jsonb("filters").$type<Record<string, string>>().notNull().default({}),
		/** Any of json | csv | xlsx | pdf. */
		formats: t.jsonb("formats").$type<string[]>().notNull(),
		/** Email recipients (delivery when SMTP is configured); files are
		 * always stored in report_files regardless. */
		recipients: t.jsonb("recipients").$type<string[]>().notNull().default([]),
		enabled: t.boolean("enabled").notNull().default(true),
		createdBy: t.text("created_by").notNull(),
		createdAt: t.timestamp("created_at", { withTimezone: true }).notNull().defaultNow(),
		updatedAt: t.timestamp("updated_at", { withTimezone: true }).notNull().defaultNow(),
		lastRunAt: t.timestamp("last_run_at", { withTimezone: true }),
	}),
	t => [index("report_schedules_enabled_idx").on(t.enabled)]
)

/** RP-6: generated report artifacts (scheduled or retained on-demand). */
export const reportFiles = createTable(
	"report_files",
	t => ({
		id: t.uuid("id").primaryKey().defaultRandom(),
		scheduleId: t.uuid("schedule_id").references(() => reportSchedules.id, {
			onDelete: "set null",
		}),
		reportKey: t.text("report_key").notNull(),
		format: t.text("format").notNull(),
		filename: t.text("filename").notNull(),
		contentType: t.text("content_type").notNull(),
		/** base64 for binary formats (xlsx/pdf), utf-8 passthrough for json/csv. */
		contentBase64: t.text("content_base64").notNull(),
		generatedBy: t.text("generated_by").notNull(),
		createdAt: t.timestamp("created_at", { withTimezone: true }).notNull().defaultNow(),
	}),
	t => [index("report_files_created_at_idx").on(t.createdAt)]
)

export const relations = defineRelations(
	{
		users,
		sessions,
		accounts,
		todos,
		tickets,
		idempotencyKeys,
		roles,
		permissions,
		userRoles,
		rolePermissions,
	},
	r => ({
		users: {
			sessions: r.many.sessions(),
			accounts: r.many.accounts(),
			userRoles: r.many.userRoles(),
		},
		sessions: {
			user: r.one.users({
				from: r.sessions.userId,
				to: r.users.id,
			}),
		},
		accounts: {
			user: r.one.users({
				from: r.accounts.userId,
				to: r.users.id,
			}),
		},
		todos: {
			author: r.one.users({
				from: r.todos.authorId,
				to: r.users.id,
			}),
		},
		tickets: {
			author: r.one.users({
				from: r.tickets.authorId,
				to: r.users.id,
			}),
		},
		roles: {
			userRoles: r.many.userRoles(),
			rolePermissions: r.many.rolePermissions(),
		},
		permissions: {
			rolePermissions: r.many.rolePermissions(),
		},
		userRoles: {
			user: r.one.users({
				from: r.userRoles.userId,
				to: r.users.id,
			}),
			role: r.one.roles({
				from: r.userRoles.roleId,
				to: r.roles.id,
			}),
		},
		rolePermissions: {
			role: r.one.roles({
				from: r.rolePermissions.roleId,
				to: r.roles.id,
			}),
			permission: r.one.permissions({
				from: r.rolePermissions.permissionId,
				to: r.permissions.id,
			}),
		},
	})
)

// ============================================================================
// SCHEMA
// ============================================================================
export const schema = Object.assign(
	{
		users,
		sessions,
		accounts,
		verifications,
		todos,
		tickets,
		idempotencyKeys,
		roles,
		permissions,
		userRoles,
		rolePermissions,
		auditLog,
		apiKeys,
		documents,
		chunks,
		ingestionJobs,
		jobDocuments,
		conversations,
		messages,
		goldenItems,
		evalRuns,
		evalBaselines,
		embeddingCache,
		contextCache,
		reembedMigrations,
		reportSchedules,
		reportFiles,
	},
	relations
)
