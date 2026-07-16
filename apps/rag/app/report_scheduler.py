"""RP-6: scheduled report generation (arq cron, reuses the IN-7 worker).

Every minute the worker looks for enabled schedules whose cron expression has
fired since their last run, asks the BACKEND (the report framework owner) for
the report rows over an authenticated internal endpoint, renders each
configured format (app/report_render.py), stores the files in report_files,
and audits the run. Email delivery to `recipients` is recorded on the audit
entry; files are always stored regardless.
"""

from __future__ import annotations

import base64
import json
import logging
import time
from datetime import UTC, datetime
from typing import Any

import asyncpg
import httpx
import jwt
from croniter import croniter

from app.config import Settings
from app.report_render import CONTENT_TYPES, render_report
from app.timeouts import with_hard_timeout

logger = logging.getLogger("app.reports")

SCHEDULER_PRINCIPAL = "report-scheduler"


class ReportScheduleRepository:
    """Scheduler-owned reads/writes: schedules state + generated files."""

    def __init__(self, settings: Settings):
        self._settings = settings

    async def _connect(self) -> asyncpg.Connection:
        return await asyncpg.connect(self._settings.database_url)

    async def list_enabled(self) -> list[dict[str, Any]]:
        conn = await self._connect()
        try:
            rows = await with_hard_timeout(
                conn.fetch(
                    """
                    SELECT id, report_key, cron, filters, formats, recipients,
                           created_at, last_run_at
                    FROM report_schedules
                    WHERE enabled
                    """
                ),
                seconds=self._settings.db_timeout_seconds,
                operation="report_schedules.list",
            )
            return [
                {
                    "id": str(row["id"]),
                    "reportKey": row["report_key"],
                    "cron": row["cron"],
                    "filters": json.loads(row["filters"]),
                    "formats": json.loads(row["formats"]),
                    "recipients": json.loads(row["recipients"]),
                    "createdAt": row["created_at"],
                    "lastRunAt": row["last_run_at"],
                }
                for row in rows
            ]
        finally:
            await conn.close()

    async def store_file(
        self,
        *,
        schedule_id: str,
        report_key: str,
        format: str,
        filename: str,
        content_type: str,
        content: bytes,
    ) -> str:
        conn = await self._connect()
        try:
            row = await with_hard_timeout(
                conn.fetchrow(
                    """
                    INSERT INTO report_files
                        (schedule_id, report_key, format, filename, content_type,
                         content_base64, generated_by)
                    VALUES ($1, $2, $3, $4, $5, $6, $7)
                    RETURNING id
                    """,
                    schedule_id,
                    report_key,
                    format,
                    filename,
                    content_type,
                    base64.b64encode(content).decode("ascii"),
                    SCHEDULER_PRINCIPAL,
                ),
                seconds=self._settings.db_timeout_seconds,
                operation="report_files.insert",
            )
            return str(row["id"])
        finally:
            await conn.close()

    async def mark_ran(self, schedule_id: str, at: datetime) -> None:
        conn = await self._connect()
        try:
            await with_hard_timeout(
                conn.execute(
                    "UPDATE report_schedules SET last_run_at = $2 WHERE id = $1",
                    schedule_id,
                    at,
                ),
                seconds=self._settings.db_timeout_seconds,
                operation="report_schedules.mark_ran",
            )
        finally:
            await conn.close()


def is_due(schedule: dict[str, Any], now: datetime) -> bool:
    """Due when the cron expression has a fire time after the last run (or
    creation, for never-run schedules) that is not in the future."""
    anchor = schedule["lastRunAt"] or schedule["createdAt"]
    try:
        next_fire = croniter(schedule["cron"], anchor).get_next(datetime)
    except (ValueError, KeyError):
        logger.warning(
            "reports.schedule_invalid_cron %s",
            {"id": schedule["id"], "cron": schedule["cron"]},
        )
        return False
    return next_fire <= now


def sign_scheduler_jwt(settings: Settings) -> str:
    """HS256 service JWT the backend's internal report endpoint verifies —
    same shared secret as the backend->rag direction, reversed."""
    now = int(time.time())
    return jwt.encode(
        {
            "sub": SCHEDULER_PRINCIPAL,
            "actorType": "service",
            "iat": now,
            "exp": now + 120,
        },
        settings.rag_service_token_secret.get_secret_value(),
        algorithm="HS256",
    )


async def fetch_report_rows(
    settings: Settings, report_key: str, filters: dict[str, str]
) -> dict[str, Any]:
    """Ask the backend (framework owner) to run the report — the queries and
    their RBAC stay in one place; the worker never re-implements a report."""
    url = f"{settings.backend_internal_url.rstrip('/')}/v1/reports/internal-run"
    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.post(
            url,
            json={"key": report_key, "filters": filters},
            headers={"Authorization": f"Bearer {sign_scheduler_jwt(settings)}"},
        )
        response.raise_for_status()
        return response.json()


async def process_report_schedules(ctx: dict[str, Any]) -> dict[str, Any]:
    """arq cron task (every minute): generate files for every due schedule."""
    settings: Settings = ctx["settings"]
    repository: ReportScheduleRepository = ctx.get(
        "report_schedule_repository"
    ) or ReportScheduleRepository(settings)
    now = datetime.now(UTC)

    generated: list[dict[str, Any]] = []
    for schedule in await repository.list_enabled():
        if not is_due(schedule, now):
            continue
        try:
            report = await fetch_report_rows(
                settings, schedule["reportKey"], schedule["filters"]
            )
            stamp = now.strftime("%Y-%m-%d-%H%M")
            file_ids = []
            for format in schedule["formats"]:
                content = render_report(
                    title=report["title"],
                    columns=report["columns"],
                    rows=report["rows"],
                    format=format,
                )
                file_id = await repository.store_file(
                    schedule_id=schedule["id"],
                    report_key=schedule["reportKey"],
                    format=format,
                    filename=f"{schedule['reportKey']}-{stamp}.{format}",
                    content_type=CONTENT_TYPES[format],
                    content=content,
                )
                file_ids.append(file_id)
            await repository.mark_ran(schedule["id"], now)

            from app.conversations import ConversationsRepository

            await ConversationsRepository(settings).write_audit(
                actor_id=SCHEDULER_PRINCIPAL,
                actor_type="service",
                action="reports.schedule.run",
                resource_type="report_schedule",
                resource_id=schedule["id"],
                before=None,
                after={
                    "reportKey": schedule["reportKey"],
                    "formats": schedule["formats"],
                    "fileIds": file_ids,
                    "rows": len(report["rows"]),
                    "recipients": schedule["recipients"],
                },
            )
            generated.append({"scheduleId": schedule["id"], "files": file_ids})
            logger.info(
                "reports.schedule_ran %s",
                {"id": schedule["id"], "files": len(file_ids)},
            )
        except Exception:
            logger.exception(
                "reports.schedule_failed %s", {"id": schedule["id"]}
            )
    return {"generated": generated}
