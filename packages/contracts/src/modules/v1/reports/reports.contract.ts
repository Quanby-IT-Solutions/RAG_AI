import { oc } from "@orpc/contract"
import { z } from "zod"

/** One filter a report accepts (RP-1 framework — RP-2..5 reuse this). */
export const ReportFilterDefSchema = z.object({
	key: z.string(),
	label: z.string(),
	type: z.enum(["text", "date", "select"]),
	options: z.array(z.string()).optional(),
})

/** A result column; `linkTemplate` renders the cell as a link with
 * `{value}` substituted (RP-5: failed docs link to the IN-7 console). */
export const ReportColumnSchema = z.object({
	key: z.string(),
	label: z.string(),
	linkTemplate: z.string().optional(),
})

export const ReportDefinitionSchema = z.object({
	key: z.string(),
	title: z.string(),
	description: z.string(),
	filters: z.array(ReportFilterDefSchema),
	columns: z.array(ReportColumnSchema),
	/** Optional bar-chart hint rendered above the table (RP-2 charts).
	 * `threshold` overlays a horizontal line (RP-4 NFR breach visibility). */
	chart: z
		.object({
			xKey: z.string(),
			yKey: z.string(),
			yLabel: z.string(),
			threshold: z.number().optional(),
			thresholdLabel: z.string().optional(),
		})
		.optional(),
})

export const RunReportRequestSchema = z.object({
	key: z.string(),
	filters: z.record(z.string(), z.string()).default({}),
	page: z.number().int().min(1).default(1),
	pageSize: z.number().int().min(1).max(200).default(50),
})

export const ReportResultSchema = z.object({
	key: z.string(),
	columns: z.array(ReportColumnSchema),
	rows: z.array(z.record(z.string(), z.unknown())),
	total: z.number().int(),
	page: z.number().int(),
	pageSize: z.number().int(),
})

export const ReportFormatSchema = z.enum(["json", "csv", "xlsx", "pdf"])

export const ExportReportRequestSchema = z.object({
	key: z.string(),
	filters: z.record(z.string(), z.string()).default({}),
	format: ReportFormatSchema,
})

export const ReportExportSchema = z.object({
	filename: z.string(),
	contentType: z.string(),
	/** The serialized report body — the client turns it into a download.
	 * utf8 for json/csv; base64 for binary formats (xlsx/pdf). */
	content: z.string(),
	encoding: z.enum(["utf8", "base64"]).default("utf8"),
})

const CRON_RE = /^\S+ \S+ \S+ \S+ \S+$/

// ── RP-6: scheduled generation ──────────────────────────────────────────────

export const ReportScheduleSchema = z.object({
	id: z.string(),
	reportKey: z.string(),
	cron: z.string(),
	filters: z.record(z.string(), z.string()),
	formats: z.array(ReportFormatSchema),
	recipients: z.array(z.string()),
	enabled: z.boolean(),
	createdBy: z.string(),
	createdAt: z.coerce.date(),
	updatedAt: z.coerce.date(),
	lastRunAt: z.coerce.date().nullable(),
})

export const CreateReportScheduleSchema = z.object({
	reportKey: z.string().min(1),
	cron: z
		.string()
		.regex(CRON_RE, "Standard 5-field cron expression required"),
	filters: z.record(z.string(), z.string()).default({}),
	formats: z.array(ReportFormatSchema).min(1),
	recipients: z.array(z.string().email()).default([]),
	enabled: z.boolean().default(true),
})

export const UpdateReportScheduleSchema = CreateReportScheduleSchema.partial().extend({
	id: z.string(),
})

export const ReportFileSummarySchema = z.object({
	id: z.string(),
	scheduleId: z.string().nullable(),
	reportKey: z.string(),
	format: ReportFormatSchema,
	filename: z.string(),
	contentType: z.string(),
	generatedBy: z.string(),
	createdAt: z.coerce.date(),
})

export const ReportFileSchema = ReportFileSummarySchema.extend({
	/** base64-encoded body for every format (uniform download path). */
	contentBase64: z.string(),
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
				summary: "Export the filtered report (JSON/CSV/Excel/PDF)",
				tags: ["Reports"],
			})
			.input(ExportReportRequestSchema)
			.output(ReportExportSchema),
	},
	schedules: {
		list: oc
			.route({
				method: "GET",
				path: "/reports-schedules",
				summary: "List report schedules",
				tags: ["Reports"],
			})
			.output(z.array(ReportScheduleSchema)),
		create: oc
			.route({
				method: "POST",
				path: "/reports-schedules",
				summary: "Create a report schedule",
				tags: ["Reports"],
			})
			.input(CreateReportScheduleSchema)
			.output(ReportScheduleSchema),
		update: oc
			.route({
				method: "PATCH",
				path: "/reports-schedules/{id}",
				summary: "Update a report schedule",
				tags: ["Reports"],
			})
			.input(UpdateReportScheduleSchema)
			.output(ReportScheduleSchema),
		delete: oc
			.route({
				method: "DELETE",
				path: "/reports-schedules/{id}",
				summary: "Delete a report schedule",
				tags: ["Reports"],
			})
			.input(z.object({ id: z.string() }))
			.output(z.object({ deleted: z.boolean() })),
	},
	files: {
		list: oc
			.route({
				method: "GET",
				path: "/reports-files",
				summary: "Generated report files (newest first)",
				tags: ["Reports"],
			})
			.output(z.array(ReportFileSummarySchema)),
		get: oc
			.route({
				method: "GET",
				path: "/reports-files/{id}",
				summary: "One generated file with its content",
				tags: ["Reports"],
			})
			.input(z.object({ id: z.string() }))
			.output(ReportFileSchema),
	},
}

export type ReportFilterDef = z.infer<typeof ReportFilterDefSchema>
export type ReportDefinition = z.infer<typeof ReportDefinitionSchema>
export type RunReportRequest = z.infer<typeof RunReportRequestSchema>
export type ReportResult = z.infer<typeof ReportResultSchema>
export type ExportReportRequest = z.infer<typeof ExportReportRequestSchema>
export type ReportExport = z.infer<typeof ReportExportSchema>
export type ReportFormat = z.infer<typeof ReportFormatSchema>
export type ReportSchedule = z.infer<typeof ReportScheduleSchema>
export type CreateReportSchedule = z.infer<typeof CreateReportScheduleSchema>
export type UpdateReportSchedule = z.infer<typeof UpdateReportScheduleSchema>
export type ReportFileSummary = z.infer<typeof ReportFileSummarySchema>
export type ReportFile = z.infer<typeof ReportFileSchema>
