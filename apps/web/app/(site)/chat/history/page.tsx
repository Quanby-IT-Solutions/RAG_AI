import type { Metadata } from "next"

import { HistoryView } from "@/features/chat/components/history-view"
import { canAccess } from "@/features/dashboard/lib/access"
import { getAccess } from "@/features/dashboard/server/get-access"

export const metadata: Metadata = {
	title: "Conversation history",
}

export default async function ChatHistoryPage() {
	const access = await getAccess()
	const hasAccess = canAccess(
		{ roles: access.roles, permissions: access.permissions },
		{ requiredPermission: "chat.history.read" }
	)

	return <HistoryView hasAccess={hasAccess} />
}
