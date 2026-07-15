import { oc } from "@orpc/contract"

import { ChatAnswerSchema, ChatQueryInputSchema } from "./chat.schema.js"

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
}
