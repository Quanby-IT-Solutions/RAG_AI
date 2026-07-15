"use client"

import { useMemo, useState } from "react"
import { Cancel01Icon, UserGroupIcon } from "@hugeicons/core-free-icons"
import { HugeiconsIcon } from "@hugeicons/react"

import { Badge } from "@/core/components/ui/badge"
import { Button } from "@/core/components/ui/button"
import { Input } from "@/core/components/ui/input"
import {
	Empty,
	EmptyDescription,
	EmptyHeader,
	EmptyMedia,
	EmptyTitle,
} from "@/core/components/ui/empty"
import {
	Select,
	SelectContent,
	SelectItem,
	SelectTrigger,
	SelectValue,
} from "@/core/components/ui/select"
import { Skeleton } from "@/core/components/ui/skeleton"
import {
	Table,
	TableBody,
	TableCell,
	TableHead,
	TableHeader,
	TableRow,
} from "@/core/components/ui/table"
import { useRateLimitToast } from "@/core/hooks/use-rate-limit-toast"
import {
	useAssignRoleMutation,
	useCreateUserMutation,
	useRemoveRoleMutation,
	useRolesQuery,
	useSetUserDisabledMutation,
	useUsersQuery,
} from "@/features/user-management/api/rbac.hooks"

export function UsersPanel() {
	const { data: users, isLoading, isError, error } = useUsersQuery()
	const { data: roles } = useRolesQuery()
	const assignRole = useAssignRoleMutation()
	const removeRole = useRemoveRoleMutation()
	const createUser = useCreateUserMutation()
	const setDisabled = useSetUserDisabledMutation()
	const [newName, setNewName] = useState("")
	const [newEmail, setNewEmail] = useState("")
	const [newPassword, setNewPassword] = useState("")
	const [createdNotice, setCreatedNotice] = useState<string | null>(null)

	function handleCreateUser(event: React.FormEvent) {
		event.preventDefault()
		setCreatedNotice(null)
		createUser.mutate(
			{ name: newName.trim(), email: newEmail.trim(), temporaryPassword: newPassword },
			{
				onSuccess: created => {
					setCreatedNotice(
						`${created.email} created — share the temporary password with them securely; it is not shown again.`
					)
					setNewName("")
					setNewEmail("")
					setNewPassword("")
				},
			}
		)
	}

	// Live 429 countdowns; keep only the triggering control disabled per window.
	const assignRateLimit = useRateLimitToast(assignRole.error, { toastId: "rbac-assign-role" })
	const removeRateLimit = useRateLimitToast(removeRole.error, { toastId: "rbac-remove-role" })

	const allRoleNames = useMemo(() => (roles ?? []).map(role => role.name), [roles])

	return (
		<div className="flex flex-col gap-4">
			<div>
				<h2 className="text-lg font-semibold">Users</h2>
				<p className="text-muted-foreground text-sm">
					Create accounts, assign roles, and disable access. There is no public registration.
				</p>
			</div>

			{/* F4: admin-provisioned account creation (email + temp password). */}
			<form
				onSubmit={handleCreateUser}
				className="flex flex-col gap-2 rounded-lg border p-3 sm:flex-row sm:items-end"
				aria-label="Create user"
			>
				<div className="flex-1">
					<label htmlFor="new-user-name" className="text-xs font-medium">
						Name
					</label>
					<Input
						id="new-user-name"
						value={newName}
						onChange={event => setNewName(event.target.value)}
						required
						disabled={createUser.isPending}
					/>
				</div>
				<div className="flex-1">
					<label htmlFor="new-user-email" className="text-xs font-medium">
						Email
					</label>
					<Input
						id="new-user-email"
						type="email"
						value={newEmail}
						onChange={event => setNewEmail(event.target.value)}
						required
						disabled={createUser.isPending}
					/>
				</div>
				<div className="flex-1">
					<label htmlFor="new-user-password" className="text-xs font-medium">
						Temporary password
					</label>
					<Input
						id="new-user-password"
						type="text"
						value={newPassword}
						onChange={event => setNewPassword(event.target.value)}
						minLength={8}
						required
						disabled={createUser.isPending}
					/>
				</div>
				<Button type="submit" disabled={createUser.isPending}>
					{createUser.isPending ? "Creating..." : "Create user"}
				</Button>
			</form>
			{createUser.isError ? (
				<p className="text-destructive text-xs" role="alert">
					{createUser.error instanceof Error ? createUser.error.message : "Failed to create user"}
				</p>
			) : null}
			{createdNotice ? (
				<p className="text-muted-foreground text-xs" role="status">
					{createdNotice}
				</p>
			) : null}
			{setDisabled.isError ? (
				<p className="text-destructive text-xs" role="alert">
					{setDisabled.error instanceof Error ? setDisabled.error.message : "Failed to update user"}
				</p>
			) : null}

			{isLoading ? (
				<div className="flex flex-col gap-2">
					<Skeleton className="h-10 w-full" />
					<Skeleton className="h-10 w-full" />
					<Skeleton className="h-10 w-full" />
				</div>
			) : null}

			{isError ? (
				<p className="text-destructive text-sm">
					{error instanceof Error ? error.message : "Failed to load users"}
				</p>
			) : null}

			{users && users.length > 0 ? (
				<div className="rounded-lg border">
					<Table>
						<TableHeader>
							<TableRow>
								<TableHead>User</TableHead>
								<TableHead>Roles</TableHead>
								<TableHead className="w-48">Assign role</TableHead>
							</TableRow>
						</TableHeader>
						<TableBody>
							{users.map(user => {
								const assigned = new Set(user.roles)
								const available = allRoleNames.filter(name => !assigned.has(name))
								return (
									<TableRow key={user.id}>
										<TableCell className="align-top">
											<div className="font-medium">{user.name ?? "—"}</div>
											<div className="text-muted-foreground text-xs">{user.email}</div>
										</TableCell>
										<TableCell className="align-top">
											{user.roles.length ? (
												<div className="flex flex-wrap gap-1">
													{user.roles.map(roleName => (
														<Badge key={roleName} variant="outline" className="gap-1 pr-1">
															{roleName}
															<button
																type="button"
																onClick={() => removeRole.mutate({ userId: user.id, roleName })}
																disabled={removeRole.isPending || removeRateLimit.isActive}
																className="hover:text-destructive inline-flex items-center"
																aria-label={`Remove ${roleName} from ${user.email}`}
															>
																<HugeiconsIcon
																	icon={Cancel01Icon}
																	strokeWidth={2}
																	className="size-3"
																/>
															</button>
														</Badge>
													))}
												</div>
											) : (
												<span className="text-muted-foreground text-xs">No roles</span>
											)}
										</TableCell>
										<TableCell className="align-top">
											<Select
												value={null}
												onValueChange={value => {
													if (typeof value === "string") {
														assignRole.mutate({ userId: user.id, roleName: value })
													}
												}}
												disabled={
													available.length === 0 || assignRole.isPending || assignRateLimit.isActive
												}
											>
												<SelectTrigger size="sm" className="w-full">
													<SelectValue>Add role</SelectValue>
												</SelectTrigger>
												<SelectContent>
													{available.map(name => (
														<SelectItem key={name} value={name}>
															{name}
														</SelectItem>
													))}
												</SelectContent>
											</Select>
										</TableCell>
									</TableRow>
								)
							})}
						</TableBody>
					</Table>
				</div>
			) : null}

			{users && users.length === 0 ? (
				<Empty className="border">
					<EmptyHeader>
						<EmptyMedia variant="icon">
							<HugeiconsIcon icon={UserGroupIcon} strokeWidth={2} />
						</EmptyMedia>
						<EmptyTitle>No users found</EmptyTitle>
						<EmptyDescription>Users will appear here once an administrator provisions them.</EmptyDescription>
					</EmptyHeader>
				</Empty>
			) : null}
		</div>
	)
}
