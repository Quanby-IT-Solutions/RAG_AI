"use client"

import { useState, type FormEvent } from "react"

import type { PermissionName } from "@repo/contracts"

import { Alert, AlertDescription, AlertTitle } from "@/core/components/ui/alert"
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
import {
	useApiKeysQuery,
	useCreateApiKeyMutation,
	useRevokeApiKeyMutation,
	type ApiKeyUi,
} from "@/features/api-keys/api/apikeys.hooks"

function formatTimestamp(iso: string | null): string {
	if (!iso) return "—"
	const date = new Date(iso)
	return Number.isNaN(date.getTime()) ? iso : date.toLocaleString()
}

function keyStatus(key: ApiKeyUi): { label: string; variant: "default" | "destructive" } {
	return key.revokedAt
		? { label: "Revoked", variant: "destructive" }
		: { label: "Active", variant: "default" }
}

/**
 * Integrator API keys console (SC-3). Keys are scoped to a subset of the
 * caller's own permissions; the secret is displayed ONCE after creation.
 */
export function ApiKeysView({
	grantablePermissions,
}: Readonly<{ grantablePermissions: readonly PermissionName[] }>) {
	const { data: keys, isPending, error } = useApiKeysQuery()
	const createKey = useCreateApiKeyMutation()
	const revokeKey = useRevokeApiKeyMutation()

	const [name, setName] = useState("")
	const [scopes, setScopes] = useState<PermissionName[]>([])
	const [createdSecret, setCreatedSecret] = useState<{ name: string; secret: string } | null>(null)

	const toggleScope = (scope: PermissionName, checked: boolean) => {
		setScopes(previous => (checked ? [...previous, scope] : previous.filter(s => s !== scope)))
	}

	const handleCreate = (event: FormEvent<HTMLFormElement>) => {
		event.preventDefault()
		if (!name.trim() || scopes.length === 0 || createKey.isPending) return
		createKey.mutate(
			{ name: name.trim(), scopes },
			{
				onSuccess: result => {
					setCreatedSecret({ name: result.name, secret: result.secret })
					setName("")
					setScopes([])
				},
			}
		)
	}

	return (
		<div className="flex flex-col gap-6 p-4 md:p-6">
			<Card>
				<CardHeader>
					<CardTitle>Create API key</CardTitle>
					<CardDescription>
						Scopes are limited to permissions you hold. The secret is shown once — store it
						securely; only a hash is kept.
					</CardDescription>
				</CardHeader>
				<CardContent>
					<form
						aria-label="Create API key"
						className="flex flex-col gap-4"
						onSubmit={handleCreate}
					>
						<div className="flex max-w-md flex-col gap-2">
							<Label htmlFor="api-key-name">Name</Label>
							<Input
								id="api-key-name"
								value={name}
								onChange={event => setName(event.target.value)}
								placeholder="e.g. CI ingestion bot"
								maxLength={120}
								required
							/>
						</div>
						<fieldset className="flex flex-col gap-2">
							<legend className="text-sm font-medium">Scopes</legend>
							{grantablePermissions.length === 0 ? (
								<p className="text-muted-foreground text-sm">
									You hold no grantable permissions.
								</p>
							) : (
								<div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
									{grantablePermissions.map(permission => (
										<div key={permission} className="flex items-center gap-2">
											<Checkbox
												id={`scope-${permission}`}
												checked={scopes.includes(permission)}
												onCheckedChange={checked =>
													toggleScope(permission, checked === true)
												}
											/>
											<Label htmlFor={`scope-${permission}`} className="font-mono text-xs">
												{permission}
											</Label>
										</div>
									))}
								</div>
							)}
						</fieldset>
						{createKey.error ? (
							<p role="alert" className="text-destructive text-sm">
								{createKey.error.message}
							</p>
						) : null}
						<div>
							<Button
								type="submit"
								disabled={createKey.isPending || !name.trim() || scopes.length === 0}
							>
								{createKey.isPending ? "Creating…" : "Create key"}
							</Button>
						</div>
					</form>
					{createdSecret ? (
						<Alert className="mt-4" data-testid="api-key-secret-notice">
							<AlertTitle>Key “{createdSecret.name}” created</AlertTitle>
							<AlertDescription className="flex flex-col gap-2">
								<span>Copy this secret now — it will not be shown again.</span>
								<code className="bg-muted rounded px-2 py-1 font-mono text-xs break-all">
									{createdSecret.secret}
								</code>
								<span>
									Send it as the <code>x-api-key</code> header.
								</span>
								<div>
									<Button
										variant="outline"
										size="sm"
										onClick={() => setCreatedSecret(null)}
									>
										Dismiss
									</Button>
								</div>
							</AlertDescription>
						</Alert>
					) : null}
				</CardContent>
			</Card>

			<Card>
				<CardHeader>
					<CardTitle>Your API keys</CardTitle>
					<CardDescription>
						Every request made with a key is audited. Revocation takes effect immediately.
					</CardDescription>
				</CardHeader>
				<CardContent>
					{isPending ? (
						<div className="flex flex-col gap-2">
							<Skeleton className="h-8 w-full" />
							<Skeleton className="h-8 w-full" />
						</div>
					) : error ? (
						<p role="alert" className="text-destructive text-sm">
							{error.message}
						</p>
					) : !keys || keys.length === 0 ? (
						<p className="text-muted-foreground text-sm">No API keys yet.</p>
					) : (
						<Table data-testid="api-keys-table">
							<TableHeader>
								<TableRow>
									<TableHead>Name</TableHead>
									<TableHead>Key</TableHead>
									<TableHead>Scopes</TableHead>
									<TableHead>Last used</TableHead>
									<TableHead>Created</TableHead>
									<TableHead>Status</TableHead>
									<TableHead className="text-right">Actions</TableHead>
								</TableRow>
							</TableHeader>
							<TableBody>
								{keys.map(key => {
									const status = keyStatus(key)
									return (
										<TableRow key={key.id}>
											<TableCell className="font-medium">{key.name}</TableCell>
											<TableCell className="font-mono text-xs">{key.keyPrefix}</TableCell>
											<TableCell>
												<div className="flex max-w-xs flex-wrap gap-1">
													{key.scopes.map(scope => (
														<Badge
															key={scope}
															variant="secondary"
															className="font-mono text-[10px]"
														>
															{scope}
														</Badge>
													))}
												</div>
											</TableCell>
											<TableCell>{formatTimestamp(key.lastUsedAt)}</TableCell>
											<TableCell>{formatTimestamp(key.createdAt)}</TableCell>
											<TableCell>
												<Badge variant={status.variant}>{status.label}</Badge>
											</TableCell>
											<TableCell className="text-right">
												{key.revokedAt ? null : (
													<Button
														variant="destructive"
														size="sm"
														aria-label={`Revoke ${key.name}`}
														disabled={revokeKey.isPending}
														onClick={() => revokeKey.mutate(key.id)}
													>
														Revoke
													</Button>
												)}
											</TableCell>
										</TableRow>
									)
								})}
							</TableBody>
						</Table>
					)}
					{revokeKey.error ? (
						<p role="alert" className="text-destructive mt-2 text-sm">
							{revokeKey.error.message}
						</p>
					) : null}
				</CardContent>
			</Card>
		</div>
	)
}
