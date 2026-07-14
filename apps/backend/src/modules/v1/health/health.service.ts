import { Injectable } from "@nestjs/common"
import { sql } from "drizzle-orm"

import { db } from "@/common/database/database.client"
import type { V1Outputs } from "@/config/contract-types"
import { env } from "@/config/env.config"

type HealthCheck = V1Outputs["health"]["check"]

@Injectable()
export class HealthService {
	async check(): Promise<HealthCheck> {
		// System information
		const now = new Date()
		const uptime = Number(process.uptime().toFixed(3))
		const version = process.env.npm_package_version ?? "0.0.0"
		const environment = env.NODE_ENV

		// Health checks
		const database = await this.checkDatabase()

		const status = database.status === "up" ? "ok" : "error"

		return {
			status,
			timestamp: now.toISOString(),
			uptime,
			version,
			environment,
			checks: {
				database,
			},
		}
	}

	private async checkDatabase(): Promise<HealthCheck["checks"]["database"]> {
		try {
			await db.execute(sql`select 1`)
			return { status: "up" }
		} catch (error: unknown) {
			return {
				status: "down",
				message: (error as Error)?.message ?? "database check failed",
			}
		}
	}
}
