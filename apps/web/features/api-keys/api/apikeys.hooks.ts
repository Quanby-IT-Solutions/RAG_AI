"use client"

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"

import type { ApiKey, CreateApiKeyRequest, CreateApiKeyResponse } from "@repo/contracts"

import { ApiError } from "@/core/lib/api-error"
import { env } from "@/env"

const API_BASE = `${env.NEXT_PUBLIC_API_BASE_URL}/${env.NEXT_PUBLIC_API_VERSION}`

async function apiFetch<T>(path: string, init?: RequestInit): Promise<T> {
	const response = await fetch(`${API_BASE}${path}`, {
		...init,
		credentials: "include",
		headers: {
			"Content-Type": "application/json",
			...init?.headers,
		},
	})

	if (!response.ok) {
		let message = `Request failed (${response.status})`
		try {
			const body = (await response.json()) as { message?: string; error?: { message?: string } }
			message = body?.message ?? body?.error?.message ?? message
		} catch {
			// ignore non-JSON error bodies
		}
		const retryAfterHeader =
			response.headers.get("retry-after") ?? response.headers.get("x-retry-after")
		const parsedRetryAfter = retryAfterHeader ? Number.parseInt(retryAfterHeader, 10) : NaN
		const retryAfter = Number.isNaN(parsedRetryAfter) ? undefined : parsedRetryAfter
		throw new ApiError(response.status, message, retryAfter)
	}

	return (await response.json()) as T
}

/** Wire shape: dates arrive as ISO strings over JSON. */
export type ApiKeyUi = Omit<ApiKey, "lastUsedAt" | "revokedAt" | "createdAt"> & {
	lastUsedAt: string | null
	revokedAt: string | null
	createdAt: string
}

export const apiKeysKeys = {
	list: ["apikeys", "list"] as const,
}

export function useApiKeysQuery() {
	return useQuery({
		queryKey: apiKeysKeys.list,
		queryFn: () => apiFetch<ApiKeyUi[]>("/apikeys"),
	})
}

export function useCreateApiKeyMutation() {
	const queryClient = useQueryClient()
	return useMutation({
		mutationFn: (input: CreateApiKeyRequest) =>
			apiFetch<CreateApiKeyResponse>("/apikeys", {
				method: "POST",
				body: JSON.stringify(input),
			}),
		onSuccess: () => {
			void queryClient.invalidateQueries({ queryKey: apiKeysKeys.list })
		},
	})
}

export function useRevokeApiKeyMutation() {
	const queryClient = useQueryClient()
	return useMutation({
		mutationFn: (id: string) =>
			apiFetch<{ id: string; revoked: true }>(`/apikeys/${id}`, { method: "DELETE" }),
		onSuccess: () => {
			void queryClient.invalidateQueries({ queryKey: apiKeysKeys.list })
		},
	})
}
