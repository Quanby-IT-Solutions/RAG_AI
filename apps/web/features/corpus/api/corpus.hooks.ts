"use client"

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"

import type { DocumentStatus } from "@repo/contracts"

import { ApiError } from "@/core/lib/api-error"
import { env } from "@/env"

const API_BASE = `${env.NEXT_PUBLIC_API_BASE_URL}/${env.NEXT_PUBLIC_API_VERSION}`

/** Mirrors the gateway Multer limit (25 MB) for a friendly client-side hint. */
export const MAX_UPLOAD_BYTES = 25 * 1024 * 1024

/** Mirrors the gateway allow-list (IN-1 PDF + IN-3 HTML/DOCX/CSV). */
export const SUPPORTED_UPLOAD_MIME_TYPES = [
	"application/pdf",
	"text/html",
	"application/vnd.openxmlformats-officedocument.wordprocessingml.document",
	"text/csv",
	"application/csv",
] as const

/** Extension fallback: browsers leave File.type empty for some local files. */
export const SUPPORTED_UPLOAD_EXTENSIONS = [".pdf", ".html", ".htm", ".docx", ".csv"] as const

/** Value for the file input's `accept` attribute. */
export const UPLOAD_ACCEPT = [...SUPPORTED_UPLOAD_MIME_TYPES, ...SUPPORTED_UPLOAD_EXTENSIONS].join(
	","
)

export function isSupportedUploadFile(file: File): boolean {
	const mime = (file.type || "").split(";")[0]?.trim().toLowerCase() ?? ""
	if ((SUPPORTED_UPLOAD_MIME_TYPES as readonly string[]).includes(mime)) return true
	const name = file.name.toLowerCase()
	return SUPPORTED_UPLOAD_EXTENSIONS.some(extension => name.endsWith(extension))
}

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
	/** IN-7: the queued ingestion job (null for dedup no-ops / parse failures). */
	jobId: string | null
}

// IN-7 jobs console shapes (dates arrive as ISO strings over JSON).
export type IngestionJobUi = {
	id: string
	kind: "ingest" | "reindex"
	status: "queued" | "running" | "completed" | "failed"
	triggeredBy: string | null
	stats: {
		documents?: number
		chunks?: number
		embedBatches?: number
		cacheHits?: number
		cacheMisses?: number
		failures?: { documentId: string; error: string | null }[]
	} | null
	error: string | null
	documentCount: number
	failedDocumentCount: number
	createdAt: string
	startedAt: string | null
	finishedAt: string | null
}

export type JobDocumentUi = {
	documentId: string
	sourceUri: string
	docType: string
	status: "pending" | "indexed" | "failed"
	chunkCount: number | null
	error: string | null
	updatedAt: string
}

export type IngestionJobDetailUi = IngestionJobUi & { documents: JobDocumentUi[] }

export const corpusKeys = {
	documents: ["corpus", "documents"] as const,
	jobs: ["corpus", "jobs"] as const,
	job: (id: string) => ["corpus", "jobs", id] as const,
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
		// IN-7: ingestion is asynchronous — keep polling while anything is
		// still processing so rows flip to indexed/failed without a reload.
		refetchInterval: query => {
			const rows = query.state.data
			return rows?.some(row => row.status === "processing" || row.status === "registered")
				? 3000
				: false
		},
	})
}

async function fetchJobs(): Promise<IngestionJobUi[]> {
	const response = await fetch(`${API_BASE}/corpus/jobs`, {
		credentials: "include",
		headers: { "Content-Type": "application/json" },
	})
	if (!response.ok) {
		throw await parseError(response)
	}
	return (await response.json()) as IngestionJobUi[]
}

async function fetchJob(id: string): Promise<IngestionJobDetailUi> {
	const response = await fetch(`${API_BASE}/corpus/jobs/${id}`, {
		credentials: "include",
		headers: { "Content-Type": "application/json" },
	})
	if (!response.ok) {
		throw await parseError(response)
	}
	return (await response.json()) as IngestionJobDetailUi
}

async function rerunJob(id: string): Promise<{ id: string; status: "queued" }> {
	const response = await fetch(`${API_BASE}/corpus/jobs/${id}/rerun`, {
		method: "POST",
		credentials: "include",
		headers: { "Content-Type": "application/json" },
	})
	if (!response.ok) {
		throw await parseError(response)
	}
	return (await response.json()) as { id: string; status: "queued" }
}

export function useIngestionJobsQuery() {
	return useQuery({
		queryKey: corpusKeys.jobs,
		queryFn: fetchJobs,
		retry: (failureCount, error) => !isPermissionError(error) && failureCount < 3,
		// Live console: poll fast while a job is queued/running, slowly otherwise.
		refetchInterval: query => {
			const jobs = query.state.data
			return jobs?.some(job => job.status === "queued" || job.status === "running")
				? 2500
				: 15000
		},
	})
}

export function useIngestionJobQuery(id: string | null) {
	return useQuery({
		queryKey: corpusKeys.job(id ?? "none"),
		queryFn: () => fetchJob(id as string),
		enabled: id !== null,
		refetchInterval: query => {
			const job = query.state.data
			return job && (job.status === "queued" || job.status === "running") ? 2500 : false
		},
	})
}

export function useRerunJobMutation() {
	const queryClient = useQueryClient()

	return useMutation<{ id: string; status: "queued" }, Error, string>({
		mutationKey: ["corpus", "jobs", "rerun"],
		mutationFn: rerunJob,
		onSettled: () => {
			void queryClient.invalidateQueries({ queryKey: corpusKeys.jobs })
			void queryClient.invalidateQueries({ queryKey: corpusKeys.documents })
		},
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

// ---------------------------------------------------- IN-5: chunk inspection

/** Wire shape of ChunkInspection (dates as ISO strings). */
export interface ChunkInspectionUi {
	document: CorpusDocumentUi
	chunking: Record<string, unknown> | null
	chunks: {
		id: string
		chunkIndex: number
		content: string
		contentHash: string
		charCount: number
		metadata: Record<string, unknown> | null
		endsMidSentence: boolean
		startsMidSentence: boolean
		overlapWithPrevious: number
	}[]
}

export function useChunkInspectionQuery(documentId: string | null) {
	return useQuery({
		queryKey: ["corpus", "documents", documentId, "chunks"] as const,
		queryFn: async () => {
			const response = await fetch(`${API_BASE}/corpus/documents/${documentId}/chunks`, {
				credentials: "include",
				headers: { "Content-Type": "application/json" },
			})
			if (!response.ok) {
				throw await parseError(response)
			}
			return (await response.json()) as ChunkInspectionUi
		},
		enabled: Boolean(documentId),
	})
}

// ---------------------------------------------------- IN-8: re-embedding

export interface ReembedMigrationUi {
	id?: string
	targetModel?: string
	status: "none" | "shadowing" | "verifying" | "ready" | "cutover" | "aborted" | "failed"
	metrics?: Record<string, number> | null
	error?: string | null
	triggeredBy?: string
	updatedAt?: string
}

export const reembedKeys = { status: ["corpus", "reembed"] as const }

async function reembedFetch(path: string, init?: RequestInit): Promise<ReembedMigrationUi> {
	const response = await fetch(`${API_BASE}${path}`, {
		credentials: "include",
		headers: { "Content-Type": "application/json" },
		...init,
	})
	if (!response.ok) {
		throw await parseError(response)
	}
	return (await response.json()) as ReembedMigrationUi
}

export function useReembedStatusQuery() {
	return useQuery({
		queryKey: reembedKeys.status,
		queryFn: () => reembedFetch("/corpus/reembed"),
		refetchInterval: query => {
			const status = query.state.data?.status
			return status === "shadowing" || status === "verifying" ? 3000 : 30000
		},
	})
}

export function useStartReembedMutation() {
	const queryClient = useQueryClient()
	return useMutation({
		mutationFn: (targetModel: string) =>
			reembedFetch("/corpus/reembed", {
				method: "POST",
				body: JSON.stringify({ targetModel }),
			}),
		onSettled: () => void queryClient.invalidateQueries({ queryKey: reembedKeys.status }),
	})
}

export function useReembedActionMutation(action: "cutover" | "abort") {
	const queryClient = useQueryClient()
	return useMutation({
		mutationFn: () => reembedFetch(`/corpus/reembed/${action}`, { method: "POST" }),
		onSettled: () => void queryClient.invalidateQueries({ queryKey: reembedKeys.status }),
	})
}
