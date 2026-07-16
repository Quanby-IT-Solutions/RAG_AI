"use client"

import { useEffect, useRef, useState } from "react"
import Link from "next/link"
import { BubbleChatIcon } from "@hugeicons/core-free-icons"
import { HugeiconsIcon } from "@hugeicons/react"

import { PageHeader } from "@/core/components/page-header"
import { Button } from "@/core/components/ui/button"
import {
	Empty,
	EmptyDescription,
	EmptyHeader,
	EmptyMedia,
	EmptyTitle,
} from "@/core/components/ui/empty"
import { useRateLimitCountdown } from "@/core/hooks/use-rate-limit-countdown"
import { parseRateLimitError } from "@/core/lib/rate-limit-utils"
import { RateLimitBanner } from "@/features/auth/components/rate-limit-banner"
import {
	ChatStreamInterruptedError,
	isPermissionError,
	streamChatQuery,
	useConversationQuery,
} from "@/features/chat/api/chat.hooks"
import { ChatComposer } from "@/features/chat/components/chat-composer"
import { MessageBubble, type ChatThreadMessage } from "@/features/chat/components/message-bubble"

interface ChatViewProps {
	/** Resolved server-side from getAccess (chat.query.submit). */
	hasAccess: boolean
	/** CH-3: user can open /chat/history (chat.history.read). */
	hasHistoryAccess?: boolean
	/** CH-3: continue an existing conversation (from /chat/history). */
	initialConversationId?: string | null
}

export function ChatView({
	hasAccess,
	hasHistoryAccess = false,
	initialConversationId = null,
}: ChatViewProps) {
	const [messages, setMessages] = useState<ChatThreadMessage[]>([])
	const [input, setInput] = useState("")
	const [isStreaming, setIsStreaming] = useState(false)
	// CE-4: agentic-loop progress label (null in linear mode / between steps).
	const [loopStatus, setLoopStatus] = useState<string | null>(null)
	// CH-3: once a conversation exists, later sends continue it server-side.
	const [conversationId, setConversationId] = useState<string | null>(initialConversationId)
	// RT-4: restrict retrieval to a corpus subset; empty = whole corpus.
	const [filterDocTypes, setFilterDocTypes] = useState<("pdf" | "html" | "docx" | "csv")[]>([])
	const [filterSource, setFilterSource] = useState("")
	const seededRef = useRef(false)
	const { data: existingConversation } = useConversationQuery(initialConversationId)
	const [requestError, setRequestError] = useState<Error | null>(null)
	const [retryQuery, setRetryQuery] = useState<string | null>(null)
	const nextIdRef = useRef(0)
	const threadEndRef = useRef<HTMLDivElement>(null)

	const rateLimit = parseRateLimitError(requestError)
	const { secondsLeft } = useRateLimitCountdown(
		rateLimit.isRateLimit ? rateLimit.retryAfter : null
	)

	useEffect(() => {
		threadEndRef.current?.scrollIntoView?.({ block: "end" })
	}, [messages])

	// CH-3: opening a past conversation loads its full thread (citations
	// intact) so the user can pick up where they left off.
	useEffect(() => {
		if (!existingConversation || seededRef.current) return
		seededRef.current = true
		nextIdRef.current = existingConversation.messages.length
		setMessages(
			existingConversation.messages.map(message =>
				message.role === "user"
					? { id: message.id, role: "user" as const, content: message.content }
					: {
							id: message.id,
							role: "assistant" as const,
							content: message.content,
							citations: message.citations ?? [],
							isIDontKnow: false,
							degraded: false,
						}
			)
		)
	}, [existingConversation])

	// B3: the nav item is already hidden without chat.query.submit; direct
	// navigation lands on this no-access state (the API would 403 anyway).
	if (!hasAccess) {
		return (
			<section className="flex w-full max-w-4xl flex-col gap-6">
				<PageHeader
					icon={BubbleChatIcon}
					title="Chat"
					description="Ask questions over the indexed corpus."
				/>
				<Empty className="border">
					<EmptyHeader>
						<EmptyMedia variant="icon">
							<HugeiconsIcon icon={BubbleChatIcon} strokeWidth={2} />
						</EmptyMedia>
						<EmptyTitle>No access</EmptyTitle>
						<EmptyDescription>
							You do not have permission to ask questions (requires chat.query.submit). Ask an
							administrator for access.
						</EmptyDescription>
					</EmptyHeader>
				</Empty>
			</section>
		)
	}

	function nextId(): string {
		nextIdRef.current += 1
		return `msg-${nextIdRef.current}`
	}

	function patchMessage(id: string, patch: Partial<ChatThreadMessage>) {
		setMessages(prev =>
			prev.map(message =>
				message.id === id ? ({ ...message, ...patch } as ChatThreadMessage) : message
			)
		)
	}

	// CH-2: progressive streaming render. Tokens append to a live assistant
	// bubble; citations attach on the final frame. Degradation states render
	// honestly: 429 -> countdown banner, provider-unavailable -> degraded
	// final, dropped connection -> partial answer + retry (never a frozen
	// spinner).
	// CE-4: human labels for the agentic loop's SSE status stages; unknown
	// stages fall back to a generic working label.
	const statusLabels: Record<string, string> = {
		retrieving: "Searching the corpus…",
		grading: "Checking relevance…",
		rewriting: "Refining the search…",
		regenerating: "Improving the answer…",
	}

	async function runQuery(query: string) {
		setRequestError(null)
		setRetryQuery(null)
		setIsStreaming(true)
		setLoopStatus(null)

		const assistantId = nextId()
		let started = false
		const ensureBubble = () => {
			if (started) return
			started = true
			setMessages(prev => [
				...prev,
				{
					id: assistantId,
					role: "assistant",
					content: "",
					citations: [],
					isIDontKnow: false,
					degraded: false,
					streaming: true,
				},
			])
		}

		try {
			const filters =
				filterDocTypes.length > 0 || filterSource.trim()
					? {
							...(filterDocTypes.length > 0 && { docTypes: filterDocTypes }),
							...(filterSource.trim() && { source: filterSource.trim() }),
						}
					: undefined
			await streamChatQuery(
				{ query, conversationId: conversationId ?? undefined, filters },
				{
					onToken: delta => {
						ensureBubble()
						setLoopStatus(null)
						setMessages(prev =>
							prev.map(message =>
								message.id === assistantId && message.role === "assistant"
									? { ...message, content: message.content + delta }
									: message
							)
						)
					},
					onFinal: final => {
						ensureBubble()
						setLoopStatus(null)
						setConversationId(final.conversationId)
						patchMessage(assistantId, {
							content: final.answer,
							citations: final.citations,
							isIDontKnow: final.isIDontKnow,
							degraded: final.degraded,
							streaming: false,
						})
					},
					// CE-4: agentic-loop progress renders in the thinking bubble
					// (and over a streamed draft while it is being regenerated).
					onStatus: status => {
						setLoopStatus(statusLabels[status.stage] ?? "Working…")
					},
				}
			)
		} catch (error) {
			if (error instanceof ChatStreamInterruptedError && started) {
				// Dropped connection mid-answer: keep the partial text, flag it,
				// and offer a retry — never a frozen spinner (CH-2 AC).
				patchMessage(assistantId, { streaming: false, interrupted: true })
				setRetryQuery(query)
			} else if (error instanceof ChatStreamInterruptedError) {
				setMessages(prev => [
					...prev,
					{
						id: nextId(),
						role: "error",
						content: "The connection dropped before an answer arrived.",
					},
				])
				setRetryQuery(query)
			} else if (parseRateLimitError(error).isRateLimit) {
				// 429: the live-countdown banner renders the retry-in-X state.
				setRequestError(error as Error)
				setRetryQuery(query)
			} else {
				setMessages(prev => [
					...prev,
					{
						id: nextId(),
						role: "error",
						content: isPermissionError(error)
							? "You do not have permission to ask questions (requires chat.query.submit)."
							: "The answer service is unavailable right now. Your question was not answered — please try again shortly.",
					},
				])
				setRetryQuery(query)
			}
		} finally {
			setIsStreaming(false)
			setLoopStatus(null)
		}
	}

	function handleSend() {
		const query = input.trim()
		if (!query || isStreaming) return

		setMessages(prev => [...prev, { id: nextId(), role: "user", content: query }])
		setInput("")
		// R1 is single-turn: each submit starts a new conversation server-side;
		// the thread here is in-session display only (history UI is CH-3).
		void runQuery(query)
	}

	function handleRetry() {
		const query = retryQuery
		if (!query || isStreaming) return
		void runQuery(query)
	}

	const showThinking =
		isStreaming &&
		!messages.some(message => message.role === "assistant" && message.streaming)

	return (
		<section className="flex w-full max-w-4xl flex-col gap-6">
			<PageHeader
				icon={BubbleChatIcon}
				title="Chat"
				description="Ask a question and get a cited answer from the indexed corpus."
			/>

			{hasHistoryAccess ? (
				<p className="text-sm">
					<Link href="/chat/history" className="text-primary underline-offset-4 hover:underline">
						View conversation history
					</Link>
				</p>
			) : null}

			<div className="flex min-h-[50vh] flex-col gap-4">
				<div className="flex-1 overflow-y-auto" aria-label="Conversation">
					{messages.length === 0 ? (
						<Empty className="border">
							<EmptyHeader>
								<EmptyMedia variant="icon">
									<HugeiconsIcon icon={BubbleChatIcon} strokeWidth={2} />
								</EmptyMedia>
								<EmptyTitle>Ask your first question</EmptyTitle>
								<EmptyDescription>
									Answers are grounded in the uploaded corpus and cite their sources.
								</EmptyDescription>
							</EmptyHeader>
						</Empty>
					) : (
						<div className="flex flex-col gap-3">
							{messages.map(message => (
								<MessageBubble key={message.id} message={message} />
							))}
							{showThinking || (isStreaming && loopStatus) ? (
								<div className="flex justify-start" role="status" aria-label="Waiting for answer">
									<div className="bg-muted text-muted-foreground max-w-[85%] animate-pulse rounded-lg px-3 py-2 text-sm">
										{/* CE-4: live agentic progress; linear mode keeps Thinking... */}
										{loopStatus ?? "Thinking..."}
									</div>
								</div>
							) : null}
							{retryQuery && !isStreaming ? (
								<div className="flex justify-start">
									<Button type="button" variant="outline" size="sm" onClick={handleRetry}>
										Retry question
									</Button>
								</div>
							) : null}
							<div ref={threadEndRef} />
						</div>
					)}
				</div>

				{rateLimit.isRateLimit ? (
					<RateLimitBanner message={rateLimit.message} secondsLeft={secondsLeft} />
				) : null}

				{/* RT-4: corpus-subset filters, applied in SQL server-side. */}
				<fieldset
					className="text-muted-foreground flex flex-wrap items-center gap-3 text-xs"
					aria-label="Retrieval filters"
				>
					<legend className="sr-only">Retrieval filters</legend>
					<span className="font-medium">Filter:</span>
					{(["pdf", "html", "docx", "csv"] as const).map(type => (
						<label key={type} className="flex items-center gap-1">
							<input
								type="checkbox"
								checked={filterDocTypes.includes(type)}
								onChange={event =>
									setFilterDocTypes(previous =>
										event.target.checked
											? [...previous, type]
											: previous.filter(t => t !== type)
									)
								}
							/>
							{type.toUpperCase()}
						</label>
					))}
					<input
						type="text"
						aria-label="Source filter"
						placeholder="source contains…"
						className="border-input bg-background h-7 rounded-md border px-2"
						value={filterSource}
						onChange={event => setFilterSource(event.target.value)}
					/>
					{filterDocTypes.length > 0 || filterSource.trim() ? (
						<span role="status">answers limited to matching documents</span>
					) : null}
				</fieldset>

				<ChatComposer
					value={input}
					onChange={setInput}
					onSend={handleSend}
					isPending={isStreaming}
					rateLimitSecondsLeft={rateLimit.isRateLimit ? secondsLeft : null}
				/>
			</div>
		</section>
	)
}
