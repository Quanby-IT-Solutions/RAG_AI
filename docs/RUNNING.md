# Running the System

Daily-use guide for starting, testing, and troubleshooting the stack.
First-time setup (installs, `.env` copies, first migration) lives in the
[README Quick Start](../README.md#quick-start).

## Option B — Native dev (recommended for development)

Only the database and Redis run in Docker; apps run natively with hot reload.

```powershell
cd D:\PROJECTS\RAG_AI

# 1. Infra (postgres/pgvector + redis)
docker compose up -d postgres redis

# 2. Migrations — one-shot container; only needed after schema changes
docker compose up migrate --no-deps

# 3. Seed roles/permissions + bootstrap admin — only on a fresh/empty DB
pnpm db:seed

# 4. All dev servers (backend :3000, rag :8000, web :3001)
pnpm dev
```

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
- **Chat returns degraded answers** — OpenAI provider error; check
  `OPENAI_API_KEY` and the rag service logs.
- **429 on login/session during parallel testing** — Better Auth rate limit
  (10/60s default); raise `AUTH_RATE_LIMIT_MAX` on the backend for local
  test runs.
