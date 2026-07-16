import { createHmac, timingSafeEqual } from "node:crypto"

import {
	Injectable,
	UnauthorizedException,
	type CanActivate,
	type ExecutionContext,
} from "@nestjs/common"
import type { Request } from "express"

import { env } from "@/config/env.config"

/**
 * RP-6: verifies HS256 service JWTs signed with RAG_SERVICE_TOKEN_SECRET —
 * the same shared secret as the backend→rag direction, reversed so the arq
 * report scheduler can call the internal report-run endpoint. Mirrors the
 * hand-rolled signing in RagGatewayService (no extra JWT dependency).
 */
@Injectable()
export class ServiceTokenGuard implements CanActivate {
	canActivate(context: ExecutionContext): boolean {
		const request = context.switchToHttp().getRequest<Request>()
		const authorization = request.headers?.authorization ?? ""
		const [scheme, token] = authorization.split(" ")
		if (scheme?.toLowerCase() !== "bearer" || !token) {
			throw new UnauthorizedException("Missing service bearer token")
		}

		const [header, payload, signature] = token.split(".")
		if (!header || !payload || !signature) {
			throw new UnauthorizedException("Malformed service token")
		}
		const expected = createHmac("sha256", env.RAG_SERVICE_TOKEN_SECRET)
			.update(`${header}.${payload}`)
			.digest("base64url")
		const provided = Buffer.from(signature)
		const wanted = Buffer.from(expected)
		if (provided.length !== wanted.length || !timingSafeEqual(provided, wanted)) {
			throw new UnauthorizedException("Invalid service token signature")
		}

		let claims: { sub?: string; exp?: number }
		try {
			claims = JSON.parse(Buffer.from(payload, "base64url").toString("utf8")) as {
				sub?: string
				exp?: number
			}
		} catch {
			throw new UnauthorizedException("Malformed service token payload")
		}
		if (!claims.sub || typeof claims.exp !== "number") {
			throw new UnauthorizedException("Service token missing sub/exp")
		}
		if (claims.exp * 1000 < Date.now()) {
			throw new UnauthorizedException("Service token expired")
		}

		;(request as Request & { servicePrincipal?: string }).servicePrincipal = claims.sub
		return true
	}
}
