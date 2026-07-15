import { z } from "zod"

// ============================================================================
// SCHEMAS
// ============================================================================

/**
 * One resolved citation on a grounded answer (GN-1): a cited chunk resolved to
 * its document source (documents.source_uri) and the page/section carried in
 * the chunk metadata. `source` is null only if the document row vanished
 * between retrieval and resolution.
 */
export const ChatCitationSchema = z.object({
	chunkId: z.string().uuid(),
	documentId: z.string().uuid(),
	source: z.string().nullable(),
	page: z.number().int().nullable().optional(),
	section: z.string().nullable().optional(),
})

/** Provider token usage for the turn; null when the model was not called
 * (empty retrieval -> "I don't know") or the provider degraded. */
export const ChatTokenUsageSchema = z
	.object({
		promptTokens: z.number().int().nonnegative(),
		completionTokens: z.number().int().nonnegative(),
		totalTokens: z.number().int().nonnegative(),
	})
	.catchall(z.unknown())

/** RT-4: restrict retrieval to a corpus subset (applied in SQL, both arms). */
export const RetrievalFiltersSchema = z.object({
	docTypes: z.array(z.enum(["pdf", "html", "docx", "csv"])).min(1).optional(),
	/** Case-insensitive substring of the document source URI. */
	source: z.string().trim().min(1).max(500).optional(),
	/** documents.ingested_at range (ISO timestamps). */
	dateFrom: z.string().datetime({ offset: true }).optional(),
	dateTo: z.string().datetime({ offset: true }).optional(),
	/** Case-insensitive substring of the chunk section label. */
	section: z.string().trim().min(1).max(200).optional(),
})

export const ChatQueryInputSchema = z.object({
	query: z.string().trim().min(1).max(4000),
	// Per-request top-k override; the RAG service default (RETRIEVAL_TOP_K) applies when omitted.
	k: z.number().int().min(1).max(50).optional(),
	// CH-3: continue an existing conversation the caller owns; omitted -> new.
	conversationId: z.string().uuid().optional(),
	// RT-4: metadata filter — a filter matching nothing yields a grounded
	// "I don't know", never an unfiltered fallback.
	filters: RetrievalFiltersSchema.optional(),
})

/**
 * Grounded chat answer (Flow B steps 5-7). `isIDontKnow` marks the grounded
 * refusal (alternate B1); `degraded` marks the bounded provider-failure
 * message (alternate B2) — both carry NO citations. R1 is single-turn: every
 * request creates a new conversation.
 */
export const ChatAnswerSchema = z.object({
	answer: z.string(),
	citations: z.array(ChatCitationSchema),
	conversationId: z.string().uuid(),
	messageId: z.string().uuid(),
	traceId: z.string(),
	model: z.string(),
	tokenUsage: ChatTokenUsageSchema.nullable(),
	isIDontKnow: z.boolean(),
	degraded: z.boolean(),
	/** CP-1: answer served from the response cache (no LLM call). */
	cached: z.boolean().optional(),
})

// ── Conversation history (CH-3) ─────────────────────────────────────────────

/** One row in the caller's history list (own conversations only). */
export const ConversationSummarySchema = z.object({
	id: z.string().uuid(),
	// Derived from the first user message (no title column) — display only.
	title: z.string(),
	messageCount: z.number().int().nonnegative(),
	createdAt: z
		.union([z.date(), z.string()])
		.transform(val => (typeof val === "string" ? new Date(val) : val)),
	lastMessageAt: z
		.union([z.date(), z.string()])
		.nullable()
		.transform(val => (typeof val === "string" ? new Date(val) : val)),
})

export const ConversationMessageSchema = z.object({
	id: z.string().uuid(),
	role: z.enum(["user", "assistant"]),
	content: z.string(),
	citations: z.array(ChatCitationSchema).nullable(),
	turnIndex: z.number().int().nonnegative(),
	createdAt: z
		.union([z.date(), z.string()])
		.transform(val => (typeof val === "string" ? new Date(val) : val)),
})

export const ConversationDetailSchema = z.object({
	id: z.string().uuid(),
	createdAt: z
		.union([z.date(), z.string()])
		.transform(val => (typeof val === "string" ? new Date(val) : val)),
	messages: z.array(ConversationMessageSchema),
})

export type ChatCitation = z.infer<typeof ChatCitationSchema>
export type ChatTokenUsage = z.infer<typeof ChatTokenUsageSchema>
export type RetrievalFilters = z.infer<typeof RetrievalFiltersSchema>
export type ChatQueryInput = z.infer<typeof ChatQueryInputSchema>
export type ChatAnswer = z.infer<typeof ChatAnswerSchema>
export type ConversationSummary = z.infer<typeof ConversationSummarySchema>
export type ConversationMessage = z.infer<typeof ConversationMessageSchema>
export type ConversationDetail = z.infer<typeof ConversationDetailSchema>
