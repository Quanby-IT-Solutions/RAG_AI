"use client"

import { useState } from "react"

import type { ReportDefinition, ReportFormat } from "@repo/contracts"

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
	isPermissionError,
	useCreateScheduleMutation,
	useDeleteScheduleMutation,
	useDownloadReportFileMutation,
	useReportFilesQuery,
	useSchedulesQuery,
	useUpdateScheduleMutation,
} from "@/features/reports/api/reports.hooks"

const ALL_FORMATS: ReportFormat[] = ["json", "csv", "xlsx", "pdf"]

/**
 * RP-6: schedule console + generated files. Schedule management needs
 * reports.schedules.manage server-side — a 403 hides the management card
 * (files remain visible with reports.reports.view).
 */
export function SchedulesPanel({ definitions }: { definitions: ReportDefinition[] }) {
	const { data: schedules, isError, error } = useSchedulesQuery()
	const files = useReportFilesQuery()
	const create = useCreateScheduleMutation()
	const update = useUpdateScheduleMutation()
	const remove = useDeleteScheduleMutation()
	const download = useDownloadReportFileMutation()

	const [reportKey, setReportKey] = useState(definitions[0]?.key ?? "")
	const [cron, setCron] = useState("0 6 * * *")
	const [formats, setFormats] = useState<ReportFormat[]>(["csv"])

	const canManage = !(isError && isPermissionError(error))
	const mutationError = create.error ?? update.error ?? remove.error

	return (
		<div className="flex flex-col gap-4">
			{canManage ? (
				<Card data-testid="schedules-panel">
					<CardHeader>
						<CardTitle>Scheduled reports</CardTitle>
						<CardDescription>
							The worker checks schedules every minute and stores the generated files
							below. Standard 5-field cron expressions (UTC).
						</CardDescription>
					</CardHeader>
					<CardContent className="flex flex-col gap-4">
						<form
							className="flex flex-wrap items-end gap-3"
							aria-label="Create schedule"
							onSubmit={event => {
								event.preventDefault()
								if (!reportKey || formats.length === 0) return
								create.mutate({
									reportKey,
									cron,
									filters: {},
									formats,
									recipients: [],
									enabled: true,
								})
							}}
						>
							<div className="flex flex-col gap-1.5">
								<Label>Report</Label>
								<Select value={reportKey} onValueChange={key => key && setReportKey(key)}>
									<SelectTrigger className="w-56">
										<SelectValue>
											{definitions.find(item => item.key === reportKey)?.title ??
												"Choose a report"}
										</SelectValue>
									</SelectTrigger>
									<SelectContent>
										{definitions.map(item => (
											<SelectItem key={item.key} value={item.key}>
												{item.title}
											</SelectItem>
										))}
									</SelectContent>
								</Select>
							</div>
							<div className="flex flex-col gap-1.5">
								<Label htmlFor="schedule-cron">Cron</Label>
								<Input
									id="schedule-cron"
									className="w-36 font-mono"
									value={cron}
									onChange={event => setCron(event.target.value)}
								/>
							</div>
							<div className="flex flex-col gap-1.5">
								<Label>Formats</Label>
								<div className="flex gap-1">
									{ALL_FORMATS.map(format => (
										<Button
											key={format}
											type="button"
											size="sm"
											variant={formats.includes(format) ? "default" : "outline"}
											onClick={() =>
												setFormats(previous =>
													previous.includes(format)
														? previous.filter(item => item !== format)
														: [...previous, format]
												)
											}
										>
											{format}
										</Button>
									))}
								</div>
							</div>
							<Button type="submit" disabled={create.isPending || formats.length === 0}>
								Add schedule
							</Button>
						</form>

						{mutationError ? (
							<p role="alert" className="text-destructive text-sm">
								{mutationError.message}
							</p>
						) : null}

						<Table>
							<TableHeader>
								<TableRow>
									<TableHead>Report</TableHead>
									<TableHead>Cron</TableHead>
									<TableHead>Formats</TableHead>
									<TableHead>Status</TableHead>
									<TableHead>Last run</TableHead>
									<TableHead className="text-right">Actions</TableHead>
								</TableRow>
							</TableHeader>
							<TableBody>
								{(schedules ?? []).map(schedule => (
									<TableRow key={schedule.id}>
										<TableCell>
											{definitions.find(item => item.key === schedule.reportKey)?.title ??
												schedule.reportKey}
										</TableCell>
										<TableCell className="font-mono text-xs">{schedule.cron}</TableCell>
										<TableCell>{schedule.formats.join(", ")}</TableCell>
										<TableCell>
											<Badge variant={schedule.enabled ? "default" : "outline"}>
												{schedule.enabled ? "enabled" : "paused"}
											</Badge>
										</TableCell>
										<TableCell className="text-muted-foreground text-xs">
											{schedule.lastRunAt
												? new Date(schedule.lastRunAt).toLocaleString()
												: "never"}
										</TableCell>
										<TableCell className="flex justify-end gap-2">
											<Button
												type="button"
												size="sm"
												variant="outline"
												disabled={update.isPending}
												onClick={() =>
													update.mutate({ id: schedule.id, enabled: !schedule.enabled })
												}
											>
												{schedule.enabled ? "Pause" : "Resume"}
											</Button>
											<Button
												type="button"
												size="sm"
												variant="destructive"
												disabled={remove.isPending}
												onClick={() => remove.mutate(schedule.id)}
											>
												Delete
											</Button>
										</TableCell>
									</TableRow>
								))}
								{schedules && schedules.length === 0 ? (
									<TableRow>
										<TableCell colSpan={6} className="text-muted-foreground text-center">
											No schedules yet.
										</TableCell>
									</TableRow>
								) : null}
							</TableBody>
						</Table>
					</CardContent>
				</Card>
			) : null}

			<Card data-testid="report-files-panel">
				<CardHeader>
					<CardTitle>Generated files</CardTitle>
					<CardDescription>
						Artifacts produced by schedules (newest first). Click to download.
					</CardDescription>
				</CardHeader>
				<CardContent>
					<Table>
						<TableHeader>
							<TableRow>
								<TableHead>Generated</TableHead>
								<TableHead>File</TableHead>
								<TableHead>Format</TableHead>
								<TableHead>By</TableHead>
								<TableHead className="text-right">Download</TableHead>
							</TableRow>
						</TableHeader>
						<TableBody>
							{(files.data ?? []).map(file => (
								<TableRow key={file.id}>
									<TableCell className="text-muted-foreground text-xs">
										{new Date(file.createdAt).toLocaleString()}
									</TableCell>
									<TableCell>{file.filename}</TableCell>
									<TableCell>{file.format}</TableCell>
									<TableCell>{file.generatedBy}</TableCell>
									<TableCell className="text-right">
										<Button
											type="button"
											size="sm"
											variant="outline"
											disabled={download.isPending}
											onClick={() => download.mutate(file.id)}
										>
											Download
										</Button>
									</TableCell>
								</TableRow>
							))}
							{files.data && files.data.length === 0 ? (
								<TableRow>
									<TableCell colSpan={5} className="text-muted-foreground text-center">
										Nothing generated yet.
									</TableCell>
								</TableRow>
							) : null}
						</TableBody>
					</Table>
				</CardContent>
			</Card>
		</div>
	)
}
