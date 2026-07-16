import { and, desc, gte, ilike, lte, sql, type SQL } from "drizzle-orm"

import type { ReportDefinition, ReportResult } from "@repo/contracts"
import { auditLog } from "@repo/db/schema"

import { db } from "@/common/database/database.client"

export interface ReportImpl {
	definition: ReportDefinition
	run(
		filters: Record<string, string>,
		page: number,
		pageSize: number
	): Promise<Pick<ReportResult, "rows" | "total">>
}

/**
 * RP-1: System Audit Trail Report — every state change, privileged action,
 * and integration/API-key activity from the append-only audit_log spine.
 * The framework interface above is what RP-2..5 implement.
 */
export const auditTrailReport: ReportImpl = {
	definition: {
		key: "audit-trail",
		title: "System Audit Trail",
		description:
			"All state changes, privileged actions, and integration activity (append-only, >= 1-year retention).",
		filters: [
			{ key: "actorId", label: "Actor (contains)", type: "text" },
			{ key: "action", label: "Action (prefix)", type: "text" },
			{
				key: "actorType",
				label: "Actor type",
				type: "select",
				options: ["user", "service", "system"],
			},
			{ key: "dateFrom", label: "From", type: "date" },
			{ key: "dateTo", label: "To", type: "date" },
		],
		columns: [
			{ key: "createdAt", label: "When" },
			{ key: "actorId", label: "Actor" },
			{ key: "actorType", label: "Type" },
			{ key: "action", label: "Action" },
			{ key: "resourceType", label: "Resource" },
			{ key: "resourceId", label: "Resource id" },
		],
	},
	async run(filters, page, pageSize) {
		const conditions: SQL[] = []
		if (filters["actorId"]) conditions.push(ilike(auditLog.actorId, `%${filters["actorId"]}%`))
		if (filters["action"]) conditions.push(ilike(auditLog.action, `${filters["action"]}%`))
		if (filters["actorType"]) conditions.push(sql`${auditLog.actorType} = ${filters["actorType"]}`)
		if (filters["dateFrom"]) conditions.push(gte(auditLog.createdAt, new Date(filters["dateFrom"])))
		if (filters["dateTo"]) conditions.push(lte(auditLog.createdAt, new Date(filters["dateTo"])))
		const where = conditions.length > 0 ? and(...conditions) : undefined

		const [countRow] = await db
			.select({ total: sql<number>`count(*)::int` })
			.from(auditLog)
			.where(where)
		const rows = await db
			.select({
				createdAt: auditLog.createdAt,
				actorId: auditLog.actorId,
				actorType: auditLog.actorType,
				action: auditLog.action,
				resourceType: auditLog.resourceType,
				resourceId: auditLog.resourceId,
			})
			.from(auditLog)
			.where(where)
			.orderBy(desc(auditLog.createdAt))
			.limit(pageSize)
			.offset((page - 1) * pageSize)

		return { rows, total: countRow?.total ?? 0 }
	},
}

/** RP-1 registry — RP-2..5 append here. */
export const REPORTS: Record<string, ReportImpl> = {
	[auditTrailReport.definition.key]: auditTrailReport,
}
