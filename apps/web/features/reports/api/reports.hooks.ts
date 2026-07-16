"use client"

import { useMutation, useQuery } from "@tanstack/react-query"

import type { ReportDefinition, ReportExport, ReportResult } from "@repo/contracts"

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

export function useExportReportMutation() {
	return useMutation({
		mutationFn: async (input: {
			key: string
			filters: Record<string, string>
			format: "json" | "csv"
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
			const blob = new Blob([exported.content], { type: exported.contentType })
			const url = URL.createObjectURL(blob)
			const anchor = document.createElement("a")
			anchor.href = url
			anchor.download = exported.filename
			anchor.click()
			URL.revokeObjectURL(url)
		},
	})
}
