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
import { Skeleton } from "@/core/components/ui/skeleton"
import {
	Table,
	TableBody,
	TableCell,
	TableHead,
	TableHeader,
	TableRow,
} from "@/core/components/ui/table"
import {
	useEvalRunComparisonQuery,
	useEvalRunsQuery,
	useExecuteEvalRunMutation,
	type EvalRunSummaryUi,
} from "@/features/eval/api/eval.hooks"

const METRIC_COLUMNS = ["hitRate", "mrr", "precisionAtK", "recallAtK"] as const

function formatMetric(value: number | undefined | null): string {
	return typeof value === "number" ? value.toFixed(3) : "—"
}

function formatDelta(value: number): string {
	const fixed = value.toFixed(3)
	return value > 0 ? `+${fixed}` : fixed
}

function statusVariant(status: EvalRunSummaryUi["status"]): "default" | "secondary" | "destructive" {
	if (status === "completed") return "default"
	if (status === "running") return "secondary"
	return "destructive"
}

/**
 * Retrieval eval runs console (EV-2): trigger runs against the golden set,
 * list run history with aggregates, and diff two runs per metric.
 */
export function RunsPanel() {
	const { data: runs, isPending, error } = useEvalRunsQuery()
	const executeRun = useExecuteEvalRunMutation()

	const [kText, setKText] = useState("")
	const [rerankMode, setRerankMode] = useState<"default" | "on" | "off">("default")
	const [withGeneration, setWithGeneration] = useState(false)
	const [baseId, setBaseId] = useState<string | null>(null)
	const [candidateId, setCandidateId] = useState<string | null>(null)
	const comparison = useEvalRunComparisonQuery(baseId, candidateId)

	const handleExecute = () => {
		const k = Number.parseInt(kText, 10)
		executeRun.mutate({
			...(Number.isNaN(k) ? {} : { k }),
			// RT-3 A/B: "default" omits the flag (configured behavior).
			...(rerankMode === "default" ? {} : { rerank: rerankMode === "on" }),
			// EV-3: also score faithfulness + answer relevancy.
			...(withGeneration && { generation: true }),
		})
	}

	const toggleCompare = (id: string) => {
		if (baseId === id) {
			setBaseId(null)
		} else if (candidateId === id) {
			setCandidateId(null)
		} else if (!baseId) {
			setBaseId(id)
		} else {
			setCandidateId(id)
		}
	}

	return (
		<Card>
			<CardHeader>
				<CardTitle>Retrieval eval runs</CardTitle>
				<CardDescription>
					Each run scores the real retrieval pipeline against the active golden set and is
					stamped with the git sha + exact config. Select two runs to compare.
				</CardDescription>
			</CardHeader>
			<CardContent className="flex flex-col gap-4">
				<div className="flex items-end gap-2">
					<div className="flex flex-col gap-2">
						<Label htmlFor="run-k">k (optional)</Label>
						<Input
							id="run-k"
							className="w-24"
							inputMode="numeric"
							value={kText}
							onChange={event => setKText(event.target.value)}
							placeholder="5"
						/>
					</div>
					<div className="flex flex-col gap-2">
						<Label htmlFor="run-rerank">Rerank</Label>
						<select
							id="run-rerank"
							className="border-input bg-background h-9 rounded-md border px-2 text-sm"
							value={rerankMode}
							onChange={event =>
								setRerankMode(event.target.value as "default" | "on" | "off")
							}
						>
							<option value="default">config default</option>
							<option value="on">on</option>
							<option value="off">off</option>
						</select>
					</div>
					<label className="flex h-9 items-center gap-1.5 text-sm">
						<input
							type="checkbox"
							checked={withGeneration}
							onChange={event => setWithGeneration(event.target.checked)}
						/>
						generation metrics
					</label>
					<Button type="button" onClick={handleExecute} disabled={executeRun.isPending}>
						{executeRun.isPending ? "Running…" : "Run evaluation"}
					</Button>
				</div>
				{executeRun.error ? (
					<p role="alert" className="text-destructive text-sm">
						{executeRun.error.message}
					</p>
				) : null}

				{isPending ? (
					<div className="flex flex-col gap-2">
						<Skeleton className="h-8 w-full" />
						<Skeleton className="h-8 w-full" />
					</div>
				) : error ? (
					<p role="alert" className="text-destructive text-sm">
						{error.message}
					</p>
				) : !runs || runs.length === 0 ? (
					<p className="text-muted-foreground text-sm">No runs yet.</p>
				) : (
					<div className="overflow-x-auto">
						<Table data-testid="eval-runs-table">
							<TableHeader>
								<TableRow>
									<TableHead>Compare</TableHead>
									<TableHead>Status</TableHead>
									<TableHead>Git SHA</TableHead>
									<TableHead>k</TableHead>
									<TableHead>Rerank</TableHead>
									<TableHead>Hit rate</TableHead>
									<TableHead>MRR</TableHead>
									<TableHead>P@k</TableHead>
									<TableHead>R@k</TableHead>
									<TableHead>Faithful</TableHead>
									<TableHead>Relevancy</TableHead>
									<TableHead>Items</TableHead>
									<TableHead>Stale</TableHead>
									<TableHead>When</TableHead>
								</TableRow>
							</TableHeader>
							<TableBody>
								{runs.map(run => {
									const selected = run.id === baseId || run.id === candidateId
									const role = run.id === baseId ? "base" : run.id === candidateId ? "candidate" : null
									return (
										<TableRow key={run.id} data-state={selected ? "selected" : undefined}>
											<TableCell>
												<Button
													type="button"
													variant={selected ? "default" : "outline"}
													size="sm"
													aria-label={`${selected ? "Deselect" : "Select"} run ${run.id.slice(0, 8)} for comparison`}
													onClick={() => toggleCompare(run.id)}
												>
													{role ?? "Select"}
												</Button>
											</TableCell>
											<TableCell>
												<Badge variant={statusVariant(run.status)}>{run.status}</Badge>
											</TableCell>
											<TableCell className="font-mono text-xs">
												{run.gitSha.slice(0, 10)}
											</TableCell>
											<TableCell>{String(run.config["k"] ?? "—")}</TableCell>
											<TableCell>{run.config["rerank"] ? "on" : "off"}</TableCell>
											{METRIC_COLUMNS.map(metric => (
												<TableCell key={metric} className="font-mono text-xs">
													{formatMetric(run.metrics?.[metric])}
												</TableCell>
											))}
											<TableCell className="font-mono text-xs">
												{formatMetric(run.metrics?.["faithfulness"])}
											</TableCell>
											<TableCell className="font-mono text-xs">
												{formatMetric(run.metrics?.["answerRelevancy"])}
											</TableCell>
											<TableCell>{formatMetric(run.metrics?.["itemCount"])}</TableCell>
											<TableCell>
												{formatMetric(run.metrics?.["skippedStaleCount"])}
											</TableCell>
											<TableCell className="text-muted-foreground text-xs">
												{new Date(run.createdAt).toLocaleString()}
											</TableCell>
										</TableRow>
									)
								})}
							</TableBody>
						</Table>
					</div>
				)}

				{baseId && candidateId ? (
					comparison.isPending ? (
						<Skeleton className="h-16 w-full" />
					) : comparison.error ? (
						<p role="alert" className="text-destructive text-sm">
							{comparison.error.message}
						</p>
					) : comparison.data ? (
						<div className="rounded-lg border p-3" data-testid="run-comparison">
							<p className="mb-2 text-sm font-medium">
								Δ candidate ({comparison.data.candidate.gitSha.slice(0, 10)}) − base (
								{comparison.data.base.gitSha.slice(0, 10)})
							</p>
							<div className="flex flex-wrap gap-3">
								{Object.entries(comparison.data.deltas).map(([metric, delta]) => (
									<Badge
										key={metric}
										variant={delta < 0 ? "destructive" : "secondary"}
										className="font-mono"
									>
										{metric}: {formatDelta(delta)}
									</Badge>
								))}
							</div>
						</div>
					) : null
				) : null}
			</CardContent>
		</Card>
	)
}
