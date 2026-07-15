import type { Metadata } from "next"

import { ChunkInspectionView } from "@/features/corpus/components/chunk-inspection-view"
import { canAccess } from "@/features/dashboard/lib/access"
import { getAccess } from "@/features/dashboard/server/get-access"

export const metadata: Metadata = {
	title: "Chunk Inspection",
}

export default async function ChunkInspectionPage({
	params,
}: Readonly<{ params: Promise<{ documentId: string }> }>) {
	const access = await getAccess()
	const grants = { roles: access.roles, permissions: access.permissions }
	const hasAccess = canAccess(grants, { requiredPermission: "corpus.chunks.inspect" })
	const { documentId } = await params

	if (!hasAccess) {
		return (
			<div className="p-6">
				<h1 className="text-lg font-semibold">Chunk Inspection</h1>
				<p className="text-muted-foreground mt-2 text-sm" role="alert">
					You do not have permission to inspect chunk boundaries. Contact an administrator to
					request the <code>corpus.chunks.inspect</code> permission.
				</p>
			</div>
		)
	}

	return <ChunkInspectionView documentId={documentId} />
}
