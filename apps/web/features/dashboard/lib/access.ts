import { ADMIN_ROLE, type PermissionName } from "@repo/contracts"

export interface AccessProfile {
	roles: readonly string[]
	/** Granted keys: catalog `PermissionName`s plus optional wildcard grants (e.g. `corpus.*`). */
	permissions: readonly string[]
}

export type PermissionRequirement = PermissionName | readonly PermissionName[]
export type RoleRequirement = string | readonly string[]

export interface AccessRequirement {
	requiredPermission?: PermissionRequirement
	requiredRole?: RoleRequirement
}

function toArray<T extends string>(value: T | readonly T[] | undefined): readonly T[] {
	if (!value) {
		return []
	}
	return typeof value === "string" ? [value] : value
}

function hasPermission(access: AccessProfile, requiredPermission: PermissionName): boolean {
	const granted = access.permissions
	if (granted.includes(requiredPermission)) {
		return true
	}

	// Progressive dotted-prefix wildcards: a granted `corpus.*` or
	// `corpus.documents.*` satisfies a required `corpus.documents.upload`.
	const segments = requiredPermission.split(".")
	for (let i = 1; i < segments.length; i++) {
		if (granted.includes(`${segments.slice(0, i).join(".")}.*`)) {
			return true
		}
	}

	return false
}

export function canAccess(access: AccessProfile, item: AccessRequirement): boolean {
	const requiredPermissions = toArray(item.requiredPermission)
	const requiredRoles = toArray(item.requiredRole)

	if (requiredPermissions.length === 0 && requiredRoles.length === 0) {
		return true
	}

	if (access.roles.includes(ADMIN_ROLE)) {
		return true
	}

	if (requiredRoles.some(role => access.roles.includes(role))) {
		return true
	}

	return requiredPermissions.some(permission => hasPermission(access, permission))
}
