import { Module } from "@nestjs/common"

import { RbacModule } from "@/common/rbac/rbac.module"

import { RagGatewayService } from "../corpus/rag-gateway.service"
import { OpsController } from "./ops.controller"

@Module({
	imports: [RbacModule],
	controllers: [OpsController],
	providers: [RagGatewayService],
})
export class OpsModule {}
