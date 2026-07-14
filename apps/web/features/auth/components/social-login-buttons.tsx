"use client"

import { Button } from "@/core/components/ui/button"
import { Field } from "@/core/components/ui/field"
import { authClient } from "@/services/better-auth/auth-client"

import { AppleIcon, GoogleIcon, MetaIcon } from "./social-icons"

interface SocialLoginButtonsProps {
	action?: "login" | "signup"
	onAppleClick?: () => void
	onGoogleClick?: () => void
	onMetaClick?: () => void
}

export function SocialLoginButtons({
	action = "login",
	onAppleClick,
	onGoogleClick,
	onMetaClick,
}: SocialLoginButtonsProps) {
	const actionText = action === "login" ? "Login" : "Sign up"

	const handleGoogleClick = async () => {
		await authClient.signIn.social({
			provider: "google",
		})
	}

	return (
		<Field className="grid grid-cols-3 gap-4">
			<Button
				variant="outline"
				type="button"
				onClick={onAppleClick}
				className="hover:cursor-pointer"
			>
				<AppleIcon />
				<span className="sr-only">{actionText} with Apple</span>
			</Button>
			<Button
				variant="outline"
				type="button"
				onClick={onGoogleClick ?? handleGoogleClick}
				className="hover:cursor-pointer"
			>
				<GoogleIcon />
				<span className="sr-only">{actionText} with Google</span>
			</Button>
			<Button
				variant="outline"
				type="button"
				onClick={onMetaClick}
				className="hover:cursor-pointer"
			>
				<MetaIcon />
				<span className="sr-only">{actionText} with Meta</span>
			</Button>
		</Field>
	)
}
