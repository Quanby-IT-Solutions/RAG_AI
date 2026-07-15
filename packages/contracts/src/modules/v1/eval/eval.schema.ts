import { z } from "zod"

/**
 * Golden evaluation set (EV-1, §5.7). Expected chunks bind by chunk CONTENT
 * HASH (stable across re-indexing); a re-chunking that orphans a hash flags
 * the item STALE rather than silently passing/failing downstream metrics.
 */

const isoDate = z
	.union([z.date(), z.string()])
	.transform(val => (typeof val === "string" ? new Date(val) : val))

export const GoldenItemStatusSchema = z.enum(["active", "retired"])

export const GoldenItemSchema = z.object({
	id: z.string().uuid(),
	question: z.string(),
	idealAnswer: z.string(),
	/** chunks.content_hash values expected in retrieval results. */
	expectedChunkHashes: z.array(z.string()),
	tags: z.array(z.string()),
	status: GoldenItemStatusSchema,
	/** Hashes no longer present in `chunks` (orphaned by re-chunking). */
	staleHashes: z.array(z.string()),
	createdBy: z.string(),
	createdAt: isoDate,
	updatedAt: isoDate,
})

const GoldenItemFieldsSchema = z.object({
	question: z.string().trim().min(1),
	idealAnswer: z.string().trim().min(1),
	expectedChunkHashes: z.array(z.string().min(1)).min(1),
	tags: z.array(z.string().trim().min(1)).default([]),
})

export const CreateGoldenItemRequestSchema = GoldenItemFieldsSchema

export const UpdateGoldenItemRequestSchema = z.object({
	id: z.string().uuid(),
	...GoldenItemFieldsSchema.partial().shape,
	status: GoldenItemStatusSchema.optional(),
})

/** Portable golden-set document (§XI portability). */
export const GoldenSetExportSchema = z.object({
	version: z.literal(1),
	items: z.array(
		GoldenItemFieldsSchema.extend({
			status: GoldenItemStatusSchema.default("active"),
		})
	),
})

export const ImportGoldenSetResponseSchema = z.object({
	imported: z.number().int(),
})

/** Live-retrieval assist for picking expected chunks (EV-1 AC). */
export const GoldenChunkSearchRequestSchema = z.object({
	query: z.string().trim().min(1),
	k: z.number().int().min(1).max(50).default(10),
})

export const GoldenChunkCandidateSchema = z.object({
	contentHash: z.string(),
	documentId: z.string(),
	content: z.string(),
	score: z.number().nullable(),
})

export type GoldenItemStatus = z.infer<typeof GoldenItemStatusSchema>
export type GoldenItem = z.infer<typeof GoldenItemSchema>
export type CreateGoldenItemRequest = z.infer<typeof CreateGoldenItemRequestSchema>
export type UpdateGoldenItemRequest = z.infer<typeof UpdateGoldenItemRequestSchema>
export type GoldenSetExport = z.infer<typeof GoldenSetExportSchema>
export type GoldenChunkSearchRequest = z.infer<typeof GoldenChunkSearchRequestSchema>
export type GoldenChunkCandidate = z.infer<typeof GoldenChunkCandidateSchema>

// ---------------------------------------------------------------- EV-2: runs

export const EvalRunStatusSchema = z.enum(["running", "completed", "failed"])
export const EvalRunKindSchema = z.enum(["retrieval", "generation"])

/** Per-golden-item score entry; stale items appear with skipped=true. */
export const EvalRunItemSchema = z.object({
	goldenItemId: z.string(),
	question: z.string(),
	skipped: z.boolean(),
	staleHashes: z.array(z.string()).optional(),
	retrievedHashes: z.array(z.string()).optional(),
	hit: z.number().optional(),
	reciprocalRank: z.number().optional(),
	precisionAtK: z.number().optional(),
	recallAtK: z.number().optional(),
	// EV-3 generation metrics (null = unscorable, never silently perfect).
	answer: z.string().optional(),
	faithfulness: z.number().nullable().optional(),
	answerRelevancy: z.number().nullable().optional(),
})

export const EvalRunSchema = z.object({
	id: z.string().uuid(),
	kind: EvalRunKindSchema,
	status: EvalRunStatusSchema,
	gitSha: z.string(),
	/** Exact snapshot of what was evaluated (k, weights, toggles). */
	config: z.record(z.string(), z.unknown()),
	metrics: z.record(z.string(), z.number()).nullable(),
	perItem: z.array(EvalRunItemSchema).nullable(),
	error: z.string().nullable(),
	triggeredBy: z.string(),
	createdAt: isoDate,
	finishedAt: isoDate.nullable(),
})

export const EvalRunSummarySchema = EvalRunSchema.omit({ perItem: true })

export const ExecuteEvalRunRequestSchema = z.object({
	k: z.number().int().min(1).max(50).optional(),
	vectorWeight: z.number().min(0).optional(),
	keywordWeight: z.number().min(0).optional(),
	/** RT-3 A/B: omit = configured default; true requires a configured reranker. */
	rerank: z.boolean().optional(),
	/** EV-3: also generate answers and score faithfulness/answer relevancy. */
	generation: z.boolean().optional(),
})

/** Per-metric delta between two runs (candidate − base). */
export const EvalRunComparisonSchema = z.object({
	base: EvalRunSummarySchema,
	candidate: EvalRunSummarySchema,
	deltas: z.record(z.string(), z.number()),
})

export type EvalRunStatus = z.infer<typeof EvalRunStatusSchema>
export type EvalRunKind = z.infer<typeof EvalRunKindSchema>
export type EvalRunItem = z.infer<typeof EvalRunItemSchema>
export type EvalRun = z.infer<typeof EvalRunSchema>
export type EvalRunSummary = z.infer<typeof EvalRunSummarySchema>
export type ExecuteEvalRunRequest = z.infer<typeof ExecuteEvalRunRequestSchema>
export type EvalRunComparison = z.infer<typeof EvalRunComparisonSchema>
