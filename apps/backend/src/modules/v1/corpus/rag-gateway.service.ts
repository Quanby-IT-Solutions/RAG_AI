import { createHmac } from "node:crypto"
import {
	BadGatewayException,
	GatewayTimeoutException,
	HttpException,
	Injectable,
} from "@nestjs/common"
import { ORPCError } from "@orpc/server"

/**
 * Map an upstream HttpException onto the oRPC error vocabulary so oRPC
 * handlers return the real status instead of a generic 500. Plain Nest
 * routes keep throwing HttpException directly.
 */
export function toORPCError(error: unknown): never {
	if (error instanceof HttpException) {
		const status = error.getStatus()
		const code =
			status === 403
				? ("FORBIDDEN" as const)
				: status === 404
					? ("NOT_FOUND" as const)
					: status === 409
						? ("CONFLICT" as const)
						: status === 429
							? ("TOO_MANY_REQUESTS" as const)
							: status >= 400 && status < 500
								? ("BAD_REQUEST" as const)
								: ("INTERNAL_SERVER_ERROR" as const)
		throw new ORPCError(code, { message: error.message })
	}
	throw error
}

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
	/** CH-3: continue an existing conversation the caller owns. */
	conversationId?: string
	/** RT-4: metadata filter forwarded verbatim to the RAG service. */
	filters?: Record<string, unknown>
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
			body: JSON.stringify({
				query: input.query,
				...(input.k !== undefined && { k: input.k }),
				...(input.conversationId !== undefined && { conversationId: input.conversationId }),
				...(input.filters !== undefined && { filters: input.filters }),
			}),
			failureMessage: "RAG chat failed",
		})
	}

	/**
	 * Re-run a finished ingestion job (IN-7): POST
	 * {RAG_SERVICE_URL}/internal/jobs/{id}/rerun. The Python side requeues the
	 * job (409 when it isn't re-runnable) and enqueues the arq task.
	 */
	async rerunJob(input: {
		jobId: string
		userId: string
		permissions: readonly string[]
		requestId: string
		traceId: string
	}): Promise<unknown> {
		const token = this.mintServiceToken({
			sub: input.userId,
			permissions: input.permissions,
			requestId: input.requestId,
			traceId: input.traceId,
		})

		return this.forward(`/internal/jobs/${input.jobId}/rerun`, {
			headers: {
				authorization: `Bearer ${token}`,
				"x-request-id": input.requestId,
			},
			body: "",
			failureMessage: "RAG job re-run failed",
		})
	}

	/**
	 * Run the REAL hybrid retrieval pipeline (EV-1 chunk picker, EV-2 harness):
	 * POST {RAG_SERVICE_URL}/internal/retrieve. Returns fused top-k chunks
	 * including each chunk's content hash (the golden-set binding key).
	 */
	async retrieveChunks(input: {
		query: string
		k?: number
		userId: string
		permissions: readonly string[]
		requestId: string
		traceId: string
	}): Promise<unknown> {
		const token = this.mintServiceToken({
			sub: input.userId,
			permissions: input.permissions,
			requestId: input.requestId,
			traceId: input.traceId,
		})

		return this.forward("/internal/retrieve", {
			headers: {
				authorization: `Bearer ${token}`,
				"content-type": "application/json",
				"x-request-id": input.requestId,
			},
			body: JSON.stringify({
				query: input.query,
				...(input.k !== undefined && { k: input.k }),
			}),
			failureMessage: "RAG retrieval failed",
		})
	}

	/**
	 * Execute a retrieval eval run (EV-2): POST
	 * {RAG_SERVICE_URL}/internal/eval/retrieval. The Python runner scores the
	 * REAL retrieval pipeline against the golden set and persists eval_runs;
	 * an empty/fully-stale set surfaces as 409 (fail loud).
	 */
	async executeRetrievalEval(input: {
		k?: number
		vectorWeight?: number
		keywordWeight?: number
		rerank?: boolean
		generation?: boolean
		userId: string
		permissions: readonly string[]
		requestId: string
		traceId: string
	}): Promise<unknown> {
		const token = this.mintServiceToken({
			sub: input.userId,
			permissions: input.permissions,
			requestId: input.requestId,
			traceId: input.traceId,
		})

		return this.forward("/internal/eval/retrieval", {
			headers: {
				authorization: `Bearer ${token}`,
				"content-type": "application/json",
				"x-request-id": input.requestId,
			},
			body: JSON.stringify({
				...(input.k !== undefined && { k: input.k }),
				...(input.vectorWeight !== undefined && { vectorWeight: input.vectorWeight }),
				...(input.keywordWeight !== undefined && { keywordWeight: input.keywordWeight }),
				...(input.rerank !== undefined && { rerank: input.rerank }),
				...(input.generation !== undefined && { generation: input.generation }),
			}),
			failureMessage: "RAG eval run failed",
		})
	}

	/**
	 * OB-5: effective configuration (read-only; secrets masked SERVER-side
	 * in the Python service): GET {RAG_SERVICE_URL}/internal/config.
	 */
	async fetchConfig(input: {
		userId: string
		permissions: readonly string[]
		requestId: string
		traceId: string
	}): Promise<unknown> {
		const token = this.mintServiceToken({
			sub: input.userId,
			permissions: input.permissions,
			requestId: input.requestId,
			traceId: input.traceId,
		})
		return this.forward("/internal/config", {
			method: "GET",
			headers: {
				authorization: `Bearer ${token}`,
				"x-request-id": input.requestId,
			},
			failureMessage: "RAG config fetch failed",
		})
	}

	/** IN-8 (Flow D): re-embed lifecycle forwards. */
	async reembed(input: {
		action: "start" | "status" | "cutover" | "abort"
		targetModel?: string
		userId: string
		permissions: readonly string[]
		requestId: string
		traceId: string
	}): Promise<unknown> {
		const token = this.mintServiceToken({
			sub: input.userId,
			permissions: input.permissions,
			requestId: input.requestId,
			traceId: input.traceId,
		})
		const path =
			input.action === "start" || input.action === "status"
				? "/internal/reembed"
				: `/internal/reembed/${input.action}`
		return this.forward(path, {
			method: input.action === "status" ? "GET" : "POST",
			headers: {
				authorization: `Bearer ${token}`,
				"content-type": "application/json",
				"x-request-id": input.requestId,
			},
			body:
				input.action === "start"
					? JSON.stringify({ targetModel: input.targetModel })
					: input.action === "status"
						? undefined
						: "{}",
			failureMessage: "RAG re-embed call failed",
		})
	}

	/** Shared transport: hard timeout + uniform error mapping. */
	private async forward(
		path: string,
		options: {
			headers: Record<string, string>
			body?: FormData | string
			method?: "GET" | "POST"
			failureMessage: string
		}
	): Promise<unknown> {
		let response: Response
		try {
			response = await fetch(`${env.RAG_SERVICE_URL}${path}`, {
				method: options.method ?? "POST",
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
