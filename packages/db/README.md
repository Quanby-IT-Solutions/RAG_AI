# @repo/db

Drizzle ORM database package with schema definitions and client.

## Structure

```
packages/db/
├── src/
│   ├── client.ts         # Database client factory
│   ├── schema.ts         # Schema exports
│   └── utils/            # Database utilities
├── drizzle.config.ts     # Drizzle configuration
└── package.json
```

## Usage

```typescript
import { db } from "@repo/db"
import { todos, users } from "@repo/db/schema"

// Query examples
const allTodos = await db.select().from(todos)
const user = await db.query.users.findFirst({ where: eq(users.id, "123") })
```

## Environment Variables

| Variable       | Description                  |
| -------------- | ---------------------------- |
| `DATABASE_URL` | PostgreSQL connection string |

## Scripts

| Command            | Description                                   |
| ------------------ | --------------------------------------------- |
| `pnpm db:studio`   | Open Drizzle Studio                           |
| `pnpm db:generate` | Generate a migration from schema changes      |
| `pnpm db:migrate`  | Apply the ordered migration history           |
| `pnpm db:seed`     | Run migrations, then seed users and mock data |

> **Note:** the repo no longer uses `drizzle-kit push`. `push` diff-applies the
> schema and **ignores `migration.sql` files**, so hand-written DDL (e.g.
> `CREATE EXTENSION IF NOT EXISTS vector;`) would never run. All schema changes
> flow through `db:generate` + `db:migrate` — one ordered, reviewable history.

## Adding Tables

1. Create schema in `src/schema/`:

```typescript
// src/schema/posts.ts
import { pgTable, text, timestamp } from "drizzle-orm/pg-core"

export const posts = pgTable("posts", {
	id: text("id").primaryKey(),
	title: text("title").notNull(),
	createdAt: timestamp("created_at").defaultNow(),
})
```

2. Export from `src/schema/index.ts`
3. Run `pnpm db:generate` to emit a migration, review (and hand-augment if
   needed) the generated `migration.sql`, then run `pnpm db:migrate`

## Hand-augmented migrations (raw DDL)

Some DDL can't be inferred by drizzle-kit (extensions, triggers, special
indexes). Workflow:

1. `pnpm db:generate` (or `drizzle-kit generate --custom --name <name>` for a
   SQL-only migration) creates `src/migrations/<timestamp>_<name>/migration.sql`.
2. Edit that file — e.g. prepend `CREATE EXTENSION IF NOT EXISTS vector;` —
   and commit it. `pnpm db:migrate` runs it in order.

### R-DDL spike findings (drizzle 1.0.0-beta.9)

Verified on `drizzle-kit 1.0.0-beta.9-e89174b`:

- A `customType` with `dataType: () => "vector(1536)"` emits `"embedding" vector(1536)` in the generated migration.
- `.generatedAlwaysAs(sql\`to_tsvector('english', "content")\`)` on a `tsvector` customType emits `GENERATED ALWAYS AS (to_tsvector('english', "content")) STORED`.
- `generate --custom` produces an empty ordered migration for raw SQL (used for the pgvector extension).

So IN-2 can define `chunks.embedding`/`chunks.tsv` in `schema.ts` via
`customType`; only extension/HNSW/GIN/triggers need hand-augmented SQL. No raw-SQL
fallback for the table itself is required.
