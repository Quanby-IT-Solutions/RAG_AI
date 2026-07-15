import { oc } from "@orpc/contract"
import { z } from "zod"

/** One effective-config entry (OB-5): secrets are masked SERVER-side. */
export const ConfigEntrySchema = z.object({
	key: z.string(),
	env: z.string(),
	value: z.unknown(),
	secret: z.boolean(),
})

export const EffectiveConfigSchema = z.object({
	groups: z.record(z.string(), z.array(ConfigEntrySchema)),
})

/**
 * Ops console (OB-5): read-only configuration review. Requires
 * `ops.config.view` (RbacGuard, denials audited). Config editing is
 * deploy-time by design (twelve-factor; PRD decision #1).
 */
export const opsContract = {
	config: {
		get: oc
			.route({
				method: "GET",
				path: "/ops/config",
				summary: "Effective configuration (read-only, secrets masked)",
				tags: ["Ops"],
			})
			.output(EffectiveConfigSchema),
	},
}

export type ConfigEntry = z.infer<typeof ConfigEntrySchema>
export type EffectiveConfig = z.infer<typeof EffectiveConfigSchema>
