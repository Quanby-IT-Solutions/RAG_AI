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
		// network request: staleTime is 30s, so the restored cache is considered
		// fresh). The IndexedDB persister throttles writes (~1s), so give it a
		// beat to flush the confirmed row before reloading — otherwise the
		// restored cache predates the create and the row never renders.
		await page.waitForTimeout(1_500)
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
		// hydration is silently dropped in Next dev mode). first(): the jobs
		// console (IN-7) adds a second table to the page.
		await expect(page.getByRole("table").first()).toBeVisible({ timeout: 30_000 })

		// Unique content per run so the upsert path (not dedup no-op) is exercised.
		const csv = `name,note\ne2e ${testInfo.workerIndex},run ${Date.now()}\n`
		await input.setInputFiles({
			name: "e2e-upload.csv",
			mimeType: "text/csv",
			buffer: Buffer.from(csv, "utf-8"),
		})

		// Ingestion is synchronous (R1): success copy appears when indexing completes.
		await expect(
			page.getByText(/Uploaded — the document is processing in a background job\.|Already ingested/)
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
		await expect(page.getByRole("table").first()).toBeVisible({ timeout: 30_000 })

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

test.describe("ingestion jobs console (IN-7)", () => {
	test("lists background jobs with status and supports detail expansion", async ({ page }) => {
		await page.goto("/corpus")
		const panel = page.getByText("Ingestion jobs", { exact: true })
		await expect(panel).toBeVisible()

		// At least one job row exists from prior uploads (live stack).
		const jobsTable = page.getByRole("table", { name: "Ingestion jobs" })
		await expect(jobsTable).toBeVisible({ timeout: 30_000 })
		const detailsButton = jobsTable.getByRole("button", { name: "Details" }).first()
		await expect(detailsButton).toBeVisible()

		await detailsButton.click()
		// The expanded row's toggle relabels to "Hide" with aria-expanded=true.
		const hideButton = jobsTable.getByRole("button", { name: "Hide" })
		await expect(hideButton).toHaveAttribute("aria-expanded", "true")
		// Per-document outcomes render inside the expanded row.
		await expect(page.getByRole("table", { name: "Documents in this job" })).toBeVisible({
			timeout: 15_000,
		})
	})

	test("upload flows through the async job to indexed in the documents list", async ({
		page,
	}, testInfo) => {
		// Semantic chunking + serial worker: a job can exceed the default 30s
		// test timeout on a loaded machine.
		testInfo.setTimeout(180_000)
		await page.goto("/corpus")
		const input = page.getByLabel("Upload document")
		await expect(input).toBeVisible()
		await expect(page.getByRole("table").first()).toBeVisible({ timeout: 30_000 })

		const marker = `e2e-job ${testInfo.workerIndex} ${Date.now()}`
		const csv = `name,note\n${marker},async ingestion validation\n`
		await input.setInputFiles({
			name: "e2e-job.csv",
			mimeType: "text/csv",
			buffer: Buffer.from(csv, "utf-8"),
		})

		await expect(
			page.getByText("Uploaded — the document is processing in a background job.")
		).toBeVisible({ timeout: 30_000 })

		// The documents list polls while processing and flips to indexed once
		// the worker finishes — no reload needed.
		const row = page.getByRole("row").filter({ hasText: "e2e-job.csv" }).first()
		await expect(row).toBeVisible({ timeout: 15_000 })
		// Semantic chunking embeds sentences, so a job takes ~20s and the
		// single worker processes the suite's uploads serially.
		await expect(row.getByText("indexed")).toBeVisible({ timeout: 120_000 })
	})
})

test.describe("chat streaming (GN-3/CH-2)", () => {
	test("streams a cited answer progressively in the browser", async ({ page }) => {
		await page.goto("/chat")
		const input = page.getByRole("textbox", { name: "Question" })
		await expect(input).toBeVisible()

		await input.fill("How many vacation days do employees receive per year?")
		await page.getByRole("button", { name: /send/i }).click()

		// The streamed answer lands in an assistant bubble with a citation
		// chip attached on the final frame.
		const answer = page.getByText(/15 vacation days/i).first()
		await expect(answer).toBeVisible({ timeout: 60_000 })
		await expect(page.getByRole("link", { name: /sample\.html/i }).first()).toBeVisible({
			timeout: 15_000,
		})
	})
})

test.describe("conversation history (CH-3)", () => {
	test("lists past conversations and opens one with its thread intact", async ({ page }) => {
		await page.goto("/chat/history")
		const table = page.getByRole("table", { name: "Your conversations" })
		await expect(table).toBeVisible({ timeout: 30_000 })

		const firstOpen = table.getByRole("link", { name: "Open" }).first()
		await firstOpen.click()

		// The chat page loads the saved thread (user + assistant bubbles).
		await expect(page).toHaveURL(/\/chat\?c=/)
		await expect(page.getByLabel("Conversation")).toBeVisible()
		await expect(page.locator("[aria-label='Conversation'] .whitespace-pre-wrap").first()).toBeVisible({
			timeout: 30_000,
		})
	})
})

test.describe("user management console (F4)", () => {
	test("creates a user and toggles disable/enable through the UI", async ({ page }, testInfo) => {
		await page.goto("/user-management")
		// Users live in the second tab (default is Roles). A click can land
		// before the tabs hydrate and silently no-op, so retry until the
		// Users panel actually opens.
		const usersTab = page.getByRole("tab", { name: "Users" })
		await expect(usersTab).toBeVisible({ timeout: 30_000 })
		await expect(async () => {
			await usersTab.click()
			await expect(page.getByRole("form", { name: "Create user" })).toBeVisible({
				timeout: 2_000,
			})
		}).toPass({ timeout: 30_000 })
		const form = page.getByRole("form", { name: "Create user" })

		const email = `e2e-f4-${testInfo.workerIndex}-${Date.now()}@example.com`
		await form.getByLabel("Name").fill("E2E Provisioned")
		await form.getByLabel("Email").fill(email)
		await form.getByLabel("Temporary password").fill("TempPass123!")
		await form.getByRole("button", { name: /create user/i }).click()

		await expect(page.getByText(`${email} created`, { exact: false })).toBeVisible({
			timeout: 30_000,
		})

		// The new user appears in the list as active with a Disable action.
		const row = page.getByRole("row").filter({ hasText: email })
		await expect(row).toBeVisible({ timeout: 15_000 })
		await expect(row.getByText("active")).toBeVisible()

		await row.getByRole("button", { name: `Disable ${email}` }).click()
		await expect(row.getByText("disabled")).toBeVisible({ timeout: 15_000 })

		await row.getByRole("button", { name: `Enable ${email}` }).click()
		await expect(row.getByText("active")).toBeVisible({ timeout: 15_000 })
	})
})

test.describe("API keys console (SC-3)", () => {
	test("creates a scoped key, shows the secret once, and revokes it", async ({
		page,
	}, testInfo) => {
		const keyName = `e2e key ${testInfo.project.name}-${Date.now()}`

		await page.goto("/api-keys")
		const form = page.getByRole("form", { name: "Create API key" })
		await expect(form).toBeVisible({ timeout: 15_000 })

		await form.getByLabel("Name").fill(keyName)
		// Radix renders a hidden native input alongside the checkbox control;
		// click the visible label text, which toggles the associated checkbox.
		await form.getByText("corpus.documents.read", { exact: true }).click()
		await form.getByRole("button", { name: /create key/i }).click()

		// The secret is shown exactly once, with the ragp_ prefix.
		const notice = page.getByTestId("api-key-secret-notice")
		await expect(notice).toBeVisible({ timeout: 30_000 })
		await expect(notice.getByText(/^ragp_/)).toBeVisible()

		// The key lists as active with its scope badge, then revokes in place.
		const row = page.getByRole("row").filter({ hasText: keyName })
		await expect(row).toBeVisible({ timeout: 15_000 })
		await expect(row.getByText("Active")).toBeVisible()
		await expect(row.getByText("corpus.documents.read")).toBeVisible()

		await row.getByRole("button", { name: `Revoke ${keyName}` }).click()
		await expect(row.getByText("Revoked")).toBeVisible({ timeout: 15_000 })
	})
})

test.describe("golden evaluation set console (EV-1)", () => {
	test("picks chunks via live retrieval, adds an item, and retires it", async ({
		page,
	}, testInfo) => {
		const question = `e2e golden ${testInfo.project.name}-${Date.now()}?`

		await page.goto("/eval")
		const form = page.getByRole("form", { name: "Add golden item" })
		await expect(form).toBeVisible({ timeout: 15_000 })

		await form.getByLabel("Question").fill(question)
		await form.getByLabel("Ideal answer").fill("Meals are reimbursed up to forty dollars.")

		// The chunk picker runs the REAL retrieval pipeline and returns
		// hash-bound candidates.
		await form.getByLabel("Retrieval query").fill("expense policy")
		await form.getByRole("button", { name: "Run retrieval" }).click()
		const candidates = page.getByTestId("chunk-candidates")
		await expect(candidates).toBeVisible({ timeout: 30_000 })
		await candidates.locator("li").first().getByRole("checkbox").click()
		await expect(page.getByText(/1 chunk\(s\) selected/)).toBeVisible()

		await form.getByRole("button", { name: "Add item" }).click()

		const row = page.getByRole("row").filter({ hasText: question.slice(0, 60) })
		await expect(row).toBeVisible({ timeout: 30_000 })
		await expect(row.getByText("active", { exact: true })).toBeVisible()

		await row.getByRole("button", { name: /^Retire / }).click()
		await expect(row.getByText("retired", { exact: true })).toBeVisible({ timeout: 15_000 })
	})
})

test.describe("retrieval eval runs console (EV-2)", () => {
	test("triggers a run, lists metrics, and compares two runs", async ({ page }) => {
		await page.goto("/eval")
		const runsTable = () => page.getByTestId("eval-runs-table")
		await expect(page.getByRole("button", { name: "Run evaluation" })).toBeVisible({
			timeout: 15_000,
		})
		// EV-4: the gate-baseline banner renders (armed or not).
		await expect(page.getByTestId("gate-baseline")).toBeVisible()

		await page.getByRole("button", { name: "Run evaluation" }).click()
		// The run scores the real pipeline (embeddings + SQL), allow time.
		await expect(runsTable()).toBeVisible({ timeout: 60_000 })
		const firstRow = runsTable().locator("tbody tr").first()
		await expect(firstRow.getByText("completed")).toBeVisible({ timeout: 60_000 })

		// Two selections produce the per-metric delta panel.
		const selectButtons = runsTable().getByRole("button", { name: /Select run/ })
		await selectButtons.nth(0).click()
		await selectButtons.nth(0).click() // second row shifted; re-query below
		const remaining = runsTable().getByRole("button", { name: /Select run/ })
		if ((await remaining.count()) > 0) {
			await remaining.first().click()
		}
		await expect(page.getByTestId("run-comparison")).toBeVisible({ timeout: 30_000 })
		await expect(page.getByTestId("run-comparison").getByText(/hitRate/)).toBeVisible()
	})
})

test.describe("chat retrieval filters (RT-4)", () => {
	test("a filter matching nothing yields a grounded I-don't-know", async ({ page }) => {
		await page.goto("/chat")
		const composer = page.getByPlaceholder(/ask/i)
		await expect(composer).toBeVisible({ timeout: 15_000 })

		// Restrict to a source that doesn't exist -> retrieval returns empty.
		await page.getByLabel("Source filter").fill("no-such-source-e2e")
		await expect(page.getByText("answers limited to matching documents")).toBeVisible()

		await composer.fill("What is the meal reimbursement limit?")
		await composer.press("Enter")

		await expect(page.getByText(/I don't know/).first()).toBeVisible({ timeout: 60_000 })
	})
})

test.describe("chunk-boundary inspection (IN-5)", () => {
	test("renders a document's chunks with boundaries and metadata badges", async ({ page }) => {
		await page.goto("/corpus")
		await expect(page.getByRole("table").first()).toBeVisible({ timeout: 30_000 })

		// Inspect an INDEXED document — the newest row may still be processing
		// (its chunks don't exist yet).
		await page
			.getByRole("row")
			.filter({ hasText: "indexed" })
			.first()
			.getByRole("link", { name: /^Inspect chunks of / })
			.click()
		await expect(page.getByText("Chunk boundaries")).toBeVisible({ timeout: 30_000 })

		const list = page.getByTestId("chunk-boundaries")
		await expect(list).toBeVisible({ timeout: 30_000 })
		const items = list.locator("li")
		expect(await items.count()).toBeGreaterThan(0)
		// Per-chunk index + size badges render.
		await expect(items.first().getByText(/^#\d+$/)).toBeVisible()
		await expect(items.first().getByText(/\d+ chars/)).toBeVisible()
	})
})

test.describe("configuration review (OB-5)", () => {
	test("renders grouped effective config with masked secrets", async ({ page }) => {
		await page.goto("/ops-config")
		await expect(page.getByText("Effective configuration")).toBeVisible({ timeout: 15_000 })
		await expect(page.getByTestId("config-group-providers")).toBeVisible({ timeout: 30_000 })
		await expect(page.getByTestId("config-group-retrieval")).toBeVisible()
		const providers = page.getByTestId("config-group-providers")
		await expect(providers.getByRole("cell", { name: "openai_api_key", exact: true })).toBeVisible()
		await expect(providers.getByText("masked").first()).toBeVisible()
	})
})

test.describe("zero-downtime re-embedding console (IN-8)", () => {
	test("renders migration state with start/cutover/abort controls", async ({ page }) => {
		await page.goto("/corpus")
		const panel = page.getByTestId("reembed-panel")
		await expect(panel).toBeVisible({ timeout: 30_000 })
		await expect(panel.getByLabel("Target model")).toBeVisible()
		await expect(panel.getByRole("button", { name: "Start shadow re-embed" })).toBeVisible()
		await expect(panel.getByRole("button", { name: "Cut over" })).toBeVisible()
		await expect(panel.getByRole("button", { name: "Abort" })).toBeVisible()
		// The status badge renders a real state once the query resolves.
		await expect(panel.locator('[data-slot="badge"]').first()).toHaveText(
			/aborted|none|cutover|ready|shadowing|verifying|failed/,
			{ timeout: 15_000 }
		)
	})
})

test.describe("mobile parity (CH-4)", () => {
	test.use({ viewport: { width: 390, height: 844 }, hasTouch: true, isMobile: true })

	test("chat streams with citations, history opens, no horizontal overflow", async ({
		page,
	}) => {
		// ── query + stream + citations on a phone viewport ──
		await page.goto("/chat")
		const composer = page.getByRole("textbox", { name: "Question" })
		await expect(composer).toBeVisible({ timeout: 15_000 })
		await composer.fill("What is the meal reimbursement limit?")
		await composer.press("Enter")
		await expect(page.locator("[aria-label='Conversation'] .whitespace-pre-wrap").first()).toBeVisible(
			{ timeout: 60_000 }
		)
		await expect(page.getByText(/\[1\]|Sources/i).first()).toBeVisible({ timeout: 60_000 })

		// No horizontal overflow at 390px (responsive AC).
		const overflow = await page.evaluate(
			() => document.documentElement.scrollWidth - document.documentElement.clientWidth
		)
		expect(overflow).toBeLessThanOrEqual(1)

		// ── history parity ──
		await page.goto("/chat/history")
		await expect(page.getByRole("link").filter({ hasText: /./ }).first()).toBeVisible({
			timeout: 30_000,
		})
		const historyOverflow = await page.evaluate(
			() => document.documentElement.scrollWidth - document.documentElement.clientWidth
		)
		expect(historyOverflow).toBeLessThanOrEqual(1)
	})
})

test.describe("reports console (RP-1)", () => {
	test("runs the audit trail report with filters, paginates, and exports CSV", async ({
		page,
	}) => {
		await page.goto("/reports")
		const view = page.getByTestId("reports-view")
		await expect(view).toBeVisible({ timeout: 15_000 })

		// The framework renders the audit-trail definition: filters + columns.
		await expect(view.locator("[data-slot=card-title]")).toHaveText("System Audit Trail")
		await expect(view.getByRole("columnheader", { name: "When" })).toBeVisible({
			timeout: 15_000,
		})
		await expect(view.getByText("Page 1 of")).toBeVisible({ timeout: 15_000 })

		// Filter by action prefix — every visible row matches.
		await view.getByLabel("Action (prefix)").fill("eval.")
		await view.getByRole("button", { name: "Apply filters" }).click()
		const firstAction = view.locator("table tbody tr td:nth-child(4)").first()
		await expect(firstAction).toHaveText(/^eval\./, { timeout: 15_000 })

		// Pagination moves without losing the filter.
		const next = view.getByRole("button", { name: "Next", exact: true })
		if (await next.isEnabled()) {
			await next.click()
			await expect(view.getByText("Page 2 of")).toBeVisible({ timeout: 15_000 })
			await expect(firstAction).toHaveText(/^eval\./)
		}

		// CSV export downloads the filtered report.
		const downloadPromise = page.waitForEvent("download", { timeout: 30_000 })
		await view.getByRole("button", { name: "Export CSV" }).click()
		const download = await downloadPromise
		expect(download.suggestedFilename()).toMatch(/^audit-trail-.*\.csv$/)
	})
})
