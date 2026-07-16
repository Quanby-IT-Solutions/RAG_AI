import { db } from "@/common/database/database.client"

import { ReportsService } from "./reports.service"

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
	db: { select: jest.fn() },
}))

const mockSelect = db.select as jest.Mock

function auditRow(overrides: Partial<Record<string, unknown>> = {}) {
	return {
		createdAt: new Date("2026-07-15T10:00:00Z"),
		actorId: "user-1",
		actorType: "user",
		action: "chat.query.submit",
		resourceType: "conversation",
		resourceId: "c1",
		...overrides,
	}
}

/** One db.select() call = one chain; resolves to `rows` at any await point. */
function chain(rows: unknown[]) {
	const builder: Record<string, unknown> = {}
	for (const method of ["from", "where", "orderBy", "limit", "offset"]) {
		builder[method] = jest.fn().mockReturnValue(builder)
	}
	builder["then"] = (resolve: (value: unknown[]) => void) => resolve(rows)
	return builder
}

describe("ReportsService (RP-1 framework)", () => {
	let service: ReportsService

	beforeEach(() => {
		jest.clearAllMocks()
		service = new ReportsService()
	})

	it("lists the audit-trail report definition with filters and columns", () => {
		const definitions = service.list()
		const audit = definitions.find(definition => definition.key === "audit-trail")
		expect(audit).toBeDefined()
		expect(audit?.filters.map(filter => filter.key)).toEqual(
			expect.arrayContaining(["actorId", "action", "actorType", "dateFrom", "dateTo"])
		)
		expect(audit?.columns.length).toBeGreaterThan(0)
	})

	it("rejects unknown report keys with NOT_FOUND", async () => {
		await expect(service.run("nope", {}, 1, 50)).rejects.toMatchObject({ code: "NOT_FOUND" })
		await expect(service.export("nope", {}, "csv")).rejects.toMatchObject({
			code: "NOT_FOUND",
		})
	})

	it("runs the audit report with pagination applied to the query", async () => {
		const countChain = chain([{ total: 120 }])
		const rowsChain = chain([auditRow()])
		mockSelect.mockReturnValueOnce(countChain).mockReturnValueOnce(rowsChain)

		const result = await service.run("audit-trail", { action: "chat." }, 3, 25)

		expect(result.total).toBe(120)
		expect(result.page).toBe(3)
		expect(result.pageSize).toBe(25)
		expect(result.rows).toHaveLength(1)
		expect(result.columns.map(column => column.key)).toContain("action")
		expect(rowsChain["limit"]).toHaveBeenCalledWith(25)
		expect(rowsChain["offset"]).toHaveBeenCalledWith(50)
		// A filter was provided, so both queries carry a WHERE clause.
		expect((countChain["where"] as jest.Mock).mock.calls[0][0]).toBeDefined()
	})

	it("runs unfiltered when no filters are given", async () => {
		const countChain = chain([{ total: 0 }])
		const rowsChain = chain([])
		mockSelect.mockReturnValueOnce(countChain).mockReturnValueOnce(rowsChain)

		const result = await service.run("audit-trail", {}, 1, 50)

		expect(result.total).toBe(0)
		expect((countChain["where"] as jest.Mock).mock.calls[0][0]).toBeUndefined()
	})

	it("exports JSON matching the filtered rows", async () => {
		mockSelect
			.mockReturnValueOnce(chain([{ total: 1 }]))
			.mockReturnValueOnce(chain([auditRow()]))

		const exported = await service.export("audit-trail", { actorId: "user-1" }, "json")

		expect(exported.contentType).toBe("application/json")
		expect(exported.filename).toMatch(/^audit-trail-\d{4}-\d{2}-\d{2}\.json$/)
		const rows = JSON.parse(exported.content) as Record<string, unknown>[]
		expect(rows).toHaveLength(1)
		expect(rows[0]?.["actorId"]).toBe("user-1")
	})

	it("exports CSV with a header row and quote-escaped values", async () => {
		mockSelect
			.mockReturnValueOnce(chain([{ total: 2 }]))
			.mockReturnValueOnce(
				chain([
					auditRow({ resourceId: 'has "quotes", commas' }),
					auditRow({ actorId: "user-2", resourceId: null }),
				])
			)

		const exported = await service.export("audit-trail", {}, "csv")

		expect(exported.contentType).toBe("text/csv")
		const lines = exported.content.split("\n")
		expect(lines[0]).toBe("When,Actor,Type,Action,Resource,Resource id")
		expect(lines).toHaveLength(3)
		expect(lines[1]).toContain('"has ""quotes"", commas"')
		// Null resource id serializes as an empty cell, not "null".
		expect(lines[2]?.endsWith(",")).toBe(true)
	})
})
