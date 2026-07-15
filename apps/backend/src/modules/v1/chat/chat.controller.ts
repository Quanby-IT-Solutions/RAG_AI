import { BadRequestException, Body, Controller, Post, Req, Res } from "@nestjs/common"
import { Implement } from "@orpc/nest"
import { implement } from "@orpc/server"
import { Session, type UserSession } from "@thallesp/nestjs-better-auth"
import type { Request, Response } from "express"

import { ChatQueryInputSchema, type ChatAnswer } from "@repo/contracts"

import { RbacService } from "@/common/rbac/rbac.service"
import { v1 } from "@/config/api-versions.config"
import { env } from "@/config/env.config"
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

	/**
	 * SSE streaming mode (GN-3) — a plain Nest route (oRPC is JSON-only, like
	 * the corpus upload). Pipes the RAG service's `token`/`final` event stream
	 * through unchanged; a client disconnect aborts the upstream fetch so the
	 * provider call is cancelled end to end (no orphaned spend).
	 */
	@Post("v1/chat/stream")
	@StrictThrottle()
	@RequirePermissions("chat.query.submit")
	async streamQuery(
		@Body() body: unknown,
		@Session() session: UserSession,
		@Req() request: Request & { id?: string | number },
		@Res() response: Response
	) {
		// Plain route: the global ZodValidationPipe only validates Zod DTO
		// metatypes, so validate here with the shared contract schema.
		const parsed = ChatQueryInputSchema.safeParse(body)
		if (!parsed.success) {
			throw new BadRequestException(parsed.error.issues[0]?.message ?? "Invalid chat query")
		}

		const access = await this.rbacService.getUserAccess(session.user.id)
		const correlationId = String(request.id ?? crypto.randomUUID())
		const token = this.ragGatewayService.mintServiceToken({
			sub: session.user.id,
			permissions: access.permissions,
			requestId: correlationId,
			traceId: correlationId,
		})

		// Abort the upstream call when the browser disconnects mid-stream.
		const abort = new AbortController()
		response.on("close", () => abort.abort())

		let upstream: globalThis.Response
		try {
			upstream = await fetch(`${env.RAG_SERVICE_URL}/internal/chat/stream`, {
				method: "POST",
				headers: {
					authorization: `Bearer ${token}`,
					"content-type": "application/json",
					"x-request-id": correlationId,
				},
				body: JSON.stringify(parsed.data),
				signal: abort.signal,
			})
		} catch {
			response.status(502).json({ message: "RAG service is unreachable" })
			return
		}
		if (!upstream.ok || upstream.body === null) {
			response
				.status(upstream.status >= 400 && upstream.status < 500 ? upstream.status : 502)
				.json({ message: "RAG chat stream failed" })
			return
		}

		response.status(200)
		response.setHeader("content-type", "text/event-stream")
		response.setHeader("cache-control", "no-cache")
		response.setHeader("connection", "keep-alive")
		// Disable proxy buffering (Nginx) so tokens flush as they arrive.
		response.setHeader("x-accel-buffering", "no")
		response.flushHeaders()

		try {
			for await (const chunk of upstream.body) {
				response.write(chunk)
			}
		} catch {
			// Upstream died mid-stream: emit an SSE-shaped degradation final so
			// the client never sees a silent hang (CH-2 renders it).
			if (!abort.signal.aborted) {
				response.write(
					`event: final\ndata: ${JSON.stringify({
						answer:
							"I couldn't generate an answer right now because the language model is unavailable. Please try again in a moment.",
						citations: [],
						degraded: true,
						isIDontKnow: false,
					})}\n\n`
				)
			}
		} finally {
			response.end()
		}
	}
}
