import { Injectable } from "@nestjs/common"
import { ORPCError } from "@orpc/server"
import { desc, eq } from "drizzle-orm"

import type { EvalRun, EvalRunComparison, EvalRunSummary } from "@repo/contracts"
import { evalRuns } from "@repo/db/schema"

import { db } from "@/common/database/database.client"

type EvalRunRow = typeof evalRuns.$inferSelect

/**
 * Eval-run reads (EV-2). READ-ONLY over the evaluation-owned `eval_runs`
 * table — the Python runner owns writes (it executes against the real
 * retrieval pipeline). Comparison produces the per-metric deltas the EV-4
 * gate and IN-8 verification consume.
 */
@Injectable()
export class EvalRunsService {
	private toRun(row: EvalRunRow): EvalRun {
		return {
			id: row.id,
			kind: row.kind,
			status: row.status,
			gitSha: row.gitSha,
			config: row.config,
			metrics: row.metrics ?? null,
			perItem: (row.perItem as EvalRun["perItem"]) ?? null,
			error: row.error,
			triggeredBy: row.triggeredBy,
			createdAt: row.createdAt,
			finishedAt: row.finishedAt,
		}
	}

	private toSummary(row: EvalRunRow): EvalRunSummary {
		const run = this.toRun(row)
		delete (run as { perItem?: unknown }).perItem
		return run
	}

	async list(): Promise<EvalRunSummary[]> {
		const rows = await db.select().from(evalRuns).orderBy(desc(evalRuns.createdAt)).limit(100)
		return rows.map(row => this.toSummary(row))
	}

	async get(id: string): Promise<EvalRun> {
		const [row] = await db.select().from(evalRuns).where(eq(evalRuns.id, id))
		if (!row) {
			throw new ORPCError("NOT_FOUND", { message: `Eval run ${id} not found` })
		}
		return this.toRun(row)
	}

	/** candidate − base for every numeric metric present in BOTH runs. */
	async compare(baseId: string, candidateId: string): Promise<EvalRunComparison> {
		const base = await this.get(baseId)
		const candidate = await this.get(candidateId)

		const deltas: Record<string, number> = {}
		for (const [metric, candidateValue] of Object.entries(candidate.metrics ?? {})) {
			const baseValue = base.metrics?.[metric]
			if (typeof baseValue === "number") {
				deltas[metric] = candidateValue - baseValue
			}
		}

		delete (base as { perItem?: unknown }).perItem
		delete (candidate as { perItem?: unknown }).perItem
		return { base, candidate, deltas }
	}
}
