"""Document generation (Markdown → PDF/docx/pptx/xlsx/md).

Where a format can be read back with the extraction libs, we round-trip through
extract.py to prove real content landed — no byte-for-byte fixtures needed.
"""
import pytest

from smart_ai_router import docgen, extract

DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
PPTX = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

SAMPLE = """# Kevin Gaasch
**Senior Engineer**

## Skills
- Python
- FastAPI

## Data
| Company | Years |
| --- | --- |
| Acme | 5 |
| Globex | 3 |
"""


# ── markdown parsing ──────────────────────────────────────────────────────────

def test_parse_headings_bullets_para_table():
    blocks = docgen.parse_markdown(SAMPLE)
    kinds = [b.kind for b in blocks]
    assert "h1" in kinds
    assert kinds.count("h2") == 2
    assert kinds.count("bullet") == 2
    assert "table" in kinds
    table = next(b for b in blocks if b.kind == "table")
    # separator row (|---|---|) is dropped; header + 2 data rows remain
    assert table.rows[0] == ["Company", "Years"]
    assert ["Acme", "5"] in table.rows
    assert len(table.rows) == 3


def test_parse_preserves_inline_markup():
    # Emphasis is kept in the block so renderers that support it can convert it.
    blocks = docgen.parse_markdown("**loud** and quiet")
    assert blocks[0].text == "**loud** and quiet"


def test_strip_inline_drops_emphasis():
    assert docgen._strip_inline("**loud** and `code`") == "loud and code"


# ── support detection ──────────────────────────────────────────────────────────

def test_is_supported():
    for ext in (".pdf", ".docx", ".pptx", ".xlsx", ".md", ".txt"):
        assert docgen.is_supported("file" + ext)
    assert not docgen.is_supported("file.doc")   # legacy binary
    assert not docgen.is_supported("file.html")
    assert not docgen.is_supported("noext")


def test_unsupported_type_raises():
    with pytest.raises(docgen.DocGenError):
        docgen.render("thing.html", "hi")


# ── text passthrough ────────────────────────────────────────────────────────────

def test_markdown_passthrough_is_verbatim():
    assert docgen.render("x.md", SAMPLE) == SAMPLE.encode("utf-8")
    assert docgen.render("x.txt", "plain body").decode() == "plain body"


# ── PDF ─────────────────────────────────────────────────────────────────────────

def test_pdf_has_pdf_magic_and_nonempty():
    data = docgen.render("resume.pdf", SAMPLE)
    assert data[:4] == b"%PDF"
    assert len(data) > 500


def test_pdf_empty_content_still_valid():
    data = docgen.render("empty.pdf", "")
    assert data[:4] == b"%PDF"


# ── docx / pptx / xlsx round-trip through the extractor ─────────────────────────

def test_docx_roundtrip():
    data = docgen.render("resume.docx", SAMPLE)
    assert data[:2] == b"PK"
    text = extract.extract_text(data, DOCX)
    assert "Kevin Gaasch" in text
    assert "Python" in text
    assert "Acme" in text and "5" in text


def test_pptx_slides_per_heading():
    data = docgen.render("deck.pptx", SAMPLE)
    assert data[:2] == b"PK"
    text = extract.extract_text(data, PPTX)
    # Three headings (h1 + two h2) → three slides.
    assert text.count("[Slide ") == 3
    assert "Kevin Gaasch" in text
    assert "FastAPI" in text


def test_xlsx_table_becomes_rows():
    data = docgen.render("data.xlsx", SAMPLE)
    assert data[:2] == b"PK"
    text = extract.extract_text(data, XLSX)
    assert "Company\tYears" in text
    assert "Acme\t5" in text


def test_xlsx_comma_lines_when_no_table():
    data = docgen.render("data.xlsx", "a,b,c\n1,2,3")
    text = extract.extract_text(data, XLSX)
    assert "a\tb\tc" in text
    assert "1\t2\t3" in text
