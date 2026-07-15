import {
	BadRequestException,
	Controller,
	Post,
	Req,
	UploadedFile,
	UseInterceptors,
} from "@nestjs/common"
import { FileInterceptor } from "@nestjs/platform-express"
import { Implement } from "@orpc/nest"
import { implement } from "@orpc/server"
import { Session, type UserSession } from "@thallesp/nestjs-better-auth"
import type { Request } from "express"

import { RbacService } from "@/common/rbac/rbac.service"
import { v1 } from "@/config/api-versions.config"
import { RequirePermissions } from "@/shared/decorators/require-permissions.decorator"
import { StrictThrottle } from "@/shared/decorators/strict-throttle.decorator"

import { CorpusService } from "./corpus.service"
import { documentUploadOptions } from "./corpus.upload-options"
import { RagGatewayService, toORPCError } from "./rag-gateway.service"

/**
 * Corpus endpoints (Flow A, IN-1).
 *
 * The binary upload is a plain Nest + Multer route (Tech Plan §5, D2) — NOT
 * oRPC, which is JSON-only. The JSON list endpoint stays on the oRPC contract.
 * No controller prefix: the oRPC contract path already includes the full
 * `/v1/corpus/documents` segment, so the upload route carries the literal
 * `v1/corpus/documents` path itself: /api/v1/corpus/documents.
 */
@Controller()
export class CorpusController {
	constructor(
		private readonly corpusService: CorpusService,
		private readonly ragGatewayService: RagGatewayService,
		private readonly rbacService: RbacService
	) {}

	/**
	 * Upload one document (PDF/HTML/DOCX/CSV — IN-3) and hand it to the Python
	 * RAG service to normalize + register (Flow A steps 1-3). Requires BOTH
	 * dotted keys — the R1 combined
	 * upload+index action (cross-artifact decision C4); RbacGuard fails closed
	 * and audits denials.
	 *
	 * The global ZodValidationPipe only validates Zod DTO metatypes, so the
	 * binary `file` part passes through untouched; likewise the
	 * ZodSerializerInterceptor is inert here (no @ZodSerializerDto metadata).
	 */
	@Post("v1/corpus/documents")
	@StrictThrottle()
	@RequirePermissions("corpus.documents.upload", "corpus.ingest.run")
	@UseInterceptors(FileInterceptor("file", documentUploadOptions))
	async uploadDocument(
		@UploadedFile() file: Express.Multer.File | undefined,
		@Session() session: UserSession,
		@Req() request: Request & { id?: string | number }
	) {
		if (!file) {
			throw new BadRequestException(
				'A document file is required: send multipart/form-data with a single "file" field.'
			)
		}

		// Forward the caller's effective permissions inside the service JWT so
		// the Python service can attribute the audit entry to the real principal.
		const access = await this.rbacService.getUserAccess(session.user.id)
		const correlationId = String(request.id ?? crypto.randomUUID())

		return this.ragGatewayService.ingestDocument({
			buffer: file.buffer,
			filename: file.originalname,
			contentType: file.mimetype,
			userId: session.user.id,
			permissions: access.permissions,
			requestId: correlationId,
			traceId: correlationId,
		})
	}

	@RequirePermissions("corpus.documents.read")
	@Implement(v1.corpus.documents.list)
	async listDocuments() {
		return implement(v1.corpus.documents.list).handler(async () => {
			return this.corpusService.listDocuments()
		})
	}

	// ── Zero-downtime re-embedding (IN-8, Flow D) ────────────────────────────
	@StrictThrottle()
	@RequirePermissions("corpus.reembed.run")
	@Implement(v1.corpus.reembed.start)
	async startReembed(
		@Session() session: UserSession,
		@Req() request: Request & { id?: string | number }
	) {
		return implement(v1.corpus.reembed.start).handler(async ({ input }) => {
			return (await this.forwardReembed("start", session, request, input.targetModel)) as never
		})
	}

	@RequirePermissions("corpus.reembed.run")
	@Implement(v1.corpus.reembed.status)
	async reembedStatus(
		@Session() session: UserSession,
		@Req() request: Request & { id?: string | number }
	) {
		return implement(v1.corpus.reembed.status).handler(async () => {
			return (await this.forwardReembed("status", session, request)) as never
		})
	}

	@StrictThrottle()
	@RequirePermissions("corpus.reembed.run")
	@Implement(v1.corpus.reembed.cutover)
	async reembedCutover(
		@Session() session: UserSession,
		@Req() request: Request & { id?: string | number }
	) {
		return implement(v1.corpus.reembed.cutover).handler(async () => {
			return (await this.forwardReembed("cutover", session, request)) as never
		})
	}

	@StrictThrottle()
	@RequirePermissions("corpus.reembed.run")
	@Implement(v1.corpus.reembed.abort)
	async reembedAbort(
		@Session() session: UserSession,
		@Req() request: Request & { id?: string | number }
	) {
		return implement(v1.corpus.reembed.abort).handler(async () => {
			return (await this.forwardReembed("abort", session, request)) as never
		})
	}

	private async forwardReembed(
		action: "start" | "status" | "cutover" | "abort",
		session: UserSession,
		request: Request & { id?: string | number },
		targetModel?: string
	) {
		const access = await this.rbacService.getUserAccess(session.user.id)
		const correlationId = String(request.id ?? crypto.randomUUID())
		return this.ragGatewayService
			.reembed({
				action,
				targetModel,
				userId: session.user.id,
				permissions: access.permissions,
				requestId: correlationId,
				traceId: correlationId,
			})
			.catch(toORPCError)
	}

	// ── Chunk-boundary inspection (IN-5) ─────────────────────────────────────
	@RequirePermissions("corpus.chunks.inspect")
	@Implement(v1.corpus.documents.inspectChunks)
	async inspectChunks() {
		return implement(v1.corpus.documents.inspectChunks).handler(async ({ input }) => {
			return this.corpusService.inspectChunks(input.id)
		})
	}

	// ── Jobs console (IN-7) — monitored by holders of corpus.ingest.run ──────

	@RequirePermissions("corpus.ingest.run")
	@Implement(v1.corpus.jobs.list)
	async listJobs() {
		return implement(v1.corpus.jobs.list).handler(async () => {
			return this.corpusService.listJobs()
		})
	}

	@RequirePermissions("corpus.ingest.run")
	@Implement(v1.corpus.jobs.get)
	async getJob() {
		return implement(v1.corpus.jobs.get).handler(async ({ input }) => {
			return this.corpusService.getJob(input.id)
		})
	}

	@StrictThrottle()
	@RequirePermissions("corpus.ingest.run")
	@Implement(v1.corpus.jobs.rerun)
	async rerunJob(
		@Session() session: UserSession,
		@Req() request: Request & { id?: string | number }
	) {
		return implement(v1.corpus.jobs.rerun).handler(async ({ input }) => {
			const access = await this.rbacService.getUserAccess(session.user.id)
			const correlationId = String(request.id ?? crypto.randomUUID())

			await this.ragGatewayService
				.rerunJob({
					jobId: input.id,
					userId: session.user.id,
					permissions: access.permissions,
					requestId: correlationId,
					traceId: correlationId,
				})
				.catch(toORPCError)
			return { id: input.id, status: "queued" as const }
		})
	}
}
