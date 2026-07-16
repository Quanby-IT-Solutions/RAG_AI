import { buildCachePerformanceReport, type PromQueryRange } from "./report-definitions"

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

const T1 = 1_784_000_000
const T2 = T1 + 3600

/** Serves per-metric series keyed by a distinctive PromQL fragment. */
function promMock(series: Record<string, [number, string][]>): PromQueryRange & jest.Mock {
	return jest.fn(async (query: string) => {
		for (const [fragment, values] of Object.entries(series)) {
			if (query.includes(fragment)) return values
		}
		return []
	})
}

describe("cache & performance report (RP-4)", () => {
	it("declares NFR threshold overlay on the chart hint", () => {
		const report = buildCachePerformanceReport(promMock({}))
		expect(report.definition.chart).toMatchObject({ yKey: "ttftP95", threshold: 2 })
		expect(report.definition.columns.map(column => column.key)).toEqual(
			expect.arrayContaining([
				"time",
				"cacheHitRatePct",
				"p95",
				"ttftP95",
				"errorRatePct",
				"provider429",
				"shed",
				"dbLongestQuerySeconds",
				"breaches",
			])
		)
	})

	it("joins the Prometheus series into per-interval rows", async () => {
		const prom = promMock({
			rag_cache_lookups_total: [
				[T1, "50"],
				[T2, "80"],
			],
			"histogram_quantile(0.95, sum by (le) (rate(rag_http_request_duration_seconds_bucket": [
				[T1, "1.2"],
			],
			rag_chat_ttft_seconds_bucket: [[T1, "0.8"]],
			rag_errors_total: [[T1, "0"]],
			rag_provider_429_total: [[T1, "3"]],
			rag_db_longest_query_seconds: [[T1, "0.05"]],
		})
		const report = buildCachePerformanceReport(prom)

		const { rows, total } = await report.run({}, 1, 50)

		expect(total).toBe(2)
		// Newest interval first; T2 only has the cache series.
		const [newest, oldest] = rows as Record<string, unknown>[]
		expect(newest?.["cacheHitRatePct"]).toBe(80)
		expect(newest?.["p95"]).toBe("")
		expect(oldest?.["p95"]).toBe(1.2)
		expect(oldest?.["ttftP95"]).toBe(0.8)
		expect(oldest?.["provider429"]).toBe(3)
		expect(oldest?.["dbLongestQuerySeconds"]).toBe(0.05)
		// 50% hit-rate, sub-threshold latency/errors: no breaches.
		expect(oldest?.["breaches"]).toBe("")
	})

	it("flags NFR breaches per interval (DP-3 targets)", async () => {
		const prom = promMock({
			rag_cache_lookups_total: [[T1, "10"]], // < 30%
			"histogram_quantile(0.95, sum by (le) (rate(rag_http_request_duration_seconds_bucket": [
				[T1, "16"], // > 15s
			],
			rag_chat_ttft_seconds_bucket: [[T1, "2.5"]], // > 2s
			rag_errors_total: [[T1, "5"]], // >= 1%
		})
		const report = buildCachePerformanceReport(prom)

		const { rows } = await report.run({}, 1, 50)

		expect((rows[0] as Record<string, unknown>)["breaches"]).toBe(
			"ttftP95>2s, p95>15s, errors>=1%, hitRate<30%"
		)
	})

	it("passes the date range and step through to Prometheus", async () => {
		const prom = promMock({})
		const report = buildCachePerformanceReport(prom)

		await report.run({ dateFrom: "2026-07-10", dateTo: "2026-07-11", step: "5m" }, 1, 50)

		const [query, start, end, step] = prom.mock.calls[0] as [string, number, number, string]
		expect(query).toContain("[5m]")
		expect(step).toBe("5m")
		expect(start).toBe(Math.floor(new Date("2026-07-10").getTime() / 1000))
		// dateTo is inclusive: the end lands a day after its midnight.
		expect(end).toBe(Math.floor((new Date("2026-07-11").getTime() + 86_400_000) / 1000))
	})
})
