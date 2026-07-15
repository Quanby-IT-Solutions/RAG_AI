"use client"

import { useMutation, useQuery } from "@tanstack/react-query"

import type { ChatAnswer, ChatCitation, ChatQueryInput } from "@repo/contracts"

import { ApiError } from "@/core/lib/api-error"
import { env } from "@/env"

const API_BASE = `${env.NEXT_PUBLIC_API_BASE_URL}/${env.NEXT_PUBLIC_API_VERSION}`

export function isPermissionError(error: unknown): boolean {
	return error instanceof ApiError && error.status === 403
}

async function parseError(response: Response): Promise<ApiError> {
	let message = `Request failed (${response.status})`
	try {
		const body = (await response.json()) as { message?: string; error?: { message?: string } }
		message = body?.message ?? body?.error?.message ?? message
	} catch {
		// ignore non-JSON error bodies
	}
	// Carry Retry-After so the shared 429 handling can drive a countdown.
	const retryAfterHeader = response.headers.get("retry-after") ?? response.headers.get("x-retry-after")
	const parsedRetryAfter = retryAfterHeader ? Number.parseInt(retryAfterHeader, 10) : NaN
	const retryAfter = Number.isNaN(parsedRetryAfter) ? undefined : parsedRetryAfter
	return new ApiError(response.status, message, retryAfter)
}

// POST /chat/query (GN-1 contract, non-streamed): the full grounded answer
// arrives in one response. R1 is single-turn — every submit starts a new
// conversation server-side.
async function submitChatQuery(input: ChatQueryInput): Promise<ChatAnswer> {
	const response = await fetch(`${API_BASE}/chat/query`, {
		method: "POST",
		credentials: "include",
		headers: { "Content-Type": "application/json" },
		body: JSON.stringify(input),
	})
	if (!response.ok) {
		throw await parseError(response)
	}
	return (await response.json()) as ChatAnswer
}

export function useChatQueryMutation() {
	return useMutation<ChatAnswer, Error, ChatQueryInput>({
		mutationKey: ["chat", "query"],
		mutationFn: submitChatQuery,
	})
}

// ── Conversation history (CH-3) ─────────────────────────────────────────────

export type ConversationSummaryUi = {
	id: string
	title: string
	messageCount: number
	createdAt: string
	lastMessageAt: string | null
}

export type ConversationMessageUi = {
	id: string
	role: "user" | "assistant"
	content: string
	citations: ChatCitation[] | null
	turnIndex: number
	createdAt: string
}

export type ConversationDetailUi = {
	id: string
	createdAt: string
	messages: ConversationMessageUi[]
}

async function fetchConversations(): Promise<ConversationSummaryUi[]> {
	const response = await fetch(`${API_BASE}/chat/history`, {
		credentials: "include",
		headers: { "Content-Type": "application/json" },
	})
	if (!response.ok) {
		throw await parseError(response)
	}
	return (await response.json()) as ConversationSummaryUi[]
}

async function fetchConversation(id: string): Promise<ConversationDetailUi> {
	const response = await fetch(`${API_BASE}/chat/history/${id}`, {
		credentials: "include",
		headers: { "Content-Type": "application/json" },
	})
	if (!response.ok) {
		throw await parseError(response)
	}
	return (await response.json()) as ConversationDetailUi
}

export function useConversationsQuery(enabled = true) {
	return useQuery({
		queryKey: ["chat", "history"],
		queryFn: fetchConversations,
		enabled,
		retry: (failureCount, error) => !isPermissionError(error) && failureCount < 3,
	})
}

export function useConversationQuery(id: string | null) {
	return useQuery({
		queryKey: ["chat", "history", id ?? "none"],
		queryFn: () => fetchConversation(id as string),
		enabled: id !== null,
		retry: (failureCount, error) => !isPermissionError(error) && failureCount < 3,
	})
}

// ── Streaming mode (GN-3 / CH-2) ─────────────────────────────────────────────

/** Final SSE frame: the GN-1 answer envelope plus stream timing. */
export type ChatStreamFinal = ChatAnswer & { ttftMs?: number | null; latencyMs?: number }

export interface ChatStreamHandlers {
	onToken: (delta: string) => void
	onFinal: (final: ChatStreamFinal) => void
}

/** Thrown when the SSE connection drops before the final frame — the caller
 * renders a partial-answer state with a retry affordance (CH-2 AC). */
export class ChatStreamInterruptedError extends Error {
	constructor(public readonly receivedTokens: boolean) {
		super("The connection dropped before the answer finished.")
		this.name = "ChatStreamInterruptedError"
	}
}

/**
 * POST /chat/stream (SSE): calls onToken per delta and onFinal exactly once.
 * Pre-stream HTTP failures throw ApiError (429 carries Retry-After); a
 * connection dropped mid-stream throws ChatStreamInterruptedError.
 */
export async function streamChatQuery(
	input: ChatQueryInput,
	handlers: ChatStreamHandlers,
	signal?: AbortSignal
): Promise<void> {
	let response: Response
	try {
		response = await fetch(`${API_BASE}/chat/stream`, {
			method: "POST",
			credentials: "include",
			headers: { "Content-Type": "application/json" },
			body: JSON.stringify(input),
			signal,
		})
	} catch (error) {
		if ((error as Error | null)?.name === "AbortError") throw error
		throw new ChatStreamInterruptedError(false)
	}
	if (!response.ok) {
		throw await parseError(response)
	}
	if (!response.body) {
		throw new ChatStreamInterruptedError(false)
	}

	const reader = response.body.getReader()
	const decoder = new TextDecoder()
	let buffer = ""
	let sawFinal = false
	let receivedTokens = false

	const handleFrame = (frame: string) => {
		let event = "message"
		let data = ""
		for (const line of frame.split("\n")) {
			if (line.startsWith("event: ")) event = line.slice(7).trim()
			else if (line.startsWith("data: ")) data += line.slice(6)
		}
		if (!data) return
		if (event === "token") {
			receivedTokens = true
			handlers.onToken((JSON.parse(data) as { delta: string }).delta)
		} else if (event === "final") {
			sawFinal = true
			handlers.onFinal(JSON.parse(data) as ChatStreamFinal)
		}
	}

	try {
		for (;;) {
			const { done, value } = await reader.read()
			if (done) break
			buffer += decoder.decode(value, { stream: true })
			let separator = buffer.indexOf("\n\n")
			while (separator !== -1) {
				handleFrame(buffer.slice(0, separator))
				buffer = buffer.slice(separator + 2)
				separator = buffer.indexOf("\n\n")
			}
		}
	} catch (error) {
		if ((error as Error | null)?.name === "AbortError") throw error
		throw new ChatStreamInterruptedError(receivedTokens)
	}

	if (!sawFinal) {
		// Server closed the stream without a final frame — treat as a drop.
		throw new ChatStreamInterruptedError(receivedTokens)
	}
}
