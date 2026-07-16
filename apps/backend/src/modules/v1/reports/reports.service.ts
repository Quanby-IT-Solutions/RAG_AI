import { Injectable } from "@nestjs/common"
import { ORPCError } from "@orpc/server"

import type { ReportDefinition, ReportExport, ReportResult } from "@repo/contracts"

import { REPORTS } from "./report-definitions"

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
	list(): ReportDefinition[] {
		return Object.values(REPORTS).map(report => report.definition)
	}

	private report(key: string) {
		const report = REPORTS[key]
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
		const { rows, total } = await report.run(filters, page, pageSize)
		return { key, columns: report.definition.columns, rows, total, page, pageSize }
	}

	/** JSON/CSV exports match the filtered view (Excel/PDF land in RP-6). */
	async export(
		key: string,
		filters: Record<string, string>,
		format: "json" | "csv"
	): Promise<ReportExport> {
		const report = this.report(key)
		const { rows } = await report.run(filters, 1, EXPORT_ROW_CAP)
		const stamp = new Date().toISOString().slice(0, 10)
		if (format === "json") {
			return {
				filename: `${key}-${stamp}.json`,
				contentType: "application/json",
				content: JSON.stringify(rows, null, 2),
			}
		}
		return {
			filename: `${key}-${stamp}.csv`,
			contentType: "text/csv",
			content: toCsv(report.definition.columns, rows as Record<string, unknown>[]),
		}
	}
}
