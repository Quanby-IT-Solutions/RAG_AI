import type { Metadata } from "next"

import { canAccess } from "@/features/dashboard/lib/access"
import { getAccess } from "@/features/dashboard/server/get-access"
import { ReportsView } from "@/features/reports/components/reports-view"

export const metadata: Metadata = {
	title: "Reports",
}

export default async function ReportsPage() {
	const access = await getAccess()
	const grants = { roles: access.roles, permissions: access.permissions }
	const hasAccess = canAccess(grants, { requiredPermission: "reports.reports.view" })

	if (!hasAccess) {
		return (
			<div className="p-6">
				<h1 className="text-lg font-semibold">Reports</h1>
				<p className="text-muted-foreground mt-2 text-sm" role="alert">
					You do not have permission to view reports. Contact an administrator to request
					the <code>reports.reports.view</code> permission.
				</p>
			</div>
		)
	}

	return <ReportsView />
}
