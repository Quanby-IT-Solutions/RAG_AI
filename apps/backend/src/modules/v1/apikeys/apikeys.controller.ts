import { Controller } from "@nestjs/common"
import { Implement } from "@orpc/nest"
import { implement } from "@orpc/server"
import { Session, type UserSession } from "@thallesp/nestjs-better-auth"

import { ApiKeysService } from "@/common/apikeys/apikeys.service"
import { v1 } from "@/config/api-versions.config"
import { RequirePermissions } from "@/shared/decorators/require-permissions.decorator"
import { StrictThrottle } from "@/shared/decorators/strict-throttle.decorator"

/**
 * Integrator API keys console (SC-3). Every route requires
 * `apikeys.keys.manage` and operates on the CALLER'S OWN keys only.
 */
@Controller()
export class ApiKeysController {
	constructor(private readonly apiKeysService: ApiKeysService) {}

	@RequirePermissions("apikeys.keys.manage")
	@Implement(v1.apikeys.keys.list)
	async listKeys(@Session() session: UserSession) {
		return implement(v1.apikeys.keys.list).handler(async () => {
			return this.apiKeysService.listOwn(session.user.id)
		})
	}

	@StrictThrottle()
	@RequirePermissions("apikeys.keys.manage")
	@Implement(v1.apikeys.keys.create)
	async createKey(@Session() session: UserSession) {
		return implement(v1.apikeys.keys.create).handler(async ({ input }) => {
			return this.apiKeysService.createKey(session.user.id, input)
		})
	}

	@StrictThrottle()
	@RequirePermissions("apikeys.keys.manage")
	@Implement(v1.apikeys.keys.revoke)
	async revokeKey(@Session() session: UserSession) {
		return implement(v1.apikeys.keys.revoke).handler(async ({ input }) => {
			return this.apiKeysService.revoke(session.user.id, input.id)
		})
	}
}
