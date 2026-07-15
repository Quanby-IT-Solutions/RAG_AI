import { Module } from "@nestjs/common"

import { RbacModule } from "@/common/rbac/rbac.module"

import { RagGatewayService } from "../corpus/rag-gateway.service"
import { ChatController } from "./chat.controller"

@Module({
	imports: [RbacModule],
	controllers: [ChatController],
	providers: [RagGatewayService],
})
export class ChatModule {}
