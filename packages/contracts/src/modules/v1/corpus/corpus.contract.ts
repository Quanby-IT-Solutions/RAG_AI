import { oc } from "@orpc/contract"
import { z } from "zod"

import {
	ChunkInspectionSchema,
	DocumentSchema,
	IngestionJobDetailSchema,
	IngestionJobSchema,
} from "./corpus.schema.js"

/**
 * Corpus contract (ingestion epic).
 *
 * JSON surface only: the binary document upload (POST /corpus/documents) is a
 * plain NestJS Multer route (Tech Plan §5, D2) and intentionally NOT part of
 * this oRPC contract. Listing requires `corpus.documents.read`; the jobs
 * console (IN-7) requires `corpus.ingest.run` (RbacGuard).
 */
export const corpusContract = {
	documents: {
		list: oc
			.route({
				method: "GET",
				path: "/corpus/documents",
				summary: "List corpus documents",
				description:
					"List ingested documents with status (registered/processing/indexed/failed), content hash, and normalization metadata.",
				tags: ["Corpus"],
			})
			.output(z.array(DocumentSchema)),
		inspectChunks: oc
			.route({
				method: "GET",
				path: "/corpus/documents/{id}/chunks",
				summary: "Inspect a document's chunk boundaries",
				description:
					"IN-5: chunks in boundary order with mid-sentence heuristics and overlap regions, so bad splits are catchable before they poison retrieval. Requires corpus.chunks.inspect.",
				tags: ["Corpus"],
			})
			.input(z.object({ id: z.string().uuid() }))
			.output(ChunkInspectionSchema),
	},
	jobs: {
		list: oc
			.route({
				method: "GET",
				path: "/corpus/jobs",
				summary: "List ingestion jobs",
				description:
					"List background ingestion jobs (IN-7) newest first with lifecycle status and run stats.",
				tags: ["Corpus"],
			})
			.output(z.array(IngestionJobSchema)),
		get: oc
			.route({
				method: "GET",
				path: "/corpus/jobs/{id}",
				summary: "Get one ingestion job",
				description:
					"Job detail including per-document outcomes (status, chunk count, failure reason).",
				tags: ["Corpus"],
			})
			.input(z.object({ id: z.string().uuid() }))
			.output(IngestionJobDetailSchema),
		rerun: oc
			.route({
				method: "POST",
				path: "/corpus/jobs/{id}/rerun",
				summary: "Re-run a finished ingestion job",
				description:
					"Requeue a completed/failed job. Safe by construction: the pipeline is upsert-idempotent (Flow A).",
				tags: ["Corpus"],
			})
			.input(z.object({ id: z.string().uuid() }))
			.output(z.object({ id: z.string(), status: z.literal("queued") })),
	},
}
