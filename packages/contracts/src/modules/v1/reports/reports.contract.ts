import { oc } from "@orpc/contract"
import { z } from "zod"

/** One filter a report accepts (RP-1 framework — RP-2..5 reuse this). */
export const ReportFilterDefSchema = z.object({
	key: z.string(),
	label: z.string(),
	type: z.enum(["text", "date", "select"]),
	options: z.array(z.string()).optional(),
})

export const ReportDefinitionSchema = z.object({
	key: z.string(),
	title: z.string(),
	description: z.string(),
	filters: z.array(ReportFilterDefSchema),
	columns: z.array(z.object({ key: z.string(), label: z.string() })),
})

export const RunReportRequestSchema = z.object({
	key: z.string(),
	filters: z.record(z.string(), z.string()).default({}),
	page: z.number().int().min(1).default(1),
	pageSize: z.number().int().min(1).max(200).default(50),
})

export const ReportResultSchema = z.object({
	key: z.string(),
	columns: z.array(z.object({ key: z.string(), label: z.string() })),
	rows: z.array(z.record(z.string(), z.unknown())),
	total: z.number().int(),
	page: z.number().int(),
	pageSize: z.number().int(),
})

export const ExportReportRequestSchema = z.object({
	key: z.string(),
	filters: z.record(z.string(), z.string()).default({}),
	format: z.enum(["json", "csv"]),
})

export const ReportExportSchema = z.object({
	filename: z.string(),
	contentType: z.string(),
	/** The serialized report body — the client turns it into a download. */
	content: z.string(),
})

/**
 * Reporting (RP-1 framework + reports). Viewing requires
 * `reports.reports.view`; exporting additionally `reports.reports.export`.
 */
export const reportsContract = {
	reports: {
		list: oc
			.route({
				method: "GET",
				path: "/reports",
				summary: "Available report definitions",
				tags: ["Reports"],
			})
			.output(z.array(ReportDefinitionSchema)),
		run: oc
			.route({
				method: "POST",
				path: "/reports/{key}/run",
				summary: "Run a report with filters (paginated)",
				tags: ["Reports"],
			})
			.input(RunReportRequestSchema)
			.output(ReportResultSchema),
		export: oc
			.route({
				method: "POST",
				path: "/reports/{key}/export",
				summary: "Export the filtered report (JSON/CSV; Excel/PDF in RP-6)",
				tags: ["Reports"],
			})
			.input(ExportReportRequestSchema)
			.output(ReportExportSchema),
	},
}

export type ReportFilterDef = z.infer<typeof ReportFilterDefSchema>
export type ReportDefinition = z.infer<typeof ReportDefinitionSchema>
export type RunReportRequest = z.infer<typeof RunReportRequestSchema>
export type ReportResult = z.infer<typeof ReportResultSchema>
export type ExportReportRequest = z.infer<typeof ExportReportRequestSchema>
export type ReportExport = z.infer<typeof ReportExportSchema>
