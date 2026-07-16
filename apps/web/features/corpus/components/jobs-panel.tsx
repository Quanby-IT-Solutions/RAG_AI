"use client"

import { Fragment, useEffect, useState } from "react"
import { useSearchParams } from "next/navigation"
import { RefreshIcon } from "@hugeicons/core-free-icons"
import { HugeiconsIcon } from "@hugeicons/react"

import { Badge } from "@/core/components/ui/badge"
import { Button } from "@/core/components/ui/button"
import {
	Card,
	CardContent,
	CardDescription,
	CardHeader,
	CardTitle,
} from "@/core/components/ui/card"
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
	useIngestionJobQuery,
	useIngestionJobsQuery,
	useRerunJobMutation,
	type IngestionJobUi,
} from "@/features/corpus/api/corpus.hooks"

const jobStatusVariant: Record<IngestionJobUi["status"], "secondary" | "default" | "destructive"> =
	{
		queued: "secondary",
		running: "secondary",
		completed: "default",
		failed: "destructive",
	}

function formatTimestamp(iso: string | null): string {
	if (!iso) return "—"
	const date = new Date(iso)
	return Number.isNaN(date.getTime()) ? iso : date.toLocaleString()
}

function JobDetailRow({ jobId }: Readonly<{ jobId: string }>) {
	const { data: detail, isPending } = useIngestionJobQuery(jobId)

	if (isPending) {
		return (
			<div className="flex flex-col gap-1 p-2">
				<Skeleton className="h-6 w-full" />
				<Skeleton className="h-6 w-full" />
			</div>
		)
	}
	if (!detail) return null

	return (
		<div className="flex flex-col gap-2 p-2">
			<p className="text-muted-foreground text-xs">
				{detail.stats
					? `${detail.stats.chunks ?? 0} chunks · ${detail.stats.embedBatches ?? 0} embed batches · ${detail.stats.cacheHits ?? 0} cache hits · ${detail.stats.cacheMisses ?? 0} misses · ${detail.stats.failures?.length ?? 0} failures`
					: "No run stats yet — the job has not finished a pass."}
			</p>
			<Table aria-label="Documents in this job">
				<TableHeader>
					<TableRow>
						<TableHead>Document</TableHead>
						<TableHead>Status</TableHead>
						<TableHead>Chunks</TableHead>
						<TableHead>Error</TableHead>
					</TableRow>
				</TableHeader>
				<TableBody>
					{detail.documents.map(document => (
						<TableRow key={document.documentId}>
							<TableCell className="max-w-56 truncate" title={document.sourceUri}>
								{document.sourceUri}
							</TableCell>
							<TableCell>
								<Badge
									variant={
										document.status === "failed"
											? "destructive"
											: document.status === "indexed"
												? "default"
												: "secondary"
									}
								>
									{document.status}
								</Badge>
							</TableCell>
							<TableCell>{document.chunkCount ?? "—"}</TableCell>
							<TableCell className="text-destructive max-w-64 truncate text-xs">
								{document.error ?? ""}
							</TableCell>
						</TableRow>
					))}
				</TableBody>
			</Table>
		</div>
	)
}

/**
 * Jobs console (IN-7): live lifecycle of background ingestion jobs with
 * per-document outcomes and a re-run action for finished jobs. Gated by
 * corpus.ingest.run — a 403 hides the panel (the corpus page itself already
 * renders a no-access state for corpus.documents.read).
 */
export function JobsPanel() {
	const { data: jobs, isPending, isError, error } = useIngestionJobsQuery()
	const rerun = useRerunJobMutation()
	// RP-5 deep link: /corpus?job=<id> opens that job's detail for re-run.
	// Applied post-hydration: the server render has no search params, so
	// seeding useState from them would mismatch.
	const searchParams = useSearchParams()
	const [expandedJobId, setExpandedJobId] = useState<string | null>(null)
	const linkedJobId = searchParams.get("job")
	useEffect(() => {
		if (linkedJobId) setExpandedJobId(linkedJobId)
	}, [linkedJobId])

	// No corpus.ingest.run -> the console simply isn't shown.
	if (isError && isPermissionError(error)) return null

	return (
		<Card>
			<CardHeader className="gap-2">
				<CardTitle className="text-base">Ingestion jobs</CardTitle>
				<CardDescription>
					Background ingestion runs (newest first). Re-running a finished job is safe — the
					pipeline never duplicates rows.
				</CardDescription>
			</CardHeader>
			<CardContent className="flex flex-col gap-2">
				{isPending ? (
					<div className="flex flex-col gap-2">
						<Skeleton className="h-8 w-full" />
						<Skeleton className="h-8 w-full" />
					</div>
				) : null}

				{isError && !isPermissionError(error) ? (
					<p className="text-destructive text-xs" role="alert">
						{error instanceof Error ? error.message : "Failed to load jobs"}
					</p>
				) : null}

				{rerun.isError ? (
					<p className="text-destructive text-xs" role="alert">
						{rerun.error.message}
					</p>
				) : null}

				{jobs && jobs.length === 0 ? (
					<p className="text-muted-foreground text-sm">No ingestion jobs yet.</p>
				) : null}

				{jobs && jobs.length > 0 ? (
					<Table aria-label="Ingestion jobs">
						<TableHeader>
							<TableRow>
								<TableHead>Status</TableHead>
								<TableHead>Kind</TableHead>
								<TableHead>Documents</TableHead>
								<TableHead>Created</TableHead>
								<TableHead>Finished</TableHead>
								<TableHead className="sr-only">Actions</TableHead>
							</TableRow>
						</TableHeader>
						<TableBody>
							{jobs.map(job => (
								<Fragment key={job.id}>
									<TableRow>
										<TableCell>
											<Badge variant={jobStatusVariant[job.status]}>{job.status}</Badge>
										</TableCell>
										<TableCell className="uppercase">{job.kind}</TableCell>
										<TableCell>
											{job.documentCount}
											{job.failedDocumentCount > 0 ? (
												<span className="text-destructive"> ({job.failedDocumentCount} failed)</span>
											) : null}
										</TableCell>
										<TableCell>{formatTimestamp(job.createdAt)}</TableCell>
										<TableCell>{formatTimestamp(job.finishedAt)}</TableCell>
										<TableCell className="flex justify-end gap-1">
											<Button
												type="button"
												variant="ghost"
												size="sm"
												aria-expanded={expandedJobId === job.id}
												onClick={() =>
													setExpandedJobId(current => (current === job.id ? null : job.id))
												}
											>
												{expandedJobId === job.id ? "Hide" : "Details"}
											</Button>
											{job.status === "completed" || job.status === "failed" ? (
												<Button
													type="button"
													variant="outline"
													size="sm"
													disabled={rerun.isPending}
													aria-label={`Re-run job created ${formatTimestamp(job.createdAt)}`}
													onClick={() => rerun.mutate(job.id)}
												>
													<HugeiconsIcon icon={RefreshIcon} strokeWidth={2} className="size-3.5" />
													Re-run
												</Button>
											) : null}
										</TableCell>
									</TableRow>
									{expandedJobId === job.id ? (
										<TableRow>
											<TableCell colSpan={6}>
												<JobDetailRow jobId={job.id} />
											</TableCell>
										</TableRow>
									) : null}
								</Fragment>
							))}
						</TableBody>
					</Table>
				) : null}
			</CardContent>
		</Card>
	)
}
