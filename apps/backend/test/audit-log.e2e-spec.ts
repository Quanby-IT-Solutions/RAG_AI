/**
 * Integration proof (Flow E / Tech Plan §6): audit_log is append-only AT THE
 * DATABASE. INSERTs succeed; any UPDATE or DELETE is rejected by the
 * BEFORE UPDATE OR DELETE trigger regardless of which service issues it.
 *
 * Requires a migrated database (pnpm db:migrate). Skipped when DATABASE_URL
 * is not set so the unit-test pipeline stays green without infrastructure.
 */
import "dotenv/config"

import { Pool } from "pg"

const connectionString = process.env.DATABASE_URL
const describeIfDb = connectionString ? describe : describe.skip

describeIfDb("audit_log append-only enforcement (integration)", () => {
	let pool: Pool
	let insertedId: string

	beforeAll(async () => {
		pool = new Pool({ connectionString })
		const result = await pool.query(
			`INSERT INTO audit_log (actor_id, actor_type, action, resource_type, resource_id, before, after)
			 VALUES ($1, $2, $3, $4, $5, $6, $7)
			 RETURNING id`,
			[
				"integration-test-actor",
				"system",
				"test.audit.insert",
				"test",
				"immutability-spec",
				null,
				JSON.stringify({ probe: true }),
			]
		)
		insertedId = result.rows[0].id
	})

	afterAll(async () => {
		await pool.end()
	})

	it("accepts INSERTs (the only allowed write)", async () => {
		const { rows } = await pool.query("SELECT * FROM audit_log WHERE id = $1", [insertedId])
		expect(rows).toHaveLength(1)
		expect(rows[0].action).toBe("test.audit.insert")
		expect(rows[0].created_at).toBeTruthy()
	})

	it("rejects UPDATE with the append-only trigger", async () => {
		await expect(
			pool.query("UPDATE audit_log SET action = 'tampered' WHERE id = $1", [insertedId])
		).rejects.toThrow(/append-only/)
	})

	it("rejects DELETE with the append-only trigger", async () => {
		await expect(pool.query("DELETE FROM audit_log WHERE id = $1", [insertedId])).rejects.toThrow(
			/append-only/
		)
	})
})
