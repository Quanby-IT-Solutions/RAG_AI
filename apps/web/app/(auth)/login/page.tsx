import { redirect } from "next/navigation"

import { getSession } from "@/services/better-auth/auth-server"
import { LoginForm } from "@/features/auth/login/components/login-form"

export default async function LoginPage() {
	// Already-authenticated users go straight to the shell.
	const session = await getSession()
	if (session) {
		redirect("/")
	}

	return (
		<section className="flex flex-1 flex-col items-center justify-center">
			<LoginForm className="w-full" />
		</section>
	)
}
