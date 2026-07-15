"use client"

import Link from "next/link"

import { Badge } from "@/core/components/ui/badge"
import {
	Card,
	CardContent,
	CardDescription,
	CardHeader,
	CardTitle,
} from "@/core/components/ui/card"
import { Skeleton } from "@/core/components/ui/skeleton"
import { useChunkInspectionQuery } from "@/features/corpus/api/corpus.hooks"

/**
 * Chunk-boundary inspection (IN-5): the document's full text rendered as its
 * actual chunks, with boundaries, overlap regions, and mid-sentence cuts
 * visually flagged — bad splits are catchable before they poison retrieval.
 */
export function ChunkInspectionView({ documentId }: Readonly<{ documentId: string }>) {
	const { data, isPending, error } = useChunkInspectionQuery(documentId)

	return (
		<div className="flex flex-col gap-4 p-4 md:p-6">
			<Card>
				<CardHeader>
					<CardTitle>Chunk boundaries</CardTitle>
					<CardDescription>
						{data
							? `${data.document.metadata?.["originalFilename"] ?? data.document.sourceUri} — ${data.chunks.length} chunk(s)${ 
								data.chunking
									? `, strategy ${String(data.chunking["strategy"] ?? "unknown")}${data.chunking["fallback"] ? " (fallback)" : ""}`
									: ""}`
							: "Boundaries, overlap regions, and mid-sentence cuts are flagged."}
					</CardDescription>
					<Link href="/corpus" className="text-muted-foreground text-xs underline">
						Back to corpus
					</Link>
				</CardHeader>
				<CardContent>
					{isPending ? (
						<div className="flex flex-col gap-2">
							<Skeleton className="h-16 w-full" />
							<Skeleton className="h-16 w-full" />
						</div>
					) : error ? (
						<p role="alert" className="text-destructive text-sm">
							{error.message}
						</p>
					) : !data || data.chunks.length === 0 ? (
						<p className="text-muted-foreground text-sm">
							No chunks yet — the document may still be processing.
						</p>
					) : (
						<ol className="flex flex-col" data-testid="chunk-boundaries">
							{data.chunks.map(chunk => {
								const overlap = chunk.overlapWithPrevious
								const overlapText = overlap > 0 ? chunk.content.slice(0, overlap) : ""
								const body = overlap > 0 ? chunk.content.slice(overlap) : chunk.content
								const midSentence = chunk.endsMidSentence || chunk.startsMidSentence
								return (
									<li
										key={chunk.id}
										className={`border-l-4 py-2 pl-3 ${midSentence ? "border-destructive" : "border-primary/40"}`}
									>
										<div className="mb-1 flex flex-wrap items-center gap-1.5">
											<Badge variant="outline">#{chunk.chunkIndex}</Badge>
											<Badge variant="secondary">{chunk.charCount} chars</Badge>
											{chunk.metadata?.["page"] !== undefined &&
											chunk.metadata?.["page"] !== null ? (
												<Badge variant="secondary">page {String(chunk.metadata["page"])}</Badge>
											) : null}
											{chunk.metadata?.["section"] ? (
												<Badge variant="secondary">{String(chunk.metadata["section"])}</Badge>
											) : null}
											{chunk.startsMidSentence ? (
												<Badge variant="destructive">starts mid-sentence</Badge>
											) : null}
											{chunk.endsMidSentence ? (
												<Badge variant="destructive">ends mid-sentence</Badge>
											) : null}
											{overlap > 0 ? (
												<Badge variant="outline">{overlap} chars overlap</Badge>
											) : null}
											<code
												className="text-muted-foreground text-[10px]"
												title={chunk.contentHash}
											>
												{chunk.contentHash.slice(0, 12)}…
											</code>
										</div>
										<p className="text-sm whitespace-pre-wrap">
											{overlapText ? (
												<mark className="bg-amber-200/60 dark:bg-amber-500/30">
													{overlapText}
												</mark>
											) : null}
											{body}
										</p>
									</li>
								)
							})}
						</ol>
					)}
				</CardContent>
			</Card>
		</div>
	)
}
