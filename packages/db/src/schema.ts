import { defineRelations, sql } from "drizzle-orm"
import { customType, index, primaryKey } from "drizzle-orm/pg-core"

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
	tsv: tsvector("tsv").generatedAlwaysAs(sql`to_tsvector('english', "content")`),
	// Page/section carried over from the document normalization payload.
	metadata: t.jsonb("metadata").$type<unknown>(),
	contentHash: t.text("content_hash").notNull().unique(),
	chunkIndex: t.integer("chunk_index").notNull(),
	embeddingModel: t.text("embedding_model").notNull(),
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
		tokenUsage: t.jsonb("token_usage").$type<unknown>(),
		traceId: t.text("trace_id"),
		modelUsed: t.text("model_used"),
		turnIndex: t.integer("turn_index").notNull(),
		createdAt: t.timestamp("created_at", { withTimezone: true }).notNull().defaultNow(),
	}),
	t => [index("messages_conversation_id_idx").on(t.conversationId)]
)

// ============================================================================
// RELATIONS
// ============================================================================
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
		documents,
		chunks,
		ingestionJobs,
		jobDocuments,
		conversations,
		messages,
	},
	relations
)
