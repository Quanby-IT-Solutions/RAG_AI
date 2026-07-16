import { Injectable } from "@nestjs/common"
import { ORPCError } from "@orpc/server"
import { desc, eq } from "drizzle-orm"

import type {
	CreateReportSchedule,
	ReportFile,
	ReportFileSummary,
	ReportSchedule,
	UpdateReportSchedule,
} from "@repo/contracts"
import { reportFiles, reportSchedules } from "@repo/db/schema"

import { AuditService } from "@/common/audit/audit.service"
import { db } from "@/common/database/database.client"

type ScheduleRow = typeof reportSchedules.$inferSelect

/**
 * RP-6: report schedule CRUD (audited; the arq worker executes due schedules)
 * plus reads over the worker-generated report_files artifacts.
 */
@Injectable()
export class ReportSchedulesService {
	constructor(private readonly auditService: AuditService) {}

	private toSchedule(row: ScheduleRow): ReportSchedule {
		return {
			id: row.id,
			reportKey: row.reportKey,
			cron: row.cron,
			filters: row.filters,
			formats: row.formats as ReportSchedule["formats"],
			recipients: row.recipients,
			enabled: row.enabled,
			createdBy: row.createdBy,
			createdAt: row.createdAt,
			updatedAt: row.updatedAt,
			lastRunAt: row.lastRunAt,
		}
	}

	async list(): Promise<ReportSchedule[]> {
		const rows = await db
			.select()
			.from(reportSchedules)
			.orderBy(desc(reportSchedules.createdAt))
		return rows.map(row => this.toSchedule(row))
	}

	private async get(id: string): Promise<ScheduleRow> {
		const [row] = await db.select().from(reportSchedules).where(eq(reportSchedules.id, id))
		if (!row) {
			throw new ORPCError("NOT_FOUND", { message: `Schedule ${id} not found` })
		}
		return row
	}

	async create(
		userId: string,
		input: CreateReportSchedule,
		validKeys: readonly string[]
	): Promise<ReportSchedule> {
		if (!validKeys.includes(input.reportKey)) {
			throw new ORPCError("BAD_REQUEST", { message: `Unknown report ${input.reportKey}` })
		}
		const [row] = await db
			.insert(reportSchedules)
			.values({ ...input, createdBy: userId })
			.returning()
		if (!row) {
			throw new ORPCError("INTERNAL_SERVER_ERROR", { message: "Insert returned no row" })
		}
		await this.auditService.writeAudit({
			actorId: userId,
			actorType: "user",
			action: "reports.schedule.create",
			resourceType: "report_schedule",
			resourceId: row.id,
			before: null,
			after: { reportKey: row.reportKey, cron: row.cron, formats: row.formats },
		})
		return this.toSchedule(row)
	}

	async update(userId: string, input: UpdateReportSchedule): Promise<ReportSchedule> {
		const { id, ...changes } = input
		const before = await this.get(id)
		const [row] = await db
			.update(reportSchedules)
			.set({ ...changes, updatedAt: new Date() })
			.where(eq(reportSchedules.id, id))
			.returning()
		if (!row) {
			throw new ORPCError("NOT_FOUND", { message: `Schedule ${id} not found` })
		}
		await this.auditService.writeAudit({
			actorId: userId,
			actorType: "user",
			action: "reports.schedule.update",
			resourceType: "report_schedule",
			resourceId: id,
			before: { cron: before.cron, enabled: before.enabled, formats: before.formats },
			after: { cron: row.cron, enabled: row.enabled, formats: row.formats },
		})
		return this.toSchedule(row)
	}

	async delete(userId: string, id: string): Promise<{ deleted: boolean }> {
		const before = await this.get(id)
		await db.delete(reportSchedules).where(eq(reportSchedules.id, id))
		await this.auditService.writeAudit({
			actorId: userId,
			actorType: "user",
			action: "reports.schedule.delete",
			resourceType: "report_schedule",
			resourceId: id,
			before: { reportKey: before.reportKey, cron: before.cron },
			after: null,
		})
		return { deleted: true }
	}

	// ── generated artifacts ─────────────────────────────────────────────────

	async listFiles(): Promise<ReportFileSummary[]> {
		const rows = await db
			.select({
				id: reportFiles.id,
				scheduleId: reportFiles.scheduleId,
				reportKey: reportFiles.reportKey,
				format: reportFiles.format,
				filename: reportFiles.filename,
				contentType: reportFiles.contentType,
				generatedBy: reportFiles.generatedBy,
				createdAt: reportFiles.createdAt,
			})
			.from(reportFiles)
			.orderBy(desc(reportFiles.createdAt))
			.limit(200)
		return rows as ReportFileSummary[]
	}

	async getFile(id: string): Promise<ReportFile> {
		const [row] = await db.select().from(reportFiles).where(eq(reportFiles.id, id))
		if (!row) {
			throw new ORPCError("NOT_FOUND", { message: `Report file ${id} not found` })
		}
		return row as ReportFile
	}
}
