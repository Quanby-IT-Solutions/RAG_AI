import { Injectable } from "@nestjs/common"
import { ORPCError } from "@orpc/server"

import type {
	ReportDefinition,
	ReportExport,
	ReportFormat,
	ReportResult,
} from "@repo/contracts"

import { env } from "@/config/env.config"

import { RagGatewayService } from "../corpus/rag-gateway.service"
import { EvalRunsService } from "../eval/eval-runs.service"
import { buildReports, makePromQueryRange, type ReportImpl } from "./report-definitions"

const EXPORT_ROW_CAP = 10_000

function toCsv(columns: { key: string; label: string }[], rows: Record<string, unknown>[]): string {
	const escape = (value: unknown): string => {
		const text =
			value === null || value === undefined
				? ""
				: value instanceof Date
					? value.toISOString()
					: String(value)
		return /[",\n]/.test(text) ? `"${text.replaceAll('"', '""')}"` : text
	}
	const header = columns.map(column => escape(column.label)).join(",")
	const body = rows.map(row => columns.map(column => escape(row[column.key])).join(","))
	return [header, ...body].join("\n")
}

/** RP-1 framework: run/export any registered report (RP-2..5 plug in). */
@Injectable()
export class ReportsService {
	private readonly reports: Record<string, ReportImpl>

	constructor(
		evalRunsService: EvalRunsService,
		private readonly ragGatewayService: RagGatewayService
	) {
		this.reports = buildReports({
			evalRunsService,
			promQueryRange: makePromQueryRange(env.PROMETHEUS_URL),
		})
	}

	list(): ReportDefinition[] {
		return Object.values(this.reports).map(report => report.definition)
	}

	private report(key: string) {
		const report = this.reports[key]
		if (!report) {
			throw new ORPCError("NOT_FOUND", { message: `Unknown report ${key}` })
		}
		return report
	}

	async run(
		key: string,
		filters: Record<string, string>,
		page: number,
		pageSize: number
	): Promise<ReportResult> {
		const report = this.report(key)
		const { rows, total, columns } = await report.run(filters, page, pageSize)
		return {
			key,
			columns: columns ?? report.definition.columns,
			rows,
			total,
			page,
			pageSize,
		}
	}

	/** Everything the export/schedule paths need to serialize a report. */
	async fullRun(key: string, filters: Record<string, string>) {
		const report = this.report(key)
		const { rows, columns } = await report.run(filters, 1, EXPORT_ROW_CAP)
		return {
			title: report.definition.title,
			columns: columns ?? report.definition.columns,
			rows: rows as Record<string, unknown>[],
		}
	}

	/** All exports match the filtered view: JSON/CSV serialize here; Excel and
	 * PDF render in the Python service (openpyxl/reportlab — RP-6). */
	async export(
		key: string,
		filters: Record<string, string>,
		format: ReportFormat,
		context: { userId: string; requestId: string }
	): Promise<ReportExport> {
		const { title, columns, rows } = await this.fullRun(key, filters)
		const stamp = new Date().toISOString().slice(0, 10)
		const filename = `${key}-${stamp}.${format}`
		if (format === "json") {
			return {
				filename,
				contentType: "application/json",
				content: JSON.stringify(rows, null, 2),
				encoding: "utf8",
			}
		}
		if (format === "csv") {
			return {
				filename,
				contentType: "text/csv",
				content: toCsv(columns, rows),
				encoding: "utf8",
			}
		}
		const rendered = await this.ragGatewayService.renderReport({
			title,
			columns,
			rows,
			format,
			userId: context.userId,
			requestId: context.requestId,
		})
		return {
			filename,
			contentType: rendered.contentType,
			content: rendered.contentBase64,
			encoding: "base64",
		}
	}
}
