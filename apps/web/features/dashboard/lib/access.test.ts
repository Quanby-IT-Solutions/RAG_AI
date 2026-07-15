import { describe, expect, it } from "vitest"

import { canAccess, type AccessProfile } from "./access"
import { getAccessibleNavItems, getDashboardRouteTitle, navItems } from "./nav-items"

const noAccess: AccessProfile = {
	roles: [],
	permissions: [],
}

describe("canAccess", () => {
	it("allows base items without requirements", () => {
		expect(canAccess(noAccess, {})).toBe(true)
	})

	it("allows the System Administrator role to satisfy permission requirements", () => {
		expect(
			canAccess(
				{ roles: ["System Administrator"], permissions: [] },
				{ requiredPermission: "users.roles.manage" }
			)
		).toBe(true)
	})

	it("allows exact permission matches", () => {
		expect(
			canAccess(
				{ roles: [], permissions: ["chat.query.submit"] },
				{ requiredPermission: "chat.query.submit" }
			)
		).toBe(true)
	})

	it("allows top-level dotted wildcard grants", () => {
		expect(
			canAccess(
				{ roles: [], permissions: ["corpus.*"] },
				{ requiredPermission: "corpus.documents.upload" }
			)
		).toBe(true)
	})

	it("allows mid-level dotted wildcard grants", () => {
		expect(
			canAccess(
				{ roles: [], permissions: ["corpus.documents.*"] },
				{ requiredPermission: "corpus.documents.upload" }
			)
		).toBe(true)
	})

	it("denies wildcard grants outside the required prefix", () => {
		expect(
			canAccess({ roles: [], permissions: ["corpus.*"] }, { requiredPermission: "chat.query.submit" })
		).toBe(false)
		expect(
			canAccess(
				{ roles: [], permissions: ["corpus.documents.*"] },
				{ requiredPermission: "corpus.ingest.run" }
			)
		).toBe(false)
	})

	it("allows any listed permission to match", () => {
		expect(
			canAccess(
				{ roles: [], permissions: ["users.roles.manage"] },
				{ requiredPermission: ["users.accounts.manage", "users.roles.manage"] }
			)
		).toBe(true)
	})

	it("allows role requirements to match", () => {
		expect(
			canAccess({ roles: ["Knowledge Manager"], permissions: [] }, { requiredRole: "Knowledge Manager" })
		).toBe(true)
	})

	it("denies restricted items when no requirement matches", () => {
		expect(canAccess(noAccess, { requiredPermission: "audit.log.view" })).toBe(false)
		expect(
			canAccess({ roles: ["End User"], permissions: [] }, { requiredRole: "Knowledge Manager" })
		).toBe(false)
	})
})

describe("navItems", () => {
	it("defines base and RBAC-gated dashboard destinations", () => {
		expect(navItems).toEqual(
			expect.arrayContaining([
				expect.objectContaining({ label: "Dashboard", href: "/dashboard" }),
				expect.objectContaining({ label: "Submit Ticket", href: "/submit-ticket" }),
				expect.objectContaining({ label: "Todos / Posts", href: "/todos" }),
				expect.objectContaining({
					label: "Corpus",
					href: "/corpus",
					requiredPermission: "corpus.documents.read",
				}),
				expect.objectContaining({
					label: "Chat",
					href: "/chat",
					requiredPermission: "chat.query.submit",
				}),
				expect.objectContaining({
					label: "User Management",
					href: "/user-management",
					section: "Administration",
					requiredPermission: ["users.accounts.manage", "users.roles.manage"],
				}),
			])
		)
	})

	it("filters visible items from the shared access helper", () => {
		expect(getAccessibleNavItems(noAccess).map(item => item.label)).toEqual([
			"Dashboard",
			"Submit Ticket",
			"Todos / Posts",
		])

		expect(
			getAccessibleNavItems({ roles: [], permissions: ["users.roles.manage"] }).map(
				item => item.label
			)
		).toContain("User Management")

		expect(
			getAccessibleNavItems({ roles: [], permissions: ["users.*"] }).map(item => item.label)
		).toContain("User Management")

		expect(
			getAccessibleNavItems({ roles: [], permissions: ["corpus.documents.read"] }).map(
				item => item.label
			)
		).toContain("Corpus")

		expect(
			getAccessibleNavItems({ roles: [], permissions: ["corpus.*"] }).map(item => item.label)
		).toContain("Corpus")

		expect(
			getAccessibleNavItems({ roles: [], permissions: ["chat.query.submit"] }).map(
				item => item.label
			)
		).toContain("Chat")

		expect(
			getAccessibleNavItems({ roles: [], permissions: ["chat.*"] }).map(item => item.label)
		).toContain("Chat")

		expect(getAccessibleNavItems(noAccess).map(item => item.label)).not.toContain("Chat")

		expect(
			getAccessibleNavItems({ roles: ["System Administrator"], permissions: [] }).map(
				item => item.label
			)
		).toEqual([
		"Dashboard",
		"Submit Ticket",
		"Todos / Posts",
		"Corpus",
		"Chat",
		"User Management",
		"API Keys",
	])
	})

	it("resolves mobile header titles from dashboard routes", () => {
		expect(getDashboardRouteTitle("/dashboard")).toBe("Dashboard")
		expect(getDashboardRouteTitle("/submit-ticket")).toBe("Submit Ticket")
		expect(getDashboardRouteTitle("/todos")).toBe("Todos / Posts")
		expect(getDashboardRouteTitle("/corpus")).toBe("Corpus")
		expect(getDashboardRouteTitle("/chat")).toBe("Chat")
		expect(getDashboardRouteTitle("/user-management")).toBe("User Management")
		expect(getDashboardRouteTitle("/account")).toBe("Account")
	})
})
