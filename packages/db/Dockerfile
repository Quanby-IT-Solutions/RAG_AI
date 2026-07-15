# ========================================
# One-shot migration runner (pnpm db:migrate)
# Runs the ordered Drizzle migration history against DATABASE_URL, then exits.
# Compose startup order: postgres -> migrate -> backend + rag.
# ========================================
FROM node:22-alpine AS base
RUN apk add --no-cache libc6-compat
RUN npm install -g pnpm@10.27.0
WORKDIR /app

# ========================================
# Prepare Stage - Prune workspace
# ========================================
FROM base AS prepare
RUN npm install -g turbo
COPY . .
RUN turbo prune @repo/db --docker

# ========================================
# Runner Stage
# ========================================
FROM base AS runner
COPY --from=prepare /app/out/json/ .
COPY --from=prepare /app/out/pnpm-lock.yaml ./pnpm-lock.yaml
RUN --mount=type=cache,id=pnpm-store,target=/pnpm/store \
  pnpm config set store-dir /pnpm/store && \
  pnpm config set fetch-retries 5 && \
  pnpm config set fetch-retry-maxtimeout 120000 && \
  pnpm install --frozen-lockfile
COPY --from=prepare /app/out/full/ .

WORKDIR /app/packages/db
# drizzle-kit reads src/schema.ts + src/migrations directly (no tsc build needed)
CMD ["pnpm", "exec", "drizzle-kit", "migrate"]
