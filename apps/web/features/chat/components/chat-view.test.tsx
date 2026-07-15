import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { render, screen } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { afterEach, describe, expect, it, vi } from "vitest"

import type { ChatAnswer } from "@repo/contracts"

import { ChatView } from "./chat-view"

const citationFixture = {
	chunkId: "3f5a2c1e-0000-4000-8000-000000000001",
	documentId: "3f5a2c1e-0000-4000-8000-000000000002",
	source: "hr-handbook.pdf",
	page: 12,
	section: "Leave policy",
}

const answerFixture: ChatAnswer = {
	answer: "Vacation carry-over is capped at five days.",
	citations: [citationFixture],
	conversationId: "3f5a2c1e-0000-4000-8000-000000000003",
	messageId: "3f5a2c1e-0000-4000-8000-000000000004",
	traceId: "trace-1",
	model: "test-model",
	tokenUsage: null,
	isIDontKnow: false,
	degraded: false,
}

function jsonResponse(body: unknown, init?: ResponseInit): Response {
	return new Response(JSON.stringify(body), {
		status: 200,
		headers: { "Content-Type": "application/json" },
		...init,
	})
}

/** SSE stream response (GN-3 shape): token frames then an optional final. */
function sseResponse(events: [string, unknown][]): Response {
	const body = events
		.map(([event, data]) => `event: ${event}
data: ${JSON.stringify(data)}

`)
		.join("")
	return new Response(body, {
		status: 200,
		headers: { "Content-Type": "text/event-stream" },
	})
}

function finalFrame(answer: ChatAnswer): [string, unknown] {
	return ["final", { ...answer, ttftMs: 12, latencyMs: 100 }]
}

function streamOf(answer: ChatAnswer): Response {
	return sseResponse([["token", { delta: answer.answer }], finalFrame(answer)])
}

function renderChatView(hasAccess = true) {
	const queryClient = new QueryClient({
		defaultOptions: { mutations: { retry: false } },
	})
	return render(
		<QueryClientProvider client={queryClient}>
			<ChatView hasAccess={hasAccess} />
		</QueryClientProvider>
	)
}

async function askQuestion(question = "How many vacation days carry over?") {
	const user = userEvent.setup()
	await user.type(screen.getByRole("textbox", { name: /question/i }), question)
	await user.click(screen.getByRole("button", { name: /send/i }))
	return user
}

afterEach(() => {
	vi.unstubAllGlobals()
	vi.restoreAllMocks()
})

describe("ChatView", () => {
	it("shows the no-access state without a composer when the user lacks chat.query.submit", () => {
		renderChatView(false)

		expect(screen.getByText(/no access/i)).toBeInTheDocument()
		expect(screen.getByText(/chat\.query\.submit/)).toBeInTheDocument()
		expect(screen.queryByRole("button", { name: /send/i })).not.toBeInTheDocument()
	})

	it("disables Send while the input is empty", () => {
		vi.stubGlobal("fetch", vi.fn())
		renderChatView()

		expect(screen.getByRole("button", { name: /send/i })).toBeDisabled()
	})

	it("renders the answer with clickable citations showing source + page/section", async () => {
		vi.stubGlobal("fetch", vi.fn().mockResolvedValue(streamOf(answerFixture)))
		renderChatView()

		await askQuestion()

		expect(await screen.findByText(answerFixture.answer)).toBeInTheDocument()
		const citation = screen.getByRole("link", {
			name: /hr-handbook\.pdf · p\. 12 · Leave policy/,
		})
		expect(citation).toHaveAttribute("href", "/corpus")
	})

	it("renders a citation with a null source as a non-clickable Unknown source chip", async () => {
		const answer: ChatAnswer = {
			...answerFixture,
			citations: [{ ...citationFixture, source: null }],
		}
		vi.stubGlobal("fetch", vi.fn().mockResolvedValue(streamOf(answer)))
		renderChatView()

		await askQuestion()

		expect(await screen.findByText(/unknown source/i)).toBeInTheDocument()
		expect(screen.queryByRole("link", { name: /unknown source/i })).not.toBeInTheDocument()
	})

	it("renders the I-don't-know state without citations and without an error", async () => {
		const answer: ChatAnswer = {
			...answerFixture,
			answer: "I don't know based on the current corpus.",
			citations: [],
			isIDontKnow: true,
		}
		vi.stubGlobal("fetch", vi.fn().mockResolvedValue(streamOf(answer)))
		renderChatView()

		await askQuestion()

		expect(await screen.findByText(answer.answer)).toBeInTheDocument()
		expect(screen.getByText(/no supporting sources/i)).toBeInTheDocument()
		expect(screen.queryByRole("list", { name: /citations/i })).not.toBeInTheDocument()
		expect(screen.queryByRole("alert")).not.toBeInTheDocument()
	})

	it("flags a degraded answer with a service-degraded notice", async () => {
		const answer: ChatAnswer = {
			...answerFixture,
			answer: "The answer service is temporarily unavailable.",
			citations: [],
			degraded: true,
		}
		vi.stubGlobal("fetch", vi.fn().mockResolvedValue(streamOf(answer)))
		renderChatView()

		await askQuestion()

		expect(await screen.findByText(/service degraded/i)).toBeInTheDocument()
		expect(screen.getByText(answer.answer)).toBeInTheDocument()
	})

	it("surfaces a graceful degradation message when the request fails", async () => {
		vi.stubGlobal(
			"fetch",
			vi.fn().mockResolvedValue(jsonResponse({ message: "boom" }, { status: 500 }))
		)
		renderChatView()

		await askQuestion()

		const alert = await screen.findByRole("alert")
		expect(alert).toHaveTextContent(/unavailable right now/i)
	})

	it("shows the rate-limit banner on a 429 instead of an in-thread error", async () => {
		vi.stubGlobal(
			"fetch",
			vi.fn().mockResolvedValue(
				jsonResponse(
					{ message: "Too many requests" },
					{ status: 429, headers: { "Content-Type": "application/json", "Retry-After": "30" } }
				)
			)
		)
		renderChatView()

		await askQuestion()

		expect(await screen.findByText(/try again in 30 seconds/i)).toBeInTheDocument()
		expect(screen.queryByRole("alert")).not.toBeInTheDocument()
	})

	it("disables Send and shows a typing indicator while awaiting the answer", async () => {
		let resolveFetch: (value: Response) => void = () => {}
		vi.stubGlobal(
			"fetch",
			vi.fn().mockReturnValue(
				new Promise<Response>(resolve => {
					resolveFetch = resolve
				})
			)
		)
		renderChatView()

		await askQuestion()

		expect(await screen.findByRole("status", { name: /waiting for answer/i })).toBeInTheDocument()
		expect(screen.getByRole("button", { name: /sending/i })).toBeDisabled()

		resolveFetch(streamOf(answerFixture))
		expect(await screen.findByText(answerFixture.answer)).toBeInTheDocument()
	})

	it("streams tokens progressively and attaches citations on the final frame (CH-2)", async () => {
		const deltas = ["Vacation carry-over ", "is capped at ", "five days."]
		vi.stubGlobal(
			"fetch",
			vi.fn().mockResolvedValue(
				sseResponse([
					...deltas.map(delta => ["token", { delta }] as [string, unknown]),
					finalFrame(answerFixture),
				])
			)
		)
		renderChatView()

		await askQuestion()

		// Final content assembled from the streamed deltas...
		expect(await screen.findByText(answerFixture.answer)).toBeInTheDocument()
		// ...with citations attached on the final frame.
		expect(
			screen.getByRole("link", { name: /hr-handbook\.pdf · p\. 12 · Leave policy/ })
		).toBeInTheDocument()
	})

	it("renders a partial answer with a retry affordance when the stream drops (CH-2)", async () => {
		const fetchMock = vi
			.fn()
			// First attempt: tokens arrive but the stream ends with NO final frame.
			.mockResolvedValueOnce(sseResponse([["token", { delta: "Partial answ" }]]))
			// Retry: a complete stream.
			.mockResolvedValueOnce(streamOf(answerFixture))
		vi.stubGlobal("fetch", fetchMock)
		renderChatView()

		const user = await askQuestion()

		// Honest partial state — text kept, flagged, never a frozen spinner.
		expect(await screen.findByText(/connection lost/i)).toBeInTheDocument()
		expect(screen.getByText("Partial answ")).toBeInTheDocument()
		expect(screen.queryByRole("status", { name: /waiting for answer/i })).not.toBeInTheDocument()

		const retry = screen.getByRole("button", { name: /retry question/i })
		await user.click(retry)

		expect(await screen.findByText(answerFixture.answer)).toBeInTheDocument()
		expect(fetchMock).toHaveBeenCalledTimes(2)
	})
})
