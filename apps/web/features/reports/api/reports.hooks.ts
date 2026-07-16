"use client"

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"

import type {
	CreateReportSchedule,
	ReportDefinition,
	ReportExport,
	ReportFile,
	ReportFileSummary,
	ReportResult,
	ReportSchedule,
	UpdateReportSchedule,
} from "@repo/contracts"

import { ApiError } from "@/core/lib/api-error"
import { env } from "@/env"

const API_BASE = `${env.NEXT_PUBLIC_API_BASE_URL}/${env.NEXT_PUBLIC_API_VERSION}`

async function parseError(response: Response): Promise<never> {
	let message = `Request failed (${response.status})`
	try {
		const body = (await response.json()) as { message?: string; error?: { message?: string } }
		message = body?.message ?? body?.error?.message ?? message
	} catch {
		// ignore non-JSON error bodies
	}
	throw new ApiError(response.status, message)
}

export function useReportsListQuery() {
	return useQuery({
		queryKey: ["reports", "list"] as const,
		queryFn: async () => {
			const response = await fetch(`${API_BASE}/reports`, { credentials: "include" })
			if (!response.ok) return parseError(response)
			return (await response.json()) as ReportDefinition[]
		},
	})
}

export function useRunReportQuery(
	key: string | null,
	filters: Record<string, string>,
	page: number,
	pageSize: number
) {
	return useQuery({
		queryKey: ["reports", "run", key, filters, page, pageSize] as const,
		enabled: key !== null,
		queryFn: async () => {
			const response = await fetch(`${API_BASE}/reports/${key}/run`, {
				method: "POST",
				credentials: "include",
				headers: { "Content-Type": "application/json" },
				body: JSON.stringify({ key, filters, page, pageSize }),
			})
			if (!response.ok) return parseError(response)
			return (await response.json()) as ReportResult
		},
	})
}

function base64ToBytes(content: string): Uint8Array {
	const binary = atob(content)
	const bytes = new Uint8Array(binary.length)
	for (let index = 0; index < binary.length; index += 1) {
		bytes[index] = binary.charCodeAt(index)
	}
	return bytes
}

function downloadFile(filename: string, contentType: string, body: string | Uint8Array) {
	const blob = new Blob([body as BlobPart], { type: contentType })
	const url = URL.createObjectURL(blob)
	const anchor = document.createElement("a")
	anchor.href = url
	anchor.download = filename
	anchor.click()
	URL.revokeObjectURL(url)
}

export function useExportReportMutation() {
	return useMutation({
		mutationFn: async (input: {
			key: string
			filters: Record<string, string>
			format: "json" | "csv" | "xlsx" | "pdf"
		}) => {
			const response = await fetch(`${API_BASE}/reports/${input.key}/export`, {
				method: "POST",
				credentials: "include",
				headers: { "Content-Type": "application/json" },
				body: JSON.stringify(input),
			})
			if (!response.ok) return parseError(response)
			return (await response.json()) as ReportExport
		},
		onSuccess: exported => {
			// Binary formats (xlsx/pdf) arrive base64-encoded (RP-6).
			const body =
				exported.encoding === "base64"
					? base64ToBytes(exported.content)
					: exported.content
			downloadFile(exported.filename, exported.contentType, body)
		},
	})
}

// ── RP-6: schedules + generated files ───────────────────────────────────────

export function isPermissionError(error: unknown): boolean {
	return error instanceof ApiError && error.status === 403
}

export function useSchedulesQuery() {
	return useQuery({
		queryKey: ["reports", "schedules"] as const,
		retry: (failureCount, error) => !isPermissionError(error) && failureCount < 3,
		queryFn: async () => {
			const response = await fetch(`${API_BASE}/reports-schedules`, {
				credentials: "include",
			})
			if (!response.ok) return parseError(response)
			return (await response.json()) as ReportSchedule[]
		},
	})
}

export function useCreateScheduleMutation() {
	const queryClient = useQueryClient()
	return useMutation({
		mutationFn: async (input: CreateReportSchedule) => {
			const response = await fetch(`${API_BASE}/reports-schedules`, {
				method: "POST",
				credentials: "include",
				headers: { "Content-Type": "application/json" },
				body: JSON.stringify(input),
			})
			if (!response.ok) return parseError(response)
			return (await response.json()) as ReportSchedule
		},
		onSuccess: () => queryClient.invalidateQueries({ queryKey: ["reports", "schedules"] }),
	})
}

export function useUpdateScheduleMutation() {
	const queryClient = useQueryClient()
	return useMutation({
		mutationFn: async (input: UpdateReportSchedule) => {
			const response = await fetch(`${API_BASE}/reports-schedules/${input.id}`, {
				method: "PATCH",
				credentials: "include",
				headers: { "Content-Type": "application/json" },
				body: JSON.stringify(input),
			})
			if (!response.ok) return parseError(response)
			return (await response.json()) as ReportSchedule
		},
		onSuccess: () => queryClient.invalidateQueries({ queryKey: ["reports", "schedules"] }),
	})
}

export function useDeleteScheduleMutation() {
	const queryClient = useQueryClient()
	return useMutation({
		mutationFn: async (id: string) => {
			const response = await fetch(`${API_BASE}/reports-schedules/${id}`, {
				method: "DELETE",
				credentials: "include",
			})
			if (!response.ok) return parseError(response)
			return (await response.json()) as { deleted: boolean }
		},
		onSuccess: () => queryClient.invalidateQueries({ queryKey: ["reports", "schedules"] }),
	})
}

export function useReportFilesQuery() {
	return useQuery({
		queryKey: ["reports", "files"] as const,
		refetchInterval: 30_000,
		queryFn: async () => {
			const response = await fetch(`${API_BASE}/reports-files`, { credentials: "include" })
			if (!response.ok) return parseError(response)
			return (await response.json()) as ReportFileSummary[]
		},
	})
}

export function useDownloadReportFileMutation() {
	return useMutation({
		mutationFn: async (id: string) => {
			const response = await fetch(`${API_BASE}/reports-files/${id}`, {
				credentials: "include",
			})
			if (!response.ok) return parseError(response)
			return (await response.json()) as ReportFile
		},
		onSuccess: file => {
			downloadFile(file.filename, file.contentType, base64ToBytes(file.contentBase64))
		},
	})
}
