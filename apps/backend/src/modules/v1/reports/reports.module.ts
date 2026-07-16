import { Module } from "@nestjs/common"

import { AuditModule } from "@/common/audit/audit.module"
import { RbacModule } from "@/common/rbac/rbac.module"
import { ServiceTokenGuard } from "@/shared/guards/service-token.guard"

import { RagGatewayService } from "../corpus/rag-gateway.service"
import { EvalModule } from "../eval/eval.module"
import { ReportSchedulesService } from "./report-schedules.service"
import { ReportsController } from "./reports.controller"
import { ReportsService } from "./reports.service"

@Module({
	imports: [RbacModule, EvalModule, AuditModule],
	controllers: [ReportsController],
	providers: [ReportsService, ReportSchedulesService, RagGatewayService, ServiceTokenGuard],
})
export class ReportsModule {}
