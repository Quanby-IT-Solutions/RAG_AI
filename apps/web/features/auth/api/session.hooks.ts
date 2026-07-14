"use client"

import { useRouter } from "next/navigation"
import { queryOptions, useMutation, useQueryClient } from "@tanstack/react-query"

import { authClient } from "@/services/better-auth/auth-client"

/**
 * Centralized query keys for session-related queries.
 * Use these constants to ensure consistent cache invalidation.
 */
export const sessionKeys = {
	all: ["session"] as const,
}

/**
 * Query hook for fetching the current user session.
 *
 * Session is cached for 5 minutes before becoming stale.
 * Uses Better Auth client which automatically handles cookies.
 */
export const sessionOptions = queryOptions({
	queryKey: sessionKeys.all,
	queryFn: async () => {
		const result = await authClient.getSession()
		if (result.error) {
			return null
		}
		return result.data
	},
	staleTime: 5 * 60 * 1000, // 5 minutes
	retry: false,
})

/**
 * Mutation hook for signing out the current user.
 *
 * Invalidates the session query and redirects to home page on success.
 */
export function useSignOutMutation() {
	const router = useRouter()
	const queryClient = useQueryClient()

	return useMutation({
		mutationFn: async () => {
			const result = await authClient.signOut()
			if (result.error) {
				throw new Error(result.error.message || "Failed to sign out")
			}
			return result
		},
		onSuccess: () => {
			queryClient.invalidateQueries({ queryKey: sessionKeys.all })
			router.push("/")
			router.refresh()
		},
	})
}
