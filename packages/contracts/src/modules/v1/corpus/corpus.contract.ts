import { oc } from "@orpc/contract"
import { z } from "zod"

import { DocumentSchema } from "./corpus.schema.js"

/**
 * Corpus contract (ingestion epic).
 *
 * JSON surface only: the binary PDF upload (POST /corpus/documents) is a plain
 * NestJS Multer route (Tech Plan §5, D2) and intentionally NOT part of this
 * oRPC contract. Listing requires `corpus.documents.read` (RbacGuard).
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
	},
}
