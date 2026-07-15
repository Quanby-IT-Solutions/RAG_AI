import { Controller, Req } from "@nestjs/common"
import { Implement } from "@orpc/nest"
import { implement } from "@orpc/server"
import { Session, type UserSession } from "@thallesp/nestjs-better-auth"
import type { Request } from "express"

import type { ChatAnswer } from "@repo/contracts"

import { RbacService } from "@/common/rbac/rbac.service"
import { v1 } from "@/config/api-versions.config"
import { RequirePermissions } from "@/shared/decorators/require-permissions.decorator"
import { StrictThrottle } from "@/shared/decorators/strict-throttle.decorator"

import { RagGatewayService } from "../corpus/rag-gateway.service"

/**
 * Chat endpoint (Flow B, GN-1) — oRPC JSON, non-streamed (Tech Plan §5, D3).
 *
 * Requires `chat.query.submit` (explicit-authz: RbacGuard fails closed and
 * audits denials). Forwards to the Python RAG service's /internal/chat with
 * an HS256 service JWT carrying the caller principal + effective permissions,
 * under the gateway hard timeout (AbortSignal.timeout). The Python side owns
 * grounding, citations, persistence, and the RAG-action audit entry.
 */
@Controller()
export class ChatController {
	constructor(
		private readonly ragGatewayService: RagGatewayService,
		private readonly rbacService: RbacService
	) {}

	@StrictThrottle()
	@RequirePermissions("chat.query.submit")
	@Implement(v1.chat.query.submit)
	async submitQuery(
		@Session() session: UserSession,
		@Req() request: Request & { id?: string | number }
	) {
		return implement(v1.chat.query.submit).handler(async ({ input }) => {
			// Forward the caller's effective permissions inside the service JWT so
			// the Python service can attribute the audit entry to the real principal.
			const access = await this.rbacService.getUserAccess(session.user.id)
			const correlationId = String(request.id ?? crypto.randomUUID())

			const answer = await this.ragGatewayService.submitChatQuery({
				query: input.query,
				k: input.k,
				userId: session.user.id,
				permissions: access.permissions,
				requestId: correlationId,
				traceId: correlationId,
			})
			return answer as ChatAnswer
		})
	}
}
