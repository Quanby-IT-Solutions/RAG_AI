import { Injectable } from "@nestjs/common"

import { auditLog } from "@repo/db/schema"

import { db } from "@/common/database/database.client"

/**
 * Canonical audit entry (Tech Plan §6 column contract). `actorType` is
 * "user" for user-initiated state changes, "system" for platform-originated
 * entries such as RBAC denials, "service" reserved for API-key principals.
 */
export interface AuditEntry {
	actorId: string | null
	actorType: "user" | "service" | "system"
	action: string
	resourceType: string
	resourceId?: string | null
	before?: unknown
	after?: unknown
}

@Injectable()
export class AuditService {
	/**
	 * Append one entry to the audit_log spine. INSERT-only by design: the DB
	 * rejects any UPDATE or DELETE on audit_log via a trigger, so this is the
	 * only write path.
	 */
	async writeAudit(entry: AuditEntry): Promise<void> {
		await db.insert(auditLog).values({
			actorId: entry.actorId,
			actorType: entry.actorType,
			action: entry.action,
			resourceType: entry.resourceType,
			resourceId: entry.resourceId ?? null,
			before: entry.before ?? null,
			after: entry.after ?? null,
		})
	}
}
