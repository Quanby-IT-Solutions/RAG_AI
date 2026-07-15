import { z } from "zod"

// ============================================================================
// SCHEMAS
// ============================================================================

/**
 * R1 document state machine (DOMAIN-MODEL "Document lifecycle"):
 * registered -> processing -> indexed | failed. IN-1 sets `processing` on a
 * successful upload+normalize; `indexed` arrives with IN-2.
 */
export const DocumentStatusSchema = z.enum(["registered", "processing", "indexed", "failed"])

/**
 * Normalization metadata persisted alongside a document (jsonb). Carries the
 * original filename, ingest timestamp, and page/section info captured by the
 * loader. Kept loose: the Python service owns the exact shape.
 */
export const DocumentMetadataSchema = z
	.object({
		originalFilename: z.string().optional(),
		ingestedAt: z.string().optional(),
		pageCount: z.number().int().nonnegative().optional(),
	})
	.catchall(z.unknown())

export const DocumentSchema = z.object({
	id: z.string().uuid(),
	sourceUri: z.string(),
	docType: z.string(),
	contentHash: z.string(),
	status: DocumentStatusSchema,
	metadata: DocumentMetadataSchema.nullable(),
	error: z.string().nullable(),
	ingestedAt: z
		.union([z.date(), z.string()])
		.transform(val => (typeof val === "string" ? new Date(val) : val)),
})

export type DocumentStatus = z.infer<typeof DocumentStatusSchema>
export type CorpusDocument = z.infer<typeof DocumentSchema>
export type CorpusDocumentMetadata = z.infer<typeof DocumentMetadataSchema>
