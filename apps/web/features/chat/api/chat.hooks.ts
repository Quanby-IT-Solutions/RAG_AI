"use client"

import { useMutation } from "@tanstack/react-query"

import type { ChatAnswer, ChatQueryInput } from "@repo/contracts"

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
