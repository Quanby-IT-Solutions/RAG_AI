import { oc } from "@orpc/contract"
import { z } from "zod"

import {
	ApiKeySchema,
	CreateApiKeyRequestSchema,
	CreateApiKeyResponseSchema,
} from "./apikeys.schema.js"

/**
 * Integrator API keys (SC-3). All routes require `apikeys.keys.manage`
 * (RbacGuard) and operate on the CALLER'S OWN keys only. Requests
 * authenticated with an API key (x-api-key header) flow through the same
 * RbacGuard with the key's scopes; all integration activity is audited.
 */
export const apiKeysContract = {
	keys: {
		list: oc
			.route({
				method: "GET",
				path: "/apikeys",
				summary: "List my API keys",
				description: "The caller's own keys with display prefix and last-used timestamps.",
				tags: ["API Keys"],
			})
			.output(z.array(ApiKeySchema)),
		create: oc
			.route({
				method: "POST",
				path: "/apikeys",
				summary: "Create an API key",
				description:
					"Scopes must be a subset of the caller's own permissions. The secret is returned once; only a hash is stored.",
				tags: ["API Keys"],
			})
			.input(CreateApiKeyRequestSchema)
			.output(CreateApiKeyResponseSchema),
		revoke: oc
			.route({
				method: "DELETE",
				path: "/apikeys/{id}",
				summary: "Revoke an API key",
				description: "Revocation is immediate: the next request with the key returns 401.",
				tags: ["API Keys"],
			})
			.input(z.object({ id: z.string().uuid() }))
			.output(z.object({ id: z.string(), revoked: z.literal(true) })),
	},
}
