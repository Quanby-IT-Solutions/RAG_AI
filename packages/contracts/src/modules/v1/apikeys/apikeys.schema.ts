import { z } from "zod"

import { PermissionNameSchema } from "../rbac/rbac.catalog.js"

/**
 * Integrator API keys (SC-3). The secret is returned ONCE at creation;
 * afterwards only the display prefix is available.
 */
export const ApiKeySchema = z.object({
	id: z.string().uuid(),
	name: z.string(),
	keyPrefix: z.string(),
	scopes: z.array(PermissionNameSchema),
	lastUsedAt: z
		.union([z.date(), z.string()])
		.nullable()
		.transform(val => (typeof val === "string" ? new Date(val) : val)),
	revokedAt: z
		.union([z.date(), z.string()])
		.nullable()
		.transform(val => (typeof val === "string" ? new Date(val) : val)),
	createdAt: z
		.union([z.date(), z.string()])
		.transform(val => (typeof val === "string" ? new Date(val) : val)),
})

export const CreateApiKeyRequestSchema = z.object({
	name: z.string().trim().min(1).max(120),
	// Must be a subset of the creator's own permissions (validated server-side).
	scopes: z.array(PermissionNameSchema).min(1),
})

export const CreateApiKeyResponseSchema = z.object({
	id: z.string().uuid(),
	name: z.string(),
	/** Shown once — only a hash is stored. */
	secret: z.string(),
	scopes: z.array(PermissionNameSchema),
})

export type ApiKey = z.infer<typeof ApiKeySchema>
export type CreateApiKeyRequest = z.infer<typeof CreateApiKeyRequestSchema>
export type CreateApiKeyResponse = z.infer<typeof CreateApiKeyResponseSchema>
