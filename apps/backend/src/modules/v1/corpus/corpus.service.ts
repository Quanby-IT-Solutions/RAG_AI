import { Injectable } from "@nestjs/common"
import { ORPCError } from "@orpc/server"
import { desc, eq, sql } from "drizzle-orm"

import type { CorpusDocumentMetadata, IngestionJobStats } from "@repo/contracts"
import { documents, ingestionJobs, jobDocuments } from "@repo/db/schema"

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
}
