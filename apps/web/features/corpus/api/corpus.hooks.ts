"use client"

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"

import type { DocumentStatus } from "@repo/contracts"

import { ApiError } from "@/core/lib/api-error"
import { env } from "@/env"

const API_BASE = `${env.NEXT_PUBLIC_API_BASE_URL}/${env.NEXT_PUBLIC_API_VERSION}`

/** Mirrors the gateway Multer limit (25 MB) for a friendly client-side hint. */
export const MAX_PDF_UPLOAD_BYTES = 25 * 1024 * 1024

// UI-facing document shape: dates arrive as ISO strings over JSON.
export type CorpusDocumentUi = {
	id: string
	sourceUri: string
	docType: string
	contentHash: string
	status: DocumentStatus
	metadata: {
		originalFilename?: string
		ingestedAt?: string
		pageCount?: number
	} | null
	error: string | null
	ingestedAt: string
}

export type UploadDocumentResult = {
	id: string
	sourceUri: string
	docType: string
	contentHash: string
	status: DocumentStatus
	error: string | null
	ingestedAt: string
	pageCount: number | null
	reused: boolean
}

export const corpusKeys = {
	documents: ["corpus", "documents"] as const,
}

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
	return new ApiError(response.status, message)
}

async function fetchDocuments(): Promise<CorpusDocumentUi[]> {
	const response = await fetch(`${API_BASE}/corpus/documents`, {
		credentials: "include",
		headers: { "Content-Type": "application/json" },
	})
	if (!response.ok) {
		throw await parseError(response)
	}
	return (await response.json()) as CorpusDocumentUi[]
}

// Multipart POST: the browser sets the multipart boundary itself, so no
// Content-Type header here (setting one manually breaks the boundary).
async function uploadDocument(file: File): Promise<UploadDocumentResult> {
	const form = new FormData()
	form.append("file", file)

	const response = await fetch(`${API_BASE}/corpus/documents`, {
		method: "POST",
		credentials: "include",
		body: form,
	})
	if (!response.ok) {
		throw await parseError(response)
	}
	return (await response.json()) as UploadDocumentResult
}

export function useCorpusDocumentsQuery() {
	return useQuery({
		queryKey: corpusKeys.documents,
		queryFn: fetchDocuments,
		// A 403 will never succeed on retry without new permissions.
		retry: (failureCount, error) => !isPermissionError(error) && failureCount < 3,
	})
}

export function useUploadDocumentMutation() {
	const queryClient = useQueryClient()

	return useMutation<UploadDocumentResult, Error, File>({
		mutationKey: ["corpus", "upload"],
		mutationFn: uploadDocument,
		onSettled: () => queryClient.invalidateQueries({ queryKey: corpusKeys.documents }),
	})
}
