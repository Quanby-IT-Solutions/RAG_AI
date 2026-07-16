"use client"

import { useState } from "react"

import type { ReportDefinition } from "@repo/contracts"

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
	Select,
	SelectContent,
	SelectItem,
	SelectTrigger,
	SelectValue,
} from "@/core/components/ui/select"
import {
	Table,
	TableBody,
	TableCell,
	TableHead,
	TableHeader,
	TableRow,
} from "@/core/components/ui/table"
import {
	useExportReportMutation,
	useReportsListQuery,
	useRunReportQuery,
} from "@/features/reports/api/reports.hooks"

const PAGE_SIZE = 25

function formatCell(value: unknown): string {
	if (value === null || value === undefined) return ""
	if (typeof value === "string") return value
	return String(value)
}

function FilterField({
	filter,
	value,
	onChange,
}: {
	filter: ReportDefinition["filters"][number]
	value: string
	onChange: (value: string) => void
}) {
	if (filter.type === "select") {
		return (
			<div className="flex flex-col gap-1.5">
				<Label>{filter.label}</Label>
				<Select
					value={value || "any"}
					onValueChange={next => onChange(!next || next === "any" ? "" : next)}
				>
					<SelectTrigger className="w-36">
						<SelectValue />
					</SelectTrigger>
					<SelectContent>
						<SelectItem value="any">Any</SelectItem>
						{(filter.options ?? []).map(option => (
							<SelectItem key={option} value={option}>
								{option}
							</SelectItem>
						))}
					</SelectContent>
				</Select>
			</div>
		)
	}
	return (
		<div className="flex flex-col gap-1.5">
			<Label htmlFor={`report-filter-${filter.key}`}>{filter.label}</Label>
			<Input
				id={`report-filter-${filter.key}`}
				type={filter.type === "date" ? "date" : "text"}
				className="w-44"
				value={value}
				onChange={event => onChange(event.target.value)}
			/>
		</div>
	)
}

/**
 * Reporting console (RP-1). Renders any registered report definition —
 * filters and columns come from the framework, so RP-2..5 reports appear
 * here without UI changes. Export needs reports.reports.export server-side.
 */
export function ReportsView() {
	const { data: definitions, isPending, error: listError } = useReportsListQuery()
	const [selectedKey, setSelectedKey] = useState<string | null>(null)
	const [draftFilters, setDraftFilters] = useState<Record<string, string>>({})
	const [appliedFilters, setAppliedFilters] = useState<Record<string, string>>({})
	const [page, setPage] = useState(1)
	const exportReport = useExportReportMutation()

	const activeKey = selectedKey ?? definitions?.[0]?.key ?? null
	const definition = definitions?.find(item => item.key === activeKey)
	const {
		data: result,
		isFetching,
		error: runError,
	} = useRunReportQuery(activeKey, appliedFilters, page, PAGE_SIZE)

	const totalPages = result ? Math.max(1, Math.ceil(result.total / result.pageSize)) : 1
	const error = listError ?? runError ?? exportReport.error

	if (isPending) {
		return <p className="text-muted-foreground p-6 text-sm">Loading reports…</p>
	}

	return (
		<div className="flex flex-col gap-4 p-6" data-testid="reports-view">
			<div className="flex flex-wrap items-end gap-3">
				<div className="flex flex-col gap-1.5">
					<Label>Report</Label>
					<Select
						value={activeKey ?? undefined}
						onValueChange={key => {
							if (!key) return
							setSelectedKey(key)
							setDraftFilters({})
							setAppliedFilters({})
							setPage(1)
						}}
					>
						<SelectTrigger className="w-64">
							<SelectValue>{definition?.title ?? "Choose a report"}</SelectValue>
						</SelectTrigger>
						<SelectContent>
							{(definitions ?? []).map(item => (
								<SelectItem key={item.key} value={item.key}>
									{item.title}
								</SelectItem>
							))}
						</SelectContent>
					</Select>
				</div>
				{definition ? (
					<>
						<Button
							type="button"
							variant="outline"
							disabled={exportReport.isPending}
							onClick={() =>
								exportReport.mutate({
									key: definition.key,
									filters: appliedFilters,
									format: "csv",
								})
							}
						>
							Export CSV
						</Button>
						<Button
							type="button"
							variant="outline"
							disabled={exportReport.isPending}
							onClick={() =>
								exportReport.mutate({
									key: definition.key,
									filters: appliedFilters,
									format: "json",
								})
							}
						>
							Export JSON
						</Button>
					</>
				) : null}
			</div>

			{definition ? (
				<Card>
					<CardHeader>
						<CardTitle>{definition.title}</CardTitle>
						<CardDescription>{definition.description}</CardDescription>
					</CardHeader>
					<CardContent className="flex flex-col gap-4">
						<form
							className="flex flex-wrap items-end gap-3"
							onSubmit={event => {
								event.preventDefault()
								setAppliedFilters(
									Object.fromEntries(
										Object.entries(draftFilters).filter(([, value]) => value !== "")
									)
								)
								setPage(1)
							}}
						>
							{definition.filters.map(filter => (
								<FilterField
									key={filter.key}
									filter={filter}
									value={draftFilters[filter.key] ?? ""}
									onChange={value =>
										setDraftFilters(previous => ({ ...previous, [filter.key]: value }))
									}
								/>
							))}
							<Button type="submit" disabled={isFetching}>
								Apply filters
							</Button>
						</form>

						{error ? (
							<p role="alert" className="text-destructive text-sm">
								{error.message}
							</p>
						) : null}

						<div className="overflow-x-auto">
							<Table>
								<TableHeader>
									<TableRow>
										{definition.columns.map(column => (
											<TableHead key={column.key}>{column.label}</TableHead>
										))}
									</TableRow>
								</TableHeader>
								<TableBody>
									{(result?.rows ?? []).map((row, index) => (
										<TableRow key={index}>
											{definition.columns.map(column => (
												<TableCell key={column.key} className="whitespace-nowrap">
													{formatCell(row[column.key])}
												</TableCell>
											))}
										</TableRow>
									))}
									{result && result.rows.length === 0 ? (
										<TableRow>
											<TableCell
												colSpan={definition.columns.length}
												className="text-muted-foreground text-center"
											>
												No rows match the current filters.
											</TableCell>
										</TableRow>
									) : null}
								</TableBody>
							</Table>
						</div>

						<div className="flex items-center gap-3 text-sm">
							<Button
								type="button"
								variant="outline"
								size="sm"
								disabled={page <= 1 || isFetching}
								onClick={() => setPage(current => current - 1)}
							>
								Previous
							</Button>
							<span className="text-muted-foreground">
								Page {result?.page ?? page} of {totalPages}
								{result ? ` · ${result.total} rows` : ""}
							</span>
							<Button
								type="button"
								variant="outline"
								size="sm"
								disabled={page >= totalPages || isFetching}
								onClick={() => setPage(current => current + 1)}
							>
								Next
							</Button>
						</div>
					</CardContent>
				</Card>
			) : (
				<p className="text-muted-foreground text-sm">No reports available.</p>
			)}
		</div>
	)
}
