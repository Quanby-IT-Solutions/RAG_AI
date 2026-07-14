import { drizzle, type NodePgDatabase } from "drizzle-orm/node-postgres"

import { schema } from "./schema.js"

/**
 * Type for the Drizzle database client
 */
export type DBClient = NodePgDatabase<typeof schema>

/**
 * Creates a Drizzle ORM client with the configured schema
 *
 * @param connectionString - Optional connection string. If not provided, uses DATABASE_URL environment variable
 * @returns A Drizzle database client
 * @throws Error if connection string is not provided and DATABASE_URL is not set
 */
export function createDBClient(connectionString?: string): DBClient {
	const connString = connectionString ?? process.env.DATABASE_URL
	if (!connString) {
		throw new Error("DATABASE_URL environment variable is not set")
	}
	return drizzle(connString, { schema })
}
