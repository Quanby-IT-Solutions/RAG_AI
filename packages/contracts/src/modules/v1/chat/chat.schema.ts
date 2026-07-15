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

export const ChatQueryInputSchema = z.object({
	query: z.string().trim().min(1).max(4000),
	// Per-request top-k override; the RAG service default (RETRIEVAL_TOP_K) applies when omitted.
	k: z.number().int().min(1).max(50).optional(),
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
})

export type ChatCitation = z.infer<typeof ChatCitationSchema>
export type ChatTokenUsage = z.infer<typeof ChatTokenUsageSchema>
export type ChatQueryInput = z.infer<typeof ChatQueryInputSchema>
export type ChatAnswer = z.infer<typeof ChatAnswerSchema>
