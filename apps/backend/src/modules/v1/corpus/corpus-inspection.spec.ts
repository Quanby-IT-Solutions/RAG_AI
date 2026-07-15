import { db } from "@/common/database/database.client"

import { CorpusService } from "./corpus.service"

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
	documents: { id: "id", ingestedAt: "ingestedAt" },
	chunks: {
		id: "id",
		documentId: "documentId",
		chunkIndex: "chunkIndex",
		content: "content",
		contentHash: "contentHash",
		metadata: "metadata",
	},
	ingestionJobs: { id: "id", createdAt: "createdAt" },
	jobDocuments: { jobId: "jobId", updatedAt: "updatedAt" },
}))

jest.mock("@/common/database/database.client", () => ({
	db: { select: jest.fn() },
}))

const mockSelect = db.select as jest.Mock

function makeDocument() {
	return {
		id: "d1",
		sourceUri: "upload://a.html",
		docType: "html",
		contentHash: "doc-hash",
		status: "indexed",
		metadata: {
			text: "SHOULD BE STRIPPED",
			chunking: { strategy: "semantic", fallback: false },
		},
		error: null,
		ingestedAt: new Date("2026-07-16T00:00:00Z"),
	}
}

function chunkRow(index: number, content: string, metadata: unknown = { page: 1 }) {
	return {
		id: `c${index}`,
		chunkIndex: index,
		content,
		contentHash: `hash-${index}`,
		metadata,
	}
}

function stubInspectionQueries(documentRows: unknown[], chunkRows: unknown[]) {
	mockSelect
		.mockReturnValueOnce({
			from: jest.fn().mockReturnValue({
				where: jest.fn().mockResolvedValue(documentRows),
			}),
		})
		.mockReturnValueOnce({
			from: jest.fn().mockReturnValue({
				where: jest.fn().mockReturnValue({
					orderBy: jest.fn().mockResolvedValue(chunkRows),
				}),
			}),
		})
}

describe("CorpusService.inspectChunks (IN-5)", () => {
	beforeEach(() => jest.clearAllMocks())

	it("flags mid-sentence boundaries with the punctuation heuristic", async () => {
		stubInspectionQueries(
			[makeDocument()],
			[
				chunkRow(0, "This sentence ends cleanly."),
				chunkRow(1, "this one starts lowercase and ends without punct"),
			]
		)

		const result = await new CorpusService().inspectChunks("d1")

		expect(result.chunks[0]).toMatchObject({
			endsMidSentence: false,
			startsMidSentence: false,
			overlapWithPrevious: 0,
		})
		expect(result.chunks[1]).toMatchObject({
			endsMidSentence: true,
			startsMidSentence: true,
		})
	})

	it("computes overlap regions and strips document text payload", async () => {
		stubInspectionQueries(
			[makeDocument()],
			[
				chunkRow(0, "Alpha beta gamma delta."),
				chunkRow(1, "gamma delta. Epsilon follows here."),
			]
		)

		const result = await new CorpusService().inspectChunks("d1")

		expect(result.chunks[1]?.overlapWithPrevious).toBe("gamma delta.".length)
		expect(result.chunks[0]?.charCount).toBe("Alpha beta gamma delta.".length)
		expect(result.document.metadata).not.toHaveProperty("text")
		expect(result.chunking).toEqual({ strategy: "semantic", fallback: false })
	})

	it("excludes RT-6 parent chunks from the inspection view", async () => {
		stubInspectionQueries(
			[makeDocument()],
			[
				chunkRow(0, "Child one."),
				chunkRow(100000, "Child one.\n\nChild two.", { role: "parent" }),
				chunkRow(1, "Child two."),
			]
		)

		const result = await new CorpusService().inspectChunks("d1")

		expect(result.chunks.map(chunk => chunk.id)).toEqual(["c0", "c1"])
	})

	it("throws NOT_FOUND for a missing document", async () => {
		stubInspectionQueries([], [])
		await expect(new CorpusService().inspectChunks("missing")).rejects.toMatchObject({
			code: "NOT_FOUND",
		})
	})
})
