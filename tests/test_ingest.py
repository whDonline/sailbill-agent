"""Ingestion: PDF text layer, Excel headers, Word merged cells, recursive EML."""

from __future__ import annotations

import pytest

from sailbill.ingest import load_tree
from sailbill.models import SourceKind


def test_pdf_text_layer_is_used_without_ocr(samples_dir):
    docs = load_tree(samples_dir / "debit_note.pdf")
    assert len(docs) == 1
    doc = docs[0]
    assert doc.kind is SourceKind.PDF
    assert "DEBIT NOTE" in doc.text.upper()
    assert not doc.meta.get("needs_ocr")


def test_excel_rows_keep_header_index(samples_dir):
    doc = load_tree(samples_dir / "debit_note.xlsx")[0]
    assert doc.kind is SourceKind.EXCEL
    table = doc.tables[0]
    assert table.header_row is not None
    assert "Charge Description" in " ".join(table.rows[table.header_row])
    assert any("Ocean Freight" in " ".join(row) for row in table.rows)


def test_word_merged_cells_do_not_swallow_equal_values(samples_dir):
    """Two real cells may hold the same number (unit price == amount)."""
    doc = load_tree(samples_dir / "credit_note.docx")[0]
    assert doc.kind is SourceKind.WORD
    rows = [row for table in doc.tables for row in table.rows]
    target = next(row for row in rows if row and row[0] == "Handling Fee")
    assert target == ["Handling Fee", "1", "40.00", "40.00", "USD"]


def test_eml_headers_body_and_attachments(samples_dir):
    docs = load_tree(samples_dir / "booking_email.eml")
    assert len(docs) == 2  # the mail itself + one attachment
    mail = docs[0]
    assert mail.kind is SourceKind.EML
    assert mail.meta["mail_headers"]["subject"].startswith("Debit Note DN-202609-001")
    assert "please find attached" in mail.text.lower()
    assert len(mail.attachments) == 1
    attachment = docs[1]
    assert attachment.meta["parent_id"] == mail.doc_id
    assert attachment.meta["root_id"] == mail.doc_id


def test_eml_attachment_is_parsed_not_just_stored(samples_dir):
    docs = load_tree(samples_dir / "booking_email.eml")
    attachment = docs[1]
    assert attachment.kind is SourceKind.PDF
    assert "COSU9510258060" in attachment.text


def test_missing_file_raises():
    with pytest.raises(FileNotFoundError):
        load_tree("does-not-exist.pdf")


def test_unknown_binary_is_reported(scratch):
    path = scratch / "blob.bin"
    path.write_bytes(b"\x00\x01\x02")
    doc = load_tree(path)[0]
    assert doc.kind is SourceKind.UNKNOWN
    assert doc.warnings or doc.text == ""


def test_html_table_in_mail_becomes_pipe_lines():
    from sailbill.normalize.html import html_to_text

    html = (
        "<p>Please find the revised note.</p>"
        "<table><tr><th>Charge</th><th>Amount</th></tr>"
        "<tr><td>THC</td><td>40.00</td></tr></table>"
    )
    text = html_to_text(html)
    assert "Please find the revised note." in text
    assert "Charge | Amount" in text
    assert "THC | 40.00" in text


def test_html_scripts_and_styles_are_dropped():
    from sailbill.normalize.html import html_to_text

    text = html_to_text("<style>.a{color:red}</style><script>alert(1)</script><p>Body</p>")
    assert "Body" in text
    assert "color" not in text and "alert" not in text


def test_outlook_msg_is_routed_and_fails_soft(scratch):
    """`.msg` is an OLE container; it must never be handed to the email module."""
    path = scratch / "broken.msg"
    path.write_bytes(b"\x00" * 128)
    doc = load_tree(path)[0]
    assert doc.kind is SourceKind.MSG
    assert doc.warnings  # failure recorded, not raised


def test_text_only_pdf_does_not_use_pdfplumber_tables(samples_dir):
    """The gate: borderless bills keep using the line parser, because pdfplumber
    fragments their cells ('DU' / 'E DATE')."""
    doc = load_tree(samples_dir / "debit_note.pdf")[0]
    assert doc.tables == []
