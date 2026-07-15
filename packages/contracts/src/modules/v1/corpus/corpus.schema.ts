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

/** IN-7 job lifecycle: queued -> running -> completed | failed. */
export const IngestionJobStatusSchema = z.enum(["queued", "running", "completed", "failed"])

/** Per-job counters the console shows (cacheHits populated once IN-6 lands). */
export const IngestionJobStatsSchema = z
	.object({
		documents: z.number().int().nonnegative().optional(),
		chunks: z.number().int().nonnegative().optional(),
		embedBatches: z.number().int().nonnegative().optional(),
		cacheHits: z.number().int().nonnegative().optional(),
		cacheMisses: z.number().int().nonnegative().optional(),
		failures: z
			.array(
				z.object({ documentId: z.string(), error: z.string().nullable() }).catchall(z.unknown())
			)
			.optional(),
	})
	.catchall(z.unknown())

export const IngestionJobSchema = z.object({
	id: z.string().uuid(),
	kind: z.enum(["ingest", "reindex"]),
	status: IngestionJobStatusSchema,
	triggeredBy: z.string().nullable(),
	stats: IngestionJobStatsSchema.nullable(),
	error: z.string().nullable(),
	documentCount: z.number().int().nonnegative(),
	failedDocumentCount: z.number().int().nonnegative(),
	createdAt: z
		.union([z.date(), z.string()])
		.transform(val => (typeof val === "string" ? new Date(val) : val)),
	startedAt: z
		.union([z.date(), z.string()])
		.nullable()
		.transform(val => (typeof val === "string" ? new Date(val) : val)),
	finishedAt: z
		.union([z.date(), z.string()])
		.nullable()
		.transform(val => (typeof val === "string" ? new Date(val) : val)),
})

export const JobDocumentSchema = z.object({
	documentId: z.string().uuid(),
	sourceUri: z.string(),
	docType: z.string(),
	status: z.enum(["pending", "indexed", "failed"]),
	chunkCount: z.number().int().nonnegative().nullable(),
	error: z.string().nullable(),
	updatedAt: z
		.union([z.date(), z.string()])
		.transform(val => (typeof val === "string" ? new Date(val) : val)),
})

export const IngestionJobDetailSchema = IngestionJobSchema.extend({
	documents: z.array(JobDocumentSchema),
})

export type DocumentStatus = z.infer<typeof DocumentStatusSchema>
export type CorpusDocument = z.infer<typeof DocumentSchema>
export type CorpusDocumentMetadata = z.infer<typeof DocumentMetadataSchema>
export type IngestionJobStatus = z.infer<typeof IngestionJobStatusSchema>
export type IngestionJobStats = z.infer<typeof IngestionJobStatsSchema>
export type IngestionJob = z.infer<typeof IngestionJobSchema>
export type IngestionJobDetail = z.infer<typeof IngestionJobDetailSchema>
export type JobDocument = z.infer<typeof JobDocumentSchema>

// ------------------------------------------------- IN-5: chunk inspection

/** One chunk in boundary-inspection order (parents excluded). */
export const InspectedChunkSchema = z.object({
	id: z.string().uuid(),
	chunkIndex: z.number().int(),
	content: z.string(),
	contentHash: z.string(),
	charCount: z.number().int(),
	/** Chunk-level metadata (page/section) from normalization. */
	metadata: z.record(z.string(), z.unknown()).nullable(),
	/** Heuristic: chunk does not END at sentence-ending punctuation. */
	endsMidSentence: z.boolean(),
	/** Heuristic: chunk does not START at a sentence beginning. */
	startsMidSentence: z.boolean(),
	/** Characters shared verbatim with the END of the previous chunk. */
	overlapWithPrevious: z.number().int(),
})

export const ChunkInspectionSchema = z.object({
	document: DocumentSchema,
	/** Chunking strategy/fallback info recorded on the document (IN-4). */
	chunking: z.record(z.string(), z.unknown()).nullable(),
	chunks: z.array(InspectedChunkSchema),
})

export type InspectedChunk = z.infer<typeof InspectedChunkSchema>
export type ChunkInspection = z.infer<typeof ChunkInspectionSchema>
