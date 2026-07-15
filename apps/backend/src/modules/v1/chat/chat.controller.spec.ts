import { ForbiddenException, type ExecutionContext } from "@nestjs/common"
import { Reflector } from "@nestjs/core"
import { Test, type TestingModule } from "@nestjs/testing"

import { type AuditService } from "@/common/audit/audit.service"
import { RbacService } from "@/common/rbac/rbac.service"
import { RbacGuard } from "@/shared/guards/rbac.guard"
import { REQUIRED_PERMISSIONS_KEY } from "@/shared/decorators/require-permissions.decorator"

import { RagGatewayService } from "../corpus/rag-gateway.service"
import { ChatController } from "./chat.controller"

const mockChatInput = { query: "How many vacation days?", k: 2 }

jest.mock("@orpc/nest", () => ({
	Implement: () => () => undefined,
}))

jest.mock("@orpc/server", () => ({
	implement: jest.fn(() => ({
		handler: jest.fn((fn: (ctx: { input: typeof mockChatInput }) => unknown) =>
			fn({ input: mockChatInput })
		),
	})),
}))

jest.mock("@/config/api-versions.config", () => ({
	v1: { chat: { query: { submit: {} } } },
}))

jest.mock("@/config/env.config", () => ({
	env: {
		NODE_ENV: "test",
		RAG_SERVICE_URL: "http://rag:8000",
		RAG_SERVICE_TOKEN_SECRET: "test-secret",
		RAG_SERVICE_TIMEOUT_MS: 5000,
		RAG_SERVICE_TOKEN_TTL_SECONDS: 120,
	},
}))

jest.mock("@/common/database/database.client", () => ({
	db: {},
}))

jest.mock("@/common/rbac/rbac.service", () => ({
	RbacService: jest.fn(),
}))

jest.mock("@/common/audit/audit.service", () => ({
	AuditService: jest.fn(),
}))

jest.mock("@thallesp/nestjs-better-auth", () => ({
	Session: () => () => undefined,
}))

// ESM modules pulled in by RbacGuard's session-resolution fallback.
jest.mock("better-auth/node", () => ({
	fromNodeHeaders: jest.fn(() => ({})),
}))
jest.mock("@repo/auth", () => ({
	getAuth: jest.fn(() => ({ api: { getSession: jest.fn().mockResolvedValue(null) } })),
}))

describe("ChatController", () => {
	let controller: ChatController
	let rbacService: { getUserAccess: jest.Mock }
	let ragGatewayService: { submitChatQuery: jest.Mock }

	beforeEach(async () => {
		rbacService = { getUserAccess: jest.fn() }
		ragGatewayService = { submitChatQuery: jest.fn() }

		const module: TestingModule = await Test.createTestingModule({
			controllers: [ChatController],
			providers: [
				{ provide: RbacService, useValue: rbacService },
				{ provide: RagGatewayService, useValue: ragGatewayService },
			],
		}).compile()

		controller = module.get(ChatController)
	})

	describe("authorization metadata (fail-closed key)", () => {
		it("requires chat.query.submit on the query route (explicit authz)", () => {
			const required = Reflect.getMetadata(
				REQUIRED_PERMISSIONS_KEY,
				ChatController.prototype.submitQuery
			)
			expect(required).toEqual(["chat.query.submit"])
		})
	})

	describe("RbacGuard enforcement (403 + audited denial)", () => {
		const buildContext = (): ExecutionContext =>
			({
				getHandler: () => ChatController.prototype.submitQuery,
				getClass: () => ChatController,
				switchToHttp: () => ({
					getRequest: () => ({
						headers: {},
						method: "POST",
						originalUrl: "/api/v1/chat/query",
						session: { user: { id: "user-1" } },
					}),
				}),
			}) as unknown as ExecutionContext

		it("denies with 403 and audits the denial when chat.query.submit is missing", async () => {
			const guardRbac = {
				hasAllPermissions: jest.fn().mockResolvedValue(false),
				getMissingPermissions: jest.fn().mockResolvedValue(["chat.query.submit"]),
			}
			const auditService = { writeAudit: jest.fn().mockResolvedValue(undefined) }
			const guard = new RbacGuard(
				new Reflector(),
				guardRbac as unknown as RbacService,
				auditService as unknown as AuditService
			)

			await expect(guard.canActivate(buildContext())).rejects.toBeInstanceOf(ForbiddenException)

			expect(guardRbac.hasAllPermissions).toHaveBeenCalledWith("user-1", ["chat.query.submit"])
			// Flow E: the denial lands on the append-only audit spine.
			expect(auditService.writeAudit).toHaveBeenCalledWith({
				actorId: "user-1",
				actorType: "system",
				action: "rbac.denied",
				resourceType: "route",
				resourceId: "POST /api/v1/chat/query",
				after: { requiredPermissions: ["chat.query.submit"] },
			})
		})

		it("allows the request when the permission is granted (no audit entry)", async () => {
			const guardRbac = {
				hasAllPermissions: jest.fn().mockResolvedValue(true),
				getMissingPermissions: jest.fn(),
			}
			const auditService = { writeAudit: jest.fn() }
			const guard = new RbacGuard(
				new Reflector(),
				guardRbac as unknown as RbacService,
				auditService as unknown as AuditService
			)

			await expect(guard.canActivate(buildContext())).resolves.toBe(true)
			expect(auditService.writeAudit).not.toHaveBeenCalled()
		})
	})

	describe("submitQuery", () => {
		const session = { user: { id: "user-1" } } as never
		const request = { id: "req-42" } as never

		it("forwards the query with the caller's principal + permissions and correlation id", async () => {
			rbacService.getUserAccess.mockResolvedValue({
				roles: ["End User"],
				permissions: ["chat.query.submit"],
			})
			const answer = {
				answer: "20 days [1].",
				citations: [],
				conversationId: "c1",
				messageId: "m1",
				traceId: "req-42",
				model: "gpt-4o-mini",
				tokenUsage: null,
				isIDontKnow: false,
				degraded: false,
			}
			ragGatewayService.submitChatQuery.mockResolvedValue(answer)

			await expect(controller.submitQuery(session, request)).resolves.toEqual(answer)

			expect(rbacService.getUserAccess).toHaveBeenCalledWith("user-1")
			expect(ragGatewayService.submitChatQuery).toHaveBeenCalledWith({
				query: "How many vacation days?",
				k: 2,
				userId: "user-1",
				permissions: ["chat.query.submit"],
				requestId: "req-42",
				traceId: "req-42",
			})
		})

		it("generates a correlation id when the request has none", async () => {
			rbacService.getUserAccess.mockResolvedValue({ roles: [], permissions: [] })
			ragGatewayService.submitChatQuery.mockResolvedValue({})

			await controller.submitQuery(session, { id: undefined } as never)

			const [forwarded] = ragGatewayService.submitChatQuery.mock.calls[0] as [
				{ requestId: string; traceId: string },
			]
			expect(forwarded.requestId).toMatch(/[0-9a-f-]{36}/)
			expect(forwarded.traceId).toBe(forwarded.requestId)
		})
	})
})
