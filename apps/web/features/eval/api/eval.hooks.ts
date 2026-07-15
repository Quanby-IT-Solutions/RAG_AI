"use client"

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"

import type {
	CreateGoldenItemRequest,
	EvalBaseline,
	EvalRun,
	ExecuteEvalRunRequest,
	GoldenChunkSearchRequest,
	GoldenItem,
	GoldenSetExport,
	UpdateGoldenItemRequest,
} from "@repo/contracts"

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
export type GoldenItemUi = Omit<GoldenItem, "createdAt" | "updatedAt"> & {
	createdAt: string
	updatedAt: string
}

export interface GoldenChunkCandidateUi {
	contentHash: string
	documentId: string
	content: string
	score: number | null
}

export const evalKeys = {
	golden: ["eval", "golden"] as const,
}

export function useGoldenItemsQuery() {
	return useQuery({
		queryKey: evalKeys.golden,
		queryFn: () => apiFetch<GoldenItemUi[]>("/eval/golden"),
	})
}

export function useCreateGoldenItemMutation() {
	const queryClient = useQueryClient()
	return useMutation({
		mutationFn: (input: CreateGoldenItemRequest) =>
			apiFetch<GoldenItemUi>("/eval/golden", {
				method: "POST",
				body: JSON.stringify(input),
			}),
		onSuccess: () => {
			void queryClient.invalidateQueries({ queryKey: evalKeys.golden })
		},
	})
}

export function useUpdateGoldenItemMutation() {
	const queryClient = useQueryClient()
	return useMutation({
		mutationFn: ({ id, ...rest }: UpdateGoldenItemRequest) =>
			apiFetch<GoldenItemUi>(`/eval/golden/${id}`, {
				method: "PUT",
				body: JSON.stringify(rest),
			}),
		onSuccess: () => {
			void queryClient.invalidateQueries({ queryKey: evalKeys.golden })
		},
	})
}

export function useImportGoldenSetMutation() {
	const queryClient = useQueryClient()
	return useMutation({
		mutationFn: (payload: GoldenSetExport) =>
			apiFetch<{ imported: number }>("/eval/golden/import", {
				method: "POST",
				body: JSON.stringify(payload),
			}),
		onSuccess: () => {
			void queryClient.invalidateQueries({ queryKey: evalKeys.golden })
		},
	})
}

export function exportGoldenSet(): Promise<GoldenSetExport> {
	return apiFetch<GoldenSetExport>("/eval/golden/export")
}

export function useSearchChunksMutation() {
	return useMutation({
		mutationFn: (input: GoldenChunkSearchRequest) =>
			apiFetch<GoldenChunkCandidateUi[]>("/eval/golden/search-chunks", {
				method: "POST",
				body: JSON.stringify(input),
			}),
	})
}

// ------------------------------------------------------------- EV-2: runs

/** Wire shape: dates arrive as ISO strings over JSON. */
export type EvalRunUi = Omit<EvalRun, "createdAt" | "finishedAt"> & {
	createdAt: string
	finishedAt: string | null
}
export type EvalRunSummaryUi = Omit<EvalRunUi, "perItem">

export interface EvalRunComparisonUi {
	base: EvalRunSummaryUi
	candidate: EvalRunSummaryUi
	deltas: Record<string, number>
}

export const evalRunKeys = {
	list: ["eval", "runs"] as const,
	detail: (id: string) => ["eval", "runs", id] as const,
	compare: (baseId: string, candidateId: string) =>
		["eval", "runs", "compare", baseId, candidateId] as const,
}

export function useEvalRunsQuery() {
	return useQuery({
		queryKey: evalRunKeys.list,
		queryFn: () => apiFetch<EvalRunSummaryUi[]>("/eval/runs"),
	})
}

export function useExecuteEvalRunMutation() {
	const queryClient = useQueryClient()
	return useMutation({
		mutationFn: (input: ExecuteEvalRunRequest) =>
			apiFetch<EvalRunUi>("/eval/runs", {
				method: "POST",
				body: JSON.stringify(input),
			}),
		onSettled: () => {
			// Failed runs are persisted too — refresh either way.
			void queryClient.invalidateQueries({ queryKey: evalRunKeys.list })
		},
	})
}

export function useEvalRunComparisonQuery(baseId: string | null, candidateId: string | null) {
	return useQuery({
		queryKey: evalRunKeys.compare(baseId ?? "-", candidateId ?? "-"),
		queryFn: () =>
			apiFetch<EvalRunComparisonUi>(`/eval/runs/${baseId}/compare/${candidateId}`),
		enabled: Boolean(baseId && candidateId && baseId !== candidateId),
	})
}

// -------------------------------------------------------------- EV-4: gate

export type EvalBaselineUi = Omit<EvalBaseline, "createdAt"> & { createdAt: string }

export const evalBaselineKeys = {
	current: ["eval", "baseline"] as const,
}

export function useEvalBaselineQuery() {
	return useQuery({
		queryKey: evalBaselineKeys.current,
		queryFn: () => apiFetch<EvalBaselineUi | null>("/eval/baseline"),
	})
}

export function usePromoteBaselineMutation() {
	const queryClient = useQueryClient()
	return useMutation({
		mutationFn: ({ id, note }: { id: string; note?: string }) =>
			apiFetch<EvalBaselineUi>(`/eval/runs/${id}/promote`, {
				method: "POST",
				body: JSON.stringify(note ? { note } : {}),
			}),
		onSuccess: () => {
			void queryClient.invalidateQueries({ queryKey: evalBaselineKeys.current })
		},
	})
}
