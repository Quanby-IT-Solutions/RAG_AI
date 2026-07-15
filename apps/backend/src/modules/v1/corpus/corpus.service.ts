import { Injectable } from "@nestjs/common"
import { ORPCError } from "@orpc/server"
import { asc, desc, eq, sql } from "drizzle-orm"

import type {
	CorpusDocumentMetadata,
	IngestionJobStats,
	InspectedChunk,
} from "@repo/contracts"
import { chunks, documents, ingestionJobs, jobDocuments } from "@repo/db/schema"

import { db } from "@/common/database/database.client"

@Injectable()
export class CorpusService {
	/**
	 * Corpus list (Flow A step 4): newest first. The `text` payload the Python
	 * service stores inside `metadata` is stripped — the list screen only needs
	 * filename/type/status/hash/timestamp/error, not megabytes of normalized
	 * text per row.
	 */
	async listDocuments() {
		const rows = await db.select().from(documents).orderBy(desc(documents.ingestedAt))

		return rows.map(row => {
			const metadata =
				row.metadata && typeof row.metadata === "object"
					? (Object.fromEntries(
							Object.entries(row.metadata as Record<string, unknown>).filter(
								([key]) => key !== "text" && key !== "pages"
							)
						) as CorpusDocumentMetadata)
					: null

			return { ...row, metadata }
		})
	}

	/**
	 * Jobs console list (IN-7): newest first with per-job document counts.
	 * Read-only over the ingestion-owned tables (the Python service + worker
	 * own all writes).
	 */
	async listJobs() {
		const rows = await db
			.select({
				id: ingestionJobs.id,
				kind: ingestionJobs.kind,
				status: ingestionJobs.status,
				triggeredBy: ingestionJobs.triggeredBy,
				stats: ingestionJobs.stats,
				error: ingestionJobs.error,
				createdAt: ingestionJobs.createdAt,
				startedAt: ingestionJobs.startedAt,
				finishedAt: ingestionJobs.finishedAt,
				documentCount: sql<number>`count(${jobDocuments.id})::int`,
				failedDocumentCount: sql<number>`count(${jobDocuments.id}) filter (where ${jobDocuments.status} = 'failed')::int`,
			})
			.from(ingestionJobs)
			.leftJoin(jobDocuments, eq(jobDocuments.jobId, ingestionJobs.id))
			.groupBy(ingestionJobs.id)
			.orderBy(desc(ingestionJobs.createdAt))
			.limit(50)

		return rows.map(row => ({ ...row, stats: (row.stats as IngestionJobStats | null) ?? null }))
	}

	/** Job detail (IN-7): lifecycle row + per-document outcomes. */
	async getJob(id: string) {
		const [job] = await db.select().from(ingestionJobs).where(eq(ingestionJobs.id, id))
		if (!job) {
			throw new ORPCError("NOT_FOUND", { message: `Ingestion job ${id} not found` })
		}

		const documentRows = await db
			.select({
				documentId: jobDocuments.documentId,
				sourceUri: documents.sourceUri,
				docType: documents.docType,
				status: jobDocuments.status,
				chunkCount: jobDocuments.chunkCount,
				error: jobDocuments.error,
				updatedAt: jobDocuments.updatedAt,
			})
			.from(jobDocuments)
			.innerJoin(documents, eq(documents.id, jobDocuments.documentId))
			.where(eq(jobDocuments.jobId, id))
			.orderBy(jobDocuments.updatedAt)

		return {
			...job,
			stats: (job.stats as IngestionJobStats | null) ?? null,
			documentCount: documentRows.length,
			failedDocumentCount: documentRows.filter(row => row.status === "failed").length,
			documents: documentRows,
		}
	}

	/**
	 * Chunk-boundary inspection (IN-5): a document's chunks in boundary order
	 * with mid-sentence heuristics and overlap regions, so bad splits are
	 * catchable before they poison retrieval. Read-only over `chunks`;
	 * RT-6 parent rows (metadata.role === 'parent') are excluded — the
	 * inspection view shows the text as it was actually split.
	 */
	async inspectChunks(id: string) {
		const [document] = await db.select().from(documents).where(eq(documents.id, id))
		if (!document) {
			throw new ORPCError("NOT_FOUND", { message: `Document ${id} not found` })
		}

		const rows = await db
			.select({
				id: chunks.id,
				chunkIndex: chunks.chunkIndex,
				content: chunks.content,
				contentHash: chunks.contentHash,
				metadata: chunks.metadata,
			})
			.from(chunks)
			.where(eq(chunks.documentId, id))
			.orderBy(asc(chunks.chunkIndex))

		const children = rows.filter(row => {
			const metadata = row.metadata as Record<string, unknown> | null
			return metadata?.["role"] !== "parent"
		})

		const inspected: InspectedChunk[] = children.map((row, index) => {
			const previous = index > 0 ? children[index - 1] : undefined
			return {
				id: row.id,
				chunkIndex: row.chunkIndex,
				content: row.content,
				contentHash: row.contentHash,
				charCount: row.content.length,
				metadata: (row.metadata as Record<string, unknown> | null) ?? null,
				endsMidSentence: endsMidSentence(row.content),
				startsMidSentence: startsMidSentence(row.content),
				overlapWithPrevious: previous ? overlapLength(previous.content, row.content) : 0,
			}
		})

		const documentMetadata =
			document.metadata && typeof document.metadata === "object"
				? (document.metadata as Record<string, unknown>)
				: null
		const strippedMetadata = documentMetadata
			? (Object.fromEntries(
					Object.entries(documentMetadata).filter(
						([key]) => key !== "text" && key !== "pages"
					)
				) as CorpusDocumentMetadata)
			: null

		return {
			document: { ...document, metadata: strippedMetadata },
			chunking: (documentMetadata?.["chunking"] as Record<string, unknown> | null) ?? null,
			chunks: inspected,
		}
	}
}

/** Heuristic: text does not end at sentence-ending punctuation. */
function endsMidSentence(content: string): boolean {
	const trimmed = content.trimEnd()
	if (!trimmed) return false
	return !/[.!?:;"'”’)\]]$/.test(trimmed)
}

/** Heuristic: text starts lowercase or with a continuation character. */
function startsMidSentence(content: string): boolean {
	const trimmed = content.trimStart()
	if (!trimmed) return false
	const first = trimmed[0] ?? ""
	return first === first.toLowerCase() && /[a-z]/.test(first)
}

/** Longest suffix of `previous` that prefixes `current` (overlap region). */
function overlapLength(previous: string, current: string): number {
	const max = Math.min(previous.length, current.length, 500)
	for (let length = max; length > 0; length--) {
		if (previous.endsWith(current.slice(0, length))) {
			return length
		}
	}
	return 0
}
