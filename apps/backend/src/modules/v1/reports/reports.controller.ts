import { Controller } from "@nestjs/common"
import { Implement } from "@orpc/nest"
import { implement } from "@orpc/server"

import { v1 } from "@/config/api-versions.config"
import { RequirePermissions } from "@/shared/decorators/require-permissions.decorator"

import { ReportsService } from "./reports.service"

/**
 * Reporting console (RP-1). Viewing requires reports.reports.view; exports
 * additionally require reports.reports.export (RbacGuard, denials audited).
 */
@Controller()
export class ReportsController {
	constructor(private readonly reportsService: ReportsService) {}

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
	async exportReport() {
		return implement(v1.reports.reports.export).handler(async ({ input }) => {
			return this.reportsService.export(input.key, input.filters, input.format)
		})
	}
}
