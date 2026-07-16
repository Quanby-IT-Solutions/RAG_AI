"""RP-6: Excel/PDF report rendering.

The reports FRAMEWORK (definitions, queries, RBAC) lives in the backend; this
module only turns already-computed {title, columns, rows} into files. The
backend calls /internal/reports/render for on-demand Excel/PDF exports, and
the arq scheduler (app/report_scheduler.py) calls the render functions
directly for scheduled generation.
"""

from __future__ import annotations

import base64
import csv
import io
import json
import logging
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.security import verify_service_jwt

logger = logging.getLogger("app.reports")

XLSX_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
PDF_CONTENT_TYPE = "application/pdf"

CONTENT_TYPES = {
    "json": "application/json",
    "csv": "text/csv",
    "xlsx": XLSX_CONTENT_TYPE,
    "pdf": PDF_CONTENT_TYPE,
}


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value)
    return str(value)


def render_json(rows: list[dict[str, Any]]) -> bytes:
    return json.dumps(rows, indent=2, default=str).encode("utf-8")


def render_csv(columns: list[dict[str, str]], rows: list[dict[str, Any]]) -> bytes:
    """Same layout as the backend's on-demand CSV: label header, column order."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow([column["label"] for column in columns])
    for row in rows:
        writer.writerow([_cell(row.get(column["key"])) for column in columns])
    return buffer.getvalue().encode("utf-8")


def render_xlsx(title: str, columns: list[dict[str, str]], rows: list[dict[str, Any]]) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Font

    workbook = Workbook()
    sheet = workbook.active
    # Excel sheet titles cap at 31 chars and reject a few characters.
    sheet.title = "".join(c for c in title if c not in r"[]:*?/\\")[:31] or "Report"
    sheet.append([column["label"] for column in columns])
    for cell in sheet[1]:
        cell.font = Font(bold=True)
    for row in rows:
        sheet.append([_cell(row.get(column["key"])) for column in columns])
    sheet.freeze_panes = "A2"
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def render_pdf(title: str, columns: list[dict[str, str]], rows: list[dict[str, Any]]) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    styles = getSampleStyleSheet()
    cell_style = styles["BodyText"]
    cell_style.fontSize = 7
    cell_style.leading = 9

    header = [Paragraph(f"<b>{column['label']}</b>", cell_style) for column in columns]
    body = [
        [Paragraph(_cell(row.get(column["key"]))[:300], cell_style) for column in columns]
        for row in rows
    ]

    buffer = io.BytesIO()
    document = SimpleDocTemplate(
        buffer, pagesize=landscape(A4), leftMargin=10 * mm, rightMargin=10 * mm
    )
    table = Table([header, *body], repeatRows=1)
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e8e8e8")),
                ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ]
        )
    )
    document.build([Paragraph(title, styles["Title"]), Spacer(1, 4 * mm), table])
    return buffer.getvalue()


def render_report(
    *, title: str, columns: list[dict[str, str]], rows: list[dict[str, Any]], format: str
) -> bytes:
    if format == "json":
        return render_json(rows)
    if format == "csv":
        return render_csv(columns, rows)
    if format == "xlsx":
        return render_xlsx(title, columns, rows)
    if format == "pdf":
        return render_pdf(title, columns, rows)
    raise ValueError(f"Unsupported report format: {format}")


# ---------------------------------------------------------------- HTTP surface
router = APIRouter(prefix="/internal", dependencies=[Depends(verify_service_jwt)])


class RenderRequest(BaseModel):
    title: str
    columns: list[dict[str, str]]
    rows: list[dict[str, Any]]
    format: str = Field(pattern="^(json|csv|xlsx|pdf)$")


@router.post("/reports/render")
async def render_endpoint(payload: RenderRequest) -> dict[str, str]:
    content = render_report(
        title=payload.title,
        columns=payload.columns,
        rows=payload.rows,
        format=payload.format,
    )
    logger.info(
        "reports.render %s",
        {"format": payload.format, "rows": len(payload.rows), "bytes": len(content)},
    )
    return {
        "contentBase64": base64.b64encode(content).decode("ascii"),
        "contentType": CONTENT_TYPES[payload.format],
    }
