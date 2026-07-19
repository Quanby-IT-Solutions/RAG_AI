# Running the System

Daily-use guide for starting, testing, and troubleshooting the stack.
First-time setup (installs, `.env` copies, first migration) lives in the
[README Quick Start](../README.md#quick-start).

## Option B — Native dev (recommended for development)

Only the database and Redis run in Docker; apps run natively with hot reload.

> **Ports come from your `.env` files.** Default: backend **:3000**, rag
> **:8000**, web **:3001**. Three values must agree if you change them:
> backend `PORT` (`apps/backend/.env`) = the port in
> `NEXT_PUBLIC_API_BASE_URL` (`apps/web/.env`), and the rag port = the port
> in `RAG_SERVICE_URL` (`apps/backend/.env`). A rag-port mismatch is a fast
> 502 on chat.

```powershell
cd D:\PROJECTS\RAG_AI

# 1. Infra (postgres/pgvector + redis)
docker compose up -d postgres redis

# 2. Migrations — one-shot container; only needed after schema changes
docker compose up migrate --no-deps

# 3. Seed roles/permissions + bootstrap admin — only on a fresh/empty DB
pnpm db:seed

# 4. All dev servers (ports per your .env; template default 3000/8000/3001)
pnpm dev
```

> **Note:** `pnpm dev` does **not** start the arq worker — ingestion jobs
> stay `queued` without it. Start it separately (see Terminal C below) or
> run the one-by-one flow.

### One-by-one (separate terminals)

Same stack without the combined `pnpm dev` — useful for isolating logs or
restarting a single service. Steps 1–3 above (infra, migrations, seed)
still apply first.

The rag terminal must serve the port your backend's `RAG_SERVICE_URL`
points at (`8000` on the template default). `pnpm dev:backend` / `dev:web`
read `PORT` / URLs from `.env`, so no port flags there.

```powershell
# Terminal A — backend (PORT from apps/backend/.env)
pnpm dev:backend

# Terminal B — rag API (port MUST match RAG_SERVICE_URL in apps/backend/.env)
cd apps/rag
uv run uvicorn --factory app.main:create_app --host 0.0.0.0 --port 8000 --reload

# Terminal C — arq worker (background ingestion jobs; uploads never index without it)
cd apps/rag
uv run arq app.worker.WorkerSettings

# Terminal D — web (port from apps/web/.env)
pnpm dev:web
```

- `pnpm dev:backend` / `pnpm dev:web` are turbo watch filters that also
  rebuild workspace dependencies, same behavior as inside `pnpm dev`.
- Rag API alternative: `pnpm -F @repo/rag dev` (same command; hardcodes
  `--port 8000` — matches the default `RAG_SERVICE_URL`).
- `RAG_SERVICE_URL` must use `127.0.0.1`, not `localhost` — hostname
  resolution has caused connection failures.
- If the rag API won't bind, check for a zombie process on its port
  (same recipe as the port-3001 fix in Troubleshooting).

URLs below use the template-default ports; substitute your `.env` ports if
you have overridden them.

| Service       | URL                               |
| ------------- | --------------------------------- |
| Web           | http://localhost:3001             |
| Backend API   | http://localhost:3000/api/v1      |
| API reference | http://localhost:3000/api/v1/docs |
| RAG health    | http://localhost:8000/health      |

**Login (seeded admin):** `admin@turbo-template.local` / `password123`

Requirements: `uv` on PATH for the Python RAG service. `OPENAI_API_KEY` must
be set in the root `.env` (Docker) and `apps/rag/.env` (native) — chat and
indexing fail without it.

**Stop:** `Ctrl+C` on `pnpm dev`, then `docker compose stop postgres redis`.

## Option A — Full Docker (prod-like)

Everything containerized behind the bundled Nginx.

```powershell
docker compose up -d --build
docker compose ps   # wait: migrate exits 0, backend + rag healthy
```

- Single URL via Nginx: http://localhost (port 80 must be free — see
  README "Run on a single port" to disable the bundled proxy)
- Direct ports still published: web :3001, backend :3000
- Seed on fresh DB: set `DATABASE_URL` at the compose-published Postgres
  port, then `pnpm db:seed`

**Stop:** `docker compose down` (add `-v` to wipe the database volume).

## Health checks

Template-default ports shown; use your `.env` ports if overridden.

```powershell
curl http://localhost:3000/api/v1/health   # backend: {"status":"ok",...}
curl http://localhost:8000/health          # rag: database + redis "up"
```

## Release 1 test checklist

1. **Auth** — login as seeded admin; `/register` does not exist
   (admin-provisioned accounts only, `AUTH_SIGN_UP_ENABLED=false`).
2. **RBAC** — sysadmin nav shows Dashboard/Todos/Users/Corpus/Chat. Create a
   low-permission user in Users panel, login as them: gated nav hidden,
   direct URLs denied, denial rows appear in `audit_log`.
3. **Ingest (Flow A)** — Corpus page → upload PDF → status flips
   `registered → indexed`. Re-upload identical file → "Already ingested",
   no duplicate row (content-hash dedup).
4. **Chat (Flow B)** — question answerable from corpus → grounded answer
   with `[n]` citation chips. Off-corpus question → fixed "I don't know"
   (B1). Rapid sends → 429 rate-limit banner.
5. **Audit** —

   ```powershell
   docker exec rag-ai-postgres psql -U postgres -d ragai -c "select action, actor_type, created_at from audit_log order by created_at desc limit 20;"
   ```

   Expect `corpus.document.upload`, `chat.query.submit`, `rbac.denied` rows.
6. **Tracing** — LangSmith project `ragp` shows `rag.chat` traces.

### Automated tests

```powershell
pnpm lint
pnpm typecheck
pnpm test
```

e2e (Playwright) needs its own backend/web on 3005/3002 with a raised auth
rate limit — see `packages/e2e-web` and the root `test:e2e:web` script.

## Enabling contextual retrieval (CE-2 rollout runbook)

Turning `CONTEXTUAL_CHUNKS_ENABLED` on (or `RAG_PROFILE=contextual`/higher)
changes what every embedding means — roll it out through the IN-8 shadow
path, never by re-ingesting in place:

1. Set the profile/flag on the **rag service and the arq worker** and restart
   both. New ingests enrich immediately; the existing corpus does not.
2. Trigger a re-embed with the **same** embedding model (the CE-2 config
   identity is model + enrichment + prompt version, so same-model is valid):
   `POST /internal/reembed {"targetModel": "<live model>"}` (or the corpus
   console button). The worker enriches every chunk (context cache — re-runs
   are free), builds shadow embeddings of `context + chunk`, and lands
   `context_text` on the rows (the keyword arm picks it up as it lands).
3. Verification runs the golden set against the shadow automatically; the
   migration only reaches `ready` when metrics hold ≥ baseline − tolerance.
4. `POST /internal/reembed/cutover` — one atomic pointer flip switches the
   vector arm to the enriched embeddings and bumps the response-cache
   generation. `POST /internal/reembed/abort` at any earlier point reverts
   shadow AND context (tsv falls back to raw text); serving is never touched.
5. Compare the before/after eval runs — see docs/CONTEXTUAL-RETRIEVAL-RESULTS.md.

## Troubleshooting

- **Port 3001 already in use** — stale Next dev server from a previous
  session:

  ```powershell
  Get-NetTCPConnection -LocalPort 3001 -State Listen
  Stop-Process -Id <pid> -Force
  ```

- **Migrations fail on `enable_pgvector_extension`** — `DATABASE_URL` points
  at a host Postgres without pgvector instead of the compose one. Check the
  port matches `POSTGRES_HOST_PORT` in the root `.env` across
  `packages/db/.env`, `apps/backend/.env`, **and** `apps/rag/.env`.
- **Hydration mismatch with `bis_skin_checked` / `bis_register` attributes**
  in the browser console — a browser extension mutating the DOM, not an app
  bug. Verify in an incognito window.
- **Fast 502 on `/api/v1/chat/stream`** (responseTime ~10-20ms) — backend
  can't reach the rag service. Almost always a port mismatch: the rag API
  is on a different port than `RAG_SERVICE_URL` in `apps/backend/.env`.
  Confirm with `Get-NetTCPConnection -State Listen -LocalPort 8000,8001`
  and `curl http://127.0.0.1:<port>/health`; restart rag on the expected
  port. (A timeout-style 502 after ~60s instead is a real rag error —
  check its logs.)
- **Chat returns degraded answers** — OpenAI provider error; check
  `OPENAI_API_KEY` and the rag service logs.
- **429 on login/session during parallel testing** — Better Auth rate limit
  (10/60s default); raise `AUTH_RATE_LIMIT_MAX` on the backend for local
  test runs.
