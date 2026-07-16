import { Module } from "@nestjs/common"

import { RbacModule } from "@/common/rbac/rbac.module"

import { ReportsController } from "./reports.controller"
import { ReportsService } from "./reports.service"

@Module({
	imports: [RbacModule],
	controllers: [ReportsController],
	providers: [ReportsService],
})
export class ReportsModule {}
