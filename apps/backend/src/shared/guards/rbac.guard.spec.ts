import { ForbiddenException, UnauthorizedException, type ExecutionContext } from "@nestjs/common"
import { Reflector } from "@nestjs/core"
import { Test, type TestingModule } from "@nestjs/testing"

import { AuditService } from "@/common/audit/audit.service"
import { RbacService } from "@/common/rbac/rbac.service"

import { RbacGuard } from "./rbac.guard"

jest.mock("@/common/rbac/rbac.service", () => ({
	RbacService: jest.fn().mockImplementation(() => ({
		hasAllPermissions: jest.fn(),
		getMissingPermissions: jest.fn(),
	})),
}))

jest.mock("@/common/audit/audit.service", () => ({
	AuditService: jest.fn().mockImplementation(() => ({
		writeAudit: jest.fn().mockResolvedValue(undefined),
	})),
}))

jest.mock("@/config/env.config", () => ({
	env: { NODE_ENV: "development" },
}))

// better-auth/node and @repo/auth are ESM; mock them so Jest doesn't try to
// parse the real modules and so the guard's session-resolution fallback is
// deterministic (returns no session unless a test overrides it).
const mockGetSession = jest.fn().mockResolvedValue(null)
jest.mock("better-auth/node", () => ({
	fromNodeHeaders: jest.fn(() => ({})),
}))
jest.mock("@repo/auth", () => ({
	getAuth: jest.fn(() => ({ api: { getSession: mockGetSession } })),
}))

const buildContext = (session?: unknown, user?: unknown): ExecutionContext =>
	({
		getHandler: () => () => undefined,
		getClass: () => class {},
		switchToHttp: () => ({
			getRequest: () => ({
				session,
				user,
				method: "POST",
				originalUrl: "/api/v1/rbac/users/u2/roles",
			}),
		}),
	}) as unknown as ExecutionContext

// Verified request shapes (source: @thallesp/nestjs-better-auth v2.2.0 README,
// "Request Object Access"). The auth middleware attaches both of these after a
// session resolves:
//   - request.session → full UserSession ({ user: { id, ... }, session: { ... } })
//   - request.user    → direct reference to request.session.user
// The guard prefers request.session?.user?.id (matches the @Session() decorator
// shape used in controllers) and falls back to request.user?.id.
describe("RbacGuard", () => {
	let guard: RbacGuard
	let reflector: { getAllAndOverride: jest.Mock }
	let rbacService: { hasAllPermissions: jest.Mock; getMissingPermissions: jest.Mock }
	let auditService: { writeAudit: jest.Mock }
	const env = require("@/config/env.config").env as { NODE_ENV: string }

	beforeEach(async () => {
		env.NODE_ENV = "development"
		mockGetSession.mockReset()
		mockGetSession.mockResolvedValue(null)

		const app: TestingModule = await Test.createTestingModule({
			providers: [RbacGuard, Reflector, RbacService, AuditService],
		})
			.overrideProvider(Reflector)
			.useValue({ getAllAndOverride: jest.fn() })
			.compile()

		guard = app.get(RbacGuard)
		reflector = app.get(Reflector) as unknown as { getAllAndOverride: jest.Mock }
		rbacService = app.get(RbacService) as unknown as {
			hasAllPermissions: jest.Mock
			getMissingPermissions: jest.Mock
		}
		auditService = app.get(AuditService) as unknown as { writeAudit: jest.Mock }
	})

	it("allows when no metadata present (no-op)", async () => {
		reflector.getAllAndOverride.mockReturnValue(undefined)

		await expect(guard.canActivate(buildContext())).resolves.toBe(true)
		expect(rbacService.hasAllPermissions).not.toHaveBeenCalled()
	})

	it("allows when permissions array is empty (no-op)", async () => {
		reflector.getAllAndOverride.mockReturnValue([])

		await expect(guard.canActivate(buildContext())).resolves.toBe(true)
		expect(rbacService.hasAllPermissions).not.toHaveBeenCalled()
	})

	it("throws UnauthorizedException when no session on protected route", async () => {
		reflector.getAllAndOverride.mockReturnValue(["users.roles.manage"])

		await expect(guard.canActivate(buildContext(undefined))).rejects.toBeInstanceOf(
			UnauthorizedException
		)
		expect(rbacService.hasAllPermissions).not.toHaveBeenCalled()
		expect(auditService.writeAudit).not.toHaveBeenCalled()
	})

	it("throws UnauthorizedException when session is absent and user is absent", async () => {
		reflector.getAllAndOverride.mockReturnValue(["users.roles.manage"])

		await expect(guard.canActivate(buildContext(undefined, undefined))).rejects.toBeInstanceOf(
			UnauthorizedException
		)
		expect(rbacService.hasAllPermissions).not.toHaveBeenCalled()
	})

	it("throws ForbiddenException when user id is on req.user (no req.session)", async () => {
		reflector.getAllAndOverride.mockReturnValue(["users.roles.manage"])
		rbacService.hasAllPermissions.mockResolvedValue(false)

		await expect(guard.canActivate(buildContext(undefined, { id: "u1" }))).rejects.toBeInstanceOf(
			ForbiddenException
		)
		expect(rbacService.hasAllPermissions).toHaveBeenCalledWith("u1", ["users.roles.manage"])
	})

	it("allows when user id is on req.user and has permission", async () => {
		reflector.getAllAndOverride.mockReturnValue(["users.roles.manage"])
		rbacService.hasAllPermissions.mockResolvedValue(true)

		await expect(guard.canActivate(buildContext(undefined, { id: "u1" }))).resolves.toBe(true)
	})

	it("prefers req.session.user.id over req.user.id when both present", async () => {
		reflector.getAllAndOverride.mockReturnValue(["users.roles.manage"])
		rbacService.hasAllPermissions.mockResolvedValue(true)

		await expect(
			guard.canActivate(buildContext({ user: { id: "session-u" } }, { id: "user-u" }))
		).resolves.toBe(true)
		expect(rbacService.hasAllPermissions).toHaveBeenCalledWith("session-u", ["users.roles.manage"])
	})

	it("throws ForbiddenException when authenticated but missing permission", async () => {
		reflector.getAllAndOverride.mockReturnValue(["users.roles.manage"])
		rbacService.hasAllPermissions.mockResolvedValue(false)

		await expect(guard.canActivate(buildContext({ user: { id: "u1" } }))).rejects.toBeInstanceOf(
			ForbiddenException
		)
		expect(rbacService.hasAllPermissions).toHaveBeenCalledWith("u1", ["users.roles.manage"])
	})

	it("allows when authenticated and has permission", async () => {
		reflector.getAllAndOverride.mockReturnValue(["users.roles.manage"])
		rbacService.hasAllPermissions.mockResolvedValue(true)

		await expect(guard.canActivate(buildContext({ user: { id: "u1" } }))).resolves.toBe(true)
	})

	it("does not audit when access is granted", async () => {
		reflector.getAllAndOverride.mockReturnValue(["users.roles.manage"])
		rbacService.hasAllPermissions.mockResolvedValue(true)

		await guard.canActivate(buildContext({ user: { id: "u1" } }))
		expect(auditService.writeAudit).not.toHaveBeenCalled()
	})

	it("writes an audit denial entry and returns 403 when permission is missing (Flow E)", async () => {
		reflector.getAllAndOverride.mockReturnValue(["users.roles.manage"])
		rbacService.hasAllPermissions.mockResolvedValue(false)
		rbacService.getMissingPermissions.mockResolvedValue(["users.roles.manage"])

		await expect(guard.canActivate(buildContext({ user: { id: "u1" } }))).rejects.toBeInstanceOf(
			ForbiddenException
		)
		expect(auditService.writeAudit).toHaveBeenCalledWith({
			actorId: "u1",
			actorType: "system",
			action: "rbac.denied",
			resourceType: "route",
			resourceId: "POST /api/v1/rbac/users/u2/roles",
			after: { requiredPermissions: ["users.roles.manage"] },
		})
	})

	it("denies + audits an upload when EITHER combined corpus key is missing (IN-1 fail-closed)", async () => {
		// The upload route requires BOTH corpus.documents.upload AND
		// corpus.ingest.run (C4). AND semantics: holding one is not enough.
		reflector.getAllAndOverride.mockReturnValue(["corpus.documents.upload", "corpus.ingest.run"])
		rbacService.hasAllPermissions.mockResolvedValue(false)
		rbacService.getMissingPermissions.mockResolvedValue(["corpus.ingest.run"])

		await expect(guard.canActivate(buildContext({ user: { id: "u1" } }))).rejects.toBeInstanceOf(
			ForbiddenException
		)
		expect(rbacService.hasAllPermissions).toHaveBeenCalledWith("u1", [
			"corpus.documents.upload",
			"corpus.ingest.run",
		])
		expect(auditService.writeAudit).toHaveBeenCalledWith(
			expect.objectContaining({
				actorId: "u1",
				action: "rbac.denied",
				resourceType: "route",
				after: { requiredPermissions: ["corpus.documents.upload", "corpus.ingest.run"] },
			})
		)
	})

	it("still returns 403 when the denial audit write fails", async () => {
		reflector.getAllAndOverride.mockReturnValue(["users.roles.manage"])
		rbacService.hasAllPermissions.mockResolvedValue(false)
		rbacService.getMissingPermissions.mockResolvedValue(["users.roles.manage"])
		auditService.writeAudit.mockRejectedValue(new Error("db down"))

		await expect(guard.canActivate(buildContext({ user: { id: "u1" } }))).rejects.toBeInstanceOf(
			ForbiddenException
		)
	})

	it("resolves the session itself when not yet attached to the request", async () => {
		// Simulates RbacGuard running before the better-auth AuthGuard: no session
		// on the request, but getSession resolves one from the headers.
		reflector.getAllAndOverride.mockReturnValue(["users.roles.manage"])
		rbacService.hasAllPermissions.mockResolvedValue(true)
		mockGetSession.mockResolvedValue({ user: { id: "resolved-u" } })

		await expect(guard.canActivate(buildContext(undefined, undefined))).resolves.toBe(true)
		expect(mockGetSession).toHaveBeenCalled()
		expect(rbacService.hasAllPermissions).toHaveBeenCalledWith("resolved-u", ["users.roles.manage"])
	})

	it("includes missingPermissions in non-production 403 response", async () => {
		env.NODE_ENV = "development"
		reflector.getAllAndOverride.mockReturnValue(["users.roles.manage"])
		rbacService.hasAllPermissions.mockResolvedValue(false)
		rbacService.getMissingPermissions.mockResolvedValue(["users.roles.manage"])

		try {
			await guard.canActivate(buildContext({ user: { id: "u1" } }))
			fail("expected ForbiddenException")
		} catch (error) {
			expect(error).toBeInstanceOf(ForbiddenException)
			const response = (error as ForbiddenException).getResponse()
			expect(response).toMatchObject({
				message: "Forbidden",
				missingPermissions: ["users.roles.manage"],
			})
		}
	})

	it("reports only unmet permissions when some required permissions are satisfied", async () => {
		env.NODE_ENV = "development"
		reflector.getAllAndOverride.mockReturnValue([
			"corpus.documents.upload",
			"corpus.ingest.run",
			"corpus.documents.read",
		])
		rbacService.hasAllPermissions.mockResolvedValue(false)
		// User has corpus.documents.upload but lacks the other two.
		rbacService.getMissingPermissions.mockResolvedValue([
			"corpus.ingest.run",
			"corpus.documents.read",
		])

		try {
			await guard.canActivate(buildContext({ user: { id: "u1" } }))
			fail("expected ForbiddenException")
		} catch (error) {
			expect(error).toBeInstanceOf(ForbiddenException)
			const response = (error as ForbiddenException).getResponse()
			expect(response).toMatchObject({
				message: "Forbidden",
				missingPermissions: ["corpus.ingest.run", "corpus.documents.read"],
			})
			expect((response as Record<string, unknown>).missingPermissions).not.toContain(
				"corpus.documents.upload"
			)
			expect(rbacService.getMissingPermissions).toHaveBeenCalledWith("u1", [
				"corpus.documents.upload",
				"corpus.ingest.run",
				"corpus.documents.read",
			])
		}
	})

	it("returns generic 403 message in production", async () => {
		env.NODE_ENV = "production"
		reflector.getAllAndOverride.mockReturnValue(["users.roles.manage"])
		rbacService.hasAllPermissions.mockResolvedValue(false)

		try {
			await guard.canActivate(buildContext({ user: { id: "u1" } }))
			fail("expected ForbiddenException")
		} catch (error) {
			expect(error).toBeInstanceOf(ForbiddenException)
			const response = (error as ForbiddenException).getResponse()
			expect(response).toMatchObject({ message: "Forbidden" })
			expect((response as Record<string, unknown>).missingPermissions).toBeUndefined()
		}
	})
})
