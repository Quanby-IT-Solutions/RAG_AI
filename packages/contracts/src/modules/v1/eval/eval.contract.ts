import { oc } from "@orpc/contract"
import { z } from "zod"

import {
	CreateGoldenItemRequestSchema,
	EvalRunComparisonSchema,
	EvalRunSchema,
	EvalRunSummarySchema,
	ExecuteEvalRunRequestSchema,
	GoldenChunkCandidateSchema,
	GoldenChunkSearchRequestSchema,
	GoldenItemSchema,
	GoldenSetExportSchema,
	ImportGoldenSetResponseSchema,
	UpdateGoldenItemRequestSchema,
} from "./eval.schema.js"

/**
 * Golden evaluation set management (EV-1). All routes require
 * `eval.golden.manage` (RbacGuard); every mutation is audited. Items list
 * with per-item staleness (expected hashes orphaned by re-chunking).
 */
export const evalContract = {
	golden: {
		list: oc
			.route({
				method: "GET",
				path: "/eval/golden",
				summary: "List golden items",
				description:
					"All golden items with per-item staleness: expectedChunkHashes no longer present in chunks are reported in staleHashes.",
				tags: ["Evaluation"],
			})
			.output(z.array(GoldenItemSchema)),
		create: oc
			.route({
				method: "POST",
				path: "/eval/golden",
				summary: "Add a golden item",
				tags: ["Evaluation"],
			})
			.input(CreateGoldenItemRequestSchema)
			.output(GoldenItemSchema),
		update: oc
			.route({
				method: "PUT",
				path: "/eval/golden/{id}",
				summary: "Edit or retire a golden item",
				description: "Retirement is a status flip (audited); rows are never deleted.",
				tags: ["Evaluation"],
			})
			.input(UpdateGoldenItemRequestSchema)
			.output(GoldenItemSchema),
		export: oc
			.route({
				method: "GET",
				path: "/eval/golden/export",
				summary: "Export the golden set as portable JSON",
				tags: ["Evaluation"],
			})
			.output(GoldenSetExportSchema),
		import: oc
			.route({
				method: "POST",
				path: "/eval/golden/import",
				summary: "Import golden items from portable JSON",
				description: "Appends the payload's items; existing items are untouched.",
				tags: ["Evaluation"],
			})
			.input(GoldenSetExportSchema)
			.output(ImportGoldenSetResponseSchema),
		searchChunks: oc
			.route({
				method: "POST",
				path: "/eval/golden/search-chunks",
				summary: "Run live retrieval to pick expected chunks",
				description:
					"Runs the REAL retrieval pipeline (Flow B stages) and returns candidate chunks with their content hashes for binding.",
				tags: ["Evaluation"],
			})
			.input(GoldenChunkSearchRequestSchema)
			.output(z.array(GoldenChunkCandidateSchema)),
	},
	// EV-2: retrieval metrics runs. All routes require `eval.runs.execute`.
	runs: {
		execute: oc
			.route({
				method: "POST",
				path: "/eval/runs",
				summary: "Execute a retrieval eval run",
				description:
					"Scores the REAL retrieval pipeline against the active golden set (hit rate, MRR, P@k, R@k). Fails loudly (409) on an empty or fully-stale set.",
				tags: ["Evaluation"],
			})
			.input(ExecuteEvalRunRequestSchema)
			.output(EvalRunSchema),
		list: oc
			.route({
				method: "GET",
				path: "/eval/runs",
				summary: "List eval runs (newest first)",
				tags: ["Evaluation"],
			})
			.output(z.array(EvalRunSummarySchema)),
		get: oc
			.route({
				method: "GET",
				path: "/eval/runs/{id}",
				summary: "One eval run with per-item scores",
				tags: ["Evaluation"],
			})
			.input(z.object({ id: z.string().uuid() }))
			.output(EvalRunSchema),
		compare: oc
			.route({
				method: "GET",
				path: "/eval/runs/{baseId}/compare/{candidateId}",
				summary: "Per-metric deltas between two runs",
				description: "candidate − base for every shared numeric metric.",
				tags: ["Evaluation"],
			})
			.input(z.object({ baseId: z.string().uuid(), candidateId: z.string().uuid() }))
			.output(EvalRunComparisonSchema),
	},
}
