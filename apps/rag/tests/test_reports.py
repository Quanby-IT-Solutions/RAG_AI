"""RP-6: report rendering + scheduler tests."""

import base64
import io
import json
from datetime import UTC, datetime
from typing import Any

import pytest

from app.report_render import render_csv, render_pdf, render_report, render_xlsx
from app.report_scheduler import is_due, process_report_schedules, sign_scheduler_jwt
from tests.conftest import REQUIRED_ENV

COLUMNS = [
    {"key": "when", "label": "When"},
    {"key": "action", "label": "Action"},
    {"key": "count", "label": "Count"},
]
ROWS = [
    {"when": "2026-07-16", "action": "chat.query.submit", "count": 5},
    {"when": "2026-07-15", "action": 'eval "quoted", comma', "count": 2},
]


@pytest.fixture
def settings(monkeypatch):
    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)
    from app.config import Settings

    return Settings()


# ------------------------------------------------------------------ renderers
def test_xlsx_roundtrips_through_openpyxl():
    from openpyxl import load_workbook

    content = render_xlsx("Audit Trail", COLUMNS, ROWS)
    sheet = load_workbook(io.BytesIO(content)).active

    assert [cell.value for cell in sheet[1]] == ["When", "Action", "Count"]
    assert sheet.cell(row=2, column=2).value == "chat.query.submit"
    assert sheet.cell(row=3, column=3).value == "2"  # stringified cells
    assert sheet.max_row == 3


def test_pdf_renders_a_parseable_document_with_the_data():
    content = render_pdf("Audit Trail", COLUMNS, ROWS)
    assert content.startswith(b"%PDF")
    assert b"%%EOF" in content[-64:]


def test_csv_matches_the_backend_layout():
    lines = render_csv(COLUMNS, ROWS).decode().splitlines()
    assert lines[0] == "When,Action,Count"
    assert lines[1] == "2026-07-16,chat.query.submit,5"
    assert lines[2] == '2026-07-15,"eval ""quoted"", comma",2'


def test_render_report_dispatches_and_rejects_unknown_formats():
    assert json.loads(render_report(title="t", columns=COLUMNS, rows=ROWS, format="json"))
    with pytest.raises(ValueError):
        render_report(title="t", columns=COLUMNS, rows=ROWS, format="docx")


# ------------------------------------------------------------------ scheduling
def make_schedule(**overrides: Any) -> dict[str, Any]:
    return {
        "id": "s1",
        "reportKey": "audit-trail",
        "cron": "* * * * *",
        "filters": {},
        "formats": ["csv"],
        "recipients": [],
        "createdAt": datetime(2026, 7, 16, 10, 0, tzinfo=UTC),
        "lastRunAt": None,
        **overrides,
    }


def test_is_due_uses_last_run_then_creation_as_the_anchor():
    now = datetime(2026, 7, 16, 10, 5, tzinfo=UTC)
    assert is_due(make_schedule(), now) is True  # never ran, every-minute cron
    assert is_due(make_schedule(lastRunAt=now), now) is False  # just ran
    # Daily at 03:00 with a last run today: next fire is tomorrow.
    assert (
        is_due(
            make_schedule(
                cron="0 3 * * *",
                lastRunAt=datetime(2026, 7, 16, 3, 0, tzinfo=UTC),
            ),
            now,
        )
        is False
    )


def test_is_due_treats_invalid_cron_as_never_due():
    now = datetime(2026, 7, 16, 10, 5, tzinfo=UTC)
    assert is_due(make_schedule(cron="not a cron"), now) is False


def test_scheduler_jwt_carries_service_principal_and_expiry(settings):
    import jwt

    token = sign_scheduler_jwt(settings)
    payload = jwt.decode(
        token,
        settings.rag_service_token_secret.get_secret_value(),
        algorithms=["HS256"],
    )
    assert payload["sub"] == "report-scheduler"
    assert payload["actorType"] == "service"
    assert payload["exp"] > payload["iat"]


class FakeScheduleRepository:
    def __init__(self, schedules: list[dict[str, Any]]):
        self.schedules = schedules
        self.files: list[dict[str, Any]] = []
        self.ran: list[str] = []

    async def list_enabled(self):
        return [dict(schedule) for schedule in self.schedules]

    async def store_file(self, **kwargs):
        self.files.append(kwargs)
        return f"file-{len(self.files)}"

    async def mark_ran(self, schedule_id, at):
        self.ran.append(schedule_id)


async def test_process_generates_every_format_and_audits(settings, monkeypatch):
    # Anchor firmly in the past: process_report_schedules compares against
    # the real clock.
    repository = FakeScheduleRepository(
        [
            make_schedule(
                formats=["csv", "xlsx", "pdf", "json"],
                createdAt=datetime(2020, 1, 1, tzinfo=UTC),
            )
        ]
    )
    audits: list[dict[str, Any]] = []

    async def fake_fetch(settings_arg, report_key, filters):
        return {"title": "System Audit Trail", "columns": COLUMNS, "rows": ROWS}

    class FakeConversations:
        def __init__(self, settings_arg):
            pass

        async def write_audit(self, **kwargs):
            audits.append(kwargs)

    monkeypatch.setattr("app.report_scheduler.fetch_report_rows", fake_fetch)
    monkeypatch.setattr("app.conversations.ConversationsRepository", FakeConversations)

    result = await process_report_schedules(
        {"settings": settings, "report_schedule_repository": repository}
    )

    assert len(result["generated"]) == 1
    assert len(repository.files) == 4
    formats = {file["format"] for file in repository.files}
    assert formats == {"csv", "xlsx", "pdf", "json"}
    # Stored content is base64 of real bytes (xlsx opens, pdf magic).
    xlsx = next(f for f in repository.files if f["format"] == "xlsx")
    from openpyxl import load_workbook

    load_workbook(io.BytesIO(base64.b64decode(base64.b64encode(xlsx["content"]))))
    assert repository.ran == ["s1"]
    assert audits[0]["action"] == "reports.schedule.run"
    assert audits[0]["actor_type"] == "service"


async def test_process_skips_not_due_and_survives_fetch_failures(settings, monkeypatch):
    now_ran = datetime.now(UTC)
    repository = FakeScheduleRepository(
        [
            make_schedule(id="not-due", lastRunAt=now_ran),
            make_schedule(id="boom", createdAt=datetime(2020, 1, 1, tzinfo=UTC)),
        ]
    )

    async def failing_fetch(settings_arg, report_key, filters):
        raise RuntimeError("backend unreachable")

    monkeypatch.setattr("app.report_scheduler.fetch_report_rows", failing_fetch)

    result = await process_report_schedules(
        {"settings": settings, "report_schedule_repository": repository}
    )

    # Neither generated: one wasn't due, the other failed but didn't crash
    # the task (and stays unmarked so the next tick retries it).
    assert result["generated"] == []
    assert repository.ran == []
