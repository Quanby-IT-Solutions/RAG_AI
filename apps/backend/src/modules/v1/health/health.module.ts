import { Module } from "@nestjs/common"

import { HealthController } from "@/modules/v1/health/health.controller"
import { HealthService } from "@/modules/v1/health/health.service"

@Module({
	controllers: [HealthController],
	providers: [HealthService],
})
export class HealthModule {}
