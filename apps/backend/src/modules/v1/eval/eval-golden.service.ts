import { Injectable } from "@nestjs/common"
import { ORPCError } from "@orpc/server"
import { desc, eq, inArray } from "drizzle-orm"

import type {
	CreateGoldenItemRequest,
	GoldenItem,
	GoldenSetExport,
	UpdateGoldenItemRequest,
} from "@repo/contracts"
import { chunks, goldenItems } from "@repo/db/schema"

import { AuditService } from "@/common/audit/audit.service"
import { db } from "@/common/database/database.client"

type GoldenRow = typeof goldenItems.$inferSelect

/**
 * Golden evaluation set management (EV-1, §5.7). Expected chunks bind by
 * chunk CONTENT HASH — stable across re-indexing of unchanged content. Every
 * read validates staleness: expected hashes no longer present in `chunks`
 * (orphaned by re-chunking) are reported per item rather than letting
 * downstream metrics silently pass/fail. Retirement is a status flip; rows
 * are never deleted (audit trail). Every mutation is audited (Flow E).
 */
@Injectable()
export class EvalGoldenService {
	constructor(private readonly auditService: AuditService) {}

	/** Which of the given hashes still exist in `chunks`. */
	private async resolveLiveHashes(hashes: string[]): Promise<Set<string>> {
		const unique = [...new Set(hashes)]
		if (unique.length === 0) {
			return new Set()
		}
		const rows = await db
			.select({ contentHash: chunks.contentHash })
			.from(chunks)
			.where(inArray(chunks.contentHash, unique))
		return new Set(rows.map(row => row.contentHash))
	}

	private toItem(row: GoldenRow, liveHashes: Set<string>): GoldenItem {
		return {
			id: row.id,
			question: row.question,
			idealAnswer: row.idealAnswer,
			expectedChunkHashes: row.expectedChunkHashes,
			tags: row.tags,
			status: row.status,
			staleHashes: row.expectedChunkHashes.filter(hash => !liveHashes.has(hash)),
			createdBy: row.createdBy,
			createdAt: row.createdAt,
			updatedAt: row.updatedAt,
		}
	}

	/** All items, newest first, each with staleness computed against `chunks`. */
	async list(): Promise<GoldenItem[]> {
		const rows = await db.select().from(goldenItems).orderBy(desc(goldenItems.createdAt))
		const liveHashes = await this.resolveLiveHashes(rows.flatMap(row => row.expectedChunkHashes))
		return rows.map(row => this.toItem(row, liveHashes))
	}

	async create(userId: string, payload: CreateGoldenItemRequest): Promise<GoldenItem> {
		const [row] = await db
			.insert(goldenItems)
			.values({
				question: payload.question,
				idealAnswer: payload.idealAnswer,
				expectedChunkHashes: payload.expectedChunkHashes,
				tags: payload.tags,
				createdBy: userId,
			})
			.returning()
		if (!row) {
			throw new ORPCError("INTERNAL_SERVER_ERROR", { message: "Insert returned no row" })
		}

		await this.auditService.writeAudit({
			actorId: userId,
			actorType: "user",
			action: "eval.golden.create",
			resourceType: "golden_item",
			resourceId: row.id,
			after: {
				question: payload.question,
				expectedChunkHashes: payload.expectedChunkHashes,
				tags: payload.tags,
			},
		})

		const liveHashes = await this.resolveLiveHashes(row.expectedChunkHashes)
		return this.toItem(row, liveHashes)
	}

	/** Edit fields and/or flip status (retire/reactivate). Audited with before/after. */
	async update(userId: string, payload: UpdateGoldenItemRequest): Promise<GoldenItem> {
		const [existing] = await db.select().from(goldenItems).where(eq(goldenItems.id, payload.id))
		if (!existing) {
			throw new ORPCError("NOT_FOUND", { message: `Golden item ${payload.id} not found` })
		}

		const [row] = await db
			.update(goldenItems)
			.set({
				...(payload.question !== undefined && { question: payload.question }),
				...(payload.idealAnswer !== undefined && { idealAnswer: payload.idealAnswer }),
				...(payload.expectedChunkHashes !== undefined && {
					expectedChunkHashes: payload.expectedChunkHashes,
				}),
				...(payload.tags !== undefined && { tags: payload.tags }),
				...(payload.status !== undefined && { status: payload.status }),
				updatedAt: new Date(),
			})
			.where(eq(goldenItems.id, payload.id))
			.returning()
		if (!row) {
			throw new ORPCError("NOT_FOUND", { message: `Golden item ${payload.id} not found` })
		}

		const retiring = payload.status !== undefined && payload.status !== existing.status
		await this.auditService.writeAudit({
			actorId: userId,
			actorType: "user",
			action: retiring
				? payload.status === "retired"
					? "eval.golden.retire"
					: "eval.golden.reactivate"
				: "eval.golden.update",
			resourceType: "golden_item",
			resourceId: row.id,
			before: {
				question: existing.question,
				idealAnswer: existing.idealAnswer,
				expectedChunkHashes: existing.expectedChunkHashes,
				tags: existing.tags,
				status: existing.status,
			},
			after: {
				question: row.question,
				idealAnswer: row.idealAnswer,
				expectedChunkHashes: row.expectedChunkHashes,
				tags: row.tags,
				status: row.status,
			},
		})

		const liveHashes = await this.resolveLiveHashes(row.expectedChunkHashes)
		return this.toItem(row, liveHashes)
	}

	/** Portable JSON export (§XI). */
	async export(): Promise<GoldenSetExport> {
		const rows = await db.select().from(goldenItems).orderBy(desc(goldenItems.createdAt))
		return {
			version: 1,
			items: rows.map(row => ({
				question: row.question,
				idealAnswer: row.idealAnswer,
				expectedChunkHashes: row.expectedChunkHashes,
				tags: row.tags,
				status: row.status,
			})),
		}
	}

	/** Append the payload's items (existing items untouched). Audited once. */
	async import(userId: string, payload: GoldenSetExport): Promise<{ imported: number }> {
		if (payload.items.length === 0) {
			return { imported: 0 }
		}
		const rows = await db
			.insert(goldenItems)
			.values(
				payload.items.map(item => ({
					question: item.question,
					idealAnswer: item.idealAnswer,
					expectedChunkHashes: item.expectedChunkHashes,
					tags: item.tags,
					status: item.status,
					createdBy: userId,
				}))
			)
			.returning({ id: goldenItems.id })

		await this.auditService.writeAudit({
			actorId: userId,
			actorType: "user",
			action: "eval.golden.import",
			resourceType: "golden_item",
			resourceId: rows.map(row => row.id).join(","),
			after: { imported: rows.length },
		})

		return { imported: rows.length }
	}
}
