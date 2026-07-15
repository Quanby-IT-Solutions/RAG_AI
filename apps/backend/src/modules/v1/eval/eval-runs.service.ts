import { Injectable } from "@nestjs/common"
import { ORPCError } from "@orpc/server"
import { desc, eq } from "drizzle-orm"

import type {
	EvalBaseline,
	EvalRun,
	EvalRunComparison,
	EvalRunSummary,
} from "@repo/contracts"
import { evalBaselines, evalRuns } from "@repo/db/schema"

import { AuditService } from "@/common/audit/audit.service"
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
	constructor(private readonly auditService: AuditService) {}

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

	/**
	 * EV-4 (Flow F): promote a COMPLETED run to the gate baseline. The
	 * baseline only ever changes through this explicit, audited action —
	 * runs themselves never touch eval_baselines.
	 */
	async promote(userId: string, runId: string, note?: string): Promise<EvalBaseline> {
		const run = await this.get(runId)
		if (run.status !== "completed") {
			throw new ORPCError("CONFLICT", {
				message: `Only completed runs can become the baseline (run is ${run.status})`,
			})
		}
		const previous = await this.currentBaseline()

		const [row] = await db
			.insert(evalBaselines)
			.values({ runId, promotedBy: userId, note: note ?? null })
			.returning()
		if (!row) {
			throw new ORPCError("INTERNAL_SERVER_ERROR", { message: "Insert returned no row" })
		}

		await this.auditService.writeAudit({
			actorId: userId,
			actorType: "user",
			action: "eval.gate.promote",
			resourceType: "eval_baseline",
			resourceId: row.id,
			before: previous ? { runId: previous.runId, gitSha: previous.gitSha } : null,
			after: { runId, gitSha: run.gitSha, note: note ?? null },
		})

		return {
			id: row.id,
			runId,
			promotedBy: row.promotedBy,
			note: row.note,
			createdAt: row.createdAt,
			metrics: run.metrics,
			gitSha: run.gitSha,
		}
	}

	/** Newest promotion wins; null when no baseline has been approved yet. */
	async currentBaseline(): Promise<EvalBaseline | null> {
		const [row] = await db
			.select({
				id: evalBaselines.id,
				runId: evalBaselines.runId,
				promotedBy: evalBaselines.promotedBy,
				note: evalBaselines.note,
				createdAt: evalBaselines.createdAt,
				metrics: evalRuns.metrics,
				gitSha: evalRuns.gitSha,
			})
			.from(evalBaselines)
			.innerJoin(evalRuns, eq(evalRuns.id, evalBaselines.runId))
			.orderBy(desc(evalBaselines.createdAt))
			.limit(1)
		if (!row) {
			return null
		}
		return { ...row, metrics: row.metrics ?? null }
	}
}
