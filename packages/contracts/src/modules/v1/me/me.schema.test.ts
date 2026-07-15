import { describe, expect, it } from "vitest"

import { MePermissionsSchema, type MePermissions } from "./me.schema"

describe("MePermissionsSchema", () => {
	it("parses current-user roles and permissions", () => {
		const result: MePermissions = MePermissionsSchema.parse({
			roles: ["Knowledge Manager", "End User"],
			permissions: ["corpus.documents.upload", "chat.query.submit"],
		})

		expect(result.roles).toEqual(["Knowledge Manager", "End User"])
		expect(result.permissions).toEqual(["corpus.documents.upload", "chat.query.submit"])
	})

	it("allows an admin role without expanded permissions", () => {
		const result = MePermissionsSchema.parse({
			roles: ["System Administrator"],
			permissions: [],
		})

		expect(result).toEqual({ roles: ["System Administrator"], permissions: [] })
	})

	it("allows custom non-catalog role names from the database", () => {
		const result = MePermissionsSchema.parse({
			roles: ["Owner"],
			permissions: [],
		})

		expect(result.roles).toEqual(["Owner"])
	})

	it("rejects non-catalog permission names", () => {
		const result = MePermissionsSchema.safeParse({
			roles: ["Owner"],
			permissions: ["billing.invoices.read"],
		})

		expect(result.success).toBe(false)
	})
})
