"use client"

import Link from "next/link"

import type { ChatCitation } from "@repo/contracts"

import { Badge } from "@/core/components/ui/badge"

function citationLabel(citation: ChatCitation): string {
	const parts: string[] = [citation.source ?? "Unknown source"]
	if (citation.page !== null && citation.page !== undefined) {
		parts.push(`p. ${citation.page}`)
	}
	if (citation.section) {
		parts.push(citation.section)
	}
	return parts.join(" · ")
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
					{citation.source ? (
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
