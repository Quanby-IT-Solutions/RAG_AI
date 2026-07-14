"use client"

import type { Route } from "next"
import Link from "next/link"

import { buttonVariants } from "@/core/components/ui/button"
import { Separator } from "@/core/components/ui/separator"
import { getApiUrl } from "@/core/lib/utils"

export function NavigationLinks() {
	return (
		<div className="flex flex-col gap-3">
			<div className="flex flex-wrap justify-center gap-x-6 gap-y-1 text-sm">
				<Link
					href={`${getApiUrl()}/docs` as Route}
					className={buttonVariants({ variant: "ghost", size: "lg" })}
					target="_blank"
				>
					API Docs
				</Link>
				<Separator orientation="vertical" />
				<Link href="/dashboard" className={buttonVariants({ variant: "ghost", size: "lg" })}>
					Dashboard
				</Link>
				<Separator orientation="vertical" />
				<Link href="/examples/todos" className={buttonVariants({ variant: "ghost", size: "lg" })}>
					Todos
				</Link>
				<Separator orientation="vertical" />
				<Link href="/session" className={buttonVariants({ variant: "ghost", size: "lg" })}>
					Session
				</Link>
			</div>
		</div>
	)
}
