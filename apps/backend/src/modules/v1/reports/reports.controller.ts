import { BadRequestException, Body, Controller, Post, Req, UseGuards } from "@nestjs/common"
import { Implement } from "@orpc/nest"
import { implement } from "@orpc/server"
import { AllowAnonymous, Session, type UserSession } from "@thallesp/nestjs-better-auth"
import type { Request } from "express"

import { v1 } from "@/config/api-versions.config"
import { RequirePermissions } from "@/shared/decorators/require-permissions.decorator"
import { ServiceTokenGuard } from "@/shared/guards/service-token.guard"

import { ReportSchedulesService } from "./report-schedules.service"
import { ReportsService } from "./reports.service"

/**
 * Reporting console (RP-1). Viewing requires reports.reports.view; exports
 * additionally require reports.reports.export; schedule management requires
 * reports.schedules.manage (RbacGuard, denials audited).
 */
@Controller()
export class ReportsController {
	constructor(
		private readonly reportsService: ReportsService,
		private readonly schedulesService: ReportSchedulesService
	) {}

	@RequirePermissions("reports.reports.view")
	@Implement(v1.reports.reports.list)
	async listReports() {
		return implement(v1.reports.reports.list).handler(async () => {
			return this.reportsService.list()
		})
	}

	@RequirePermissions("reports.reports.view")
	@Implement(v1.reports.reports.run)
	async runReport() {
		return implement(v1.reports.reports.run).handler(async ({ input }) => {
			return this.reportsService.run(input.key, input.filters, input.page, input.pageSize)
		})
	}

	@RequirePermissions("reports.reports.export")
	@Implement(v1.reports.reports.export)
	async exportReport(
		@Session() session: UserSession,
		@Req() request: Request & { id?: string | number }
	) {
		return implement(v1.reports.reports.export).handler(async ({ input }) => {
			return this.reportsService.export(input.key, input.filters, input.format, {
				userId: session.user.id,
				requestId: String(request.id ?? crypto.randomUUID()),
			})
		})
	}

	// ── RP-6: schedules ─────────────────────────────────────────────────────

	@RequirePermissions("reports.schedules.manage")
	@Implement(v1.reports.schedules.list)
	async listSchedules() {
		return implement(v1.reports.schedules.list).handler(async () => {
			return this.schedulesService.list()
		})
	}

	@RequirePermissions("reports.schedules.manage")
	@Implement(v1.reports.schedules.create)
	async createSchedule(@Session() session: UserSession) {
		return implement(v1.reports.schedules.create).handler(async ({ input }) => {
			const validKeys = this.reportsService.list().map(definition => definition.key)
			return this.schedulesService.create(session.user.id, input, validKeys)
		})
	}

	@RequirePermissions("reports.schedules.manage")
	@Implement(v1.reports.schedules.update)
	async updateSchedule(@Session() session: UserSession) {
		return implement(v1.reports.schedules.update).handler(async ({ input }) => {
			return this.schedulesService.update(session.user.id, input)
		})
	}

	@RequirePermissions("reports.schedules.manage")
	@Implement(v1.reports.schedules.delete)
	async deleteSchedule(@Session() session: UserSession) {
		return implement(v1.reports.schedules.delete).handler(async ({ input }) => {
			return this.schedulesService.delete(session.user.id, input.id)
		})
	}

	// ── RP-6: generated files ───────────────────────────────────────────────

	@RequirePermissions("reports.reports.view")
	@Implement(v1.reports.files.list)
	async listFiles() {
		return implement(v1.reports.files.list).handler(async () => {
			return this.schedulesService.listFiles()
		})
	}

	@RequirePermissions("reports.reports.view")
	@Implement(v1.reports.files.get)
	async getFile() {
		return implement(v1.reports.files.get).handler(async ({ input }) => {
			return this.schedulesService.getFile(input.id)
		})
	}

	/**
	 * RP-6 internal: the arq report scheduler fetches report rows here with an
	 * HS256 service JWT (shared secret) — the framework's queries and report
	 * definitions never leave the backend. Not session-authenticated by
	 * design; ServiceTokenGuard fails closed.
	 */
	@AllowAnonymous()
	@UseGuards(ServiceTokenGuard)
	@Post("v1/reports/internal-run")
	async internalRun(@Body() body: { key?: string; filters?: Record<string, string> }) {
		if (!body?.key) {
			throw new BadRequestException("key is required")
		}
		return this.reportsService.fullRun(body.key, body.filters ?? {})
	}
}
