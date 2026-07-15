"use client"

import { useState } from "react"
import { Task01Icon } from "@hugeicons/core-free-icons"
import { HugeiconsIcon } from "@hugeicons/react"

import { PageHeader } from "@/core/components/page-header"
import { Button } from "@/core/components/ui/button"
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
import { useRateLimitToast } from "@/core/hooks/use-rate-limit-toast"
import { ApiError } from "@/core/lib/api-error"
import {
	useCreateTodoMutation,
	useTodoReplayErrors,
	useTodosQuery,
} from "@/features/todos/api/todos.hooks"
import { TodoItem } from "@/features/todos/components/todo-item"

// F3 removed the legacy `posts:*` colon keys; this example view is ungated
// (auth-only) until the module is retired, so every action is available.
export function TodosView() {
	const canCreate = true
	const canEdit = true
	const canDelete = true

	const { data: todos, isLoading, isError, error } = useTodosQuery()
	const createTodo = useCreateTodoMutation()
	const replay = useTodoReplayErrors()
	const [title, setTitle] = useState("")

	// Live 429 countdown for the create action. Only the Add control is disabled
	// during the server-advertised retry window; the toast text ticks each second.
	const createRateLimit = useRateLimitToast(createTodo.error, { toastId: "todo-create-rate-limit" })

	// A mutation paused by `networkMode: "online"` stays `isPending` for the whole
	// offline session. Only treat an actively in-flight (non-paused) submission as
	// "submitting" so the user can keep queuing todos while disconnected.
	const isSubmitting = createTodo.isPending && !createTodo.isPaused

	function handleCreate(event: React.FormEvent) {
		event.preventDefault()
		if (createRateLimit.isActive) return
		const trimmed = title.trim()
		if (!trimmed) return
		// Clear the input immediately; the optimistic row appears in the same tick.
		setTitle("")
		createTodo.mutate(
			{ title: trimmed, completed: false },
			{
				onError: error => {
					// Restore the draft when the create was rejected by the rate limiter so
					// the user's entered title survives the 429 countdown (Core Flow 6).
					if (error instanceof ApiError && error.status === 429) {
						setTitle(prev => (prev.trim() ? prev : trimmed))
					}
				},
			}
		)
	}

	return (
		<section className="flex w-full max-w-2xl flex-col gap-6">
			<PageHeader
				icon={Task01Icon}
				title="Todos / Posts"
				description="CRUD wired to the protected NestJS endpoints. Buttons reflect your permissions."
			/>

			<Card>
				<CardHeader className="gap-2">
					<CardTitle className="text-base">Your access</CardTitle>
					<CardDescription>Example module — available to every signed-in user</CardDescription>
				</CardHeader>
				<CardContent>
					<form className="flex gap-2" onSubmit={handleCreate}>
						<Input
							value={title}
							onChange={e => setTitle(e.target.value)}
							placeholder="New todo title"
							disabled={!canCreate}
						/>
						<Button type="submit" disabled={!canCreate || createRateLimit.isActive}>
							{createRateLimit.isActive
								? `Try again in ${createRateLimit.secondsLeft}s`
								: isSubmitting
									? "Adding..."
									: "Add"}
						</Button>
					</form>
					{createTodo.isError ? (
						<p className="text-destructive pt-2 text-xs">
							{createTodo.error instanceof Error ? createTodo.error.message : "Create failed"}
						</p>
					) : null}
					{replay.hasErrors ? (
						<p
							className="text-destructive pt-2 text-xs"
							role="alert"
							data-testid="todo-replay-error"
						>
							{replay.message}
						</p>
					) : null}
				</CardContent>
			</Card>

			<div className="flex flex-col gap-2">
				{isLoading ? (
					<>
						<Skeleton className="h-12 w-full" />
						<Skeleton className="h-12 w-full" />
						<Skeleton className="h-12 w-full" />
					</>
				) : null}
				{isError ? (
					<p className="text-destructive text-sm">
						{error instanceof Error ? error.message : "Failed to load todos"}
					</p>
				) : null}
				{todos && todos.length === 0 ? (
					<Empty className="border">
						<EmptyHeader>
							<EmptyMedia variant="icon">
								<HugeiconsIcon icon={Task01Icon} strokeWidth={2} />
							</EmptyMedia>
							<EmptyTitle>No todos yet</EmptyTitle>
							<EmptyDescription>
								{canCreate ? "Add your first todo above." : "Items will appear here once created."}
							</EmptyDescription>
						</EmptyHeader>
					</Empty>
				) : null}
				{todos?.map(todo => (
					<TodoItem key={todo.id} todo={todo} canEdit={canEdit} canDelete={canDelete} />
				))}
			</div>
		</section>
	)
}
