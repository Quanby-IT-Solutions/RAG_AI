"use client"

import type { ChatCitation } from "@repo/contracts"

import { cn } from "@/core/lib/utils"
import { CitationList } from "@/features/chat/components/citation-list"

export type ChatThreadMessage =
	| { id: string; role: "user"; content: string }
	| {
			id: string
			role: "assistant"
			content: string
			citations: readonly ChatCitation[]
			isIDontKnow: boolean
			degraded: boolean
	  }
	| { id: string; role: "error"; content: string }

export function MessageBubble({ message }: { message: ChatThreadMessage }) {
	if (message.role === "user") {
		return (
			<div className="flex justify-end">
				<div className="bg-primary text-primary-foreground max-w-[85%] rounded-lg px-3 py-2 text-sm whitespace-pre-wrap">
					{message.content}
				</div>
			</div>
		)
	}

	// Request-level failure (Flow B alternate B2): shown in-thread so the turn
	// never fails silently.
	if (message.role === "error") {
		return (
			<div className="flex justify-start">
				<div
					role="alert"
					className="bg-destructive/10 text-destructive dark:bg-destructive/20 max-w-[85%] rounded-lg px-3 py-2 text-sm"
				>
					{message.content}
				</div>
			</div>
		)
	}

	return (
		<div className="flex justify-start">
			<div
				className={cn(
					"bg-muted max-w-[85%] rounded-lg px-3 py-2 text-sm",
					message.degraded && "border-destructive/40 border"
				)}
			>
				{/* B2: bounded provider-failure answer — flag it, don't fail silently. */}
				{message.degraded ? (
					<p className="text-destructive mb-1 text-xs font-medium" role="status">
						Service degraded — this may be incomplete. Please try again shortly.
					</p>
				) : null}
				<p className="whitespace-pre-wrap">{message.content}</p>
				{/* B1: a grounded "I don't know" is an honest answer, not an error. */}
				{message.isIDontKnow ? (
					<p className="text-muted-foreground mt-1 text-xs">
						No supporting sources were found in the corpus for this question.
					</p>
				) : (
					<div className="mt-2">
						<CitationList citations={message.citations} />
					</div>
				)}
			</div>
		</div>
	)
}
