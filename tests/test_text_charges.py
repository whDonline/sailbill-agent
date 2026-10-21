"""Free-text charge rows: the shapes that real arrival notices / debit notes use."""

from __future__ import annotations

from sailbill.extract.rules import extract_rules
from sailbill.models import NormalizedDocument, SourceKind


def make_doc(*lines: str) -> NormalizedDocument:
    doc = NormalizedDocument(
        doc_id="doc-text",
        filename="arrival-notice.txt",
        kind=SourceKind.TEXT,
        sha256="x" * 64,
    )
    for i, line in enumerate(lines):
        doc.add_block(line, kind="paragraph", line=i)
    return doc


def test_arrival_notice_style_row():
    """``On Carriage Additional - US EmOCA17 USD  245.0000`` is one fee row."""
    doc = make_doc(
        "ARRIVAL NOTICE",
        "On Carriage Additional - US EmOCA17 USD  245.0000",
        "TOTAL CHARGES TO BE PAID:  245.0000",
    )
    extraction = extract_rules([doc])
    assert len(extraction.charges) == 1
    charge = extraction.charges[0]
    assert charge.charge_item == "On Carriage"
    assert charge.amount == 245.0
    assert charge.currency == "USD"
    assert extraction.document_type.value == "Arrival Notice"
    assert extraction.total_amount.value == 245.0


def test_description_with_reference_code_is_trimmed():
    """A trailing reference must not stop the name from matching."""
    doc = make_doc("HANDLING CHARGE - REF AB1234 USD 40.00")
    extraction = extract_rules([doc])
    assert extraction.charges[0].charge_item == "Handling Charge"
    assert extraction.charges[0].amount == 40.0


def test_prose_with_currency_and_number_is_not_a_charge():
    """A sentence mentioning a fee must not become a booked charge row."""
    doc = make_doc(
        "EFFECTIVE ON JUNE 1st 2016, IF THE CARRIER ARRANGES AN I.T. FOR CARGO MANIFESTED",
        "TO MOVE INLAND AND THE MERCHANT REQUESTS THE I.T. TO BE CANCELLED, A SERVICE FEE OF $25.00 PER",
    )
    extraction = extract_rules([doc])
    assert extraction.charges == []


def test_line_number_prefix_is_stripped():
    doc = make_doc("4 ADD'L DETENTION FREE TIME CHARGE - 1,310.40 1.00 C 1,310.40")
    extraction = extract_rules([doc])
    assert len(extraction.charges) == 1
    assert extraction.charges[0].charge_item == "Detention Free Time Charge"
    assert extraction.charges[0].amount == 1310.40
    assert extraction.charges[0].payment_term == "CC"
