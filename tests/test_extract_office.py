"""Office (.docx/.pptx/.xlsx) text extraction — built hermetically with the
same libraries the extractor uses, so no binary fixtures are needed."""
import io

import pytest

from smart_ai_router import extract

DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
PPTX = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _docx_bytes(paragraphs, table_rows=None):
    docx = pytest.importorskip("docx")
    doc = docx.Document()
    for p in paragraphs:
        doc.add_paragraph(p)
    if table_rows:
        t = doc.add_table(rows=len(table_rows), cols=len(table_rows[0]))
        for r, row in enumerate(table_rows):
            for c, val in enumerate(row):
                t.rows[r].cells[c].text = val
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def _pptx_bytes(slides):
    pptx = pytest.importorskip("pptx")
    prs = pptx.Presentation()
    blank = prs.slide_layouts[6]  # fully blank
    for texts in slides:
        slide = prs.slides.add_slide(blank)
        for i, text in enumerate(texts):
            box = slide.shapes.add_textbox(0, i * 100, 3000000, 500000)
            box.text_frame.text = text
    buf = io.BytesIO()
    prs.save(buf)
    return buf.getvalue()


def _xlsx_bytes(sheet_rows):
    openpyxl = pytest.importorskip("openpyxl")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Data"
    for row in sheet_rows:
        ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ── is_extractable ─────────────────────────────────────────────────────────────

def test_office_mimes_are_extractable():
    assert extract.is_extractable(DOCX)
    assert extract.is_extractable(PPTX)
    assert extract.is_extractable(XLSX)


def test_legacy_office_is_not_extractable():
    assert not extract.is_extractable("application/msword")
    assert not extract.is_extractable("application/vnd.ms-powerpoint")
    assert not extract.is_extractable("application/vnd.ms-excel")


# ── extraction ─────────────────────────────────────────────────────────────────

def test_docx_paragraphs_and_tables():
    data = _docx_bytes(
        ["Kevin Gaasch", "Senior Engineer"],
        table_rows=[["Skill", "Years"], ["Python", "10"]],
    )
    text = extract.extract_text(data, DOCX, filename="resume.docx")
    assert "Kevin Gaasch" in text
    assert "Senior Engineer" in text
    assert "Skill | Years" in text
    assert "Python | 10" in text


def test_pptx_slides():
    data = _pptx_bytes([["Title Slide", "subtitle"], ["Second slide bullet"]])
    text = extract.extract_text(data, PPTX)
    assert "Title Slide" in text
    assert "Second slide bullet" in text
    assert "[Slide 1]" in text and "[Slide 2]" in text


def test_xlsx_rows():
    data = _xlsx_bytes([["Name", "Score"], ["Alice", 42], ["Bob", 99]])
    text = extract.extract_text(data, XLSX)
    assert "Name\tScore" in text
    assert "Alice\t42" in text
    assert "Bob\t99" in text
    assert "[Sheet: Data]" in text


def test_malformed_office_returns_empty_not_raise():
    assert extract.extract_text(b"not a real docx", DOCX) == ""
    assert extract.extract_text(b"PK\x03\x04garbage", PPTX) == ""
    assert extract.extract_text(b"", XLSX) == ""
