"use client"

import { useQuery } from "@tanstack/react-query"

import type { EffectiveConfig } from "@repo/contracts"

import { ApiError } from "@/core/lib/api-error"
import { env } from "@/env"

const API_BASE = `${env.NEXT_PUBLIC_API_BASE_URL}/${env.NEXT_PUBLIC_API_VERSION}`

export function useEffectiveConfigQuery() {
	return useQuery({
		queryKey: ["ops", "config"] as const,
		queryFn: async () => {
			const response = await fetch(`${API_BASE}/ops/config`, {
				credentials: "include",
				headers: { "Content-Type": "application/json" },
			})
			if (!response.ok) {
				let message = `Request failed (${response.status})`
				try {
					const body = (await response.json()) as {
						message?: string
						error?: { message?: string }
					}
					message = body?.message ?? body?.error?.message ?? message
				} catch {
					// ignore non-JSON error bodies
				}
				throw new ApiError(response.status, message)
			}
			return (await response.json()) as EffectiveConfig
		},
	})
}
