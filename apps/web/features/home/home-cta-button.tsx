"use client"

import type { Route } from "next"
import Link from "next/link"
import { ArrowRight01FreeIcons, Logout01FreeIcons } from "@hugeicons/core-free-icons"
import { HugeiconsIcon } from "@hugeicons/react"

import { Button, buttonVariants } from "@/core/components/ui/button"
import { useSignOutMutation } from "@/features/auth/api/session.hooks"

interface HomeCtaButtonProps {
	href: string
	isLoggedIn: boolean
}

/**
 * Primary call-to-action button for the home page.
 *
 * Note: the server page (`app/(home)/page.tsx`) already knows whether the user
 * is logged in. We accept `isLoggedIn` as a prop to avoid client-side session
 * flicker on first paint.
 */
export function HomeCtaButton({ href, isLoggedIn }: HomeCtaButtonProps) {
	const signOutMutation = useSignOutMutation()

	if (isLoggedIn) {
		return (
			<Button
				size="lg"
				onClick={() => signOutMutation.mutate()}
				disabled={signOutMutation.isPending}
			>
				<HugeiconsIcon icon={Logout01FreeIcons} strokeWidth={2} className="size-4" />
				{signOutMutation.isPending ? "Logging out..." : "Logout"}
			</Button>
		)
	}

	return (
		<Link href={href as Route} className={buttonVariants({ size: "lg" })}>
			<HugeiconsIcon icon={ArrowRight01FreeIcons} strokeWidth={2} className="size-4" />
			Login
		</Link>
	)
}
