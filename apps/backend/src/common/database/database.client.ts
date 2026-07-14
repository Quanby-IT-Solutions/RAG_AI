import { drizzle } from "drizzle-orm/node-postgres"

import { schema } from "@repo/db/schema"

import { env } from "@/config/env.config"

/**
 * Singleton database client instance
 * Initialized once at module load time using validated environment variables
 *
 * Drizzle manages its own connection pool internally.
 *
 * @example
 * ```typescript
 * import { db } from "@/common/database/database.client"
 *
 * const todos = await db.select().from(todos)
 * ```
 */
export const db = drizzle(env.DATABASE_URL, { schema })
