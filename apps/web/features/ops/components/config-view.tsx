"use client"

import { Badge } from "@/core/components/ui/badge"
import {
	Card,
	CardContent,
	CardDescription,
	CardHeader,
	CardTitle,
} from "@/core/components/ui/card"
import { Skeleton } from "@/core/components/ui/skeleton"
import {
	Table,
	TableBody,
	TableCell,
	TableHead,
	TableHeader,
	TableRow,
} from "@/core/components/ui/table"
import { useEffectiveConfigQuery } from "@/features/ops/api/ops.hooks"

function renderValue(value: unknown): string {
	if (value === null || value === undefined) return "—"
	if (typeof value === "object") return JSON.stringify(value)
	return String(value)
}

/**
 * Configuration review (OB-5): the running deployment's effective settings,
 * grouped by module, secrets masked server-side. Read-only by design —
 * config changes are deploy-time (twelve-factor).
 */
export function ConfigView() {
	const { data, isPending, error } = useEffectiveConfigQuery()

	return (
		<div className="flex flex-col gap-4 p-4 md:p-6">
			<Card>
				<CardHeader>
					<CardTitle>Effective configuration</CardTitle>
					<CardDescription>
						Read-only view of the RAG service&apos;s running settings. Secret-typed values are
						masked before they ever leave the server.
					</CardDescription>
				</CardHeader>
				<CardContent className="flex flex-col gap-6">
					{isPending ? (
						<div className="flex flex-col gap-2">
							<Skeleton className="h-8 w-full" />
							<Skeleton className="h-8 w-full" />
						</div>
					) : error ? (
						<p role="alert" className="text-destructive text-sm">
							{error.message}
						</p>
					) : data ? (
						Object.entries(data.groups).map(([group, entries]) => (
							<section key={group} data-testid={`config-group-${group}`}>
								<h3 className="mb-2 text-sm font-semibold capitalize">{group}</h3>
								<Table>
									<TableHeader>
										<TableRow>
											<TableHead className="w-72">Setting</TableHead>
											<TableHead>Env</TableHead>
											<TableHead>Value</TableHead>
										</TableRow>
									</TableHeader>
									<TableBody>
										{entries.map(entry => (
											<TableRow key={entry.key}>
												<TableCell className="font-mono text-xs">{entry.key}</TableCell>
												<TableCell className="text-muted-foreground font-mono text-xs">
													{entry.env}
												</TableCell>
												<TableCell className="font-mono text-xs break-all">
													{entry.secret ? (
														<Badge variant="secondary">masked</Badge>
													) : (
														renderValue(entry.value)
													)}
												</TableCell>
											</TableRow>
										))}
									</TableBody>
								</Table>
							</section>
						))
					) : null}
				</CardContent>
			</Card>
		</div>
	)
}
