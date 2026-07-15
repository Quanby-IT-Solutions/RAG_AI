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
