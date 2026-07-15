import {
	ForbiddenException,
	Injectable,
	UnauthorizedException,
	type CanActivate,
	type ExecutionContext,
} from "@nestjs/common"
import { Reflector } from "@nestjs/core"
import type { IncomingHttpHeaders } from "http"
import { AuthGuard } from "@thallesp/nestjs-better-auth"

import { ApiKeysService, type ApiKeyPrincipal } from "@/common/apikeys/apikeys.service"
import { REQUIRED_PERMISSIONS_KEY } from "@/shared/decorators/require-permissions.decorator"

/**
 * Authentication layer (SC-3). Replaces the library's global AuthGuard
 * (disableGlobalAuthGuard) so the guard chain is DETERMINISTIC:
 * Throttler → ApiKeyAuthGuard (authentication) → RbacGuard (authorization).
 *
 * Requests carrying `x-api-key` authenticate as the key: invalid/revoked
 * keys 401 immediately, and keys may ONLY reach permission-decorated routes
 * (so every integration request lands on the audit spine via RbacGuard).
 * All other requests delegate verbatim to the library AuthGuard, which
 * resolves the Better Auth session and honors @Public/@OptionalAuth.
 */
@Injectable()
export class ApiKeyAuthGuard implements CanActivate {
	constructor(
		private readonly reflector: Reflector,
		private readonly sessionAuthGuard: AuthGuard,
		private readonly apiKeysService: ApiKeysService
	) {}

	async canActivate(context: ExecutionContext): Promise<boolean> {
		if (context.getType() !== "http") {
			return (await this.sessionAuthGuard.canActivate(context)) as boolean
		}

		const request = context.switchToHttp().getRequest<{
			headers?: IncomingHttpHeaders
			session?: unknown
			user?: unknown
			apiKey?: ApiKeyPrincipal
		}>()

		const rawApiKey = request.headers?.["x-api-key"]
		if (typeof rawApiKey === "string" && rawApiKey.length > 0) {
			const key = await this.apiKeysService.authenticate(rawApiKey)
			// Revocation is checked per request: a revoked key 401s immediately.
			if (!key || key.revoked) {
				throw new UnauthorizedException("Invalid or revoked API key")
			}

			// Keys are scope-gated; a route without @RequirePermissions has no
			// scope to check against, so keys are rejected there (fail closed).
			const requiredPermissions = this.reflector.getAllAndOverride<string[]>(
				REQUIRED_PERMISSIONS_KEY,
				[context.getHandler(), context.getClass()]
			)
			if (!requiredPermissions || requiredPermissions.length === 0) {
				throw new ForbiddenException("API keys can only access permission-scoped routes")
			}

			// The key acts on behalf of its owner: downstream handlers read the
			// principal from the standard session shape. RbacGuard picks up
			// request.apiKey to enforce scopes and audit the request.
			request.apiKey = key
			request.session = { user: { id: key.ownerUserId } }
			request.user = { id: key.ownerUserId }
			return true
		}

		return (await this.sessionAuthGuard.canActivate(context)) as boolean
	}
}
