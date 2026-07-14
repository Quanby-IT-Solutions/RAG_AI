"use client"

import { useQuery } from "@tanstack/react-query"

import { Skeleton } from "@/core/components/ui/skeleton"
import { sessionOptions } from "@/features/auth/api/session.hooks"

export function ClientSession() {
	const { data: session, isPending, error } = useQuery(sessionOptions)

	if (isPending) {
		return (
			<div className="space-y-2">
				<p className="text-sm font-medium">Client Session</p>
				<Skeleton className="h-20 w-full" />
			</div>
		)
	}

	return (
		<div className="overflow-x-auto">
			<p className="text-sm font-medium">Client Session</p>
			<pre className="text-xs">
				{error && <p className="text-destructive text-xs">{error.message}</p>}
				{!error && JSON.stringify(session, null, 2)}
			</pre>
		</div>
	)
}
