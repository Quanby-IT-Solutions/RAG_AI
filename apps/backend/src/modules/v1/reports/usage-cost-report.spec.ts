import { db } from "@/common/database/database.client"

import { usageCostReport } from "./report-definitions"

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
	auditLog: {
		actorId: "actorId",
		actorType: "actorType",
		action: "action",
		resourceType: "resourceType",
		resourceId: "resourceId",
		createdAt: "createdAt",
	},
}))

jest.mock("@/common/database/database.client", () => ({
	db: { select: jest.fn(), execute: jest.fn() },
}))

const mockExecute = db.execute as jest.Mock

function usageRow(overrides: Partial<Record<string, unknown>> = {}) {
	return {
		day: "2026-07-16",
		spend: "user",
		model: "gpt-4o-mini",
		provider: "openai",
		tier: "cheap-fast",
		requests: 8,
		prompt_tokens: "4000",
		completion_tokens: "200",
		cost_usd: "0.0012",
		...overrides,
	}
}

describe("usageCostReport (RP-2)", () => {
	beforeEach(() => jest.clearAllMocks())

	it("declares date/model/spend filters and a cost chart hint", () => {
		expect(usageCostReport.definition.key).toBe("usage-cost")
		expect(usageCostReport.definition.filters.map(filter => filter.key)).toEqual(
			expect.arrayContaining(["model", "spend", "dateFrom", "dateTo"])
		)
		expect(usageCostReport.definition.chart).toEqual({
			xKey: "day",
			yKey: "costUsd",
			yLabel: "Cost (USD)",
		})
	})

	it("numbers the aggregates and computes per-day routing tier share", async () => {
		mockExecute.mockResolvedValue({
			rows: [
				usageRow({ tier: "cheap-fast", requests: 6 }),
				usageRow({ model: "gpt-4o", tier: "strong", requests: 2, cost_usd: "0.04" }),
				usageRow({ day: "2026-07-15", tier: "cheap-fast", requests: 3 }),
			],
		})

		const { rows, total } = await usageCostReport.run({}, 1, 50)

		expect(total).toBe(3)
		const first = rows[0] as Record<string, unknown>
		expect(first["promptTokens"]).toBe(4000)
		expect(first["costUsd"]).toBeCloseTo(0.0012)
		// 2026-07-16 has 8 user requests: 6 cheap-fast (75%), 2 strong (25%).
		expect(first["tierShare"]).toBe("75%")
		expect((rows[1] as Record<string, unknown>)["tierShare"]).toBe("25%")
		// A different day forms its own distribution (100% of its 3 requests).
		expect((rows[2] as Record<string, unknown>)["tierShare"]).toBe("100%")
	})

	it("keeps eval spend separable: its share pool never mixes with user rows", async () => {
		mockExecute.mockResolvedValue({
			rows: [
				usageRow({ spend: "user", requests: 9 }),
				usageRow({ spend: "eval", tier: "eval", requests: 1, cost_usd: "0" }),
			],
		})

		const { rows } = await usageCostReport.run({}, 1, 50)

		// Same day, but eval is 100% of the EVAL bucket — not 10% of the day.
		expect((rows[0] as Record<string, unknown>)["tierShare"]).toBe("100%")
		expect((rows[1] as Record<string, unknown>)["tierShare"]).toBe("100%")
	})

	it("paginates the aggregated rows", async () => {
		mockExecute.mockResolvedValue({
			rows: [usageRow({ day: "2026-07-16" }), usageRow({ day: "2026-07-15" })],
		})

		const pageTwo = await usageCostReport.run({}, 2, 1)

		expect(pageTwo.total).toBe(2)
		expect(pageTwo.rows).toHaveLength(1)
		expect((pageTwo.rows[0] as Record<string, unknown>)["day"]).toBe("2026-07-15")
	})
})
