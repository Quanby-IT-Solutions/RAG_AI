import { RbacService } from "./rbac.service"

jest.mock("@repo/contracts", () => ({
	ADMIN_ROLE: "System Administrator",
	PERMISSION_NAMES: [
		"users.accounts.manage",
		"users.roles.manage",
		"audit.log.view",
		"corpus.documents.upload",
		"corpus.documents.read",
		"corpus.ingest.run",
		"corpus.chunks.inspect",
		"corpus.reembed.run",
		"chat.query.submit",
		"chat.history.read",
		"eval.golden.manage",
		"eval.runs.execute",
		"eval.gate.approve",
		"apikeys.keys.manage",
		"ops.dashboards.view",
		"ops.config.view",
		"reports.reports.view",
		"reports.reports.export",
		"reports.schedules.manage",
	],
}))

jest.mock("@repo/db/schema", () => ({
	permissions: { id: "permissionId", name: "permissionName" },
	rolePermissions: { roleId: "roleId", permissionId: "permissionId" },
	roles: { id: "roleId", name: "roleName" },
	userRoles: { userId: "userId", roleId: "roleId" },
	users: { id: "userId" },
}))

jest.mock("@/common/database/database.client", () => ({
	db: {
		select: jest.fn(),
		insert: jest.fn(),
		delete: jest.fn(),
	},
}))

function serviceWithGrants(roleNames: string[], permissionNames: string[]): RbacService {
	const cacheService = {
		get: jest.fn(() => ({
			roleNames: new Set(roleNames),
			permissionNames: new Set(permissionNames),
		})),
		set: jest.fn(),
		invalidate: jest.fn(),
	}
	return new RbacService(cacheService as never)
}

describe("RbacService", () => {
	describe("matchPermission (via hasPermission)", () => {
		it("matches an exact granted dotted key", async () => {
			const service = serviceWithGrants([], ["chat.query.submit"])
			await expect(service.hasPermission("u1", "chat.query.submit")).resolves.toBe(true)
		})

		it("denies when the key is not granted", async () => {
			const service = serviceWithGrants([], ["chat.query.submit"])
			await expect(service.hasPermission("u1", "audit.log.view")).resolves.toBe(false)
		})

		it("matches a top-level wildcard grant (corpus.*)", async () => {
			const service = serviceWithGrants([], ["corpus.*"])
			await expect(service.hasPermission("u1", "corpus.documents.upload")).resolves.toBe(true)
			await expect(service.hasPermission("u1", "corpus.ingest.run")).resolves.toBe(true)
		})

		it("matches a mid-level wildcard grant (corpus.documents.*)", async () => {
			const service = serviceWithGrants([], ["corpus.documents.*"])
			await expect(service.hasPermission("u1", "corpus.documents.upload")).resolves.toBe(true)
			await expect(service.hasPermission("u1", "corpus.documents.read")).resolves.toBe(true)
		})

		it("does not let a wildcard leak outside its prefix", async () => {
			const service = serviceWithGrants([], ["corpus.documents.*"])
			await expect(service.hasPermission("u1", "corpus.ingest.run")).resolves.toBe(false)
			await expect(service.hasPermission("u1", "chat.query.submit")).resolves.toBe(false)
		})

		it("fails closed for unknown/non-catalog required keys", async () => {
			const service = serviceWithGrants([], ["corpus.*", "made.up.key"])
			await expect(service.hasPermission("u1", "made.up.key")).resolves.toBe(false)
			await expect(service.hasPermission("u1", "corpus.documents.destroy")).resolves.toBe(false)
		})

		it("short-circuits for the System Administrator role", async () => {
			const service = serviceWithGrants(["System Administrator"], [])
			await expect(service.hasPermission("u1", "users.roles.manage")).resolves.toBe(true)
			await expect(service.hasPermission("u1", "audit.log.view")).resolves.toBe(true)
		})
	})

	describe("getUserAccess", () => {
		it("returns roles plus effective catalog keys, expanding wildcard grants", async () => {
			const service = serviceWithGrants(["Knowledge Manager"], ["corpus.*", "chat.query.submit"])

			await expect(service.getUserAccess("user-1")).resolves.toEqual({
				roles: ["Knowledge Manager"],
				permissions: [
					"corpus.documents.upload",
					"corpus.documents.read",
					"corpus.ingest.run",
					"corpus.chunks.inspect",
					"corpus.reembed.run",
					"chat.query.submit",
				],
			})
		})

		it("returns the full catalog for the admin role", async () => {
			const service = serviceWithGrants(["System Administrator"], [])
			const access = await service.getUserAccess("user-1")
			expect(access.permissions).toHaveLength(19)
		})
	})
})
