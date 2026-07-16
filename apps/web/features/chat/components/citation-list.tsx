"use client"

import Link from "next/link"

import type { ChatCitation } from "@repo/contracts"

import { Badge } from "@/core/components/ui/badge"
import { env } from "@/env"

function citationLabel(citation: ChatCitation): string {
	const parts: string[] = [citation.source ?? "Unknown source"]
	if (citation.page !== null && citation.page !== undefined) {
		parts.push(`p. ${citation.page}`)
	}
	if (citation.section) {
		parts.push(citation.section)
	}
	// CE-8: image citations are visually distinct — the answer READ this page.
	if (citation.modality === "image") {
		parts.push("chart/figure")
	}
	return parts.join(" · ")
}

/** CE-8: authed page-image URL behind an image citation (opens in a new tab;
 * the backend enforces corpus.documents.read before serving the bytes). */
function pageImageHref(imageId: string): string {
	return `${env.NEXT_PUBLIC_API_BASE_URL}/${env.NEXT_PUBLIC_API_VERSION}/corpus/page-images/${imageId}`
}

// Clickable citation chips under a grounded answer (Flow B step 7): each shows
// source + page/section and resolves to the corpus screen where the source
// document lives. A citation whose document row vanished (null source) renders
// as a non-clickable "Unknown source" chip.
export function CitationList({ citations }: { citations: readonly ChatCitation[] }) {
	if (citations.length === 0) {
		return null
	}

	return (
		<ul className="flex flex-wrap gap-1.5" aria-label="Citations">
			{citations.map(citation => (
				<li key={citation.chunkId}>
					{citation.modality === "image" && citation.imageId ? (
						// CE-8: opens the page image the answer actually read
						// (authed endpoint — corpus permission enforced server-side).
						<a
							href={pageImageHref(citation.imageId)}
							target="_blank"
							rel="noreferrer"
							title={`${citationLabel(citation)} — open page image`}
							className="focus-visible:ring-ring rounded-md outline-none focus-visible:ring-2"
						>
							<Badge variant="secondary" className="max-w-64 cursor-pointer truncate">
								{citationLabel(citation)}
							</Badge>
						</a>
					) : citation.source ? (
						<Link
							href="/corpus"
							title={citationLabel(citation)}
							className="focus-visible:ring-ring rounded-md outline-none focus-visible:ring-2"
						>
							<Badge variant="secondary" className="max-w-64 cursor-pointer truncate">
								{citationLabel(citation)}
							</Badge>
						</Link>
					) : (
						<Badge variant="outline" className="text-muted-foreground max-w-64 truncate">
							{citationLabel(citation)}
						</Badge>
					)}
				</li>
			))}
		</ul>
	)
}
