import { expect, test } from "@playwright/test"

/**
 * CH-4: PWA packaging. Runs against the production webServer (port 3001) —
 * the dev server disables the service worker by design.
 */
test.describe("PWA packaging (CH-4)", () => {
	// The dev server (BASE_URL override) disables the service worker by
	// design — these checks only mean anything on the production build.
	test.skip(
		Boolean(process.env.BASE_URL) && process.env.BASE_URL !== "http://localhost:3001",
		"PWA installability checks require the production webServer"
	)

	test("web manifest is served with installability fields", async ({ request, baseURL }) => {
		const response = await request.get(`${baseURL}/manifest.webmanifest`)
		expect(response.status()).toBe(200)
		const manifest = await response.json()
		expect(manifest.name).toBeTruthy()
		expect(manifest.display).toBe("standalone")
		expect(manifest.start_url).toBeTruthy()
		const sizes = manifest.icons.map((icon: { sizes: string }) => icon.sizes)
		expect(sizes).toContain("192x192")
		expect(sizes).toContain("512x512")
		expect(
			manifest.icons.some((icon: { purpose?: string }) => icon.purpose === "maskable")
		).toBe(true)
	})

	test("service worker script is served and never caches /api", async ({ request, baseURL }) => {
		const response = await request.get(`${baseURL}/serwist/sw.js`)
		expect(response.status()).toBe(200)
		const body = await response.text()
		// The compiled worker keeps the NetworkOnly /api rule (privacy rule:
		// chat answers are never cached).
		expect(body).toContain("/api")
	})

	test("offline page renders an honest offline state", async ({ page }) => {
		await page.goto("/~offline")
		await expect(page.getByText(/offline/i).first()).toBeVisible()
	})
})
