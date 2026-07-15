import type { Metadata } from "next"

import { ChatView } from "@/features/chat/components/chat-view"
import { canAccess } from "@/features/dashboard/lib/access"
import { getAccess } from "@/features/dashboard/server/get-access"

export const metadata: Metadata = {
	title: "Chat",
}

export default async function ChatPage() {
	const access = await getAccess()
	const hasAccess = canAccess(
		{ roles: access.roles, permissions: access.permissions },
		{ requiredPermission: "chat.query.submit" }
	)

	return <ChatView hasAccess={hasAccess} />
}
