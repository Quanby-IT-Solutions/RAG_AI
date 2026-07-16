import { db } from "@/common/database/database.client"

import { ingestionStatusReport } from "./report-definitions"

jest.mock("@orpc/server", () => ({
	ORPCError: class ORPCError extends Error {
		code: string
		constructor(code: string, options?: { message?: string }) {
			super(options?.message ?? code)
			this.code = code
		}
	},
}))

jest.mock("@repo/db/schema", () => ({
	auditLog: { actorId: "actorId", action: "action", createdAt: "createdAt" },
}))

jest.mock("@/common/database/database.client", () => ({
	db: { select: jest.fn(), execute: jest.fn() },
}))

const mockExecute = db.execute as jest.Mock

function jobRow(overrides: Partial<Record<string, unknown>> = {}) {
	return {
		id: "job-1",
		created_at: new Date("2026-07-16T08:00:00Z"),
		status: "completed",
		stats: {
			documents: 3,
			chunks: 42,
			embedBatches: 2,
			cacheHits: 30,
			cacheMisses: 12,
			failures: [],
		},
		error: null,
		doc_types: "csv/indexed: 2, pdf/indexed: 1",
		...overrides,
	}
}

describe("ingestion status report (RP-5)", () => {
	beforeEach(() => jest.clearAllMocks())

	it("links the job column to the IN-7 console for re-run", () => {
		const job = ingestionStatusReport.definition.columns.find(
			column => column.key === "jobId"
		)
		expect(job?.linkTemplate).toBe("/corpus?job={value}")
	})

	it("summarizes documents, chunks, batches, and the IN-6 cache-hit ratio", async () => {
		mockExecute.mockResolvedValueOnce({ rows: [jobRow()] }).mockResolvedValueOnce({ rows: [] })

		const { rows, total } = await ingestionStatusReport.run({}, 1, 50)

		expect(total).toBe(1)
		const row = rows[0] as Record<string, unknown>
		expect(row["documents"]).toBe(3)
		expect(row["chunks"]).toBe(42)
		expect(row["embedBatches"]).toBe(2)
		// 30 hits / 42 lookups ~= 71% — the dedupe/upsert visibility.
		expect(row["cacheHitRatioPct"]).toBe(71)
		expect(row["docTypes"]).toBe("csv/indexed: 2, pdf/indexed: 1")
		expect(row["failedDocuments"]).toBe(0)
		expect(row["failureReasons"]).toBe("")
	})

	it("lists failed documents with their reasons (job error first)", async () => {
		mockExecute
			.mockResolvedValueOnce({
				rows: [
					jobRow({
						status: "failed",
						error: "all documents failed",
						stats: {
							documents: 2,
							chunks: 0,
							embedBatches: 0,
							cacheHits: 0,
							cacheMisses: 0,
							failures: [
								{ documentId: "d1", error: "unsupported encoding" },
								{ documentId: "d2", error: "empty document" },
							],
						},
					}),
				],
			})
			.mockResolvedValueOnce({ rows: [] })

		const { rows } = await ingestionStatusReport.run({}, 1, 50)

		const row = rows[0] as Record<string, unknown>
		expect(row["failedDocuments"]).toBe(2)
		expect(row["failureReasons"]).toBe(
			"all documents failed; unsupported encoding; empty document"
		)
		expect(row["cacheHitRatioPct"]).toBe("")
	})

	it("surfaces parse-failed documents that never reached a job", async () => {
		mockExecute
			.mockResolvedValueOnce({ rows: [] }) // no jobs
			.mockResolvedValueOnce({
				rows: [
					{
						id: "doc-broken",
						ingested_at: new Date("2026-07-16T09:00:00Z"),
						doc_type: "docx",
						source_uri: "upload://broken.docx",
						error: "DOCX parse failed: File is not a zip file",
					},
				],
			})

		const { rows, total } = await ingestionStatusReport.run({}, 1, 50)

		expect(total).toBe(1)
		const row = rows[0] as Record<string, unknown>
		expect(row["status"]).toBe("failed (parse)")
		expect(row["jobId"]).toBe("")
		expect(row["failureReasons"]).toBe(
			"upload://broken.docx: DOCX parse failed: File is not a zip file"
		)
	})

	it("skips the parse-failed query when filtering to non-failed statuses", async () => {
		mockExecute.mockResolvedValueOnce({ rows: [jobRow()] })

		await ingestionStatusReport.run({ status: "completed" }, 1, 50)

		expect(mockExecute).toHaveBeenCalledTimes(1)
	})

	it("paginates job rows", async () => {
		mockExecute
			.mockResolvedValueOnce({
				rows: [
					jobRow({ id: "job-1", created_at: new Date("2026-07-16T09:00:00Z") }),
					jobRow({ id: "job-2", created_at: new Date("2026-07-16T08:00:00Z") }),
				],
			})
			.mockResolvedValueOnce({ rows: [] })

		const pageTwo = await ingestionStatusReport.run({}, 2, 1)

		expect(pageTwo.total).toBe(2)
		expect((pageTwo.rows[0] as Record<string, unknown>)["jobId"]).toBe("job-2")
	})
})
