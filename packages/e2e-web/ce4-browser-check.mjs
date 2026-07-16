// CE-4 browser validation (standalone, not a spec): login → /chat → ask →
// assert agentic status label renders → answer renders → console clean.
import { chromium, request as pwRequest } from "@playwright/test"

const BASE = process.env.BASE_URL ?? "http://localhost:3001"
const EMAIL = "admin@turbo-template.local"
const PASSWORD = "password123"

const consoleIssues = []
const failedRequests = []

// API sign-in (global.setup.ts pattern) — sturdier than driving the form.
const api = await pwRequest.newContext({
	baseURL: "http://localhost:3005/api/v1/auth/",
	// Better Auth checks Origin against trustedOrigins for CSRF protection.
	extraHTTPHeaders: { "Content-Type": "application/json", Origin: BASE },
})
const authRes = await api.post("sign-in/email", { data: { email: EMAIL, password: PASSWORD } })
if (!authRes.ok()) {
	console.error("sign-in failed", authRes.status(), await authRes.text())
	process.exit(1)
}
const storageState = await api.storageState()
await api.dispose()

const browser = await chromium.launch()
const context = await browser.newContext({ storageState })
const page = await context.newPage()
page.on("console", message => {
	if (message.type() === "error" || message.type() === "warning") {
		consoleIssues.push(`${message.type()}: ${message.text()}`)
	}
})
page.on("requestfailed", request => {
	failedRequests.push(`${request.method()} ${request.url()} :: ${request.failure()?.errorText}`)
})
page.on("response", response => {
	if (response.status() >= 400) {
		failedRequests.push(`${response.status()} ${response.url()}`)
	}
})

try {
	await page.goto(`${BASE}/chat`)
	const question = `Who verifies medication dosing against patient weight? (ce4 ${Date.now()})`
	await page.getByRole("textbox", { name: /question/i }).fill(question)

	// Watch for the CE-4 status label the instant the send fires.
	const statusSeen = page
		.waitForSelector("text=/Searching the corpus|Checking relevance|Refining the search/", {
			timeout: 60_000,
		})
		.then(() => true)
		.catch(() => false)
	await page.getByRole("button", { name: /send/i }).click()
	const sawStatus = await statusSeen

	// Wait for a cited assistant answer (or IDK) to land.
	await page.waitForSelector("text=/\\[?1\\]?|I don't know/", { timeout: 120_000 })
	await page.waitForTimeout(1500)
	await page.screenshot({ path: process.env.SHOT ?? "ce4-chat.png", fullPage: true })

	const report = {
		sawStatusLabel: sawStatus,
		consoleIssues,
		failedRequests: failedRequests.filter(
			entry => !entry.includes("favicon") && !entry.includes("_next/static")
		),
	}
	console.log(JSON.stringify(report, null, 2))
} finally {
	await browser.close()
}
