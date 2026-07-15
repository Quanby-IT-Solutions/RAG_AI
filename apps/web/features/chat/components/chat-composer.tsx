"use client"

import { Button } from "@/core/components/ui/button"
import { Spinner } from "@/core/components/ui/spinner"
import { Textarea } from "@/core/components/ui/textarea"

interface ChatComposerProps {
	value: string
	onChange: (value: string) => void
	onSend: () => void
	isPending: boolean
	/** Live rate-limit countdown (seconds) when a 429 window is active. */
	rateLimitSecondsLeft: number | null
}

// Single-turn composer (Flow B step 1): Enter sends, Shift+Enter adds a line.
export function ChatComposer({
	value,
	onChange,
	onSend,
	isPending,
	rateLimitSecondsLeft,
}: ChatComposerProps) {
	const isRateLimited = rateLimitSecondsLeft !== null && rateLimitSecondsLeft > 0
	const canSend = value.trim().length > 0 && !isPending && !isRateLimited

	function handleKeyDown(event: React.KeyboardEvent<HTMLTextAreaElement>) {
		if (event.key === "Enter" && !event.shiftKey) {
			event.preventDefault()
			if (canSend) {
				onSend()
			}
		}
	}

	return (
		<form
			className="flex items-end gap-2"
			onSubmit={event => {
				event.preventDefault()
				if (canSend) {
					onSend()
				}
			}}
		>
			<Textarea
				value={value}
				onChange={event => onChange(event.target.value)}
				onKeyDown={handleKeyDown}
				placeholder="Ask a question about the corpus..."
				aria-label="Question"
				maxLength={4000}
				disabled={isPending}
				className="min-h-12 flex-1"
			/>
			<Button type="submit" disabled={!canSend} className="shrink-0">
				{isPending ? (
					<>
						<Spinner />
						Sending...
					</>
				) : isRateLimited ? (
					`Try again in ${rateLimitSecondsLeft}s`
				) : (
					"Send"
				)}
			</Button>
		</form>
	)
}
