import type { Metadata } from "next"

import { PERMISSION_NAMES, type PermissionName } from "@repo/contracts"

import { ApiKeysView } from "@/features/api-keys/components/api-keys-view"
import { canAccess } from "@/features/dashboard/lib/access"
import { getAccess } from "@/features/dashboard/server/get-access"

export const metadata: Metadata = {
	title: "API Keys",
}

export default async function ApiKeysPage() {
	const access = await getAccess()
	const grants = { roles: access.roles, permissions: access.permissions }
	const hasAccess = canAccess(grants, { requiredPermission: "apikeys.keys.manage" })

	if (!hasAccess) {
		return (
			<div className="p-6">
				<h1 className="text-lg font-semibold">API Keys</h1>
				<p className="text-muted-foreground mt-2 text-sm" role="alert">
					You do not have permission to manage API keys. Contact an administrator to request
					the <code>apikeys.keys.manage</code> permission.
				</p>
			</div>
		)
	}

	// Key scopes are capped at the caller's own effective permissions
	// (enforced server-side too); admins can grant any catalog key.
	const grantablePermissions: PermissionName[] = PERMISSION_NAMES.filter(permission =>
		canAccess(grants, { requiredPermission: permission })
	)

	return <ApiKeysView grantablePermissions={grantablePermissions} />
}
