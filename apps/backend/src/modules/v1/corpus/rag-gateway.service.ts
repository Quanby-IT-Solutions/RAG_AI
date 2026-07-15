import { createHmac } from "node:crypto"
import {
	BadGatewayException,
	GatewayTimeoutException,
	HttpException,
	Injectable,
} from "@nestjs/common"

import { env } from "@/config/env.config"

/** Principal + correlation context carried by the service JWT (Tech Plan §5, D4). */
export interface ServiceTokenClaims {
	sub: string
	permissions: readonly string[]
	requestId: string
	traceId: string
}

export interface IngestForwardInput {
	buffer: Buffer
	filename: string
	contentType: string
	userId: string
	permissions: readonly string[]
	requestId: string
	traceId: string
}

export interface ChatForwardInput {
	query: string
	k?: number
	userId: string
	permissions: readonly string[]
	requestId: string
	traceId: string
}

function base64UrlJson(value: unknown): string {
	return Buffer.from(JSON.stringify(value)).toString("base64url")
}

/**
 * Gateway -> Python RAG service transport (Tech Plan §5, D4).
 *
 * Mints a short-lived HS256 service JWT (shared RAG_SERVICE_TOKEN_SECRET)
 * carrying the acting user + permissions + correlation ids, and forwards the
 * uploaded PDF as multipart via undici's fetch/FormData (Node's built-ins).
 * Every call is wrapped in AbortSignal.timeout(...) — the gateway mirror of
 * the Python service's hard-timeout helper.
 */
@Injectable()
export class RagGatewayService {
	/**
	 * HS256 JWT signed with RAG_SERVICE_TOKEN_SECRET. Hand-rolled signing (no
	 * verification/parsing on this side) keeps the gateway dependency-free; the
	 * Python service verifies signature + expiry with PyJWT.
	 */
	mintServiceToken(claims: ServiceTokenClaims): string {
		const nowSeconds = Math.floor(Date.now() / 1000)
		const header = base64UrlJson({ alg: "HS256", typ: "JWT" })
		const payload = base64UrlJson({
			sub: claims.sub,
			permissions: claims.permissions,
			requestId: claims.requestId,
			traceId: claims.traceId,
			// The audit actorType the RAG service records for this principal.
			actorType: "user",
			iat: nowSeconds,
			exp: nowSeconds + env.RAG_SERVICE_TOKEN_TTL_SECONDS,
		})
		const signature = createHmac("sha256", env.RAG_SERVICE_TOKEN_SECRET)
			.update(`${header}.${payload}`)
			.digest("base64url")
		return `${header}.${payload}.${signature}`
	}

	/** Forward one validated PDF to POST {RAG_SERVICE_URL}/internal/ingest. */
	async ingestDocument(input: IngestForwardInput): Promise<unknown> {
		const token = this.mintServiceToken({
			sub: input.userId,
			permissions: input.permissions,
			requestId: input.requestId,
			traceId: input.traceId,
		})

		const form = new FormData()
		form.append(
			"file",
			new Blob([new Uint8Array(input.buffer)], { type: input.contentType }),
			input.filename
		)

		return this.forward("/internal/ingest", {
			headers: {
				authorization: `Bearer ${token}`,
				"x-request-id": input.requestId,
			},
			body: form,
			failureMessage: "RAG ingest failed",
		})
	}

	/**
	 * Forward one chat query (JSON, non-streamed — Tech Plan §5 D3; streaming
	 * is GN-3) to POST {RAG_SERVICE_URL}/internal/chat with the caller's
	 * principal + permissions in the service JWT.
	 */
	async submitChatQuery(input: ChatForwardInput): Promise<unknown> {
		const token = this.mintServiceToken({
			sub: input.userId,
			permissions: input.permissions,
			requestId: input.requestId,
			traceId: input.traceId,
		})

		return this.forward("/internal/chat", {
			headers: {
				authorization: `Bearer ${token}`,
				"content-type": "application/json",
				"x-request-id": input.requestId,
			},
			body: JSON.stringify({ query: input.query, ...(input.k !== undefined && { k: input.k }) }),
			failureMessage: "RAG chat failed",
		})
	}

	/** Shared POST transport: hard timeout + uniform error mapping. */
	private async forward(
		path: string,
		options: { headers: Record<string, string>; body: FormData | string; failureMessage: string }
	): Promise<unknown> {
		let response: Response
		try {
			response = await fetch(`${env.RAG_SERVICE_URL}${path}`, {
				method: "POST",
				headers: options.headers,
				body: options.body,
				signal: AbortSignal.timeout(env.RAG_SERVICE_TIMEOUT_MS),
			})
		} catch (error) {
			// AbortSignal.timeout rejects with a DOMException named "TimeoutError"
			// (DOMException does NOT extend Error, so match on the name only).
			if ((error as { name?: string } | null)?.name === "TimeoutError") {
				throw new GatewayTimeoutException(
					`RAG service did not respond within ${env.RAG_SERVICE_TIMEOUT_MS}ms`
				)
			}
			throw new BadGatewayException("RAG service is unreachable")
		}

		const body = (await response.json().catch(() => null)) as
			| { detail?: unknown; message?: unknown }
			| null

		if (!response.ok) {
			const detail = body?.detail ?? body?.message
			const message = typeof detail === "string" ? detail : options.failureMessage
			// Client-class errors from the Python defensive re-checks pass through;
			// service failures surface as 502.
			if (response.status >= 400 && response.status < 500) {
				throw new HttpException(message, response.status)
			}
			throw new BadGatewayException(message)
		}

		return body
	}
}
