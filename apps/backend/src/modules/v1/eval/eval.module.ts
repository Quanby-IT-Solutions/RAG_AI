import { Module } from "@nestjs/common"

import { AuditModule } from "@/common/audit/audit.module"
import { RbacModule } from "@/common/rbac/rbac.module"

import { RagGatewayService } from "../corpus/rag-gateway.service"
import { EvalGoldenService } from "./eval-golden.service"
import { EvalRunsService } from "./eval-runs.service"
import { EvalController } from "./eval.controller"

@Module({
	imports: [RbacModule, AuditModule],
	controllers: [EvalController],
	providers: [EvalGoldenService, EvalRunsService, RagGatewayService],
})
export class EvalModule {}
