"use client"

import Link from "next/link"

import { useRef, useState } from "react"
import { FileUploadIcon, FolderLibraryIcon } from "@hugeicons/core-free-icons"
import { HugeiconsIcon } from "@hugeicons/react"

import { PageHeader } from "@/core/components/page-header"
import { Badge } from "@/core/components/ui/badge"
import {
	Card,
	CardContent,
	CardDescription,
	CardHeader,
	CardTitle,
} from "@/core/components/ui/card"
import {
	Empty,
	EmptyDescription,
	EmptyHeader,
	EmptyMedia,
	EmptyTitle,
} from "@/core/components/ui/empty"
import { Input } from "@/core/components/ui/input"
import { Skeleton } from "@/core/components/ui/skeleton"
import {
	Table,
	TableBody,
	TableCell,
	TableHead,
	TableHeader,
	TableRow,
} from "@/core/components/ui/table"
import {
	isPermissionError,
	isSupportedUploadFile,
	MAX_UPLOAD_BYTES,
	UPLOAD_ACCEPT,
	useCorpusDocumentsQuery,
	useUploadDocumentMutation,
	type CorpusDocumentUi,
} from "@/features/corpus/api/corpus.hooks"
import { JobsPanel } from "@/features/corpus/components/jobs-panel"

// R1 document state machine badge styling (registered/processing/indexed/failed).
const statusVariant: Record<CorpusDocumentUi["status"], "secondary" | "default" | "destructive"> = {
	registered: "secondary",
	processing: "secondary",
	indexed: "default",
	failed: "destructive",
}

function formatTimestamp(iso: string): string {
	const date = new Date(iso)
	return Number.isNaN(date.getTime()) ? iso : date.toLocaleString()
}

export function CorpusView() {
	// isPending (not isLoading): during the persisted-cache restore the query
	// is pending with fetchStatus "idle", so isLoading is false on the client's
	// FIRST render while the server rendered the skeleton — a hydration
	// mismatch. isPending is true in both passes.
	const { data: documents, isPending, isError, error } = useCorpusDocumentsQuery()
	const upload = useUploadDocumentMutation()
	const [clientError, setClientError] = useState<string | null>(null)
	const fileInputRef = useRef<HTMLInputElement>(null)

	// Read access failed with 403: the whole screen is permission-gated.
	if (isError && isPermissionError(error)) {
		return (
			<section className="flex w-full max-w-4xl flex-col gap-6">
				<PageHeader
					icon={FolderLibraryIcon}
					title="Corpus"
					description="Sources ingested into the knowledge base."
				/>
				<Empty className="border">
					<EmptyHeader>
						<EmptyMedia variant="icon">
							<HugeiconsIcon icon={FolderLibraryIcon} strokeWidth={2} />
						</EmptyMedia>
						<EmptyTitle>No access</EmptyTitle>
						<EmptyDescription>
							You do not have permission to view the corpus (requires corpus.documents.read). Ask
							an administrator for access.
						</EmptyDescription>
					</EmptyHeader>
				</Empty>
			</section>
		)
	}

	function handleFileChange(event: React.ChangeEvent<HTMLInputElement>) {
		setClientError(null)
		const file = event.target.files?.[0]
		if (!file) return

		// Client-side hints; the gateway (Multer) and the RAG service both
		// re-enforce the type allow-list and the 25 MB cap.
		if (!isSupportedUploadFile(file)) {
			setClientError("Only PDF, HTML, DOCX, and CSV files are accepted.")
			event.target.value = ""
			return
		}
		if (file.size > MAX_UPLOAD_BYTES) {
			setClientError("This file exceeds the 25 MB upload limit.")
			event.target.value = ""
			return
		}

		upload.mutate(file, {
			onSettled: () => {
				if (fileInputRef.current) fileInputRef.current.value = ""
			},
		})
	}

	const uploadErrorMessage = upload.isError
		? isPermissionError(upload.error)
			? "You do not have permission to upload documents (requires corpus.documents.upload and corpus.ingest.run)."
			: upload.error.message
		: null

	return (
		<section className="flex w-full max-w-4xl flex-col gap-6">
			<PageHeader
				icon={FolderLibraryIcon}
				title="Corpus"
				description="Upload PDF, HTML, DOCX, or CSV sources and track their ingestion status."
			/>

			<Card>
				<CardHeader className="gap-2">
					<CardTitle className="text-base">Upload a document</CardTitle>
					<CardDescription>
						Single PDF, HTML, DOCX, or CSV file, up to 25 MB. Re-uploading identical content is
						idempotent; previously failed documents are retried.
					</CardDescription>
				</CardHeader>
				<CardContent className="flex flex-col gap-2">
					<div className="flex items-center gap-2">
						<Input
							ref={fileInputRef}
							type="file"
							accept={UPLOAD_ACCEPT}
							onChange={handleFileChange}
							disabled={upload.isPending}
							aria-label="Upload document"
						/>
						{upload.isPending ? (
							<span className="text-muted-foreground flex items-center gap-1 text-xs whitespace-nowrap">
								<HugeiconsIcon icon={FileUploadIcon} strokeWidth={2} className="size-4" />
								Uploading...
							</span>
						) : null}
					</div>
					{clientError ? <p className="text-destructive text-xs">{clientError}</p> : null}
					{uploadErrorMessage ? (
						<p className="text-destructive text-xs" role="alert">
							{uploadErrorMessage}
						</p>
					) : null}
					{upload.isSuccess ? (
						<p className="text-muted-foreground text-xs">
							{upload.data.reused
								? "Already ingested — identical content, no duplicate created."
								: upload.data.status === "failed"
									? `Upload registered but parsing failed: ${upload.data.error ?? "unknown reason"}`
									: "Uploaded — the document is processing in a background job."}
						</p>
					) : null}
				</CardContent>
			</Card>

			<JobsPanel />

			{isPending ? (
				<div className="flex flex-col gap-2">
					<Skeleton className="h-10 w-full" />
					<Skeleton className="h-10 w-full" />
					<Skeleton className="h-10 w-full" />
				</div>
			) : null}

			{isError && !isPermissionError(error) ? (
				<p className="text-destructive text-sm">
					{error instanceof Error ? error.message : "Failed to load documents"}
				</p>
			) : null}

			{documents && documents.length === 0 ? (
				<Empty className="border">
					<EmptyHeader>
						<EmptyMedia variant="icon">
							<HugeiconsIcon icon={FolderLibraryIcon} strokeWidth={2} />
						</EmptyMedia>
						<EmptyTitle>No documents yet</EmptyTitle>
						<EmptyDescription>Upload your first PDF above to start the corpus.</EmptyDescription>
					</EmptyHeader>
				</Empty>
			) : null}

			{documents && documents.length > 0 ? (
				<div className="rounded-lg border">
					<Table>
						<TableHeader>
							<TableRow>
								<TableHead>Name</TableHead>
								<TableHead>Type</TableHead>
								<TableHead>Status</TableHead>
								<TableHead>Content hash</TableHead>
								<TableHead>Ingested</TableHead>
							</TableRow>
						</TableHeader>
						<TableBody>
							{documents.map(document => (
								<TableRow key={document.id}>
									<TableCell className="align-top">
										<div className="font-medium">
											{document.metadata?.originalFilename ?? document.sourceUri}
										</div>
										<div className="text-muted-foreground text-xs">{document.sourceUri}</div>
									</TableCell>
									<TableCell className="align-top uppercase">{document.docType}</TableCell>
									<TableCell className="align-top">
										<Badge variant={statusVariant[document.status] ?? "secondary"}>
											{document.status}
										</Badge>
										{document.status === "failed" && document.error ? (
											<p className="text-destructive mt-1 max-w-56 text-xs">{document.error}</p>
										) : null}
									</TableCell>
									<TableCell className="align-top">
										<code className="text-muted-foreground text-xs" title={document.contentHash}>
											{document.contentHash.slice(0, 12)}…
										</code>
									</TableCell>
									<TableCell className="text-muted-foreground align-top text-xs">
										{formatTimestamp(document.ingestedAt)}
									</TableCell>
									<TableCell className="align-top text-right">
										{/* IN-5: boundary inspection (page enforces corpus.chunks.inspect). */}
										<Link
											href={`/corpus/${document.id}`}
											className="text-primary text-xs underline"
											aria-label={`Inspect chunks of ${document.metadata?.originalFilename ?? document.sourceUri}`}
										>
											Inspect
										</Link>
									</TableCell>
								</TableRow>
							))}
						</TableBody>
					</Table>
				</div>
			) : null}
		</section>
	)
}
