"use client"

import * as React from "react"
import type { Route } from "next"
import Link from "next/link"
import { GitbookFreeIcons } from "@hugeicons/core-free-icons"
import { HugeiconsIcon } from "@hugeicons/react"

import { Button, buttonVariants, type ButtonVariants } from "@/core/components/ui/button"
import { cn } from "@/core/lib/utils"

export interface LogoIconProps extends Omit<
	React.ComponentPropsWithoutRef<typeof HugeiconsIcon>,
	"icon"
> {
	icon?: typeof GitbookFreeIcons
	strokeWidth?: number
	ref?: React.Ref<SVGSVGElement>
}

export const LogoIcon = React.memo(
	({ icon, strokeWidth = 2, className, ref, ...props }: LogoIconProps) => {
		const iconToUse = icon || GitbookFreeIcons
		return (
			<HugeiconsIcon
				ref={ref}
				icon={iconToUse}
				strokeWidth={strokeWidth}
				className={cn("text-primary", className)}
				{...props}
			/>
		)
	}
)
LogoIcon.displayName = "LogoIcon"

export interface LogoProps {
	text?: string
	href?: Route<string>
	showIcon?: boolean
	icon?: typeof GitbookFreeIcons
	strokeWidth?: number
	size?: ButtonVariants["size"]
	variant?: ButtonVariants["variant"]
	className?: string
}

export function Logo({
	text = "Turbo Template",
	href,
	size = "lg",
	variant = "link",
	showIcon = true,
	icon,
	strokeWidth,
	className,
}: LogoProps) {
	const content = (
		<>
			{showIcon && <LogoIcon icon={icon} strokeWidth={strokeWidth} data-icon="inline-start" />}
			{text}
		</>
	)

	if (href) {
		return (
			<Link
				className={cn(buttonVariants({ size, variant }), "text-foreground", className)}
				href={href}
			>
				{content}
			</Link>
		)
	}

	return (
		<Button size={size} variant={variant} className={cn("text-foreground", className)}>
			{content}
		</Button>
	)
}
