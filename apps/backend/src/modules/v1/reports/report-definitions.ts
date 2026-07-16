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

/**
 * RP-2: Usage & Cost Report — request counts, token totals, stored per-request
 * cost (computed by the rag service's pricing table, OB-1 — single source; the
 * report only SUMS costUsd, it never re-prices), and routing distribution.
 * User-facing spend comes from chat.query.submit audit payloads joined to
 * messages token_usage; eval-tagged spend (EV-3) comes from eval_runs and is
 * reported as separate `spend = eval` rows.
 */
export const usageCostReport: ReportImpl = {
	definition: {
		key: "usage-cost",
		title: "Usage & Cost",
		description:
			"Requests, prompt/completion tokens, cost, and routing split per day/model/provider. Eval spend (EV-3) is separated from user-facing spend.",
		filters: [
			{ key: "model", label: "Model (contains)", type: "text" },
			{
				key: "spend",
				label: "Spend",
				type: "select",
				options: ["user", "eval"],
			},
			{ key: "dateFrom", label: "From", type: "date" },
			{ key: "dateTo", label: "To", type: "date" },
		],
		columns: [
			{ key: "day", label: "Day" },
			{ key: "spend", label: "Spend" },
			{ key: "model", label: "Model" },
			{ key: "provider", label: "Provider" },
			{ key: "tier", label: "Routing tier" },
			{ key: "requests", label: "Requests" },
			{ key: "tierShare", label: "Tier share" },
			{ key: "promptTokens", label: "Prompt tokens" },
			{ key: "completionTokens", label: "Completion tokens" },
			{ key: "costUsd", label: "Cost (USD)" },
		],
		chart: { xKey: "day", yKey: "costUsd", yLabel: "Cost (USD)" },
	},
	async run(filters, page, pageSize) {
		const dateFrom = filters["dateFrom"] ? new Date(filters["dateFrom"]) : null
		const dateTo = filters["dateTo"] ? new Date(filters["dateTo"]) : null
		const model = filters["model"] ? `%${filters["model"]}%` : null

		// User-facing spend: one chat.query.submit audit row per answered query
		// (OB-1 payload carries model/provider/routing/costUsd); token totals
		// join from the persisted assistant message (GN-2 owns those writes).
		const userRows = sql`
			SELECT
				to_char(date_trunc('day', a.created_at), 'YYYY-MM-DD') AS day,
				'user' AS spend,
				COALESCE(a.after->>'model', m.model_used, 'unknown') AS model,
				COALESCE(a.after->>'provider', 'unknown') AS provider,
				COALESCE(a.after->'routing'->>'tier', 'unknown') AS tier,
				count(*)::int AS requests,
				COALESCE(sum((m.token_usage->>'promptTokens')::bigint), 0)::bigint AS prompt_tokens,
				COALESCE(sum((m.token_usage->>'completionTokens')::bigint), 0)::bigint AS completion_tokens,
				COALESCE(sum((a.after->>'costUsd')::numeric), 0) AS cost_usd
			FROM audit_log a
			LEFT JOIN messages m ON m.id = NULLIF(a.after->>'messageId', '')::uuid
			WHERE a.action = 'chat.query.submit'
			GROUP BY 1, 3, 4, 5
		`
		// Eval-tagged spend (EV-3): generation eval runs tally their own token
		// spend on the run record — never mixed into user rows.
		const evalRows = sql`
			SELECT
				to_char(date_trunc('day', e.created_at), 'YYYY-MM-DD') AS day,
				'eval' AS spend,
				COALESCE(e.config->>'generationModel', 'unknown') AS model,
				'openai' AS provider,
				'eval' AS tier,
				count(*)::int AS requests,
				COALESCE(sum((e.metrics->>'evalPromptTokens')::numeric), 0)::bigint AS prompt_tokens,
				COALESCE(sum((e.metrics->>'evalCompletionTokens')::numeric), 0)::bigint AS completion_tokens,
				0 AS cost_usd
			FROM eval_runs e
			WHERE e.kind = 'generation' AND e.metrics IS NOT NULL
			GROUP BY 1, 3
		`
		const result = await db.execute(sql`
			WITH usage_rows AS (${userRows} UNION ALL ${evalRows})
			SELECT * FROM usage_rows
			WHERE (${dateFrom}::timestamptz IS NULL OR day >= to_char(${dateFrom}::timestamptz, 'YYYY-MM-DD'))
				AND (${dateTo}::timestamptz IS NULL OR day <= to_char(${dateTo}::timestamptz, 'YYYY-MM-DD'))
				AND (${model}::text IS NULL OR model ILIKE ${model})
				AND (${filters["spend"] ?? null}::text IS NULL OR spend = ${filters["spend"] ?? null})
			ORDER BY day DESC, spend, model
		`)
		const rows = (result.rows as Record<string, unknown>[]).map(row => ({
			day: row["day"],
			spend: row["spend"],
			model: row["model"],
			provider: row["provider"],
			tier: row["tier"],
			requests: Number(row["requests"]),
			promptTokens: Number(row["prompt_tokens"]),
			completionTokens: Number(row["completion_tokens"]),
			costUsd: Number(row["cost_usd"]),
		}))
		// Routing distribution: each row's share of that day's requests within
		// the same spend bucket (cheap-fast vs strong share per day).
		const dayTotals = new Map<string, number>()
		for (const row of rows) {
			const dayKey = `${row.day}|${row.spend}`
			dayTotals.set(dayKey, (dayTotals.get(dayKey) ?? 0) + row.requests)
		}
		const withShare = rows.map(row => ({
			...row,
			tierShare: `${Math.round((row.requests / (dayTotals.get(`${row.day}|${row.spend}`) ?? 1)) * 100)}%`,
		}))
		return {
			rows: withShare.slice((page - 1) * pageSize, page * pageSize),
			total: withShare.length,
		}
	},
}

/** RP-1 registry — RP-2..5 append here. */
export const REPORTS: Record<string, ReportImpl> = {
	[auditTrailReport.definition.key]: auditTrailReport,
	[usageCostReport.definition.key]: usageCostReport,
}
