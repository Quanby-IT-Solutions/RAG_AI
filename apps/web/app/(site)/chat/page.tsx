import type { Metadata } from "next"

import { ChatView } from "@/features/chat/components/chat-view"
import { canAccess } from "@/features/dashboard/lib/access"
import { getAccess } from "@/features/dashboard/server/get-access"

export const metadata: Metadata = {
	title: "Chat",
}

export default async function ChatPage({
	searchParams,
}: Readonly<{ searchParams: Promise<{ c?: string }> }>) {
	const access = await getAccess()
	const grants = { roles: access.roles, permissions: access.permissions }
	const hasAccess = canAccess(grants, { requiredPermission: "chat.query.submit" })
	const hasHistoryAccess = canAccess(grants, { requiredPermission: "chat.history.read" })
	// CH-3: /chat?c=<id> continues an existing conversation from history.
	const { c } = await searchParams

	return (
		<ChatView
			hasAccess={hasAccess}
			hasHistoryAccess={hasHistoryAccess}
			initialConversationId={c ?? null}
		/>
	)
}
