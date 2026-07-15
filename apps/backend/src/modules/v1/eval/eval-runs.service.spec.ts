import { db } from "@/common/database/database.client"

import { EvalRunsService } from "./eval-runs.service"

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
	evalRuns: { id: "id", createdAt: "createdAt", metrics: "metrics", gitSha: "gitSha" },
	evalBaselines: {
		id: "id",
		runId: "runId",
		promotedBy: "promotedBy",
		note: "note",
		createdAt: "createdAt",
	},
}))

jest.mock("@/common/database/database.client", () => ({
	db: { select: jest.fn(), insert: jest.fn() },
}))

const mockSelect = db.select as jest.Mock
const mockInsert = db.insert as jest.Mock

function auditServiceMock() {
	return { writeAudit: jest.fn().mockResolvedValue(undefined) }
}

function makeRun(overrides: Partial<Record<string, unknown>> = {}) {
	return {
		id: "r1",
		kind: "retrieval",
		status: "completed",
		gitSha: "sha-1",
		config: { k: 5 },
		metrics: { hitRate: 0.5, mrr: 0.4, itemCount: 2 },
		perItem: [{ goldenItemId: "g1", question: "q", skipped: false }],
		error: null,
		triggeredBy: "u1",
		createdAt: new Date("2026-07-16T00:00:00Z"),
		finishedAt: new Date("2026-07-16T00:00:01Z"),
		...overrides,
	}
}

function stubWhere(rows: unknown[]) {
	mockSelect.mockReturnValueOnce({
		from: jest.fn().mockReturnValue({
			where: jest.fn().mockResolvedValue(rows),
		}),
	})
}

describe("EvalRunsService (EV-2)", () => {
	beforeEach(() => jest.clearAllMocks())

	it("get returns the run and throws NOT_FOUND when missing", async () => {
		const service = new EvalRunsService(auditServiceMock() as never)
		stubWhere([makeRun()])
		await expect(service.get("r1")).resolves.toMatchObject({ id: "r1", gitSha: "sha-1" })

		stubWhere([])
		await expect(service.get("missing")).rejects.toMatchObject({ code: "NOT_FOUND" })
	})

	it("list omits perItem from summaries", async () => {
		mockSelect.mockReturnValueOnce({
			from: jest.fn().mockReturnValue({
				orderBy: jest.fn().mockReturnValue({
					limit: jest.fn().mockResolvedValue([makeRun()]),
				}),
			}),
		})

		const [summary] = await new EvalRunsService(auditServiceMock() as never).list()
		expect(summary).not.toHaveProperty("perItem")
		expect(summary?.metrics).toEqual({ hitRate: 0.5, mrr: 0.4, itemCount: 2 })
	})

	it("compare computes candidate minus base for shared metrics only", async () => {
		const service = new EvalRunsService(auditServiceMock() as never)
		stubWhere([makeRun({ id: "base", metrics: { hitRate: 0.5, mrr: 0.4 } })])
		stubWhere([
			makeRun({ id: "cand", metrics: { hitRate: 0.75, mrr: 0.3, precisionAtK: 0.2 } }),
		])

		const comparison = await service.compare("base", "cand")

		expect(comparison.deltas.hitRate).toBeCloseTo(0.25)
		expect(comparison.deltas.mrr).toBeCloseTo(-0.1)
		// precisionAtK exists only on the candidate -> no delta emitted.
		expect(comparison.deltas).not.toHaveProperty("precisionAtK")
		expect(comparison.base.id).toBe("base")
		expect(comparison.candidate.id).toBe("cand")
	})
})

describe("EvalRunsService baseline promotion (EV-4)", () => {
	beforeEach(() => jest.clearAllMocks())

	function stubBaselineJoin(rows: unknown[]) {
		mockSelect.mockReturnValueOnce({
			from: jest.fn().mockReturnValue({
				innerJoin: jest.fn().mockReturnValue({
					orderBy: jest.fn().mockReturnValue({
						limit: jest.fn().mockResolvedValue(rows),
					}),
				}),
			}),
		})
	}

	it("promote inserts a baseline row and audits with before/after", async () => {
		const audit = auditServiceMock()
		const service = new EvalRunsService(audit as never)
		stubWhere([makeRun({ id: "r2", status: "completed", gitSha: "sha-2" })]) // get(runId)
		stubBaselineJoin([
			{
				id: "b0",
				runId: "r1",
				promotedBy: "u0",
				note: null,
				createdAt: new Date(),
				metrics: {},
				gitSha: "sha-1",
			},
		])
		mockInsert.mockReturnValue({
			values: jest.fn().mockReturnValue({
				returning: jest.fn().mockResolvedValue([
					{
						id: "b1",
						runId: "r2",
						promotedBy: "u1",
						note: "better",
						createdAt: new Date(),
					},
				]),
			}),
		})

		const baseline = await service.promote("u1", "r2", "better")

		expect(baseline.runId).toBe("r2")
		expect(audit.writeAudit).toHaveBeenCalledWith(
			expect.objectContaining({
				action: "eval.gate.promote",
				before: expect.objectContaining({ runId: "r1" }),
				after: expect.objectContaining({ runId: "r2", note: "better" }),
			})
		)
	})

	it("promote refuses non-completed runs (baseline never moves implicitly)", async () => {
		const service = new EvalRunsService(auditServiceMock() as never)
		stubWhere([makeRun({ id: "r3", status: "failed" })])

		await expect(service.promote("u1", "r3")).rejects.toMatchObject({ code: "CONFLICT" })
		expect(mockInsert).not.toHaveBeenCalled()
	})

	it("currentBaseline returns null before any promotion", async () => {
		const service = new EvalRunsService(auditServiceMock() as never)
		stubBaselineJoin([])
		await expect(service.currentBaseline()).resolves.toBeNull()
	})
})
