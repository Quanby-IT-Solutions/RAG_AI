import { createHmac } from "node:crypto"
import {
	BadGatewayException,
	GatewayTimeoutException,
	HttpException,
} from "@nestjs/common"

import { RagGatewayService } from "./rag-gateway.service"

jest.mock("@/config/env.config", () => ({
	env: {
		RAG_SERVICE_URL: "http://rag:8000",
		RAG_SERVICE_TOKEN_SECRET: "test-secret",
		RAG_SERVICE_TIMEOUT_MS: 1234,
		RAG_SERVICE_TOKEN_TTL_SECONDS: 120,
	},
}))

const decodeSegment = (segment: string) =>
	JSON.parse(Buffer.from(segment, "base64url").toString("utf8")) as Record<string, unknown>

describe("RagGatewayService", () => {
	let service: RagGatewayService
	let fetchMock: jest.Mock
	const originalFetch = global.fetch

	const input = {
		buffer: Buffer.from("%PDF-1.4 body"),
		filename: "handbook.pdf",
		contentType: "application/pdf",
		userId: "user-1",
		permissions: ["corpus.documents.upload", "corpus.ingest.run"],
		requestId: "req-9",
		traceId: "req-9",
	}

	const okResponse = () =>
		({
			ok: true,
			status: 200,
			json: jest.fn().mockResolvedValue({ id: "d1", status: "processing" }),
		}) as unknown as Response

	beforeEach(() => {
		service = new RagGatewayService()
		fetchMock = jest.fn()
		global.fetch = fetchMock as unknown as typeof fetch
	})

	afterEach(() => {
		global.fetch = originalFetch
	})

	describe("mintServiceToken", () => {
		it("mints a short-lived HS256 JWT with sub/permissions/requestId/traceId", () => {
			const token = service.mintServiceToken({
				sub: "user-1",
				permissions: ["corpus.documents.upload"],
				requestId: "req-1",
				traceId: "trace-1",
			})

			const [headerSegment = "", payloadSegment = "", signature = ""] = token.split(".")
			expect(decodeSegment(headerSegment)).toEqual({ alg: "HS256", typ: "JWT" })

			const payload = decodeSegment(payloadSegment)
			expect(payload).toMatchObject({
				sub: "user-1",
				permissions: ["corpus.documents.upload"],
				requestId: "req-1",
				traceId: "trace-1",
				actorType: "user",
			})
			// Short-lived: exp = iat + configured TTL (120s).
			expect((payload.exp as number) - (payload.iat as number)).toBe(120)

			// Signature verifies against the shared secret (what PyJWT checks).
			const expected = createHmac("sha256", "test-secret")
				.update(`${headerSegment}.${payloadSegment}`)
				.digest("base64url")
			expect(signature).toBe(expected)
		})
	})

	describe("ingestDocument", () => {
		it("forwards multipart to /internal/ingest with a bearer token under a hard timeout", async () => {
			fetchMock.mockResolvedValue(okResponse())

			await expect(service.ingestDocument(input)).resolves.toEqual({
				id: "d1",
				status: "processing",
			})

			expect(fetchMock).toHaveBeenCalledTimes(1)
			const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit]
			expect(url).toBe("http://rag:8000/internal/ingest")
			expect(init.method).toBe("POST")

			const headers = init.headers as Record<string, string>
			expect(headers.authorization).toMatch(/^Bearer [\w-]+\.[\w-]+\.[\w-]+$/)
			expect(headers["x-request-id"]).toBe("req-9")

			expect(init.body).toBeInstanceOf(FormData)
			const filePart = (init.body as FormData).get("file")
			expect(filePart).toBeInstanceOf(Blob)
			expect((filePart as File).name).toBe("handbook.pdf")
			expect((filePart as Blob).type).toBe("application/pdf")

			// Hard-timeout contract: an AbortSignal is always attached.
			expect(init.signal).toBeInstanceOf(AbortSignal)
		})

		it("maps AbortSignal timeouts to 504", async () => {
			fetchMock.mockRejectedValue(new DOMException("aborted", "TimeoutError"))

			await expect(service.ingestDocument(input)).rejects.toBeInstanceOf(GatewayTimeoutException)
		})

		it("maps network failures to 502", async () => {
			fetchMock.mockRejectedValue(new TypeError("fetch failed"))

			await expect(service.ingestDocument(input)).rejects.toBeInstanceOf(BadGatewayException)
		})

		it("passes through 4xx errors from the RAG service's defensive re-checks", async () => {
			fetchMock.mockResolvedValue({
				ok: false,
				status: 415,
				json: jest.fn().mockResolvedValue({ detail: "Only PDF uploads are accepted" }),
			} as unknown as Response)

			try {
				await service.ingestDocument(input)
				fail("expected HttpException")
			} catch (error) {
				expect(error).toBeInstanceOf(HttpException)
				expect((error as HttpException).getStatus()).toBe(415)
				expect((error as HttpException).message).toBe("Only PDF uploads are accepted")
			}
		})

		it("maps RAG service 5xx to 502", async () => {
			fetchMock.mockResolvedValue({
				ok: false,
				status: 500,
				json: jest.fn().mockRejectedValue(new Error("not json")),
			} as unknown as Response)

			await expect(service.ingestDocument(input)).rejects.toBeInstanceOf(BadGatewayException)
		})
	})

	describe("submitChatQuery", () => {
		const chatInput = {
			query: "How many vacation days?",
			k: 3,
			userId: "user-1",
			permissions: ["chat.query.submit"],
			requestId: "req-7",
			traceId: "req-7",
		}

		const chatOkResponse = () =>
			({
				ok: true,
				status: 200,
				json: jest.fn().mockResolvedValue({ answer: "20 days [1].", conversationId: "c1" }),
			}) as unknown as Response

		it("forwards JSON to /internal/chat with a minted bearer token under a hard timeout", async () => {
			fetchMock.mockResolvedValue(chatOkResponse())

			await expect(service.submitChatQuery(chatInput)).resolves.toEqual({
				answer: "20 days [1].",
				conversationId: "c1",
			})

			expect(fetchMock).toHaveBeenCalledTimes(1)
			const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit]
			expect(url).toBe("http://rag:8000/internal/chat")
			expect(init.method).toBe("POST")

			const headers = init.headers as Record<string, string>
			expect(headers.authorization).toMatch(/^Bearer [\w-]+\.[\w-]+\.[\w-]+$/)
			expect(headers["content-type"]).toBe("application/json")
			expect(headers["x-request-id"]).toBe("req-7")

			// The service JWT carries the caller principal + permissions.
			const [, payloadSegment = ""] = ((headers.authorization ?? "").split(" ")[1] ?? "").split(".")
			expect(decodeSegment(payloadSegment)).toMatchObject({
				sub: "user-1",
				permissions: ["chat.query.submit"],
				requestId: "req-7",
				traceId: "req-7",
				actorType: "user",
			})

			expect(JSON.parse(init.body as string)).toEqual({
				query: "How many vacation days?",
				k: 3,
			})

			// Hard-timeout contract: an AbortSignal is always attached.
			expect(init.signal).toBeInstanceOf(AbortSignal)
		})

		it("omits k from the forwarded body when not provided", async () => {
			fetchMock.mockResolvedValue(chatOkResponse())

			await service.submitChatQuery({ ...chatInput, k: undefined })

			const [, init] = fetchMock.mock.calls[0] as [string, RequestInit]
			expect(JSON.parse(init.body as string)).toEqual({ query: "How many vacation days?" })
		})

		it("maps AbortSignal timeouts to 504", async () => {
			fetchMock.mockRejectedValue(new DOMException("aborted", "TimeoutError"))

			await expect(service.submitChatQuery(chatInput)).rejects.toBeInstanceOf(
				GatewayTimeoutException
			)
		})

		it("maps network failures to 502", async () => {
			fetchMock.mockRejectedValue(new TypeError("fetch failed"))

			await expect(service.submitChatQuery(chatInput)).rejects.toBeInstanceOf(BadGatewayException)
		})

		it("maps RAG service 5xx to 502", async () => {
			fetchMock.mockResolvedValue({
				ok: false,
				status: 500,
				json: jest.fn().mockRejectedValue(new Error("not json")),
			} as unknown as Response)

			await expect(service.submitChatQuery(chatInput)).rejects.toBeInstanceOf(BadGatewayException)
		})
	})
})
