// CE-8 browser validation: ask about the chart → image citation chip renders
// with the page-image link → console clean.
import { chromium, request as pwRequest } from "@playwright/test"

const BASE = process.env.BASE_URL ?? "http://localhost:3002"

const api = await pwRequest.newContext({
	baseURL: "http://localhost:3005/api/v1/auth/",
	extraHTTPHeaders: { "Content-Type": "application/json", Origin: BASE },
})
const auth = await api.post("sign-in/email", {
	data: { email: "admin@turbo-template.local", password: "password123" },
})
if (!auth.ok()) {
	console.error("sign-in failed", auth.status())
	process.exit(1)
}
const storageState = await api.storageState()
await api.dispose()

const consoleIssues = []
const browser = await chromium.launch()
const context = await browser.newContext({ storageState })
const page = await context.newPage()
page.on("console", m => {
	if (m.type() === "error" || m.type() === "warning") consoleIssues.push(`${m.type()}: ${m.text()}`)
})

try {
	await page.goto(`${BASE}/chat`)
	await page
		.getByRole("textbox", { name: /question/i })
		.fill("In the quarterly revenue bar chart, which bar is the tallest and what does the page label say?")
	await page.getByRole("button", { name: /send/i }).click()

	const chip = page.getByTitle(/open page image/)
	await chip.waitFor({ timeout: 120_000 })
	const href = await chip.getAttribute("href")
	await page.screenshot({ path: process.env.SHOT ?? "ce8-chat.png", fullPage: true })
	console.log(
		JSON.stringify(
			{ imageCitationHref: href, consoleIssues: consoleIssues.filter(e => !e.includes("gstatic")) },
			null,
			2
		)
	)
} finally {
	await browser.close()
}
