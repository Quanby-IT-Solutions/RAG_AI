import { ORPCError } from "@orpc/server"
import { and, desc, gte, ilike, lte, sql, type SQL } from "drizzle-orm"

import type { ReportDefinition, ReportResult } from "@repo/contracts"
import { auditLog } from "@repo/db/schema"

import { db } from "@/common/database/database.client"

import type { EvalRunsService } from "../eval/eval-runs.service"

export interface ReportImpl {
	definition: ReportDefinition
	run(
		filters: Record<string, string>,
		page: number,
		pageSize: number
	): Promise<
		Pick<ReportResult, "rows" | "total"> & {
			/** Overrides the definition columns for mode-dependent shapes
			 * (e.g. RP-3's two-run comparison rows). */
			columns?: ReportResult["columns"]
		}
	>
}

/** One Prometheus range-query series: [unixSeconds, value] pairs. */
export type PromQueryRange = (
	query: string,
	startSeconds: number,
	endSeconds: number,
	step: string
) => Promise<[number, string][]>

/** Services report implementations may close over (framework DI seam). */
export interface ReportDeps {
	evalRunsService: EvalRunsService
	promQueryRange: PromQueryRange
}

/** Prometheus HTTP range API (OB-2 stack) — the single source for RP-4. */
export function makePromQueryRange(baseUrl: string): PromQueryRange {
	return async (query, startSeconds, endSeconds, step) => {
		const url = new URL("/api/v1/query_range", baseUrl)
		url.searchParams.set("query", query)
		url.searchParams.set("start", String(startSeconds))
		url.searchParams.set("end", String(endSeconds))
		url.searchParams.set("step", step)
		const response = await fetch(url, { signal: AbortSignal.timeout(10_000) })
		if (!response.ok) {
			throw new ORPCError("BAD_GATEWAY", {
				message: `Prometheus query failed (${response.status})`,
			})
		}
		const body = (await response.json()) as {
			status: string
			data?: { result?: { values?: [number, string][] }[] }
		}
		if (body.status !== "success") {
			throw new ORPCError("BAD_GATEWAY", { message: "Prometheus query failed" })
		}
		return body.data?.result?.[0]?.values ?? []
	}
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

/**
 * Mirrors the rag service's EVAL_GATE_TOLERANCE default (app/eval_gate.py) —
 * the gate itself always runs in Python; this value only drives the report's
 * highlighting of runs that WOULD fail it.
 */
const GATE_TOLERANCE = 0.02
const GATED_METRICS = [
	"hitRate",
	"mrr",
	"precisionAtK",
	"recallAtK",
	"faithfulness",
	"answerRelevancy",
] as const

const QUALITY_METRIC_COLUMNS = GATED_METRICS.map(metric => ({
	key: metric,
	label: metric,
}))

function metricValue(metrics: Record<string, number> | null, key: string): number | "" {
	const value = metrics?.[key]
	return typeof value === "number" ? Number(value.toFixed(4)) : ""
}

/**
 * RP-3: Retrieval & Answer Quality Report — eval-run metrics trended over
 * time with the active baseline (EV-4) marked, per-run config snapshots, and
 * gate-failure highlighting. Setting BOTH compareBase and compareCandidate
 * switches the result shape to per-metric deltas, reusing EV-2's comparison.
 */
export function buildQualityReport(evalRunsService: EvalRunsService): ReportImpl {
	return {
		definition: {
			key: "quality",
			title: "Retrieval & Answer Quality",
			description:
				"Golden-set metrics per eval run with the active baseline marked and would-fail-gate metrics highlighted. Fill both compare fields with run ids for per-metric deltas (EV-2).",
			filters: [
				{
					key: "kind",
					label: "Kind",
					type: "select",
					options: ["retrieval", "generation"],
				},
				{
					key: "status",
					label: "Status",
					type: "select",
					options: ["completed", "failed"],
				},
				{ key: "dateFrom", label: "From", type: "date" },
				{ key: "dateTo", label: "To", type: "date" },
				{ key: "compareBase", label: "Compare: base run id", type: "text" },
				{ key: "compareCandidate", label: "Compare: candidate run id", type: "text" },
			],
			columns: [
				{ key: "createdAt", label: "When" },
				{ key: "runId", label: "Run" },
				{ key: "kind", label: "Kind" },
				{ key: "status", label: "Status" },
				{ key: "gitSha", label: "Git SHA" },
				{ key: "baseline", label: "Baseline" },
				...QUALITY_METRIC_COLUMNS,
				{ key: "gateFailures", label: "Below baseline (would fail gate)" },
				{ key: "config", label: "Config snapshot" },
			],
			chart: { xKey: "day", yKey: "hitRate", yLabel: "Hit rate" },
		},
		async run(filters, page, pageSize) {
			// Comparison mode: EV-2's per-metric deltas as the result shape.
			if (filters["compareBase"] && filters["compareCandidate"]) {
				const comparison = await evalRunsService.compare(
					filters["compareBase"],
					filters["compareCandidate"]
				)
				const rows = Object.entries(comparison.deltas).map(([metric, delta]) => ({
					metric,
					base: metricValue(comparison.base.metrics, metric),
					candidate: metricValue(comparison.candidate.metrics, metric),
					delta: Number(delta.toFixed(4)),
				}))
				return {
					columns: [
						{ key: "metric", label: "Metric" },
						{ key: "base", label: `Base (${comparison.base.gitSha})` },
						{ key: "candidate", label: `Candidate (${comparison.candidate.gitSha})` },
						{ key: "delta", label: "Delta" },
					],
					rows: rows.slice((page - 1) * pageSize, page * pageSize),
					total: rows.length,
				}
			}

			const [runs, baseline] = await Promise.all([
				evalRunsService.list(),
				evalRunsService.currentBaseline(),
			])
			const filtered = runs.filter(run => {
				if (filters["kind"] && run.kind !== filters["kind"]) return false
				if (filters["status"] && run.status !== filters["status"]) return false
				const created = new Date(run.createdAt).getTime()
				if (filters["dateFrom"] && created < new Date(filters["dateFrom"]).getTime()) {
					return false
				}
				if (filters["dateTo"]) {
					const end = new Date(filters["dateTo"])
					end.setDate(end.getDate() + 1)
					if (created >= end.getTime()) return false
				}
				return true
			})
			const rows = filtered.map(run => {
				const failing = baseline
					? GATED_METRICS.filter(metric => {
							const candidate = run.metrics?.[metric]
							const base = baseline.metrics?.[metric]
							return (
								typeof candidate === "number" &&
								typeof base === "number" &&
								candidate < base - GATE_TOLERANCE
							)
						})
					: []
				return {
					createdAt: run.createdAt,
					day: new Date(run.createdAt).toISOString().slice(0, 10),
					runId: run.id,
					kind: run.kind,
					status: run.status,
					gitSha: run.gitSha,
					baseline: baseline?.runId === run.id ? "active" : "",
					...Object.fromEntries(
						GATED_METRICS.map(metric => [metric, metricValue(run.metrics, metric)])
					),
					gateFailures: failing.join(", "),
					config: JSON.stringify(run.config),
				}
			})
			return { rows: rows.slice((page - 1) * pageSize, page * pageSize), total: rows.length }
		},
	}
}

/** DP-3 / PRD NFR acceptance targets — the report's breach thresholds. */
const NFR = {
	ttftP95Seconds: 2,
	answerP95Seconds: 15,
	errorRatePct: 1,
	cacheHitRatePct: 30,
} as const

const PERF_SERIES: { key: string; label: string; query: (window: string) => string }[] = [
	{
		key: "cacheHitRatePct",
		label: "Cache hit rate %",
		query: window =>
			`100 * (sum(rate(rag_cache_lookups_total{result=~"hits|semanticHits"}[${window}])) / sum(rate(rag_cache_lookups_total[${window}])))`,
	},
	{
		key: "p50",
		label: "API p50 (s)",
		query: window =>
			`histogram_quantile(0.5, sum by (le) (rate(rag_http_request_duration_seconds_bucket[${window}])))`,
	},
	{
		key: "p95",
		label: "API p95 (s)",
		query: window =>
			`histogram_quantile(0.95, sum by (le) (rate(rag_http_request_duration_seconds_bucket[${window}])))`,
	},
	{
		key: "p99",
		label: "API p99 (s)",
		query: window =>
			`histogram_quantile(0.99, sum by (le) (rate(rag_http_request_duration_seconds_bucket[${window}])))`,
	},
	{
		key: "ttftP95",
		label: "TTFT p95 (s)",
		query: window =>
			`histogram_quantile(0.95, sum by (le) (rate(rag_chat_ttft_seconds_bucket[${window}])))`,
	},
	{
		key: "errorRatePct",
		label: "Error rate %",
		query: window =>
			`100 * (sum(rate(rag_errors_total[${window}])) / sum(rate(rag_http_requests_total[${window}])))`,
	},
	{
		key: "provider429",
		label: "Provider 429s",
		query: window => `sum(increase(rag_provider_429_total[${window}]))`,
	},
	{
		key: "shed",
		label: "Shed (bucket)",
		query: window =>
			`sum(increase(rag_provider_bucket_events_total{event="shed"}[${window}]))`,
	},
	{
		key: "dbLongestQuerySeconds",
		label: "DB longest query (s)",
		query: () => `max(rag_db_longest_query_seconds)`,
	},
]

function breachesFor(row: Record<string, number | string>): string {
	const breaches: string[] = []
	const number = (key: string) => (typeof row[key] === "number" ? (row[key] as number) : null)
	const ttft = number("ttftP95")
	if (ttft !== null && ttft > NFR.ttftP95Seconds) breaches.push(`ttftP95>${NFR.ttftP95Seconds}s`)
	const p95 = number("p95")
	if (p95 !== null && p95 > NFR.answerP95Seconds) breaches.push(`p95>${NFR.answerP95Seconds}s`)
	const errors = number("errorRatePct")
	if (errors !== null && errors >= NFR.errorRatePct) breaches.push(`errors>=${NFR.errorRatePct}%`)
	const hits = number("cacheHitRatePct")
	if (hits !== null && hits < NFR.cacheHitRatePct) breaches.push(`hitRate<${NFR.cacheHitRatePct}%`)
	return breaches.join(", ")
}

/**
 * RP-4: Cache & Performance Report — periodic decision report sourced from
 * Prometheus (OB-2 stays the single metrics truth; Grafana remains the live
 * view). Breach column + chart threshold line come from the DP-3/PRD NFR
 * acceptance targets.
 */
export function buildCachePerformanceReport(promQueryRange: PromQueryRange): ReportImpl {
	return {
		definition: {
			key: "cache-performance",
			title: "Cache & Performance",
			description:
				"Cache hit-rate, latency percentiles incl. TTFT, error rate, provider 429/shed events, and DB-vs-API latency from Prometheus. Breaches of the load-test NFR targets (TTFT p95 <= 2s, p95 <= 15s, errors < 1%, hit-rate >= 30%) are flagged per interval.",
			filters: [
				{ key: "dateFrom", label: "From", type: "date" },
				{ key: "dateTo", label: "To", type: "date" },
				{ key: "step", label: "Interval", type: "select", options: ["5m", "1h"] },
			],
			columns: [
				{ key: "time", label: "Time" },
				...PERF_SERIES.map(series => ({ key: series.key, label: series.label })),
				{ key: "breaches", label: "NFR breaches" },
			],
			chart: {
				xKey: "time",
				yKey: "ttftP95",
				yLabel: "TTFT p95 (s)",
				threshold: NFR.ttftP95Seconds,
				thresholdLabel: "NFR: TTFT p95 <= 2s",
			},
		},
		async run(filters, page, pageSize) {
			const step = filters["step"] === "5m" ? "5m" : "1h"
			const end = filters["dateTo"]
				? new Date(filters["dateTo"]).getTime() + 24 * 3600 * 1000
				: Date.now()
			const start = filters["dateFrom"]
				? new Date(filters["dateFrom"]).getTime()
				: end - 24 * 3600 * 1000
			const startSeconds = Math.floor(start / 1000)
			const endSeconds = Math.floor(end / 1000)

			const series = await Promise.all(
				PERF_SERIES.map(async definition => ({
					key: definition.key,
					values: await promQueryRange(
						definition.query(step),
						startSeconds,
						endSeconds,
						step
					),
				}))
			)
			const byTime = new Map<number, Record<string, number | string>>()
			for (const { key, values } of series) {
				for (const [timestamp, value] of values) {
					const numeric = Number.parseFloat(value)
					if (Number.isNaN(numeric)) continue
					const row = byTime.get(timestamp) ?? {}
					row[key] = Number(numeric.toFixed(4))
					byTime.set(timestamp, row)
				}
			}
			const rows = [...byTime.entries()]
				.sort(([a], [b]) => b - a)
				.map(([timestamp, values]) => ({
					time: new Date(timestamp * 1000).toISOString(),
					...Object.fromEntries(
						PERF_SERIES.map(definition => [definition.key, values[definition.key] ?? ""])
					),
					breaches: breachesFor(values),
				}))
			return { rows: rows.slice((page - 1) * pageSize, page * pageSize), total: rows.length }
		},
	}
}

/**
 * RP-5: Ingestion Status Report — per-job outcomes over the ingestion-owned
 * tables (read-only): documents by type/status, chunks produced, embedding
 * batches + IN-6 cache-hit ratio (the dedupe layer), and failed documents
 * with reasons linking to the IN-7 console for re-run.
 */
export const ingestionStatusReport: ReportImpl = {
	definition: {
		key: "ingestion-status",
		title: "Ingestion Status",
		description:
			"Ingestion jobs with documents by type/status, chunks produced, embedding batches and cache-hit ratio (IN-6 dedupe), and failed documents with reasons. Job links open the IN-7 console for re-run.",
		filters: [
			{
				key: "status",
				label: "Job status",
				type: "select",
				options: ["queued", "running", "completed", "failed"],
			},
			{ key: "docType", label: "Doc type", type: "text" },
			{ key: "dateFrom", label: "From", type: "date" },
			{ key: "dateTo", label: "To", type: "date" },
		],
		columns: [
			{ key: "createdAt", label: "When" },
			{ key: "jobId", label: "Job", linkTemplate: "/corpus?job={value}" },
			{ key: "status", label: "Status" },
			{ key: "documents", label: "Documents" },
			{ key: "docTypes", label: "By type/status" },
			{ key: "chunks", label: "Chunks" },
			{ key: "embedBatches", label: "Embed batches" },
			{ key: "cacheHitRatioPct", label: "Embed cache hit %" },
			{ key: "failedDocuments", label: "Failed docs" },
			{ key: "failureReasons", label: "Failure reasons" },
		],
		chart: { xKey: "day", yKey: "chunks", yLabel: "Chunks produced" },
	},
	async run(filters, page, pageSize) {
		const dateFrom = filters["dateFrom"] ? new Date(filters["dateFrom"]) : null
		const dateTo = filters["dateTo"]
			? new Date(new Date(filters["dateTo"]).getTime() + 24 * 3600 * 1000)
			: null
		const status = filters["status"] || null
		const docType = filters["docType"] ? `%${filters["docType"]}%` : null

		const result = await db.execute(sql`
			SELECT
				j.id,
				j.created_at,
				j.status,
				j.stats,
				j.error,
				(
					SELECT string_agg(breakdown.doc_type || '/' || breakdown.status || ': ' || breakdown.n, ', ')
					FROM (
						SELECT d.doc_type, jd.status, count(*)::int AS n
						FROM job_documents jd
						JOIN documents d ON d.id = jd.document_id
						WHERE jd.job_id = j.id
						GROUP BY d.doc_type, jd.status
					) breakdown
				) AS doc_types
			FROM ingestion_jobs j
			WHERE (${dateFrom}::timestamptz IS NULL OR j.created_at >= ${dateFrom}::timestamptz)
				AND (${dateTo}::timestamptz IS NULL OR j.created_at < ${dateTo}::timestamptz)
				AND (${status}::text IS NULL OR j.status = ${status})
				AND (${docType}::text IS NULL OR EXISTS (
					SELECT 1 FROM job_documents jd
					JOIN documents d ON d.id = jd.document_id
					WHERE jd.job_id = j.id AND d.doc_type ILIKE ${docType}
				))
			ORDER BY j.created_at DESC
		`)
		const jobRows = (result.rows as Record<string, unknown>[]).map(row => {
			const stats = (row["stats"] ?? {}) as Record<string, unknown>
			const failures = Array.isArray(stats["failures"])
				? (stats["failures"] as Record<string, unknown>[])
				: []
			const hits = Number(stats["cacheHits"] ?? 0)
			const misses = Number(stats["cacheMisses"] ?? 0)
			const lookups = hits + misses
			// Raw execute() rows come back driver-typed (string or Date).
			const createdAt = new Date(row["created_at"] as string | Date)
			const reasons = failures
				.map(failure => String(failure["error"] ?? "unknown"))
				.join("; ")
			return {
				createdAt,
				day: createdAt.toISOString().slice(0, 10),
				jobId: row["id"],
				status: row["status"],
				documents: Number(stats["documents"] ?? 0),
				docTypes: row["doc_types"] ?? "",
				chunks: Number(stats["chunks"] ?? 0),
				embedBatches: Number(stats["embedBatches"] ?? 0),
				cacheHitRatioPct: lookups > 0 ? Math.round((hits / lookups) * 100) : "",
				failedDocuments: failures.length,
				failureReasons: row["error"]
					? [String(row["error"]), reasons].filter(Boolean).join("; ")
					: reasons,
			}
		})

		// Documents that fail at PARSE never reach a job (the sync register
		// step rejects them) — surface them as their own failed rows.
		let parseRows: typeof jobRows = []
		if (!status || status === "failed") {
			const failedDocs = await db.execute(sql`
				SELECT id, ingested_at, doc_type, source_uri, error
				FROM documents
				WHERE status = 'failed'
					AND (${dateFrom}::timestamptz IS NULL OR ingested_at >= ${dateFrom}::timestamptz)
					AND (${dateTo}::timestamptz IS NULL OR ingested_at < ${dateTo}::timestamptz)
					AND (${docType}::text IS NULL OR doc_type ILIKE ${docType})
				ORDER BY ingested_at DESC
			`)
			parseRows = (failedDocs.rows as Record<string, unknown>[]).map(row => {
				const createdAt = new Date(row["ingested_at"] as string | Date)
				return {
					createdAt,
					day: createdAt.toISOString().slice(0, 10),
					jobId: "",
					status: "failed (parse)",
					documents: 1,
					docTypes: `${String(row["doc_type"])}/failed: 1`,
					chunks: 0,
					embedBatches: 0,
					cacheHitRatioPct: "" as const,
					failedDocuments: 1,
					failureReasons: `${String(row["source_uri"])}: ${String(row["error"] ?? "unknown")}`,
				}
			})
		}

		const rows = [...jobRows, ...parseRows].sort(
			(a, b) => b.createdAt.getTime() - a.createdAt.getTime()
		)
		return { rows: rows.slice((page - 1) * pageSize, page * pageSize), total: rows.length }
	},
}

/** RP-1 registry — grown through its consumers (RP-2..5). */
export function buildReports(deps: ReportDeps): Record<string, ReportImpl> {
	const reports = [
		auditTrailReport,
		usageCostReport,
		buildQualityReport(deps.evalRunsService),
		buildCachePerformanceReport(deps.promQueryRange),
		ingestionStatusReport,
	]
	return Object.fromEntries(reports.map(report => [report.definition.key, report]))
}
