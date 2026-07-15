import { expect, request, test as setup } from "@playwright/test"

import { AUTH_FILE } from "./constants"

// Accounts are admin-provisioned (no public sign-up), so e2e authenticates as
// the seeded bootstrap admin from packages/db/src/seed.ts by default.
const TEST_EMAIL = process.env.E2E_TEST_EMAIL ?? "admin@turbo-template.local"
const TEST_PASSWORD = process.env.E2E_TEST_PASSWORD ?? "password123"

// Backend auth API — trailing slash is required for correct URL resolution with relative paths
const AUTH_API = process.env.E2E_AUTH_API_URL ?? "http://localhost:3000/api/v1/auth/"

// Web app origin — must be in BETTER_AUTH_TRUSTED_ORIGINS
const WEB_ORIGIN = process.env.BASE_URL ?? "http://localhost:3001"

setup("authenticate", async ({ page }) => {
	// ── Call the backend auth API directly — more reliable than form interaction ──
	const api = await request.newContext({
		baseURL: AUTH_API,
		extraHTTPHeaders: {
			"Content-Type": "application/json",
			// Better Auth checks Origin against trustedOrigins for CSRF protection
			"Origin": WEB_ORIGIN,
		},
	})

	// Sign in with provisioned credentials. Self-registration is disabled, so
	// there is no sign-up fallback: the account must already exist (run
	// `pnpm db:seed` for the bootstrap admin, or provision one and set
	// E2E_TEST_EMAIL / E2E_TEST_PASSWORD).
	const authRes = await api.post("sign-in/email", {
		data: { email: TEST_EMAIL, password: TEST_PASSWORD },
	})

	if (!authRes.ok()) {
		const body = await authRes.text()
		throw new Error(
			`[e2e setup] Failed to sign in as ${TEST_EMAIL}: ${authRes.status()} – ${body}\n` +
				"Accounts are admin-provisioned; seed the database (pnpm db:seed) or set " +
				"E2E_TEST_EMAIL / E2E_TEST_PASSWORD to an existing account."
		)
	}

	// ── Transfer auth cookies from API context → browser context ───────────────
	const { cookies } = await api.storageState()

	if (cookies.length === 0) {
		throw new Error("[e2e setup] Sign-in succeeded but no session cookies were returned")
	}

	const hasSessionCookie = cookies.some(c => c.name === "better-auth.session_token")
	expect(hasSessionCookie).toBe(true)

	await page.context().addCookies(cookies)

	// Navigate to the home page so Next.js validates the session server-side
	await page.goto("/")

	await expect(page.locator("p").filter({ hasText: /^Hello\s/ })).toBeVisible()

	await page.goto("/dashboard")
	await expect(page).toHaveURL(/\/dashboard/)

	// Persist the full browser context (cookies + localStorage) for authenticated tests
	await page.context().storageState({ path: AUTH_FILE })
	await api.dispose()
})
