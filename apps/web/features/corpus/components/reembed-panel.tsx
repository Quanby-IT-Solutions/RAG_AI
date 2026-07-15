"use client"

import { useState } from "react"

import { Badge } from "@/core/components/ui/badge"
import { Button } from "@/core/components/ui/button"
import {
	Card,
	CardContent,
	CardDescription,
	CardHeader,
	CardTitle,
} from "@/core/components/ui/card"
import { Input } from "@/core/components/ui/input"
import { Label } from "@/core/components/ui/label"
import {
	useReembedActionMutation,
	useReembedStatusQuery,
	useStartReembedMutation,
	type ReembedMigrationUi,
} from "@/features/corpus/api/corpus.hooks"

const STATUS_VARIANTS: Record<
	ReembedMigrationUi["status"],
	"default" | "secondary" | "destructive" | "outline"
> = {
	none: "outline",
	shadowing: "secondary",
	verifying: "secondary",
	ready: "default",
	cutover: "default",
	aborted: "outline",
	failed: "destructive",
}

/**
 * Zero-downtime re-embedding console (IN-8, Flow D): shadow → verify →
 * explicit cutover, with abort available until cutover. Serving stays on
 * the live column until the operator confirms.
 */
export function ReembedPanel() {
	const { data: migration } = useReembedStatusQuery()
	const start = useStartReembedMutation()
	const cutover = useReembedActionMutation("cutover")
	const abort = useReembedActionMutation("abort")
	const [targetModel, setTargetModel] = useState("")

	const status = migration?.status ?? "none"
	const canStart = ["none", "cutover", "aborted", "failed"].includes(status)
	const canCutover = status === "ready"
	const canAbort = ["shadowing", "verifying", "ready"].includes(status)
	const actionError = start.error ?? cutover.error ?? abort.error

	return (
		<Card data-testid="reembed-panel">
			<CardHeader>
				<CardTitle>Re-embedding (zero downtime)</CardTitle>
				<CardDescription>
					Shadow-embed the corpus to a new model, verify against the golden set, then cut
					over explicitly. Serving stays on the live index until cutover; abort removes the
					shadow without touching it.
				</CardDescription>
			</CardHeader>
			<CardContent className="flex flex-col gap-3">
				<div className="flex flex-wrap items-center gap-2 text-sm">
					<Badge variant={STATUS_VARIANTS[status]}>{status}</Badge>
					{migration?.targetModel ? (
						<code className="text-xs">{migration.targetModel}</code>
					) : null}
					{migration?.metrics?.["hitRate"] !== undefined ? (
						<span className="text-muted-foreground text-xs">
							shadow hitRate {migration.metrics["hitRate"]?.toFixed(3)} · MRR{" "}
							{migration.metrics["mrr"]?.toFixed(3)}
						</span>
					) : null}
					{migration?.error ? (
						<span className="text-destructive text-xs">{migration.error}</span>
					) : null}
				</div>
				<div className="flex flex-wrap items-end gap-2">
					<div className="flex flex-col gap-1.5">
						<Label htmlFor="reembed-model">Target model</Label>
						<Input
							id="reembed-model"
							className="w-64"
							placeholder="text-embedding-3-small"
							value={targetModel}
							onChange={event => setTargetModel(event.target.value)}
							disabled={!canStart}
						/>
					</div>
					<Button
						type="button"
						disabled={!canStart || !targetModel.trim() || start.isPending}
						onClick={() => start.mutate(targetModel.trim())}
					>
						Start shadow re-embed
					</Button>
					<Button
						type="button"
						variant="default"
						disabled={!canCutover || cutover.isPending}
						onClick={() => cutover.mutate()}
					>
						Cut over
					</Button>
					<Button
						type="button"
						variant="destructive"
						disabled={!canAbort || abort.isPending}
						onClick={() => abort.mutate()}
					>
						Abort
					</Button>
				</div>
				{actionError ? (
					<p role="alert" className="text-destructive text-sm">
						{actionError.message}
					</p>
				) : null}
			</CardContent>
		</Card>
	)
}
