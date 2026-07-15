import { expect, request, test } from "@playwright/test"

// All tests in this file run with stored auth session (chromium-authenticated project)

test.describe("authenticated home page", () => {
	test.beforeEach(async ({ page }) => {
		await page.goto("/")
	})

	test("shows the logged-in user's name", async ({ page }) => {
		// Authenticated home page greets with "Hello [name]" instead of "Welcome Everyone!"
		await expect(page.locator("p").filter({ hasText: /^Hello\s/ })).toBeVisible()
	})

	test("shows a Logout button instead of a Login link", async ({ page }) => {
		await expect(page.getByRole("button", { name: /logout/i })).toBeVisible()
		await expect(page.getByRole("link", { name: /^login$/i })).not.toBeVisible()
	})

	test("login page redirects to home when already authenticated", async ({ page }) => {
		await page.goto("/login")
		await expect(page).toHaveURL(/\/$/)
		await expect(page.locator("p").filter({ hasText: /^Hello\s/ })).toBeVisible()
	})

	test("logout returns to logged-out state", async ({ page }) => {
		// Important: this suite uses a shared `storageState` file. Clicking Logout invalidates the
		// session token server-side, which can make other authenticated tests flaky when run in parallel.
		// Refresh the session token for THIS test only before logging out.
		const authApi = process.env.E2E_AUTH_API_URL ?? "http://localhost:3000/api/v1/auth/"
		const webOrigin = process.env.BASE_URL ?? "http://localhost:3001"
		const testEmail = process.env.E2E_TEST_EMAIL ?? "admin@turbo-template.local"
		const testPassword = process.env.E2E_TEST_PASSWORD ?? "password123"

		const api = await request.newContext({
			baseURL: authApi,
			extraHTTPHeaders: {
				"Content-Type": "application/json",
				Origin: webOrigin,
			},
		})

		await api.post("sign-in/email", { data: { email: testEmail, password: testPassword } })
		const { cookies } = await api.storageState()
		await page.context().addCookies(cookies)
		await api.dispose()

		await page.goto("/")
		await page.getByRole("button", { name: /logout/i }).click()
		await expect(page.getByRole("link", { name: /^login$/i })).toBeVisible()
		await expect(page.locator("p").filter({ hasText: /welcome\s+everyone!/i })).toBeVisible()

		await page.goto("/dashboard")
		await expect(page).toHaveURL(/\/login/)
	})
})

test.describe("authenticated dashboard", () => {
	test("is accessible without redirect", async ({ page }) => {
		await page.goto("/dashboard")
		await expect(page).toHaveURL(/\/dashboard/)
	})

	test("shows the Dashboard breadcrumb", async ({ page }) => {
		await page.goto("/dashboard")
		await expect(page.locator('[data-slot="breadcrumb-page"]', { hasText: /dashboard/i })).toBeVisible()
	})
})

test.describe("authenticated session page", () => {
	test.beforeEach(async ({ page }) => {
		await page.goto("/session")
	})

	test("loads without redirecting to login", async ({ page }) => {
		await expect(page).toHaveURL(/\/session/)
	})

	test("displays session information", async ({ page }) => {
		await expect(page.getByText("Server Session")).toBeVisible()
		await expect(page.getByText("Client Session")).toBeVisible()
	})

	test("server session is not null", async ({ page }) => {
		// Session JSON is rendered inside a <pre> in the ServerSession component.
		await expect(page.locator("pre").first()).not.toContainText("\"null\"")
	})

	test("server session includes user email", async ({ page }) => {
		const testEmail = process.env.E2E_TEST_EMAIL ?? "admin@turbo-template.local"
		await expect(page.locator("pre").first()).toContainText(testEmail)
	})
})

test.describe("authenticated todos", () => {
	/** Navigate to /todos and wait for the client-side todos query to settle so
	 * the page is hydrated before interacting (fill/click on a non-hydrated form
	 * is silently dropped by React). */
	async function gotoTodos(page: import("@playwright/test").Page) {
		const todosLoaded = page.waitForResponse(
			response =>
				response.url().includes("/example/todos") && response.request().method() === "GET"
		)
		await page.goto("/todos")
		await todosLoaded
	}

	test("can create, complete, and delete a todo", async ({ page }, testInfo) => {
		const title = `e2e todo ${testInfo.workerIndex} ${Date.now()}`

		await gotoTodos(page)
		await expect(page.getByRole("heading", { name: /todos \/ posts/i })).toBeVisible()

		await page.getByPlaceholder("New todo title").fill(title)
		await page.getByRole("button", { name: /^add$/i }).click()

		const checkbox = page.getByRole("checkbox", { name: `Toggle ${title}` })
		await expect(checkbox).toBeVisible()
		await expect(checkbox).not.toBeChecked()

		// Optimistic rows are disabled until the server assigns a real id.
		await expect(checkbox).toBeEnabled()
		await checkbox.click()
		await expect(checkbox).toBeChecked()

		await page.getByRole("button", { name: `Delete ${title}` }).click()
		await expect(checkbox).toHaveCount(0)
	})

	test("created todo persists after reload", async ({ page }, testInfo) => {
		const title = `e2e persist ${testInfo.workerIndex} ${Date.now()}`

		await gotoTodos(page)
		await page.getByPlaceholder("New todo title").fill(title)
		await page.getByRole("button", { name: /^add$/i }).click()

		const checkbox = page.getByRole("checkbox", { name: `Toggle ${title}` })
		await expect(checkbox).toBeVisible()
		// Wait for the optimistic row to be confirmed by the server before reloading.
		await expect(checkbox).toBeEnabled()

		// After reload the list may be served from the persisted query cache (no
		// network request), so just wait for the row to render post-hydration.
		await page.reload()
		await expect(checkbox).toBeVisible({ timeout: 15_000 })

		await page.getByRole("button", { name: `Delete ${title}` }).click()
		await expect(checkbox).toHaveCount(0)
	})
})

test.describe("corpus upload (IN-3 formats)", () => {
	test("file input advertises all four supported formats", async ({ page }) => {
		await page.goto("/corpus")
		const input = page.getByLabel("Upload document")
		await expect(input).toBeVisible()

		const accept = (await input.getAttribute("accept")) ?? ""
		expect(accept).toContain("application/pdf")
		expect(accept).toContain("text/html")
		expect(accept).toContain(
			"application/vnd.openxmlformats-officedocument.wordprocessingml.document"
		)
		expect(accept).toContain("text/csv")

		await expect(
			page.getByText(/Single PDF, HTML, DOCX, or CSV file, up to 25 MB/)
		).toBeVisible()
	})

	test("uploads a CSV through the UI and lists it as csv", async ({ page }, testInfo) => {
		await page.goto("/corpus")
		const input = page.getByLabel("Upload document")
		await expect(input).toBeVisible()
		// The documents table only renders client-side after the list query
		// resolves — its presence proves hydration (setInputFiles before
		// hydration is silently dropped in Next dev mode).
		await expect(page.getByRole("table")).toBeVisible({ timeout: 30_000 })

		// Unique content per run so the upsert path (not dedup no-op) is exercised.
		const csv = `name,note\ne2e ${testInfo.workerIndex},run ${Date.now()}\n`
		await input.setInputFiles({
			name: "e2e-upload.csv",
			mimeType: "text/csv",
			buffer: Buffer.from(csv, "utf-8"),
		})

		// Ingestion is synchronous (R1): success copy appears when indexing completes.
		await expect(
			page.getByText(/Uploaded — the document is processing\.|Already ingested/)
		).toBeVisible({ timeout: 60_000 })

		// The corpus list refreshes and shows the new csv document.
		await expect(
			page.getByRole("row").filter({ hasText: "e2e-upload.csv" }).first()
		).toBeVisible({ timeout: 15_000 })
	})

	test("client-side rejects unsupported file types with a clear message", async ({ page }) => {
		await page.goto("/corpus")
		const input = page.getByLabel("Upload document")
		await expect(input).toBeVisible()
		// Hydration gate — see the upload test above.
		await expect(page.getByRole("table")).toBeVisible({ timeout: 30_000 })

		await input.setInputFiles({
			name: "notes.txt",
			mimeType: "text/plain",
			buffer: Buffer.from("plain text", "utf-8"),
		})

		await expect(
			page.getByText("Only PDF, HTML, DOCX, and CSV files are accepted.")
		).toBeVisible()
	})
})
