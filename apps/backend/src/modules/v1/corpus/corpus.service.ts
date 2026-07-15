import { Injectable } from "@nestjs/common"
import { desc } from "drizzle-orm"

import type { CorpusDocumentMetadata } from "@repo/contracts"
import { documents } from "@repo/db/schema"

import { db } from "@/common/database/database.client"

@Injectable()
export class CorpusService {
	/**
	 * Corpus list (Flow A step 4): newest first. The `text` payload the Python
	 * service stores inside `metadata` is stripped — the list screen only needs
	 * filename/type/status/hash/timestamp/error, not megabytes of normalized
	 * text per row.
	 */
	async listDocuments() {
		const rows = await db.select().from(documents).orderBy(desc(documents.ingestedAt))

		return rows.map(row => {
			const metadata =
				row.metadata && typeof row.metadata === "object"
					? (Object.fromEntries(
							Object.entries(row.metadata as Record<string, unknown>).filter(
								([key]) => key !== "text" && key !== "pages"
							)
						) as CorpusDocumentMetadata)
					: null

			return { ...row, metadata }
		})
	}
}
