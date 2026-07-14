import Link from "next/link"

import { buttonVariants } from "@/core/components/ui/button"
import { FieldDescription } from "@/core/components/ui/field"
import { cn } from "@/core/lib/utils"

export function TermsPrivacyNote() {
	return (
		<FieldDescription className="px-6 text-center">
			By clicking continue, you agree to our{" "}
			<Link
				className={cn(buttonVariants({ variant: "link" }), "text-muted-foreground h-auto px-0")}
				href="#"
			>
				Terms of Service
			</Link>{" "}
			and{" "}
			<Link
				className={cn(buttonVariants({ variant: "link" }), "text-muted-foreground h-auto px-0")}
				href="#"
			>
				Privacy Policy
			</Link>
			.
		</FieldDescription>
	)
}
