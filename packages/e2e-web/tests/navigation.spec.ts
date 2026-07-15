import { expect, test } from "@playwright/test"

test.describe("protected route guards", () => {
	test("redirects /dashboard to /login when unauthenticated", async ({ page }) => {
		await page.goto("/dashboard")
		await expect(page).toHaveURL(/\/login/)
	})

	test("redirects /examples/todos to /login when unauthenticated", async ({ page }) => {
		await page.goto("/examples/todos")
		await expect(page).toHaveURL(/\/login/)
	})
})

test.describe("auth page cross-links", () => {
	test("Login CTA on home navigates to /login", async ({ page }) => {
		await page.goto("/")
		await page.getByRole("link", { name: /login/i }).click()
		await expect(page).toHaveURL(/\/login/)
	})

	test("login page offers no register navigation", async ({ page }) => {
		await page.goto("/login")
		await expect(page.getByRole("link", { name: /sign up|register/i })).toHaveCount(0)
	})
})
