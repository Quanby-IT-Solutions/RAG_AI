import { oc } from "@orpc/contract"
import { z } from "zod"

import {
	CreateGoldenItemRequestSchema,
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
}
