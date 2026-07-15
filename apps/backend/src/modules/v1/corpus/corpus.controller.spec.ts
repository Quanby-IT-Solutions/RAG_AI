import { BadRequestException, UnsupportedMediaTypeException } from "@nestjs/common"
import { Test, type TestingModule } from "@nestjs/testing"

import { RbacService } from "@/common/rbac/rbac.service"
import { REQUIRED_PERMISSIONS_KEY } from "@/shared/decorators/require-permissions.decorator"

import { CorpusController } from "./corpus.controller"
import { CorpusService } from "./corpus.service"
import { MAX_PDF_UPLOAD_BYTES, pdfFileFilter, pdfUploadOptions } from "./corpus.upload-options"
import { RagGatewayService } from "./rag-gateway.service"

jest.mock("@orpc/nest", () => ({
	Implement: () => () => undefined,
}))

jest.mock("@orpc/server", () => ({
	implement: jest.fn(() => ({
		handler: jest.fn((fn: () => unknown) => fn()),
	})),
}))

jest.mock("@/config/api-versions.config", () => ({
	v1: { corpus: { documents: { list: {} } } },
}))

jest.mock("@/config/env.config", () => ({
	env: {
		NODE_ENV: "test",
		RAG_SERVICE_URL: "http://rag:8000",
		RAG_SERVICE_TOKEN_SECRET: "test-secret",
		RAG_SERVICE_TIMEOUT_MS: 5000,
		RAG_SERVICE_TOKEN_TTL_SECONDS: 120,
	},
}))

jest.mock("@/common/database/database.client", () => ({
	db: {},
}))

jest.mock("@/common/rbac/rbac.service", () => ({
	RbacService: jest.fn(),
}))

jest.mock("@thallesp/nestjs-better-auth", () => ({
	Session: () => () => undefined,
}))

describe("CorpusController", () => {
	let controller: CorpusController
	let rbacService: { getUserAccess: jest.Mock }
	let ragGatewayService: { ingestDocument: jest.Mock }
	let corpusService: { listDocuments: jest.Mock }

	beforeEach(async () => {
		rbacService = { getUserAccess: jest.fn() }
		ragGatewayService = { ingestDocument: jest.fn() }
		corpusService = { listDocuments: jest.fn() }

		const module: TestingModule = await Test.createTestingModule({
			controllers: [CorpusController],
			providers: [
				{ provide: RbacService, useValue: rbacService },
				{ provide: RagGatewayService, useValue: ragGatewayService },
				{ provide: CorpusService, useValue: corpusService },
			],
		}).compile()

		controller = module.get(CorpusController)
	})

	describe("authorization metadata (fail-closed keys)", () => {
		it("requires BOTH corpus.documents.upload AND corpus.ingest.run on upload (C4)", () => {
			const required = Reflect.getMetadata(
				REQUIRED_PERMISSIONS_KEY,
				CorpusController.prototype.uploadDocument
			)
			expect(required).toEqual(["corpus.documents.upload", "corpus.ingest.run"])
		})

		it("requires corpus.documents.read on the list route", () => {
			const required = Reflect.getMetadata(
				REQUIRED_PERMISSIONS_KEY,
				CorpusController.prototype.listDocuments
			)
			expect(required).toEqual(["corpus.documents.read"])
		})
	})

	describe("Multer gateway limits (rejected BEFORE forwarding)", () => {
		const asMulterFile = (mimetype: string) =>
			({ mimetype, originalname: "f", fieldname: "file" }) as Express.Multer.File

		it("caps the file size at exactly 25 MB with a single file", () => {
			expect(pdfUploadOptions.limits).toEqual({ fileSize: 25 * 1024 * 1024, files: 1 })
			expect(MAX_PDF_UPLOAD_BYTES).toBe(26214400)
		})

		it("accepts application/pdf", () => {
			const callback = jest.fn()
			pdfFileFilter({} as never, asMulterFile("application/pdf"), callback)
			expect(callback).toHaveBeenCalledWith(null, true)
		})

		it.each(["text/plain", "image/png", "application/msword", "application/octet-stream"])(
			"rejects %s with a clear 415",
			mimetype => {
				const callback = jest.fn()
				pdfFileFilter({} as never, asMulterFile(mimetype), callback)
				const [error] = callback.mock.calls[0] as [Error]
				expect(error).toBeInstanceOf(UnsupportedMediaTypeException)
				expect(error.message).toContain("Only PDF uploads are accepted")
			}
		)
	})

	describe("uploadDocument", () => {
		const session = { user: { id: "user-1" } } as never
		const request = { id: "req-42" } as never
		const file = {
			buffer: Buffer.from("%PDF-1.4 test"),
			originalname: "handbook.pdf",
			mimetype: "application/pdf",
			size: 13,
		} as Express.Multer.File

		it("rejects when no file part is present", async () => {
			await expect(controller.uploadDocument(undefined, session, request)).rejects.toBeInstanceOf(
				BadRequestException
			)
			expect(ragGatewayService.ingestDocument).not.toHaveBeenCalled()
		})

		it("forwards the file with the caller's principal + permissions and correlation id", async () => {
			rbacService.getUserAccess.mockResolvedValue({
				roles: ["Knowledge Manager"],
				permissions: ["corpus.documents.upload", "corpus.ingest.run"],
			})
			ragGatewayService.ingestDocument.mockResolvedValue({ status: "processing" })

			await expect(controller.uploadDocument(file, session, request)).resolves.toEqual({
				status: "processing",
			})

			expect(rbacService.getUserAccess).toHaveBeenCalledWith("user-1")
			expect(ragGatewayService.ingestDocument).toHaveBeenCalledWith({
				buffer: file.buffer,
				filename: "handbook.pdf",
				contentType: "application/pdf",
				userId: "user-1",
				permissions: ["corpus.documents.upload", "corpus.ingest.run"],
				requestId: "req-42",
				traceId: "req-42",
			})
		})
	})

	describe("listDocuments", () => {
		it("returns the corpus list from the service", async () => {
			const rows = [{ id: "d1", status: "processing" }]
			corpusService.listDocuments.mockResolvedValue(rows)

			await expect(controller.listDocuments()).resolves.toEqual(rows)
		})
	})
})
