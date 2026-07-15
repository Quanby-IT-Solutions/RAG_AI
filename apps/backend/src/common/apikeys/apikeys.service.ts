import { createHash, randomBytes, randomUUID } from "node:crypto"
import { Injectable } from "@nestjs/common"
import { ORPCError } from "@orpc/server"
import { and, desc, eq } from "drizzle-orm"

import type { CreateApiKeyRequest, PermissionName } from "@repo/contracts"
import { apiKeys } from "@repo/db/schema"

import { AuditService } from "@/common/audit/audit.service"
import { db } from "@/common/database/database.client"
import { RbacService } from "@/common/rbac/rbac.service"

const KEY_SECRET_PREFIX = "ragp_"

export interface ApiKeyPrincipal {
	id: string
	ownerUserId: string
	scopes: string[]
	revoked: boolean
}

/**
 * Integrator API keys (SC-3). Secrets are `ragp_<random>`; only a sha256 hash
 * is stored (the plaintext is shown once at creation). Key scopes must be a
 * subset of the CREATOR'S own permissions; RbacGuard enforces the scopes per
 * request through the same Flow E interface used for user sessions.
 */
@Injectable()
export class ApiKeysService {
	constructor(
		private readonly rbacService: RbacService,
		private readonly auditService: AuditService
	) {}

	private hash(secret: string): string {
		return createHash("sha256").update(secret).digest("hex")
	}

	async createKey(ownerUserId: string, payload: CreateApiKeyRequest) {
		// Scopes are capped at the creator's OWN effective permissions.
		const access = await this.rbacService.getUserAccess(ownerUserId)
		const held = new Set(access.permissions)
		const outOfReach = payload.scopes.filter(scope => !held.has(scope))
		if (outOfReach.length > 0) {
			throw new ORPCError("FORBIDDEN", {
				message: `You cannot grant scopes you do not hold: ${outOfReach.join(", ")}`,
			})
		}

		const secret = `${KEY_SECRET_PREFIX}${randomBytes(32).toString("base64url")}`
		const id = randomUUID()
		await db.insert(apiKeys).values({
			id,
			name: payload.name,
			keyHash: this.hash(secret),
			keyPrefix: `${secret.slice(0, 10)}…`,
			ownerUserId,
			scopes: payload.scopes,
		})

		await this.auditService.writeAudit({
			actorId: ownerUserId,
			actorType: "user",
			action: "apikeys.keys.create",
			resourceType: "api_key",
			resourceId: id,
			after: { name: payload.name, scopes: payload.scopes },
		})

		// The plaintext secret is returned ONCE and never stored.
		return { id, name: payload.name, secret, scopes: payload.scopes }
	}

	/** The caller's own keys only (SC-3 AC). */
	async listOwn(ownerUserId: string) {
		const rows = await db
			.select()
			.from(apiKeys)
			.where(eq(apiKeys.ownerUserId, ownerUserId))
			.orderBy(desc(apiKeys.createdAt))
		return rows.map(row => ({
			id: row.id,
			name: row.name,
			keyPrefix: row.keyPrefix,
			scopes: row.scopes as PermissionName[],
			lastUsedAt: row.lastUsedAt,
			revokedAt: row.revokedAt,
			createdAt: row.createdAt,
		}))
	}

	async revoke(ownerUserId: string, id: string) {
		const [row] = await db
			.select()
			.from(apiKeys)
			.where(and(eq(apiKeys.id, id), eq(apiKeys.ownerUserId, ownerUserId)))
		if (!row) {
			throw new ORPCError("NOT_FOUND", { message: `API key ${id} not found` })
		}
		if (!row.revokedAt) {
			await db.update(apiKeys).set({ revokedAt: new Date() }).where(eq(apiKeys.id, id))
			await this.auditService.writeAudit({
				actorId: ownerUserId,
				actorType: "user",
				action: "apikeys.keys.revoke",
				resourceType: "api_key",
				resourceId: id,
				before: { revoked: false },
				after: { revoked: true },
			})
		}
		return { id, revoked: true as const }
	}

	/** Per-request key authentication (RbacGuard). Revocation is checked on
	 * every request, so a revoked key 401s immediately. */
	async authenticate(secret: string): Promise<ApiKeyPrincipal | null> {
		if (!secret.startsWith(KEY_SECRET_PREFIX)) return null
		const [row] = await db.select().from(apiKeys).where(eq(apiKeys.keyHash, this.hash(secret)))
		if (!row) return null
		return {
			id: row.id,
			ownerUserId: row.ownerUserId,
			scopes: row.scopes as string[],
			revoked: row.revokedAt !== null,
		}
	}

	/** Fire-and-forget last-used stamp. */
	async touch(id: string): Promise<void> {
		await db.update(apiKeys).set({ lastUsedAt: new Date() }).where(eq(apiKeys.id, id))
	}
}
