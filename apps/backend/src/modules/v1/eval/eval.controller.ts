import { Controller, Req } from "@nestjs/common"
import { Implement } from "@orpc/nest"
import { implement } from "@orpc/server"
import { Session, type UserSession } from "@thallesp/nestjs-better-auth"
import type { Request } from "express"

import type { GoldenChunkCandidate } from "@repo/contracts"

import { RbacService } from "@/common/rbac/rbac.service"
import { v1 } from "@/config/api-versions.config"
import { RequirePermissions } from "@/shared/decorators/require-permissions.decorator"
import { StrictThrottle } from "@/shared/decorators/strict-throttle.decorator"

import { RagGatewayService, toORPCError } from "../corpus/rag-gateway.service"
import { EvalGoldenService } from "./eval-golden.service"

/**
 * Golden evaluation set console (EV-1). Every route requires
 * `eval.golden.manage` (RbacGuard fails closed and audits denials). The
 * chunk picker runs the REAL retrieval pipeline via the RAG gateway so
 * curated hashes come from what retrieval actually returns.
 */
@Controller()
export class EvalController {
	constructor(
		private readonly evalGoldenService: EvalGoldenService,
		private readonly ragGatewayService: RagGatewayService,
		private readonly rbacService: RbacService
	) {}

	@RequirePermissions("eval.golden.manage")
	@Implement(v1.eval.golden.list)
	async listGolden() {
		return implement(v1.eval.golden.list).handler(async () => {
			return this.evalGoldenService.list()
		})
	}

	@StrictThrottle()
	@RequirePermissions("eval.golden.manage")
	@Implement(v1.eval.golden.create)
	async createGolden(@Session() session: UserSession) {
		return implement(v1.eval.golden.create).handler(async ({ input }) => {
			return this.evalGoldenService.create(session.user.id, input)
		})
	}

	@StrictThrottle()
	@RequirePermissions("eval.golden.manage")
	@Implement(v1.eval.golden.update)
	async updateGolden(@Session() session: UserSession) {
		return implement(v1.eval.golden.update).handler(async ({ input }) => {
			return this.evalGoldenService.update(session.user.id, input)
		})
	}

	@RequirePermissions("eval.golden.manage")
	@Implement(v1.eval.golden.export)
	async exportGolden() {
		return implement(v1.eval.golden.export).handler(async () => {
			return this.evalGoldenService.export()
		})
	}

	@StrictThrottle()
	@RequirePermissions("eval.golden.manage")
	@Implement(v1.eval.golden.import)
	async importGolden(@Session() session: UserSession) {
		return implement(v1.eval.golden.import).handler(async ({ input }) => {
			return this.evalGoldenService.import(session.user.id, input)
		})
	}

	@RequirePermissions("eval.golden.manage")
	@Implement(v1.eval.golden.searchChunks)
	async searchChunks(
		@Session() session: UserSession,
		@Req() request: Request & { id?: string | number }
	) {
		return implement(v1.eval.golden.searchChunks).handler(async ({ input }) => {
			const access = await this.rbacService.getUserAccess(session.user.id)
			const correlationId = String(request.id ?? crypto.randomUUID())

			const result = (await this.ragGatewayService
				.retrieveChunks({
					query: input.query,
					k: input.k,
					userId: session.user.id,
					permissions: access.permissions,
					requestId: correlationId,
					traceId: correlationId,
				})
				.catch(toORPCError)) as { chunks?: GoldenChunkCandidate[] } | null

			return (result?.chunks ?? []).map(chunk => ({
				contentHash: chunk.contentHash,
				documentId: chunk.documentId,
				content: chunk.content,
				score: chunk.score ?? null,
			}))
		})
	}
}
