"use client"

import { useEffect, useRef, useState } from "react"
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
} from "@/features/chat/api/chat.hooks"
import { ChatComposer } from "@/features/chat/components/chat-composer"
import { MessageBubble, type ChatThreadMessage } from "@/features/chat/components/message-bubble"

interface ChatViewProps {
	/** Resolved server-side from getAccess (chat.query.submit). */
	hasAccess: boolean
}

export function ChatView({ hasAccess }: ChatViewProps) {
	const [messages, setMessages] = useState<ChatThreadMessage[]>([])
	const [input, setInput] = useState("")
	const [isStreaming, setIsStreaming] = useState(false)
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
	async function runQuery(query: string) {
		setRequestError(null)
		setRetryQuery(null)
		setIsStreaming(true)

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
			await streamChatQuery(
				{ query },
				{
					onToken: delta => {
						ensureBubble()
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
						patchMessage(assistantId, {
							content: final.answer,
							citations: final.citations,
							isIDontKnow: final.isIDontKnow,
							degraded: final.degraded,
							streaming: false,
						})
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
							{showThinking ? (
								<div className="flex justify-start" role="status" aria-label="Waiting for answer">
									<div className="bg-muted text-muted-foreground max-w-[85%] animate-pulse rounded-lg px-3 py-2 text-sm">
										Thinking...
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
