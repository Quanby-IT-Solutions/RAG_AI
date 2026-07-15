import type { Metadata } from "next"

import { TodosView } from "@/features/todos/components/todos-view"

export const metadata: Metadata = {
	title: "Todos / Posts",
}

export default function TodosPage() {
	return <TodosView />
}
