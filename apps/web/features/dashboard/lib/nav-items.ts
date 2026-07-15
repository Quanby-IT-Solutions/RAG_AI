import {
	BubbleChatIcon,
	CheckmarkBadge01Icon,
	DashboardSquare01Icon,
	FolderLibraryIcon,
	Key01Icon,
	Settings02Icon,
	Task01Icon,
	Ticket01Icon,
	UserMultipleIcon,
} from "@hugeicons/core-free-icons"
import { type IconSvgElement } from "@hugeicons/react"

import { type PermissionName } from "@repo/contracts"

import { canAccess, type AccessProfile, type RoleRequirement } from "./access"

type DashboardHref = `/${string}`

export interface DashboardNavItem {
	label: string
	href: DashboardHref
	icon: IconSvgElement
	section?: string
	requiredPermission?: PermissionName | readonly PermissionName[]
	requiredRole?: RoleRequirement
}

export const navItems: readonly DashboardNavItem[] = [
	{
		label: "Dashboard",
		href: "/dashboard",
		icon: DashboardSquare01Icon,
	},
	{
		label: "Submit Ticket",
		href: "/submit-ticket",
		icon: Ticket01Icon,
	},
	{
		// Example module: ungated since F3 removed the legacy `posts:*` keys.
		label: "Todos / Posts",
		href: "/todos",
		icon: Task01Icon,
	},
	{
		// Corpus list + PDF upload (IN-1). Visible with read access; the upload
		// action itself needs corpus.documents.upload + corpus.ingest.run
		// (enforced server-side by the RbacGuard).
		label: "Corpus",
		href: "/corpus",
		icon: FolderLibraryIcon,
		requiredPermission: "corpus.documents.read",
	},
	{
		// Grounded Q&A over the corpus (CH-1). Only users who can ask see it;
		// the chat route itself is guarded server-side by chat.query.submit.
		label: "Chat",
		href: "/chat",
		icon: BubbleChatIcon,
		requiredPermission: "chat.query.submit",
	},
	{
		label: "User Management",
		href: "/user-management",
		icon: UserMultipleIcon,
		section: "Administration",
		requiredPermission: ["users.accounts.manage", "users.roles.manage"],
	},
	{
		// Golden evaluation set console (EV-1). Quality Analysts curate the
		// questions/chunks/answers everything downstream (EV-2..4) reads.
		label: "Evaluation",
		href: "/eval",
		icon: CheckmarkBadge01Icon,
		section: "Administration",
		requiredPermission: ["eval.golden.manage", "eval.runs.execute"],
	},
	{
		// OB-5: read-only effective-config review (secrets masked server-side).
		label: "Configuration",
		href: "/ops-config",
		icon: Settings02Icon,
		section: "Administration",
		requiredPermission: "ops.config.view",
	},
	{
		// Integrator API keys console (SC-3). Keys act with the owner's
		// delegated scopes; every keyed request is audited server-side.
		label: "API Keys",
		href: "/api-keys",
		icon: Key01Icon,
		section: "Administration",
		requiredPermission: "apikeys.keys.manage",
	},
]

export const dashboardRouteTitles: readonly Pick<DashboardNavItem, "href" | "label">[] = [
	...navItems.map(({ href, label }) => ({ href, label })),
	{
		label: "Account",
		href: "/account",
	},
]

export function getDashboardRouteTitle(pathname: string): string {
	const currentRoute = dashboardRouteTitles.find(
		route => pathname === route.href || pathname.startsWith(`${route.href}/`)
	)

	return currentRoute?.label ?? "Dashboard"
}

export function getAccessibleNavItems(access: AccessProfile): DashboardNavItem[] {
	return navItems.filter(item =>
		canAccess(access, {
			requiredPermission: item.requiredPermission,
			requiredRole: item.requiredRole,
		})
	)
}
