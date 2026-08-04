# Quanby Turbo Template

A TypeScript-first monorepo template using **Turborepo** and **pnpm** for full-stack development.

## Tech Stack

| Layer     | Technology                          |
| --------- | ----------------------------------- |
| Frontend  | Next.js 16, Tailwind CSS, shadcn/ui |
| Backend   | NestJS                              |
| RAG       | Python FastAPI (uv) — internal-only |
| Mobile    | Flutter                             |
| Database  | Drizzle ORM + PostgreSQL (pgvector) |
| Auth      | Better Auth                         |
| Contracts | Zod schemas + DTOs                  |

## Quick Start

> Already set up? See **[docs/RUNNING.md](docs/RUNNING.md)** for the daily
> run guide, Release 1 test checklist, and troubleshooting.

```bash
# Install dependencies
pnpm install

# Set up environment files (copy from .env.example)
cp apps/backend/.env.example apps/backend/.env
cp apps/web/.env.example apps/web/.env
cp packages/db/.env.example packages/db/.env
cp apps/mobile/.env.example apps/mobile/.env
cp apps/rag/.env.example apps/rag/.env

# Apply the ordered database migrations (replaces the old `pnpm db:push`)
pnpm db:migrate

# Seed roles/permissions and the bootstrap admin (System Administrator)
pnpm db:seed

# Build packages (required for first run)
pnpm build

# Generate AI agent rules (recommended - includes sub-agent-first workflow policy)
pnpm dlx @intellectronica/ruler apply

# Start development (web :3001, backend :3000, rag :8000 — separate ports)
pnpm dev
```

> The Python RAG service (`apps/rag`) needs [uv](https://docs.astral.sh/uv/)
> installed — its workspace scripts (`dev`/`build`/`lint`/`test`) shell out to
> `uv`/`uvicorn`/`ruff`/`pytest`.

> **Database port:** migrations require the **pgvector** Postgres from
> `docker-compose.yml` (service `postgres`). If host port 5432 is already
> taken by another Postgres, set `POSTGRES_HOST_PORT` in the root `.env`
> (e.g. `5434`) and update the port in the `DATABASE_URL` of
> `packages/db/.env` and `apps/backend/.env` to match. A plain host Postgres
> without the pgvector extension will fail the
> `enable_pgvector_extension` migration.

After `pnpm dev`:

| Service        | URL                                  |
| -------------- | ------------------------------------ |
| Web            | http://localhost:3001                |
| Backend API    | http://localhost:3000/api/v1         |
| API reference  | http://localhost:3000/api/v1/docs    |
| RAG service    | http://localhost:8000/health         |

## Coverage thresholds are a floor

These coverage numbers are the minimum for the scaffold. In your real project, raise them as you add tested features following the template's patterns — never lower them. Example modules (todo/notes) are excluded from coverage because you'll replace them. See `docs/QA-SETUP-CHANGES.md` for the full table.

### Run on a single port (Docker + Nginx)

Serve **web and backend through one origin** (and, in production, one TLS
cert) via the bundled Nginx reverse proxy — `/api/*` → backend, `/*` → web.

```bash
# Copy the root env (single-origin build args + postgres/redis/rag settings)
cp .env.example .env

# Build + start nginx + web + backend + rag + postgres (pgvector) + redis
docker compose up -d --build

# New clone tip: start/restart only the Nginx proxy without dependencies
docker compose up -d --no-deps nginx
```

| Services        | URL                            |
| -------------- | ------------------------------ |
| App (web)      | http://localhost               |
| Backend API    | http://localhost/api/v1        |
| API reference  | http://localhost/api/v1/docs   |

Notes:

- Startup order: `postgres` → `migrate` (one-shot `pnpm db:migrate`, applies
  the ordered Drizzle migrations incl. `CREATE EXTENSION vector`) →
  `backend` + `rag`.
- The `rag` service is **internal-only**: no published port and no Nginx
  route — the backend reaches it at `http://rag:8000` on `app-network`.
- The bundled Nginx publishes port `80`. web/backend also bind to loopback
  (`127.0.0.1:3001` / `127.0.0.1:3000`) so an external proxy can reach them;
  they're not exposed to the LAN.
- Same origin → no browser CORS. Proxy config lives in `nginx/nginx.conf`
  (volume-mounted — edit then `docker compose restart nginx`).
- Postgres runs **in compose** (`pgvector/pgvector:pg16`) and is published on
  `127.0.0.1:5432` for local tooling; backend/rag/migrate all use the
  in-network `postgres` service via compose-injected `DATABASE_URL`.
- `CORS_ORIGINS` / `BETTER_AUTH_TRUSTED_ORIGINS` must include the proxy origin
  (`http://localhost`).
- Change the host port by editing the `nginx` service `ports` (e.g. `8080:80`),
  and update `NEXT_PUBLIC_*` in `.env` to match.

#### Use your own Nginx instead

The bundled Nginx is gated behind the `docker-proxy` compose profile, enabled by
default via `COMPOSE_PROFILES=docker-proxy` in the root `.env`. To swap in an
external/host Nginx, set `COMPOSE_PROFILES=` (empty) and bring the stack up:

```bash
docker compose up -d --build   # web + backend only; port 80 is free
```

Point your Nginx at `127.0.0.1:3001` (web) and `127.0.0.1:3000` (backend) —
reuse the routing in `nginx/nginx.conf`. If the external proxy keeps the same
single origin (`/api/*` → backend, `/*` → web), no CORS or `NEXT_PUBLIC_*`
changes are needed; if it splits web/api across different domains, update
`NEXT_PUBLIC_API_BASE_URL` (rebuild web) and the backend `CORS_ORIGINS` /
`BETTER_AUTH_TRUSTED_ORIGINS`.

## Project Structure

```
├── apps/
│   ├── web/           # Next.js frontend (port 3001)
│   ├── backend/       # NestJS API (port 3000)
│   ├── rag/           # Python FastAPI RAG service (port 8000, internal-only)
│   └── mobile/        # Flutter app
├── packages/
│   ├── auth/          # Shared auth config
│   ├── contracts/     # API contracts & DTOs
│   └── db/            # Database schema
└── tooling/           # Shared configs (ESLint, Prettier, TypeScript)
```

## RAG Profiles — pick your variant with one line

This template ships four RAG architectures in ONE codebase. Set
`RAG_PROFILE` in `apps/rag/.env` (both the API service and the arq worker):

| `RAG_PROFILE` | What you get | Extra infra | Cost/latency |
|---|---|---|---|
| `baseline` (default) | hybrid retrieval + grounded generation — the core template | none | reference point |
| `contextual` | + LLM-generated chunk context in both retrieval indexes | none | +1 cheap LLM call per chunk at ingest (cached) |
| `agentic` | + self-correcting retrieval loop (grade → rewrite → groundedness gate) | none | +2–5 cheap LLM calls / +3.4–7.5 s per uncached question |
| `full` | + Graph RAG (Neo4j) + multimodal (page images + vision answers) | `docker compose --profile graph up -d neo4j` + `NEO4J_PASSWORD` | + extraction/caption calls at ingest (cached); ~5× on vision-worthy pages |

Rules that make this safe:

- An explicitly set flag (`CONTEXTUAL_CHUNKS_ENABLED`, `RAG_MODE`,
  `GRAPH_RAG_ENABLED`, `MULTIMODAL_ENABLED`) always overrides the preset.
- Any flag change auto-invalidates the response cache (config fingerprint).
- Enabling `contextual` (or higher) on an **existing** corpus requires the
  zero-downtime shadow re-index — follow the runbook in
  [docs/RUNNING.md](docs/RUNNING.md#enabling-contextual-retrieval-ce-2-rollout-runbook).
- Every optional stage degrades gracefully: loop failures fall through to
  linear, Neo4j outages fall back to hybrid, vision failures answer from
  captions — flagged in traces, never an error.
- Measured per-profile numbers: [docs/PROFILE-MATRIX.md](docs/PROFILE-MATRIX.md)
  and [docs/CONTEXTUAL-RETRIEVAL-RESULTS.md](docs/CONTEXTUAL-RETRIEVAL-RESULTS.md).
  The CI eval gate compares each run against a baseline with the SAME
  profile identity, so switching profiles never trips it against another
  profile's numbers.

The default quickstart below runs `baseline` and needs none of the Phase 7
infrastructure.

## Environment Variables

| Variable                      | Required | App         | Description                           |
| ----------------------------- | -------- | ----------- | ------------------------------------- |
| `DATABASE_URL`                | ✅       | Backend, DB | PostgreSQL connection string          |
| `BETTER_AUTH_SECRET`          | ✅       | Backend     | Auth secret (openssl rand -base64 32) |
| `BETTER_AUTH_TRUSTED_ORIGINS` | ✅       | Backend     | Comma-separated trusted origins       |
| `CORS_ORIGINS`                | ✅       | Backend     | Comma-separated CORS origins          |
| `PORT`                        | ❌       | Backend     | Server port (default: 3000)           |
| `GOOGLE_CLIENT_ID`            | ❌       | Backend     | Google OAuth client ID                |
| `GOOGLE_CLIENT_SECRET`        | ❌       | Backend     | Google OAuth client secret            |
| `NEXT_PUBLIC_APP_URL`         | ✅       | Web         | Web app URL                           |
| `NEXT_PUBLIC_API_BASE_URL`    | ✅       | Web         | Backend API base URL                  |
| `NEXT_PUBLIC_API_VERSION`     | ✅       | Web         | API version (default: v1)             |
| `INTERNAL_API_BASE_URL`       | ❌       | Web         | SSR-only API URL (Docker net)         |
| `COMPOSE_PROFILES`            | ❌       | Root/Docker | `docker-proxy` runs bundled Nginx; empty = external proxy |
| `REDIS_URL`                   | ✅       | RAG         | Redis connection string               |
| `OPENAI_API_KEY`              | ✅       | RAG         | OpenAI API key (embeddings + chat)    |
| `WATSONX_ENABLED`             | ❌       | RAG         | Enables IBM as tertiary chat failover (default: false) |
| `IBM_CLOUD_API_KEY`           | ❌       | RAG         | Required only when Watsonx is enabled |
| `WATSONX_PROJECT_ID`          | ❌       | RAG         | Project with an associated Watsonx Runtime service |
| `WATSONX_URL`                 | ❌       | RAG         | Regional endpoint (default: Tokyo)    |
| `WATSONX_MODEL`               | ❌       | RAG         | IBM fallback model ID                 |
| `WATSONX_API_VERSION`         | ❌       | RAG         | IBM REST API version                  |
| `PROVIDER_RPM_WATSONX`        | ❌       | RAG         | Shared Watsonx request budget         |
| `RAG_SERVICE_TOKEN_SECRET`    | ✅       | Backend, RAG | Shared HS256 secret for the service JWT |
| `RAG_SERVICE_URL`             | ✅       | Backend     | Internal RAG base URL (`http://rag:8000` in Docker) |
| `LANGSMITH_API_KEY`           | ✅       | RAG         | Tracing key (or Langfuse keys with `TRACING_PROVIDER=langfuse`) |
| `POSTGRES_USER/PASSWORD/DB`   | ❌       | Root/Docker | Compose postgres credentials (also build the injected `DATABASE_URL`) |

Copy from `.env.example` in each app: `apps/backend/.env`, `apps/web/.env`, `packages/db/.env`, `apps/rag/.env`.

**Separate ports (native `pnpm dev`):** `NEXT_PUBLIC_APP_URL=http://localhost:3001`,
`NEXT_PUBLIC_API_BASE_URL=http://localhost:3000/api`.
**Single port (Docker proxy):** `NEXT_PUBLIC_APP_URL=http://localhost`,
`NEXT_PUBLIC_API_BASE_URL=http://localhost/api` (set in the root `.env`; baked
into the web image at build time).

### Optional IBM watsonx.ai fallback

Watsonx is an optional tertiary chat failover only. Normal routing remains
OpenAI for the cheap/default tier and Anthropic for the strong tier; OpenAI
also remains the required embeddings provider. When both normal provider
circuits are unavailable, the reliability router may select Watsonx. Setting
`WATSONX_ENABLED=false` makes no IBM requests.

The default regional endpoint is `https://jp-tok.ml.cloud.ibm.com`, with
`meta-llama/llama-4-maverick-17b-128e-instruct-fp8` as the quality-first
fallback model. Before enabling it, associate an IBM watsonx.ai Runtime service
with the project and add the API key's service ID as a project collaborator.
Store production credentials in your deployment secret manager, never in a
tracked `.env` file. The example environment files contain the complete,
disabled-by-default configuration contract.

## Scripts

| Command           | Description                |
| ----------------- | -------------------------- |
| `pnpm dev`        | Start all apps in dev mode |
| `pnpm build`      | Build all apps             |
| `pnpm lint`       | Run ESLint                 |
| `pnpm typecheck`  | Run TypeScript checks      |
| `pnpm format:fix` | Format code with Prettier  |
| `pnpm db:generate` | Generate a migration from schema changes |
| `pnpm db:migrate` | Apply the ordered migration history |
| `pnpm db:studio`  | Open Drizzle Studio        |

> **Migrations, not push:** the repo no longer uses `drizzle-kit push` — it
> ignores hand-written `migration.sql` files (e.g. `CREATE EXTENSION vector`).
> All schema changes go through `pnpm db:generate` + `pnpm db:migrate`; see
> `packages/db/README.md`.

## Backend: Production-like Local Run

To mirror Docker/ECS behavior for backend startup, build workspace dependencies first, then run backend start:

```bash
pnpm build
pnpm --filter @repo/backend start
```

This avoids cases where a backend-only build misses required workspace package build outputs.

## Shared Packages

```typescript
// Auth configuration
import { auth } from "@repo/auth"
// API contracts & DTOs
import { CreateTodoDto, TodoSchema } from "@repo/contracts"
// Database client & schema
import { db } from "@repo/db"
import { todos, users } from "@repo/db/schema"
```

## Deployment

### How It Works

| Branch       | Environment | Strategy                            |
| ------------ | ----------- | ----------------------------------- |
| `staging`    | Staging     | Single EC2 + Docker Compose + Nginx |
| `production` | Production  | ECS Fargate + ALB (auto-scaling)    |

Merge to the branch → CI runs → Docker images build → deploy automatically.

Infrastructure is managed separately via [turbo-infrastructure](https://github.com/Quanby-IT-Solutions/turbo-infrastructure) (Terraform).

### Deploy

```bash
# Deploy to staging
git checkout staging && git merge dev && git push

# Deploy to production
git checkout production && git merge staging && git push
```

Monitor progress in **GitHub → Actions**.

### GitHub Environment Setup

Before your first deploy, go to **Settings → Environments** and create `staging` and `production`.

#### Variables

| Variable                    | Description               | Staging                             | Production                          |
| --------------------------- | ------------------------- | ----------------------------------- | ----------------------------------- |
| `AWS_REGION`                | AWS region                | `ap-southeast-1`                    | `ap-southeast-1`                    |
| `PROJECT_NAME`              | Project identifier        | `turbo-template`                    | `turbo-template`                    |
| `ECR_REPOSITORY_WEB`        | ECR repo name for web     | `turbo-template-web-staging`        | `turbo-template-web-production`     |
| `ECR_REPOSITORY_BACKEND`    | ECR repo name for backend | `turbo-template-backend-staging`    | `turbo-template-backend-production` |
| `DOMAIN_WEB`                | Web domain                | `stg-turbo.quanbyit.com`            | `turbo.quanbyit.com`                |
| `DOMAIN_API`                | API domain                | `stg-turbo-be.quanbyit.com`         | `turbo-be.quanbyit.com`             |
| `ECS_CLUSTER`               | ECS cluster name          |                                     | `turbo-template-production`         |
| `ECS_SERVICE_WEB`           | ECS web service name      |                                     | `turbo-template-web-production`     |
| `ECS_SERVICE_BACKEND`       | ECS backend service name  |                                     | `turbo-template-backend-production` |
| `ECS_EXECUTION_ROLE_ARN`    | ECS execution role ARN    |                                     | `arn:aws:iam::123...:role/...`      |
| `ECS_TASK_ROLE_ARN`         | ECS task role ARN         |                                     | `arn:aws:iam::123...:role/...`      |
| `NEXT_PUBLIC_APP_URL`       | Public web URL            | `https://stg-turbo.quanbyit.com`    | `https://turbo.quanbyit.com`        |
| `NEXT_PUBLIC_API_BASE_URL`  | Public API URL            | `https://stg-turbo-be.quanbyit.com` | `https://turbo-be.quanbyit.com`     |
| `BETTER_AUTH_COOKIE_DOMAIN` | Cookie domain for auth    |                                     | `.quanbyit.com`                     |

#### Secrets

| Secret                        | Description              | Staging                               | Production                            |
| ----------------------------- | ------------------------ | ------------------------------------- | ------------------------------------- |
| `AWS_ACCESS_KEY_ID`           | IAM access key           | `AKIA...`                             | `AKIA...`                             |
| `AWS_SECRET_ACCESS_KEY`       | IAM secret key           | `wJal...`                             | `wJal...`                             |
| `EC2_HOST`                    | EC2 public IP / hostname | `54.123.45.67`                        |                                       |
| `EC2_USER`                    | SSH user                 | `ubuntu`                              |                                       |
| `EC2_SSH_KEY`                 | EC2 SSH private key      | `-----BEGIN RSA PRIVATE KEY-----...`  |                                       |
| `DATABASE_URL`                | PostgreSQL connection    | `postgresql://user:pass@host:5432/db` | `postgresql://user:pass@host:5432/db` |
| `BETTER_AUTH_SECRET`          | Auth signing secret      | `openssl rand -base64 32`             | `openssl rand -base64 32`             |
| `BETTER_AUTH_TRUSTED_ORIGINS` | Trusted origins          | `https://stg-turbo.quanbyit.com`      | `https://turbo.quanbyit.com`          |
| `CORS_ORIGINS`                | Allowed CORS origins     | `https://stg-turbo.quanbyit.com`      | `https://turbo.quanbyit.com`          |
| `GOOGLE_CLIENT_ID`            | Google OAuth client ID   | `123...apps.googleusercontent.com`    | `456...apps.googleusercontent.com`    |
| `GOOGLE_CLIENT_SECRET`        | Google OAuth secret      | `GOCSPX-...`                          | `GOCSPX-...`                          |

> Get values from `terraform output` in the [turbo-infrastructure](https://github.com/Quanby-IT-Solutions/turbo-infrastructure) repo.

### First-Time Staging SSL

After the first staging deployment, SSH into EC2 to set up HTTPS:

```bash
ssh -i your-key.pem ubuntu@EC2_HOST
sudo certbot --nginx -d stg-turbo.quanbyit.com -d stg-turbo-be.quanbyit.com
sudo certbot renew --dry-run
```

Production uses AWS ACM certificates via the ALB — no manual SSL needed.

### Docker (Local) — single-port reverse proxy

The local Docker stack runs behind an Nginx reverse proxy, so both apps share
**one origin / one port** (and in production, **one TLS cert**):

- `/api/*` → backend (NestJS)
- `/*` → web (Next.js)

```bash
cp .env.example .env            # single-origin build args for the web image
docker-compose up -d --build    # start nginx + web + backend
```

Open **http://localhost** for the whole app. web (`3001`) and backend
(`3000`) are bound to loopback (`127.0.0.1`) for an external proxy but not
exposed to the LAN — the bundled Nginx is the entrypoint on port `80`.

Because the browser uses one origin, there is no CORS on the client path. Config
lives in `nginx/nginx.conf`. SSR still talks to the backend over the internal
Docker network via `INTERNAL_API_BASE_URL=http://backend:3000/api`.

**Swap the proxy:** the bundled Nginx runs under the `docker-proxy` compose
profile (`COMPOSE_PROFILES=docker-proxy` in `.env`, on by default). Set
`COMPOSE_PROFILES=` empty to skip it and use your own Nginx against
`127.0.0.1:3001` / `127.0.0.1:3000`.

**Production extension:** uncomment the 443/TLS server block in
`nginx/nginx.conf`, set `server_name` to your one domain, and drop in a single
certbot/ACM cert. Same path routing — no second domain or cert needed.

### Troubleshooting

<details>
<summary>Staging</summary>

```bash
ssh -i your-key.pem ubuntu@EC2_HOST
cd /opt/staging && docker compose -f docker-compose.staging.yml ps
docker compose -f docker-compose.staging.yml logs web
docker compose -f docker-compose.staging.yml logs backend
sudo nginx -t && sudo systemctl status nginx
```

</details>

<details>
<summary>Production</summary>

```bash
aws ecs describe-services --cluster CLUSTER --services SERVICE --query 'services[0].events[:5]'
aws logs tail /ecs/PROJECT_NAME-web-production --follow
aws logs tail /ecs/PROJECT_NAME-backend-production --follow
```

</details>

## Git Hooks

This repo uses [Husky](https://typicode.github.io/husky/) to run tests before pushing.

| Hook       | What it does                                           |
| ---------- | ------------------------------------------------------ |
| `pre-push` | Runs `turbo test --affected` to block failing pushes   |

To bypass the hook (e.g., for WIP pushes):

```bash
git push --no-verify
```

## Links

- [Turborepo](https://turbo.build/docs) · [Next.js](https://nextjs.org/docs) · [NestJS](https://docs.nestjs.com/) · [Drizzle](https://orm.drizzle.team/) · [Better Auth](https://better-auth.com/docs)
