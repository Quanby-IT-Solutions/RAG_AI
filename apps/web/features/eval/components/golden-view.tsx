"use client"

import { useRef, useState, type FormEvent } from "react"

import { GoldenSetExportSchema } from "@repo/contracts"

import { Badge } from "@/core/components/ui/badge"
import { Button } from "@/core/components/ui/button"
import {
	Card,
	CardContent,
	CardDescription,
	CardHeader,
	CardTitle,
} from "@/core/components/ui/card"
import { Checkbox } from "@/core/components/ui/checkbox"
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
import { Textarea } from "@/core/components/ui/textarea"
import {
	exportGoldenSet,
	useCreateGoldenItemMutation,
	useGoldenItemsQuery,
	useImportGoldenSetMutation,
	useSearchChunksMutation,
	useUpdateGoldenItemMutation,
	type GoldenItemUi,
} from "@/features/eval/api/eval.hooks"

function truncate(text: string, length = 120): string {
	return text.length > length ? `${text.slice(0, length - 1)}…` : text
}

function itemBadges(item: GoldenItemUi) {
	const badges: { label: string; variant: "default" | "destructive" | "secondary" }[] = []
	badges.push(
		item.status === "active"
			? { label: "active", variant: "default" }
			: { label: "retired", variant: "secondary" }
	)
	if (item.staleHashes.length > 0) {
		// AC: orphaned expected hashes flag the item stale, never silent.
		badges.push({ label: `stale (${item.staleHashes.length})`, variant: "destructive" })
	}
	return badges
}

/**
 * Golden evaluation set console (EV-1). Curate questions with expected
 * chunks (bound by content hash via live retrieval) and ideal answers.
 */
export function GoldenView() {
	const { data: items, isPending, error } = useGoldenItemsQuery()
	const createItem = useCreateGoldenItemMutation()
	const updateItem = useUpdateGoldenItemMutation()
	const importSet = useImportGoldenSetMutation()
	const searchChunks = useSearchChunksMutation()

	const [question, setQuestion] = useState("")
	const [idealAnswer, setIdealAnswer] = useState("")
	const [tagsText, setTagsText] = useState("")
	const [pickerQuery, setPickerQuery] = useState("")
	const [selectedHashes, setSelectedHashes] = useState<string[]>([])
	const [importError, setImportError] = useState<string | null>(null)
	const [importedNotice, setImportedNotice] = useState<string | null>(null)
	const fileInputRef = useRef<HTMLInputElement>(null)

	const toggleHash = (hash: string, checked: boolean) => {
		setSelectedHashes(previous =>
			checked ? [...previous, hash] : previous.filter(h => h !== hash)
		)
	}

	const handleSearch = () => {
		if (!pickerQuery.trim() || searchChunks.isPending) return
		searchChunks.mutate({ query: pickerQuery.trim(), k: 10 })
	}

	const handleCreate = (event: FormEvent<HTMLFormElement>) => {
		event.preventDefault()
		if (!question.trim() || !idealAnswer.trim() || selectedHashes.length === 0) return
		const tags = tagsText
			.split(",")
			.map(tag => tag.trim())
			.filter(Boolean)
		createItem.mutate(
			{
				question: question.trim(),
				idealAnswer: idealAnswer.trim(),
				expectedChunkHashes: selectedHashes,
				tags,
			},
			{
				onSuccess: () => {
					setQuestion("")
					setIdealAnswer("")
					setTagsText("")
					setSelectedHashes([])
					searchChunks.reset()
					setPickerQuery("")
				},
			}
		)
	}

	const handleExport = async () => {
		const payload = await exportGoldenSet()
		const blob = new Blob([JSON.stringify(payload, null, 2)], { type: "application/json" })
		const url = URL.createObjectURL(blob)
		const anchor = document.createElement("a")
		anchor.href = url
		anchor.download = "golden-set.json"
		anchor.click()
		URL.revokeObjectURL(url)
	}

	const handleImportFile = async (file: File) => {
		setImportError(null)
		setImportedNotice(null)
		try {
			const parsed = GoldenSetExportSchema.safeParse(JSON.parse(await file.text()))
			if (!parsed.success) {
				setImportError("Invalid golden-set file: expected { version: 1, items: [...] }")
				return
			}
			importSet.mutate(parsed.data, {
				onSuccess: result => setImportedNotice(`${result.imported} item(s) imported`),
				onError: err =>
					setImportError(err instanceof Error ? err.message : "Import failed"),
			})
		} catch {
			setImportError("File is not valid JSON")
		}
	}

	return (
		<div className="flex flex-col gap-6 p-4 md:p-6">
			<Card>
				<CardHeader>
					<CardTitle>Add golden item</CardTitle>
					<CardDescription>
						Expected chunks bind by content hash — run live retrieval below and pick the
						chunk(s) that SHOULD come back for the question.
					</CardDescription>
				</CardHeader>
				<CardContent>
					<form aria-label="Add golden item" className="flex flex-col gap-4" onSubmit={handleCreate}>
						<div className="flex flex-col gap-2">
							<Label htmlFor="golden-question">Question</Label>
							<Input
								id="golden-question"
								value={question}
								onChange={event => setQuestion(event.target.value)}
								placeholder="How many vacation days do employees get?"
								required
							/>
						</div>
						<div className="flex flex-col gap-2">
							<Label htmlFor="golden-answer">Ideal answer</Label>
							<Textarea
								id="golden-answer"
								value={idealAnswer}
								onChange={event => setIdealAnswer(event.target.value)}
								placeholder="Employees get 20 vacation days per year."
								required
							/>
						</div>
						<div className="flex max-w-md flex-col gap-2">
							<Label htmlFor="golden-tags">Tags (comma-separated)</Label>
							<Input
								id="golden-tags"
								value={tagsText}
								onChange={event => setTagsText(event.target.value)}
								placeholder="hr, policies"
							/>
						</div>

						<fieldset className="flex flex-col gap-2 rounded-lg border p-3">
							<legend className="px-1 text-sm font-medium">Expected chunks</legend>
							<div className="flex gap-2">
								<Input
									aria-label="Retrieval query"
									value={pickerQuery}
									onChange={event => setPickerQuery(event.target.value)}
									placeholder="Search the corpus with live retrieval…"
									onKeyDown={event => {
										if (event.key === "Enter") {
											event.preventDefault()
											handleSearch()
										}
									}}
								/>
								<Button
									type="button"
									variant="secondary"
									onClick={handleSearch}
									disabled={searchChunks.isPending || !pickerQuery.trim()}
								>
									{searchChunks.isPending ? "Searching…" : "Run retrieval"}
								</Button>
							</div>
							{searchChunks.error ? (
								<p role="alert" className="text-destructive text-sm">
									{searchChunks.error.message}
								</p>
							) : null}
							{searchChunks.data ? (
								searchChunks.data.length === 0 ? (
									<p className="text-muted-foreground text-sm">No chunks retrieved.</p>
								) : (
									<ul className="flex flex-col gap-2" data-testid="chunk-candidates">
										{searchChunks.data.map(candidate => (
											<li key={candidate.contentHash} className="flex items-start gap-2">
												<Checkbox
													id={`pick-${candidate.contentHash}`}
													checked={selectedHashes.includes(candidate.contentHash)}
													onCheckedChange={checked =>
														toggleHash(candidate.contentHash, checked === true)
													}
												/>
												<Label
													htmlFor={`pick-${candidate.contentHash}`}
													className="flex flex-col items-start gap-1 text-left font-normal"
												>
													<span className="text-sm">{truncate(candidate.content)}</span>
													<span className="text-muted-foreground font-mono text-[10px]">
														{candidate.contentHash.slice(0, 16)}…
													</span>
												</Label>
											</li>
										))}
									</ul>
								)
							) : null}
							{selectedHashes.length > 0 ? (
								<p className="text-muted-foreground text-xs">
									{selectedHashes.length} chunk(s) selected
								</p>
							) : null}
						</fieldset>

						{createItem.error ? (
							<p role="alert" className="text-destructive text-sm">
								{createItem.error.message}
							</p>
						) : null}
						<div>
							<Button
								type="submit"
								disabled={
									createItem.isPending ||
									!question.trim() ||
									!idealAnswer.trim() ||
									selectedHashes.length === 0
								}
							>
								{createItem.isPending ? "Saving…" : "Add item"}
							</Button>
						</div>
					</form>
				</CardContent>
			</Card>

			<Card>
				<CardHeader className="flex flex-row items-start justify-between gap-2">
					<div className="flex flex-col gap-1.5">
						<CardTitle>Golden set</CardTitle>
						<CardDescription>
							Items with orphaned chunk hashes are flagged stale after re-chunking.
						</CardDescription>
					</div>
					<div className="flex gap-2">
						<Button type="button" variant="outline" size="sm" onClick={() => void handleExport()}>
							Export JSON
						</Button>
						<Button
							type="button"
							variant="outline"
							size="sm"
							onClick={() => fileInputRef.current?.click()}
							disabled={importSet.isPending}
						>
							Import JSON
						</Button>
						<input
							ref={fileInputRef}
							type="file"
							accept="application/json"
							className="hidden"
							aria-label="Import golden set file"
							onChange={event => {
								const file = event.target.files?.[0]
								if (file) void handleImportFile(file)
								event.target.value = ""
							}}
						/>
					</div>
				</CardHeader>
				<CardContent>
					{importError ? (
						<p role="alert" className="text-destructive mb-2 text-sm">
							{importError}
						</p>
					) : null}
					{importedNotice ? (
						<p role="status" className="text-muted-foreground mb-2 text-sm">
							{importedNotice}
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
					) : !items || items.length === 0 ? (
						<p className="text-muted-foreground text-sm">
							No golden items yet. Seeding needs an ingested corpus.
						</p>
					) : (
						<Table data-testid="golden-items-table">
							<TableHeader>
								<TableRow>
									<TableHead>Question</TableHead>
									<TableHead>Ideal answer</TableHead>
									<TableHead>Chunks</TableHead>
									<TableHead>Tags</TableHead>
									<TableHead>Status</TableHead>
									<TableHead className="text-right">Actions</TableHead>
								</TableRow>
							</TableHeader>
							<TableBody>
								{items.map(item => (
									<TableRow key={item.id}>
										<TableCell className="max-w-xs align-top font-medium">
											{truncate(item.question, 80)}
										</TableCell>
										<TableCell className="text-muted-foreground max-w-xs align-top text-sm">
											{truncate(item.idealAnswer, 80)}
										</TableCell>
										<TableCell className="align-top">
											<span className="font-mono text-xs">
												{item.expectedChunkHashes.length}
											</span>
										</TableCell>
										<TableCell className="align-top">
											<div className="flex max-w-40 flex-wrap gap-1">
												{item.tags.map(tag => (
													<Badge key={tag} variant="outline" className="text-[10px]">
														{tag}
													</Badge>
												))}
											</div>
										</TableCell>
										<TableCell className="align-top">
											<div className="flex flex-wrap gap-1">
												{itemBadges(item).map(badge => (
													<Badge key={badge.label} variant={badge.variant}>
														{badge.label}
													</Badge>
												))}
											</div>
										</TableCell>
										<TableCell className="text-right align-top">
											<Button
												type="button"
												variant={item.status === "active" ? "destructive" : "outline"}
												size="sm"
												aria-label={`${item.status === "active" ? "Retire" : "Reactivate"} ${truncate(item.question, 40)}`}
												disabled={updateItem.isPending}
												onClick={() =>
													updateItem.mutate({
														id: item.id,
														status: item.status === "active" ? "retired" : "active",
													})
												}
											>
												{item.status === "active" ? "Retire" : "Reactivate"}
											</Button>
										</TableCell>
									</TableRow>
								))}
							</TableBody>
						</Table>
					)}
					{updateItem.error ? (
						<p role="alert" className="text-destructive mt-2 text-sm">
							{updateItem.error.message}
						</p>
					) : null}
				</CardContent>
			</Card>
		</div>
	)
}
