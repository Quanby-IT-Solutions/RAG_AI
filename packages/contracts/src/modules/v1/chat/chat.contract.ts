import { oc } from "@orpc/contract"
import { z } from "zod"

import {
	ChatAnswerSchema,
	ChatQueryInputSchema,
	ConversationDetailSchema,
	ConversationSummarySchema,
} from "./chat.schema.js"

/**
 * Chat contract (generation epic, GN-1).
 *
 * JSON, non-streamed (Tech Plan §5, D3 — streaming/SSE is GN-3). Submitting a
 * query requires `chat.query.submit` (RbacGuard fails closed and audits
 * denials); the gateway forwards to the Python service's /internal/chat with
 * the HS256 service JWT.
 */
export const chatContract = {
	query: {
		submit: oc
			.route({
				method: "POST",
				path: "/chat/query",
				summary: "Submit a chat query",
				description:
					"Ask a question over the indexed corpus. Returns a grounded, cited answer — or a grounded \"I don't know\" when the corpus can't support one. Single-turn in R1: each request starts a new conversation.",
				tags: ["Chat"],
			})
			.input(ChatQueryInputSchema)
			.output(ChatAnswerSchema),
	},
	history: {
		list: oc
			.route({
				method: "GET",
				path: "/chat/history",
				summary: "List my conversations",
				description:
					"Conversations owned by the caller, newest first (CH-3). Requires chat.history.read (own-scope).",
				tags: ["Chat"],
			})
			.output(z.array(ConversationSummarySchema)),
		get: oc
			.route({
				method: "GET",
				path: "/chat/history/{id}",
				summary: "Get one of my conversations",
				description:
					"Full message thread with citations intact. Another user's conversation returns 403 and the denial is audited.",
				tags: ["Chat"],
			})
			.input(z.object({ id: z.string().uuid() }))
			.output(ConversationDetailSchema),
	},
}
