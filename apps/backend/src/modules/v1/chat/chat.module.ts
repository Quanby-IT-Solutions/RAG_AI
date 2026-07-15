import { Module } from "@nestjs/common"

import { AuditModule } from "@/common/audit/audit.module"
import { RbacModule } from "@/common/rbac/rbac.module"

import { RagGatewayService } from "../corpus/rag-gateway.service"
import { ChatHistoryService } from "./chat-history.service"
import { ChatController } from "./chat.controller"

@Module({
	imports: [RbacModule, AuditModule],
	controllers: [ChatController],
	providers: [RagGatewayService, ChatHistoryService],
})
export class ChatModule {}
