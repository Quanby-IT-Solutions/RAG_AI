"use client"

import Link from "next/link"
import { Clock01Icon } from "@hugeicons/core-free-icons"
import { HugeiconsIcon } from "@hugeicons/react"

import { PageHeader } from "@/core/components/page-header"
import {
	Empty,
	EmptyDescription,
	EmptyHeader,
	EmptyMedia,
	EmptyTitle,
} from "@/core/components/ui/empty"
import { Skeleton } from "@/core/components/ui/skeleton"
import {
	Table,
	TableBody,
	TableCell,
	TableHead,
	TableHeader,
	TableRow,
} from "@/core/components/ui/table"
import { isPermissionError, useConversationsQuery } from "@/features/chat/api/chat.hooks"

function formatTimestamp(iso: string | null): string {
	if (!iso) return "—"
	const date = new Date(iso)
	return Number.isNaN(date.getTime()) ? iso : date.toLocaleString()
}

/**
 * Conversation history (CH-3): the caller's own conversations, newest first.
 * Opening one navigates to /chat?c=<id>, which loads the full thread
 * (citations intact) and lets the user continue it.
 */
export function HistoryView({ hasAccess }: Readonly<{ hasAccess: boolean }>) {
	const { data: conversations, isPending, isError, error } = useConversationsQuery(hasAccess)

	if (!hasAccess || (isError && isPermissionError(error))) {
		return (
			<section className="flex w-full max-w-4xl flex-col gap-6">
				<PageHeader
					icon={Clock01Icon}
					title="Conversation history"
					description="Your past conversations."
				/>
				<Empty className="border">
					<EmptyHeader>
						<EmptyMedia variant="icon">
							<HugeiconsIcon icon={Clock01Icon} strokeWidth={2} />
						</EmptyMedia>
						<EmptyTitle>No access</EmptyTitle>
						<EmptyDescription>
							You do not have permission to view conversation history (requires
							chat.history.read). Ask an administrator for access.
						</EmptyDescription>
					</EmptyHeader>
				</Empty>
			</section>
		)
	}

	return (
		<section className="flex w-full max-w-4xl flex-col gap-6">
			<PageHeader
				icon={Clock01Icon}
				title="Conversation history"
				description="Pick up a past thread or re-check a cited answer."
			/>

			{isPending ? (
				<div className="flex flex-col gap-2">
					<Skeleton className="h-10 w-full" />
					<Skeleton className="h-10 w-full" />
				</div>
			) : null}

			{isError && !isPermissionError(error) ? (
				<p className="text-destructive text-sm" role="alert">
					{error instanceof Error ? error.message : "Failed to load history"}
				</p>
			) : null}

			{conversations && conversations.length === 0 ? (
				<Empty className="border">
					<EmptyHeader>
						<EmptyMedia variant="icon">
							<HugeiconsIcon icon={Clock01Icon} strokeWidth={2} />
						</EmptyMedia>
						<EmptyTitle>No conversations yet</EmptyTitle>
						<EmptyDescription>Ask your first question in Chat to start one.</EmptyDescription>
					</EmptyHeader>
				</Empty>
			) : null}

			{conversations && conversations.length > 0 ? (
				<Table aria-label="Your conversations">
					<TableHeader>
						<TableRow>
							<TableHead>Conversation</TableHead>
							<TableHead>Messages</TableHead>
							<TableHead>Started</TableHead>
							<TableHead>Last activity</TableHead>
							<TableHead className="sr-only">Actions</TableHead>
						</TableRow>
					</TableHeader>
					<TableBody>
						{conversations.map(conversation => (
							<TableRow key={conversation.id}>
								<TableCell className="max-w-72 truncate" title={conversation.title}>
									{conversation.title}
								</TableCell>
								<TableCell>{conversation.messageCount}</TableCell>
								<TableCell>{formatTimestamp(conversation.createdAt)}</TableCell>
								<TableCell>{formatTimestamp(conversation.lastMessageAt)}</TableCell>
								<TableCell className="text-right">
									<Link
										href={`/chat?c=${conversation.id}`}
										className="text-primary text-sm underline-offset-4 hover:underline"
									>
										Open
									</Link>
								</TableCell>
							</TableRow>
						))}
					</TableBody>
				</Table>
			) : null}
		</section>
	)
}
