import {
	ForbiddenException,
	Injectable,
	UnauthorizedException,
	type CanActivate,
	type ExecutionContext,
} from "@nestjs/common"
import { Reflector } from "@nestjs/core"
import type { IncomingHttpHeaders } from "http"
import { fromNodeHeaders } from "better-auth/node"

import { getAuth } from "@repo/auth"

import { AuditService } from "@/common/audit/audit.service"
import { ApiKeysService, type ApiKeyPrincipal } from "@/common/apikeys/apikeys.service"
import { RbacService } from "@/common/rbac/rbac.service"
import { env } from "@/config/env.config"
import { REQUIRED_PERMISSIONS_KEY } from "@/shared/decorators/require-permissions.decorator"

@Injectable()
export class RbacGuard implements CanActivate {
	constructor(
		private readonly reflector: Reflector,
		private readonly rbacService: RbacService,
		private readonly auditService: AuditService,
		private readonly apiKeysService: ApiKeysService
	) {}

	async canActivate(context: ExecutionContext): Promise<boolean> {
		// Handler-level metadata overrides class-level (standard NestJS precedence):
		// a method's @RequirePermissions takes priority over the controller's.
		const requiredPermissions = this.reflector.getAllAndOverride<string[]>(
			REQUIRED_PERMISSIONS_KEY,
			[context.getHandler(), context.getClass()]
		)

		// No decorator applied → guard is inert.
		if (!requiredPermissions || requiredPermissions.length === 0) {
			return true
		}

		// Resolve the authenticated user id from the request.
		//
		// Per the @thallesp/nestjs-better-auth v2.2.0 README ("Request Object
		// Access"), the library's authentication middleware attaches TWO things
		// to the raw request after a session is resolved:
		//   - request.session → the full UserSession ({ user: { id, ... }, session: { ... } })
		//   - request.user    → a direct reference to request.session.user
		//
		// Primary path: request.session?.user?.id — this is the verified shape the
		// @Session() decorator reads in controllers (e.g. todos.controller.ts), so it
		// is the source of truth.
		// Fallback path: request.user?.id — documented direct user reference, tried
		// only if the session object is absent. Defensive hardening against request
		// shape variation across library versions.
		const request = context.switchToHttp().getRequest<{
			headers: IncomingHttpHeaders
			method?: string
			originalUrl?: string
			url?: string
			session?: { user?: { id?: string } }
			user?: { id?: string }
		}>()
		// ── SC-3: API-key principals share this guard (Flow E, one interface).
		// A request carrying x-api-key authenticates as the key: the key's
		// SCOPES (not the owner's roles) gate the route, revocation is checked
		// per request (immediate 401), and all integration activity is audited.
		const rawApiKey = request.headers?.["x-api-key"]
		if (typeof rawApiKey === "string" && rawApiKey.length > 0) {
			// ApiKeyAuthGuard (authentication layer) has usually already resolved
			// the principal; fall back to authenticating here so this guard is
			// self-contained (unit tests, alternate guard orders).
			const key =
				(request as { apiKey?: ApiKeyPrincipal }).apiKey ??
				(await this.apiKeysService.authenticate(rawApiKey))
			if (!key || key.revoked) {
				throw new UnauthorizedException("Invalid or revoked API key")
			}
			const route = `${request.method ?? "UNKNOWN"} ${request.originalUrl ?? request.url ?? "unknown"}`
			const inScope = requiredPermissions.every(permission =>
				key.scopes.includes(permission)
			)
			// All integration activity lands on the audit spine (SC-3 AC).
			try {
				await this.auditService.writeAudit({
					actorId: `apikey:${key.id}`,
					actorType: "service",
					action: inScope ? "integration.request" : "rbac.denied",
					resourceType: "route",
					resourceId: route,
					after: inScope ? { scopes: requiredPermissions } : { requiredPermissions },
				})
			} catch {
				// best-effort; the decision below is authoritative
			}
			if (!inScope) {
				throw new ForbiddenException("API key is not scoped for this operation")
			}
			void this.apiKeysService.touch(key.id).catch(() => undefined)
			// Downstream handlers read the acting principal from the session
			// shape; the key acts on behalf of its owner.
			request.session = { user: { id: key.ownerUserId } }
			request.user = { id: key.ownerUserId }
			return true
		}

		let userId: string | undefined = request.session?.user?.id ?? request.user?.id

		// The library's AuthGuard (@thallesp/nestjs-better-auth) attaches
		// request.session in its own canActivate. Global guard execution order is
		// not guaranteed, so RbacGuard may run BEFORE it and see no session. When
		// that happens, resolve the session directly from the request headers (the
		// same mechanism the library uses) and attach it for downstream consumers.
		if (!userId) {
			const session = await getAuth().api.getSession({
				headers: fromNodeHeaders(request.headers),
			})
			if (session) {
				request.session = session
				request.user = session.user
				userId = session.user?.id
			}
		}

		// Fail closed: a protected route without a resolvable session is rejected.
		if (!userId) {
			throw new UnauthorizedException("Authentication required")
		}

		const allowed = await this.rbacService.hasAllPermissions(userId, requiredPermissions)
		if (allowed) {
			return true
		}

		// Flow E: every RBAC denial is audited (append-only spine). The entry is
		// written by the system on the actor's behalf; a failed audit write must
		// never mask the 403 itself.
		const route = `${request.method ?? "UNKNOWN"} ${request.originalUrl ?? request.url ?? "unknown"}`
		try {
			await this.auditService.writeAudit({
				actorId: userId,
				actorType: "system",
				action: "rbac.denied",
				resourceType: "route",
				resourceId: route,
				after: { requiredPermissions },
			})
		} catch {
			// Swallow: denial auditing is best-effort; the 403 below is authoritative.
		}

		// Verbose in non-production to aid debugging; HttpExceptionFilter surfaces
		// `missingPermissions` as details. Generic message in production.
		// Only report permissions the user actually lacks, not the full required list.
		if (env.NODE_ENV !== "production") {
			const missingPermissions = await this.rbacService.getMissingPermissions(
				userId,
				requiredPermissions
			)
			throw new ForbiddenException({ message: "Forbidden", missingPermissions })
		}

		throw new ForbiddenException("Forbidden")
	}
}
