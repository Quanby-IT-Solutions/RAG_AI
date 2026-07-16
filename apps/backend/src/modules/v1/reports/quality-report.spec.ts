import { buildQualityReport } from "./report-definitions"

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

function makeRun(overrides: Partial<Record<string, unknown>> = {}) {
	return {
		id: "run-1",
		kind: "retrieval",
		status: "completed",
		gitSha: "sha-1",
		config: { k: 5, rerank: false },
		metrics: { hitRate: 0.9, mrr: 0.8, precisionAtK: 0.4, recallAtK: 0.9, itemCount: 3 },
		error: null,
		triggeredBy: "u1",
		createdAt: new Date("2026-07-15T10:00:00Z"),
		finishedAt: new Date("2026-07-15T10:00:05Z"),
		...overrides,
	}
}

function serviceMock(runs: unknown[], baseline: unknown = null) {
	return {
		list: jest.fn().mockResolvedValue(runs),
		currentBaseline: jest.fn().mockResolvedValue(baseline),
		compare: jest.fn(),
	}
}

describe("quality report (RP-3)", () => {
	it("trends runs with the active baseline marked and config snapshot attached", async () => {
		const baselineRun = makeRun({ id: "run-base" })
		const evalRuns = serviceMock(
			[makeRun({ id: "run-2", createdAt: new Date("2026-07-16T10:00:00Z") }), baselineRun],
			{ runId: "run-base", metrics: baselineRun.metrics, gitSha: "sha-1" }
		)
		const report = buildQualityReport(evalRuns as never)

		const { rows, total } = await report.run({}, 1, 50)

		expect(total).toBe(2)
		const marked = rows.find(row => (row as Record<string, unknown>)["runId"] === "run-base")
		expect((marked as Record<string, unknown>)["baseline"]).toBe("active")
		const other = rows.find(row => (row as Record<string, unknown>)["runId"] === "run-2")
		expect((other as Record<string, unknown>)["baseline"]).toBe("")
		expect((other as Record<string, unknown>)["hitRate"]).toBe(0.9)
		expect((other as Record<string, unknown>)["config"]).toContain('"k":5')
	})

	it("highlights metrics that would fail the gate (below baseline - tolerance)", async () => {
		const baselineMetrics = { hitRate: 0.9, mrr: 0.8, precisionAtK: 0.4, recallAtK: 0.9 }
		const evalRuns = serviceMock(
			[
				makeRun({
					id: "run-regressed",
					metrics: { hitRate: 0.5, mrr: 0.79, precisionAtK: 0.4, recallAtK: 0.6 },
				}),
			],
			{ runId: "run-base", metrics: baselineMetrics, gitSha: "sha-0" }
		)
		const report = buildQualityReport(evalRuns as never)

		const { rows } = await report.run({}, 1, 50)

		// hitRate 0.5 < 0.88 and recallAtK 0.6 < 0.88 fail; mrr 0.79 is
		// within the 0.02 tolerance of 0.8 and passes.
		expect((rows[0] as Record<string, unknown>)["gateFailures"]).toBe("hitRate, recallAtK")
	})

	it("filters by kind, status, and date range", async () => {
		const evalRuns = serviceMock([
			makeRun({ id: "r-ret", kind: "retrieval", createdAt: new Date("2026-07-10T00:00:00Z") }),
			makeRun({ id: "r-gen", kind: "generation", createdAt: new Date("2026-07-15T00:00:00Z") }),
			makeRun({ id: "r-fail", status: "failed", createdAt: new Date("2026-07-15T00:00:00Z") }),
		])
		const report = buildQualityReport(evalRuns as never)

		const byKind = await report.run({ kind: "generation" }, 1, 50)
		expect(byKind.rows.map(row => (row as Record<string, unknown>)["runId"])).toEqual(["r-gen"])

		const byStatus = await report.run({ status: "failed" }, 1, 50)
		expect(byStatus.rows.map(row => (row as Record<string, unknown>)["runId"])).toEqual([
			"r-fail",
		])

		const byDate = await report.run({ dateFrom: "2026-07-14", dateTo: "2026-07-15" }, 1, 50)
		expect(byDate.total).toBe(2)
	})

	it("switches to EV-2 per-metric deltas when both compare ids are set", async () => {
		const evalRuns = serviceMock([])
		evalRuns.compare.mockResolvedValue({
			base: { gitSha: "sha-a", metrics: { hitRate: 0.8, mrr: 0.7 } },
			candidate: { gitSha: "sha-b", metrics: { hitRate: 0.9, mrr: 0.65 } },
			deltas: { hitRate: 0.1, mrr: -0.05 },
		})
		const report = buildQualityReport(evalRuns as never)

		const result = await report.run(
			{ compareBase: "run-a", compareCandidate: "run-b" },
			1,
			50
		)

		expect(evalRuns.compare).toHaveBeenCalledWith("run-a", "run-b")
		expect(result.columns?.map(column => column.key)).toEqual([
			"metric",
			"base",
			"candidate",
			"delta",
		])
		const hitRate = result.rows.find(
			row => (row as Record<string, unknown>)["metric"] === "hitRate"
		) as Record<string, unknown>
		expect(hitRate["base"]).toBe(0.8)
		expect(hitRate["candidate"]).toBe(0.9)
		expect(hitRate["delta"]).toBe(0.1)
	})
})
