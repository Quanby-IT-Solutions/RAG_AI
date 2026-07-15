"""Conversations + messages persistence (Flow B / GN-1) — asyncpg over the
Drizzle-owned schema.

Python runs NO migrations: `conversations` and `messages` are created by the
Drizzle migration chain (packages/db). The `generation` epic OWNS writes to
both tables (DOMAIN-MODEL cross-epic rule); `documents` is only READ here (to
resolve citation sources). Every query goes through the shared hard-timeout
helper. R1 is single-turn: one /chat request persists one NEW conversation
plus its user/assistant message pair atomically.
"""

import json
from typing import Any

import asyncpg

from app.config import Settings
from app.documents import _AUDIT_INSERT_SQL
from app.timeouts import with_hard_timeout

_INSERT_CONVERSATION_SQL = """
INSERT INTO conversations (user_id) VALUES ($1)
RETURNING id
"""

_INSERT_MESSAGE_SQL = """
INSERT INTO messages
    (conversation_id, role, content, citations, retrieved_chunks,
     token_usage, trace_id, model_used, turn_index)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
RETURNING id
"""

_SELECT_DOCUMENT_SOURCES_SQL = """
SELECT id, source_uri FROM documents WHERE id = ANY($1::uuid[])
"""


class ConversationsRepository:
    """INSERT-only access to `conversations`/`messages`, plus the read-only
    document-source lookup for citations and append-only audit writes."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def _connect(self) -> asyncpg.Connection:
        return await with_hard_timeout(
            asyncpg.connect(dsn=self._settings.database_url),
            seconds=self._settings.db_timeout_seconds,
            operation="postgres.connect",
        )

    async def fetch_document_sources(self, document_ids: list[str]) -> dict[str, str]:
        """Resolve document ids -> source_uri for citation building (read-only
        over the ingestion-owned `documents` table)."""
        if not document_ids:
            return {}
        conn = await self._connect()
        try:
            rows = await with_hard_timeout(
                conn.fetch(_SELECT_DOCUMENT_SOURCES_SQL, document_ids),
                seconds=self._settings.db_timeout_seconds,
                operation="documents.fetch_sources",
            )
        finally:
            await conn.close()
        return {str(row["id"]): row["source_uri"] for row in rows}

    async def persist_turn(
        self,
        *,
        user_id: str,
        query: str,
        answer: str,
        citations: list[dict[str, Any]],
        retrieved_chunks: list[dict[str, Any]],
        token_usage: dict[str, Any] | None,
        trace_id: str,
        model_used: str,
    ) -> tuple[str, str]:
        """Create the NEW conversation (single-turn R1) and write the user +
        assistant message rows in ONE transaction — a partial turn is never
        persisted. Returns (conversation_id, assistant_message_id)."""
        conn = await self._connect()
        try:
            async with conn.transaction():
                conversation_id = await with_hard_timeout(
                    conn.fetchval(_INSERT_CONVERSATION_SQL, user_id),
                    seconds=self._settings.db_timeout_seconds,
                    operation="conversations.insert",
                )
                await with_hard_timeout(
                    conn.fetchval(
                        _INSERT_MESSAGE_SQL,
                        conversation_id,
                        "user",
                        query,
                        None,
                        None,
                        None,
                        trace_id,
                        None,
                        0,
                    ),
                    seconds=self._settings.db_timeout_seconds,
                    operation="messages.insert_user",
                )
                message_id = await with_hard_timeout(
                    conn.fetchval(
                        _INSERT_MESSAGE_SQL,
                        conversation_id,
                        "assistant",
                        answer,
                        json.dumps(citations),
                        json.dumps(retrieved_chunks),
                        json.dumps(token_usage) if token_usage is not None else None,
                        trace_id,
                        model_used,
                        1,
                    ),
                    seconds=self._settings.db_timeout_seconds,
                    operation="messages.insert_assistant",
                )
        finally:
            await conn.close()
        return str(conversation_id), str(message_id)

    async def write_audit(
        self,
        *,
        actor_id: str,
        actor_type: str,
        action: str,
        resource_type: str,
        resource_id: str,
        before: dict[str, Any] | None,
        after: dict[str, Any] | None,
    ) -> None:
        """Append one entry to the shared audit_log spine (INSERT-only; the DB
        trigger rejects UPDATE/DELETE)."""
        conn = await self._connect()
        try:
            await with_hard_timeout(
                conn.execute(
                    _AUDIT_INSERT_SQL,
                    actor_id,
                    actor_type,
                    action,
                    resource_type,
                    resource_id,
                    json.dumps(before) if before is not None else None,
                    json.dumps(after) if after is not None else None,
                ),
                seconds=self._settings.db_timeout_seconds,
                operation="audit_log.insert",
            )
        finally:
            await conn.close()
