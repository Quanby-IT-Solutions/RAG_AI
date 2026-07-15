import type { Metadata } from "next"

import { GoldenView } from "@/features/eval/components/golden-view"
import { canAccess } from "@/features/dashboard/lib/access"
import { getAccess } from "@/features/dashboard/server/get-access"

export const metadata: Metadata = {
	title: "Evaluation",
}

export default async function EvalPage() {
	const access = await getAccess()
	const grants = { roles: access.roles, permissions: access.permissions }
	const hasAccess = canAccess(grants, { requiredPermission: "eval.golden.manage" })

	if (!hasAccess) {
		return (
			<div className="p-6">
				<h1 className="text-lg font-semibold">Evaluation</h1>
				<p className="text-muted-foreground mt-2 text-sm" role="alert">
					You do not have permission to manage the golden evaluation set. Contact an
					administrator to request the <code>eval.golden.manage</code> permission.
				</p>
			</div>
		)
	}

	return <GoldenView />
}
