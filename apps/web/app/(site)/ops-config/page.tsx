import type { Metadata } from "next"

import { canAccess } from "@/features/dashboard/lib/access"
import { getAccess } from "@/features/dashboard/server/get-access"
import { ConfigView } from "@/features/ops/components/config-view"

export const metadata: Metadata = {
	title: "Configuration",
}

export default async function OpsConfigPage() {
	const access = await getAccess()
	const grants = { roles: access.roles, permissions: access.permissions }
	const hasAccess = canAccess(grants, { requiredPermission: "ops.config.view" })

	if (!hasAccess) {
		return (
			<div className="p-6">
				<h1 className="text-lg font-semibold">Configuration</h1>
				<p className="text-muted-foreground mt-2 text-sm" role="alert">
					You do not have permission to review configuration. Contact an administrator to
					request the <code>ops.config.view</code> permission.
				</p>
			</div>
		)
	}

	return <ConfigView />
}
