"""Loader-stage tests (IN-3): every supported format normalizes to the same
{text, source, metadata} shape, CSV preserves row/column structure with
per-row-group section metadata, and MIME resolution covers aliases/parameters
while rejecting unknown types."""

import io
import zipfile

from app.loaders import (
    CSV_ROW_GROUP_SIZE,
    SEGMENT_SEPARATOR,
    normalize_csv,
    normalize_docx,
    normalize_html,
    resolve_loader,
    supported_mime_types,
)

SOURCE = "upload://sample"


def make_docx_bytes(text: str) -> bytes:
    """Minimal OOXML container docx2txt can read (document.xml only)."""
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body></w:document>"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="xml" ContentType="application/xml"/></Types>',
        )
        archive.writestr("word/document.xml", document)
    return buffer.getvalue()


# ------------------------------------------------------------------ resolve
def test_resolve_loader_covers_all_supported_types():
    assert resolve_loader("application/pdf").doc_type == "pdf"
    assert resolve_loader("text/html").doc_type == "html"
    assert (
        resolve_loader(
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        ).doc_type
        == "docx"
    )
    assert resolve_loader("text/csv").doc_type == "csv"
    assert resolve_loader("application/csv").doc_type == "csv"


def test_resolve_loader_strips_parameters_and_normalizes_case():
    assert resolve_loader("text/csv; charset=utf-8").doc_type == "csv"
    assert resolve_loader("Text/HTML").doc_type == "html"


def test_resolve_loader_rejects_unknown_types():
    assert resolve_loader("text/plain") is None
    assert resolve_loader("application/zip") is None
    assert resolve_loader(None) is None
    assert resolve_loader("") is None


def test_supported_mime_types_lists_every_alias():
    assert set(supported_mime_types()) == {
        "application/pdf",
        "text/html",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "text/csv",
        "application/csv",
    }


# --------------------------------------------------------------------- html
def test_html_strips_scripts_styles_and_keeps_title():
    raw = (
        b"<html><head><title> Policy Manual </title><style>body{color:red}</style></head>"
        b"<body><h1>Leave policy</h1><script>alert(1)</script><p>Ten days.</p></body></html>"
    )
    result = normalize_html(raw, "policy.html", SOURCE)

    assert result["source"] == SOURCE
    assert result["originalFilename"] == "policy.html"
    assert result["title"] == "Policy Manual"
    assert "Leave policy" in result["text"]
    assert "Ten days." in result["text"]
    assert "alert(1)" not in result["text"]
    assert "color:red" not in result["text"]
    assert result["ingestedAt"]


def test_html_without_title_omits_title_key():
    result = normalize_html(b"<p>plain</p>", "x.html", SOURCE)
    assert "title" not in result
    assert result["text"] == "plain"


# --------------------------------------------------------------------- docx
def test_docx_extracts_paragraph_text():
    raw = make_docx_bytes("Quarterly revenue grew twelve percent.")
    result = normalize_docx(raw, "report.docx", SOURCE)

    assert result["text"] == "Quarterly revenue grew twelve percent."
    assert result["source"] == SOURCE
    assert result["originalFilename"] == "report.docx"


def test_docx_rejects_non_zip_bytes():
    try:
        normalize_docx(b"not a docx", "bad.docx", SOURCE)
    except Exception:
        return
    raise AssertionError("corrupt DOCX must raise so the document is marked failed")


# ---------------------------------------------------------------------- csv
def test_csv_preserves_row_column_structure():
    raw = b"name,role\nAda,Engineer\nGrace,Admiral\n"
    result = normalize_csv(raw, "people.csv", SOURCE)

    assert result["columns"] == ["name", "role"]
    assert result["rowCount"] == 2
    assert result["text"] == "name: Ada | role: Engineer\nname: Grace | role: Admiral"
    assert result["pages"] == [
        {"section": "rows 1-2", "chars": len(result["text"])},
    ]


def test_csv_groups_rows_into_sections():
    rows = "\n".join(f"r{i},v{i}" for i in range(1, CSV_ROW_GROUP_SIZE + 2))
    raw = f"key,value\n{rows}\n".encode()
    result = normalize_csv(raw, "big.csv", SOURCE)

    sections = [segment["section"] for segment in result["pages"]]
    assert sections == [
        f"rows 1-{CSV_ROW_GROUP_SIZE}",
        f"rows {CSV_ROW_GROUP_SIZE + 1}-{CSV_ROW_GROUP_SIZE + 1}",
    ]
    # Segment char counts must reconstruct the text exactly (chunking contract).
    body_lengths = [segment["chars"] for segment in result["pages"]]
    parts = result["text"].split(SEGMENT_SEPARATOR)
    assert [len(part) for part in parts] == body_lengths


def test_csv_unnamed_columns_get_positional_labels():
    raw = b"a,,c\n1,2,3\n"
    result = normalize_csv(raw, "x.csv", SOURCE)
    assert result["text"] == "a: 1 | column_2: 2 | c: 3"


def test_csv_header_only_indexes_header_segment():
    result = normalize_csv(b"alpha,beta\n", "h.csv", SOURCE)
    assert result["rowCount"] == 0
    assert result["pages"] == [{"section": "header", "chars": len("alpha | beta")}]
    assert result["text"] == "alpha | beta"


def test_csv_empty_file_raises():
    try:
        normalize_csv(b"", "empty.csv", SOURCE)
    except ValueError:
        return
    raise AssertionError("empty CSV must raise so the document is marked failed")
