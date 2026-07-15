import { expect, test } from "@playwright/test"

test.describe("login page", () => {
	test.beforeEach(async ({ page }) => {
		await page.goto("/login")
	})

	test("displays the login heading", async ({ page }) => {
		await expect(page.getByRole("heading", { name: /welcome back/i })).toBeVisible()
	})

	test("has an email input", async ({ page }) => {
		await expect(page.getByLabel(/email/i)).toBeVisible()
	})

	test("has a password input", async ({ page }) => {
		await expect(page.getByLabel("Password", { exact: true })).toBeVisible()
	})

	test("has a login submit button", async ({ page }) => {
		await expect(page.getByRole("button", { name: /^login$/i })).toBeVisible()
	})

	test("has no sign-up / register link (accounts are admin-provisioned)", async ({ page }) => {
		await expect(page.getByRole("link", { name: /sign up/i })).toHaveCount(0)
		await expect(page.getByRole("link", { name: /register/i })).toHaveCount(0)
	})

	test("shows an error and stays on /login with invalid credentials", async ({ page }) => {
		test.skip(!process.env.E2E_AUTH_API_URL, "Requires backend")

		await page.getByLabel(/email/i).fill("test@gmail.com")
		await page.getByLabel("Password", { exact: true }).fill("WrongPassword123")
		await page.getByRole("button", { name: /^login$/i }).click()

		await expect(page).toHaveURL(/\/login/)
		// Error banner is a red/destructive block; we assert by its message rather than styling.
		await expect(page.getByText(/failed to sign in|invalid|incorrect|unauthorized/i)).toBeVisible()
	})

	test("blocks submit with invalid email / missing password (native validation)", async ({ page }) => {
		const email = page.getByLabel(/email/i)
		const password = page.getByLabel("Password", { exact: true })

		await email.fill("not-an-email")
		await password.fill("")

		await page.getByRole("button", { name: /^login$/i }).click()
		await expect(page).toHaveURL(/\/login/)

		// Login form uses native constraints (type=email + required). Assert validity states.
		await expect(email.evaluate(el => el.validity.typeMismatch)).resolves.toBe(true)
		await expect(password.evaluate(el => el.validity.valueMissing)).resolves.toBe(true)
	})
})

test.describe("no self-registration", () => {
	test("register page does not exist (404)", async ({ page }) => {
		const response = await page.goto("/register")
		expect(response?.status()).toBe(404)
	})

	test("sign-up API endpoint is disabled", async ({ request }) => {
		test.skip(!process.env.E2E_AUTH_API_URL, "Requires backend")

		const authApi = process.env.E2E_AUTH_API_URL ?? "http://localhost:3000/api/v1/auth/"
		const webOrigin = process.env.BASE_URL ?? "http://localhost:3001"

		const res = await request.post(`${authApi}sign-up/email`, {
			headers: { "Content-Type": "application/json", Origin: webOrigin },
			data: {
				email: `e2e-blocked-${Date.now()}@example.com`,
				password: "SomePassword123",
				name: "Blocked Signup",
			},
		})

		expect(res.ok()).toBe(false)
		expect(res.status()).toBe(400)
	})
})
