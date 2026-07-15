import { afterEach, describe, expect, it, vi } from "vitest"

const BASE_ENV: Record<string, string> = {
	BETTER_AUTH_SECRET: "test-secret",
	BETTER_AUTH_TRUSTED_ORIGINS: "http://localhost:3001",
	DATABASE_URL: "postgres://postgres:password@localhost:5432/config-spec-test",
}

/**
 * config.ts snapshots env vars at import time (createEnv), so each scenario
 * stubs the environment and re-imports the module with a fresh registry.
 */
async function loadConfig(env: Record<string, string> = {}) {
	vi.resetModules()
	for (const [key, value] of Object.entries({ ...BASE_ENV, ...env })) {
		vi.stubEnv(key, value)
	}
	return import("./config.js")
}

afterEach(() => {
	vi.unstubAllEnvs()
})

describe("admin-provisioned sign-up policy", () => {
	it("disables email/password sign-up by default", async () => {
		const { createAuth } = await loadConfig()
		const auth = createAuth()

		expect(auth.options.emailAndPassword?.disableSignUp).toBe(true)
	})

	it("rejects the public email sign-up endpoint", async () => {
		const { createAuth } = await loadConfig()
		const auth = createAuth()

		await expect(
			auth.api.signUpEmail({
				body: {
					email: "blocked@example.com",
					password: "Password123",
					name: "Blocked Signup",
				},
			})
		).rejects.toThrowError(/sign up is not enabled/i)
	})

	it("disables social sign-up (implicit and explicit) by default", async () => {
		const { createAuth } = await loadConfig()
		const auth = createAuth()

		expect(auth.options.socialProviders?.google?.disableSignUp).toBe(true)
		expect(auth.options.socialProviders?.google?.disableImplicitSignUp).toBe(true)
	})

	it("re-enables sign-up when AUTH_SIGN_UP_ENABLED=true", async () => {
		const { createAuth } = await loadConfig({ AUTH_SIGN_UP_ENABLED: "true" })
		const auth = createAuth()

		expect(auth.options.emailAndPassword?.disableSignUp).toBe(false)
		expect(auth.options.socialProviders?.google?.disableSignUp).toBe(false)
	})

	it("keeps email/password sign-in enabled", async () => {
		const { createAuth } = await loadConfig()
		const auth = createAuth()

		expect(auth.options.emailAndPassword?.enabled).toBe(true)
	})
})
