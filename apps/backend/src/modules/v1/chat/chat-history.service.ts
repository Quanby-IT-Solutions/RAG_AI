import { Injectable } from "@nestjs/common"
import { ORPCError } from "@orpc/server"
import { asc, desc, eq, sql } from "drizzle-orm"

import type { ChatCitation } from "@repo/contracts"
import { conversations, messages } from "@repo/db/schema"

import { AuditService } from "@/common/audit/audit.service"
import { db } from "@/common/database/database.client"

/**
 * Conversation history reads (CH-3). READ-ONLY over the generation-owned
 * `conversations`/`messages` tables (cross-epic rule: generation owns writes).
 * Own-scope is enforced here: a caller only ever sees their own
 * conversations, and touching someone else's returns 403 with the denial on
 * the append-only audit spine (Flow E).
 */
@Injectable()
export class ChatHistoryService {
	constructor(private readonly auditService: AuditService) {}

	/** Newest-first list of the CALLER'S conversations with derived titles. */
	async listConversations(userId: string) {
		const rows = await db
			.select({
				id: conversations.id,
				createdAt: conversations.createdAt,
				messageCount: sql<number>`count(${messages.id})::int`,
				lastMessageAt: sql<Date | null>`max(${messages.createdAt})`,
				// No title column: derive from the first user message.
				title: sql<string>`coalesce(min(${messages.content}) filter (where ${messages.role} = 'user' and ${messages.turnIndex} = 0), 'Conversation')`,
			})
			.from(conversations)
			.leftJoin(messages, eq(messages.conversationId, conversations.id))
			.where(eq(conversations.userId, userId))
			.groupBy(conversations.id)
			.orderBy(desc(conversations.createdAt))
			.limit(100)

		return rows.map(row => ({
			...row,
			title: row.title.length > 80 ? `${row.title.slice(0, 77)}...` : row.title,
		}))
	}

	/** Full thread with citations intact; own-scope enforced + denial audited. */
	async getConversation(userId: string, conversationId: string) {
		const [conversation] = await db
			.select()
			.from(conversations)
			.where(eq(conversations.id, conversationId))

		if (!conversation) {
			throw new ORPCError("NOT_FOUND", { message: `Conversation ${conversationId} not found` })
		}
		if (conversation.userId !== userId) {
			// CH-3 AC: user A requesting user B's conversation -> 403 + audited.
			await this.auditService.writeAudit({
				actorId: userId,
				actorType: "user",
				action: "chat.history.denied",
				resourceType: "conversation",
				resourceId: conversationId,
				after: { reason: "not-owner" },
			})
			throw new ORPCError("FORBIDDEN", {
				message: "You do not have access to that conversation",
			})
		}

		const rows = await db
			.select({
				id: messages.id,
				role: messages.role,
				content: messages.content,
				citations: messages.citations,
				turnIndex: messages.turnIndex,
				createdAt: messages.createdAt,
			})
			.from(messages)
			.where(eq(messages.conversationId, conversationId))
			.orderBy(asc(messages.turnIndex))

		return {
			id: conversation.id,
			createdAt: conversation.createdAt,
			messages: rows.map(row => ({
				...row,
				citations: (row.citations as ChatCitation[] | null) ?? null,
			})),
		}
	}
}
