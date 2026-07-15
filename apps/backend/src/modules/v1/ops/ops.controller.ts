import { Controller, Req } from "@nestjs/common"
import { Implement } from "@orpc/nest"
import { implement } from "@orpc/server"
import { Session, type UserSession } from "@thallesp/nestjs-better-auth"
import type { Request } from "express"

import type { EffectiveConfig } from "@repo/contracts"

import { RbacService } from "@/common/rbac/rbac.service"
import { v1 } from "@/config/api-versions.config"
import { RequirePermissions } from "@/shared/decorators/require-permissions.decorator"

import { RagGatewayService, toORPCError } from "../corpus/rag-gateway.service"

/**
 * Ops console (OB-5): read-only configuration review. Secrets are masked in
 * the Python service (by TYPE) — this layer never sees them. RbacGuard fails
 * closed on ops.config.view and audits denials.
 */
@Controller()
export class OpsController {
	constructor(
		private readonly ragGatewayService: RagGatewayService,
		private readonly rbacService: RbacService
	) {}

	@RequirePermissions("ops.config.view")
	@Implement(v1.ops.config.get)
	async getConfig(
		@Session() session: UserSession,
		@Req() request: Request & { id?: string | number }
	) {
		return implement(v1.ops.config.get).handler(async () => {
			const access = await this.rbacService.getUserAccess(session.user.id)
			const correlationId = String(request.id ?? crypto.randomUUID())
			const config = await this.ragGatewayService
				.fetchConfig({
					userId: session.user.id,
					permissions: access.permissions,
					requestId: correlationId,
					traceId: correlationId,
				})
				.catch(toORPCError)
			return config as EffectiveConfig
		})
	}
}
