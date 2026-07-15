import { randomBytes, randomUUID, scryptSync } from "node:crypto"
import { Injectable } from "@nestjs/common"
import { ORPCError } from "@orpc/server"
import { eq, inArray, sql } from "drizzle-orm"

import {
	ADMIN_ROLE,
	PERMISSION_NAMES,
	type AssignRoleRequest,
	type CreateRoleInput,
	type PermissionCatalogEntry,
	type PermissionName,
	type RemoveRoleRequest,
	type Role,
	type SetRolePermissionsRequest,
	type UpdateRoleRequest,
	type UserWithRoles,
	type CreateUserRequest,
	type SetUserDisabledRequest,
} from "@repo/contracts"
import { accounts, permissions, rolePermissions, roles, sessions, userRoles, users } from "@repo/db/schema"

import { AuditService } from "@/common/audit/audit.service"
import { db } from "@/common/database/database.client"
import { RbacCacheService } from "@/common/rbac/rbac-cache.service"
import { RbacService } from "@/common/rbac/rbac.service"

const PERMISSION_CATALOG = new Set<string>(PERMISSION_NAMES)

@Injectable()
export class RbacAdminService {
	constructor(
		private readonly rbacService: RbacService,
		private readonly cacheService: RbacCacheService,
		private readonly auditService: AuditService
	) {}

	// ---------------------------------------------------------------- roles
	async listRoles(): Promise<Role[]> {
		const roleRows = await db.select().from(roles).orderBy(roles.name)

		const permRows = await db
			.select({ roleId: rolePermissions.roleId, name: permissions.name })
			.from(rolePermissions)
			.innerJoin(permissions, eq(rolePermissions.permissionId, permissions.id))

		const countRows = await db
			.select({ roleId: userRoles.roleId, count: sql<number>`count(*)::int` })
			.from(userRoles)
			.groupBy(userRoles.roleId)

		const permsByRole = new Map<number, PermissionName[]>()
		for (const row of permRows) {
			if (!PERMISSION_CATALOG.has(row.name)) continue
			const list = permsByRole.get(row.roleId) ?? []
			list.push(row.name as PermissionName)
			permsByRole.set(row.roleId, list)
		}

		const countByRole = new Map<number, number>()
		for (const row of countRows) {
			countByRole.set(row.roleId, row.count)
		}

		return roleRows.map(role => ({
			id: role.id,
			name: role.name,
			description: role.description,
			permissions: this.orderPermissions(permsByRole.get(role.id) ?? []),
			userCount: countByRole.get(role.id) ?? 0,
			createdAt: role.createdAt,
			updatedAt: role.updatedAt,
		}))
	}

	async createRole(payload: CreateRoleInput): Promise<Role> {
		const [existing] = await db.select().from(roles).where(eq(roles.name, payload.name))
		if (existing) {
			throw new ORPCError("CONFLICT", { message: `Role "${payload.name}" already exists` })
		}

		const [role] = await db
			.insert(roles)
			.values({ name: payload.name, description: payload.description ?? null })
			.returning()

		if (!role) {
			throw new ORPCError("INTERNAL_SERVER_ERROR", { message: "Role could not be created" })
		}

		return this.buildRole(role.id)
	}

	async updateRole(payload: UpdateRoleRequest): Promise<Role> {
		const role = await this.getRoleRowOrThrow(payload.id)

		if (role.name === ADMIN_ROLE && payload.name !== undefined && payload.name !== ADMIN_ROLE) {
			throw new ORPCError("FORBIDDEN", { message: "The Admin role cannot be renamed" })
		}

		if (payload.name !== undefined && payload.name !== role.name) {
			const [clash] = await db.select().from(roles).where(eq(roles.name, payload.name))
			if (clash) {
				throw new ORPCError("CONFLICT", { message: `Role "${payload.name}" already exists` })
			}
		}

		await db
			.update(roles)
			.set({
				name: payload.name ?? role.name,
				description: payload.description !== undefined ? payload.description : role.description,
				updatedAt: new Date(),
			})
			.where(eq(roles.id, role.id))

		return this.buildRole(role.id)
	}

	async setRolePermissions(payload: SetRolePermissionsRequest): Promise<Role> {
		const role = await this.getRoleRowOrThrow(payload.id)

		const uniqueNames = Array.from(new Set(payload.permissions))
		const permissionRows = uniqueNames.length
			? await db.select().from(permissions).where(inArray(permissions.name, uniqueNames))
			: []

		const foundNames = new Set(permissionRows.map(p => p.name))
		const missing = uniqueNames.filter(name => !foundNames.has(name))
		if (missing.length) {
			throw new ORPCError("BAD_REQUEST", { message: `Unknown permissions: ${missing.join(", ")}` })
		}

		await db.transaction(async tx => {
			await tx.delete(rolePermissions).where(eq(rolePermissions.roleId, role.id))
			if (permissionRows.length) {
				await tx
					.insert(rolePermissions)
					.values(permissionRows.map(p => ({ roleId: role.id, permissionId: p.id })))
			}
		})

		this.cacheService.clear()
		return this.buildRole(role.id)
	}

	async deleteRole(id: number): Promise<{ success: boolean; id: number }> {
		const role = await this.getRoleRowOrThrow(id)

		if (role.name === ADMIN_ROLE) {
			throw new ORPCError("FORBIDDEN", { message: "The Admin role cannot be deleted" })
		}

		await db.delete(roles).where(eq(roles.id, role.id))
		this.cacheService.clear()
		return { success: true, id: role.id }
	}

	// ---------------------------------------------------------- permissions
	async listPermissions(): Promise<PermissionCatalogEntry[]> {
		const rows = await db.select().from(permissions)
		const descByName = new Map(rows.map(row => [row.name, row.description]))

		return PERMISSION_NAMES.filter(name => descByName.has(name)).map(name => ({
			name,
			description: descByName.get(name) ?? null,
		}))
	}

	// ---------------------------------------------------------------- users
	async listUsers(): Promise<UserWithRoles[]> {
		const userRows = await db.select().from(users).orderBy(users.email)

		const roleRows = await db
			.select({ userId: userRoles.userId, name: roles.name })
			.from(userRoles)
			.innerJoin(roles, eq(userRoles.roleId, roles.id))

		const rolesByUser = new Map<string, string[]>()
		for (const row of roleRows) {
			const list = rolesByUser.get(row.userId) ?? []
			list.push(row.name)
			rolesByUser.set(row.userId, list)
		}

		return userRows.map(user => ({
			id: user.id,
			name: user.name,
			email: user.email,
			roles: (rolesByUser.get(user.id) ?? []).sort(),
			disabled: user.disabled,
		}))
	}

	/**
	 * F4: admin-provisioned account creation (no public registration). Same
	 * scrypt credential format Better Auth verifies (mirrors packages/db seed);
	 * the user can sign in with the temporary password immediately.
	 */
	async createUser(payload: CreateUserRequest, actorId: string) {
		const [existing] = await db.select().from(users).where(eq(users.email, payload.email))
		if (existing) {
			throw new ORPCError("CONFLICT", {
				message: `A user with email ${payload.email} already exists`,
			})
		}

		const userId = randomUUID()
		await db.transaction(async tx => {
			await tx.insert(users).values({
				id: userId,
				name: payload.name,
				email: payload.email,
				emailVerified: true, // admin-provisioned: no verification loop
			})
			await tx.insert(accounts).values({
				id: randomUUID(),
				userId,
				providerId: "credential",
				accountId: userId,
				password: this.hashPassword(payload.temporaryPassword),
			})
		})

		await this.auditService.writeAudit({
			actorId,
			actorType: "user",
			action: "users.accounts.create",
			resourceType: "user",
			resourceId: userId,
			after: { email: payload.email, name: payload.name },
		})

		return { id: userId, name: payload.name, email: payload.email }
	}

	/**
	 * F4: disable = no new sessions (Better Auth session-create hook) AND all
	 * existing sessions revoked; re-enable restores sign-in. Audited.
	 */
	async setUserDisabled(payload: SetUserDisabledRequest, actorId: string) {
		const [user] = await db.select().from(users).where(eq(users.id, payload.userId))
		if (!user) {
			throw new ORPCError("NOT_FOUND", { message: `User ${payload.userId} not found` })
		}
		if (user.id === actorId) {
			throw new ORPCError("BAD_REQUEST", { message: "You cannot disable your own account" })
		}

		await db.transaction(async tx => {
			await tx
				.update(users)
				.set({ disabled: payload.disabled, updatedAt: new Date() })
				.where(eq(users.id, payload.userId))
			if (payload.disabled) {
				// Revoke every live session so the lockout is immediate.
				await tx.delete(sessions).where(eq(sessions.userId, payload.userId))
			}
		})

		await this.auditService.writeAudit({
			actorId,
			actorType: "user",
			action: payload.disabled ? "users.accounts.disable" : "users.accounts.enable",
			resourceType: "user",
			resourceId: payload.userId,
			before: { disabled: user.disabled },
			after: { disabled: payload.disabled },
		})

		return { id: payload.userId, disabled: payload.disabled }
	}

	/** Better Auth-compatible scrypt hash (same params as packages/db seed). */
	private hashPassword(password: string): string {
		const saltHex = randomBytes(16).toString("hex")
		const key = scryptSync(password.normalize("NFKC"), saltHex, 64, {
			N: 16384,
			r: 16,
			p: 1,
			maxmem: 128 * 16384 * 16 * 2,
		})
		return `${saltHex}:${key.toString("hex")}`
	}

	async assignUserRole(payload: AssignRoleRequest, actorId: string) {
		const before = await this.getUserRoleNames(payload.userId)
		await this.rbacService.assignRole(payload.userId, payload.roleName)
		const after = await this.getUserRoleNames(payload.userId)

		// Flow E: role assignment is a privileged state change — audit actor,
		// action, and before -> after role sets on the append-only spine.
		await this.auditService.writeAudit({
			actorId,
			actorType: "user",
			action: "users.roles.assign",
			resourceType: "user",
			resourceId: payload.userId,
			before: { roles: before },
			after: { roles: after },
		})

		return { success: true, userId: payload.userId, roleName: payload.roleName }
	}

	async removeUserRole(payload: RemoveRoleRequest, actorId: string) {
		const before = await this.getUserRoleNames(payload.userId)
		await this.rbacService.removeRole(payload.userId, payload.roleName)
		const after = await this.getUserRoleNames(payload.userId)

		await this.auditService.writeAudit({
			actorId,
			actorType: "user",
			action: "users.roles.remove",
			resourceType: "user",
			resourceId: payload.userId,
			before: { roles: before },
			after: { roles: after },
		})

		return { success: true, userId: payload.userId, roleName: payload.roleName }
	}

	// -------------------------------------------------------------- helpers
	private async getUserRoleNames(userId: string): Promise<string[]> {
		const rows = await db
			.select({ name: roles.name })
			.from(userRoles)
			.innerJoin(roles, eq(userRoles.roleId, roles.id))
			.where(eq(userRoles.userId, userId))
		return rows.map(row => row.name).sort()
	}

	private async getRoleRowOrThrow(id: number) {
		const [role] = await db.select().from(roles).where(eq(roles.id, id))
		if (!role) {
			throw new ORPCError("NOT_FOUND", { message: `Role with ID ${id} not found` })
		}
		return role
	}

	private async buildRole(id: number): Promise<Role> {
		const role = await this.getRoleRowOrThrow(id)

		const permRows = await db
			.select({ name: permissions.name })
			.from(rolePermissions)
			.innerJoin(permissions, eq(rolePermissions.permissionId, permissions.id))
			.where(eq(rolePermissions.roleId, id))

		const [{ count } = { count: 0 }] = await db
			.select({ count: sql<number>`count(*)::int` })
			.from(userRoles)
			.where(eq(userRoles.roleId, id))

		const grantedPermissions = permRows
			.map(p => p.name)
			.filter((name): name is PermissionName => PERMISSION_CATALOG.has(name))

		return {
			id: role.id,
			name: role.name,
			description: role.description,
			permissions: this.orderPermissions(grantedPermissions),
			userCount: count,
			createdAt: role.createdAt,
			updatedAt: role.updatedAt,
		}
	}

	private orderPermissions(names: PermissionName[]): PermissionName[] {
		const held = new Set(names)
		return PERMISSION_NAMES.filter(name => held.has(name))
	}
}
