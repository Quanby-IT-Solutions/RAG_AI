import { db } from "@/common/database/database.client"

import { EvalGoldenService } from "./eval-golden.service"

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
	chunks: { contentHash: "contentHash" },
	goldenItems: {
		id: "id",
		createdAt: "createdAt",
		$inferSelect: {},
	},
}))

jest.mock("@/common/database/database.client", () => ({
	db: {
		select: jest.fn(),
		insert: jest.fn(),
		update: jest.fn(),
	},
}))

const mockSelect = db.select as jest.Mock
const mockInsert = db.insert as jest.Mock
const mockUpdate = db.update as jest.Mock

function makeRow(overrides: Partial<Record<string, unknown>> = {}) {
	return {
		id: "g1",
		question: "How many vacation days?",
		idealAnswer: "20 days.",
		expectedChunkHashes: ["hash-live", "hash-gone"],
		tags: ["hr"],
		status: "active",
		createdBy: "u1",
		createdAt: new Date("2026-07-01T00:00:00Z"),
		updatedAt: new Date("2026-07-01T00:00:00Z"),
		...overrides,
	}
}

function auditServiceMock() {
	return { writeAudit: jest.fn().mockResolvedValue(undefined) }
}

/** db.select() chain returning golden rows first, then live chunk hashes. */
function stubSelectSequence(goldenRows: unknown[], liveHashes: string[]) {
	mockSelect
		.mockReturnValueOnce({
			from: jest.fn().mockReturnValue({
				orderBy: jest.fn().mockResolvedValue(goldenRows),
			}),
		})
		.mockReturnValueOnce({
			from: jest.fn().mockReturnValue({
				where: jest.fn().mockResolvedValue(liveHashes.map(hash => ({ contentHash: hash }))),
			}),
		})
}

describe("EvalGoldenService (EV-1)", () => {
	beforeEach(() => {
		jest.clearAllMocks()
	})

	it("list flags expected hashes orphaned by re-chunking as stale", async () => {
		stubSelectSequence([makeRow()], ["hash-live"])

		const service = new EvalGoldenService(auditServiceMock() as never)
		const [item] = await service.list()

		// AC: an orphaned hash flags the item stale rather than silently passing.
		expect(item?.staleHashes).toEqual(["hash-gone"])
		expect(item?.expectedChunkHashes).toEqual(["hash-live", "hash-gone"])
	})

	it("list reports no staleness when every hash is live", async () => {
		stubSelectSequence([makeRow()], ["hash-live", "hash-gone"])

		const service = new EvalGoldenService(auditServiceMock() as never)
		const [item] = await service.list()

		expect(item?.staleHashes).toEqual([])
	})

	it("create persists the item and audits eval.golden.create", async () => {
		const row = makeRow()
		mockInsert.mockReturnValue({
			values: jest.fn().mockReturnValue({
				returning: jest.fn().mockResolvedValue([row]),
			}),
		})
		mockSelect.mockReturnValue({
			from: jest.fn().mockReturnValue({
				where: jest.fn().mockResolvedValue([
					{ contentHash: "hash-live" },
					{ contentHash: "hash-gone" },
				]),
			}),
		})
		const audit = auditServiceMock()

		const service = new EvalGoldenService(audit as never)
		const item = await service.create("u1", {
			question: row.question,
			idealAnswer: row.idealAnswer,
			expectedChunkHashes: row.expectedChunkHashes,
			tags: row.tags,
		})

		expect(item.id).toBe("g1")
		expect(audit.writeAudit).toHaveBeenCalledWith(
			expect.objectContaining({
				actorId: "u1",
				action: "eval.golden.create",
				resourceType: "golden_item",
				resourceId: "g1",
			})
		)
	})

	it("update to retired audits eval.golden.retire with before/after", async () => {
		const existing = makeRow()
		const updated = makeRow({ status: "retired" })
		mockSelect
			.mockReturnValueOnce({
				from: jest.fn().mockReturnValue({
					where: jest.fn().mockResolvedValue([existing]),
				}),
			})
			.mockReturnValueOnce({
				from: jest.fn().mockReturnValue({
					where: jest.fn().mockResolvedValue([{ contentHash: "hash-live" }]),
				}),
			})
		mockUpdate.mockReturnValue({
			set: jest.fn().mockReturnValue({
				where: jest.fn().mockReturnValue({
					returning: jest.fn().mockResolvedValue([updated]),
				}),
			}),
		})
		const audit = auditServiceMock()

		const service = new EvalGoldenService(audit as never)
		const item = await service.update("u1", { id: "g1", status: "retired" })

		expect(item.status).toBe("retired")
		expect(audit.writeAudit).toHaveBeenCalledWith(
			expect.objectContaining({
				action: "eval.golden.retire",
				before: expect.objectContaining({ status: "active" }),
				after: expect.objectContaining({ status: "retired" }),
			})
		)
	})

	it("update of a missing item throws NOT_FOUND", async () => {
		mockSelect.mockReturnValueOnce({
			from: jest.fn().mockReturnValue({
				where: jest.fn().mockResolvedValue([]),
			}),
		})

		const service = new EvalGoldenService(auditServiceMock() as never)
		await expect(service.update("u1", { id: "missing" })).rejects.toMatchObject({
			code: "NOT_FOUND",
		})
	})

	it("import appends items, audits once, and reports the count", async () => {
		mockInsert.mockReturnValue({
			values: jest.fn().mockReturnValue({
				returning: jest.fn().mockResolvedValue([{ id: "a" }, { id: "b" }]),
			}),
		})
		const audit = auditServiceMock()

		const service = new EvalGoldenService(audit as never)
		const result = await service.import("u1", {
			version: 1,
			items: [
				{
					question: "q1",
					idealAnswer: "a1",
					expectedChunkHashes: ["h1"],
					tags: [],
					status: "active",
				},
				{
					question: "q2",
					idealAnswer: "a2",
					expectedChunkHashes: ["h2"],
					tags: ["x"],
					status: "retired",
				},
			],
		})

		expect(result).toEqual({ imported: 2 })
		expect(audit.writeAudit).toHaveBeenCalledTimes(1)
		expect(audit.writeAudit).toHaveBeenCalledWith(
			expect.objectContaining({ action: "eval.golden.import" })
		)
	})

	it("import of an empty set is a no-op (no insert, no audit)", async () => {
		const audit = auditServiceMock()
		const service = new EvalGoldenService(audit as never)

		await expect(service.import("u1", { version: 1, items: [] })).resolves.toEqual({
			imported: 0,
		})
		expect(mockInsert).not.toHaveBeenCalled()
		expect(audit.writeAudit).not.toHaveBeenCalled()
	})
})
