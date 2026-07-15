import { Module } from "@nestjs/common"

import { RbacModule } from "@/common/rbac/rbac.module"

import { CorpusController } from "./corpus.controller"
import { CorpusService } from "./corpus.service"
import { RagGatewayService } from "./rag-gateway.service"

@Module({
	imports: [RbacModule],
	controllers: [CorpusController],
	providers: [CorpusService, RagGatewayService],
})
export class CorpusModule {}
