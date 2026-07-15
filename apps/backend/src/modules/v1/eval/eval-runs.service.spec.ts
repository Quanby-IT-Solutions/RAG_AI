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
	evalRuns: { id: "id", createdAt: "createdAt" },
}))

jest.mock("@/common/database/database.client", () => ({
	db: { select: jest.fn() },
}))

const mockSelect = db.select as jest.Mock

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
		const service = new EvalRunsService()
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

		const [summary] = await new EvalRunsService().list()
		expect(summary).not.toHaveProperty("perItem")
		expect(summary?.metrics).toEqual({ hitRate: 0.5, mrr: 0.4, itemCount: 2 })
	})

	it("compare computes candidate minus base for shared metrics only", async () => {
		const service = new EvalRunsService()
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
