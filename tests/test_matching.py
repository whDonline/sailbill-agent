"""Matching: the four rules, their priority, and ambiguity handling."""

from __future__ import annotations

from datetime import date

from sailbill.matching import match_charge
from sailbill.models import Bill, BillLine, ChargeExtraction, DocExtraction, MatchRule, Tri


def make_bill(**kwargs) -> Bill:
    defaults = dict(
        bill_id="B-1",
        document_type="Debit Note",
        reference_no="DN-001",
        document_date="2026-09-20",
        issuer="ABC Logistics",
        recipient="XYZ Forwarding",
        accounting_entity="XYZ Forwarding",
        posting_type="AP",
        references={"bl_no": ["COSU123"], "booking_no": ["BK001"], "container_no": ["CSLU1234567"]},
        lines=[
            BillLine(line_id="B-1-L1", charge_item="Ocean Freight", amount=2750.0, currency="USD"),
            BillLine(line_id="B-1-L2", charge_item="Terminal Handling Charge", amount=280.0, currency="USD"),
        ],
    )
    defaults.update(kwargs)
    return Bill(**defaults)


def make_doc(**kwargs) -> DocExtraction:
    doc = DocExtraction()
    doc.document_type = Tri.present("Debit Note")
    doc.reference_no = Tri.present(kwargs.get("reference_no", "DN-001"))
    doc.document_date = Tri.present(kwargs.get("document_date", date(2026, 9, 21)))
    doc.issuer = Tri.present(kwargs.get("issuer", "ABC Logistics"))
    doc.recipient = Tri.present(kwargs.get("recipient", "XYZ Forwarding"))
    doc.references = kwargs.get("references", {"bl_no": ["COSU123"], "booking_no": ["BK001"]})
    return doc


def charge(name="Ocean Freight", amount=2750.0, currency="USD") -> ChargeExtraction:
    return ChargeExtraction(charge_item_raw=name, charge_item=name, amount=amount, currency=currency)


def test_rule1_reference_and_charge_wins():
    outcome = match_charge(charge("Terminal Handling Charge", 300.0), make_doc(), [make_bill()])
    assert outcome.match is not None
    assert outcome.match.rule is MatchRule.REF_AND_CHARGE
    assert outcome.match.line.amount == 280.0


def test_rule1_has_priority_over_rule2():
    """A bill matching by B/L but not by DN number must not win over rule 1."""
    other = make_bill(bill_id="B-2", reference_no="DN-999")
    outcome = match_charge(charge("Terminal Handling Charge", 300.0), make_doc(), [other, make_bill()])
    assert outcome.match is not None
    assert outcome.match.bill.bill_id == "B-1"
    assert outcome.match.rule is MatchRule.REF_AND_CHARGE


def test_rule2_bl_and_charge_and_currency():
    doc = make_doc(reference_no="DN-OTHER")
    outcome = match_charge(charge("Ocean Freight", 2750.0, "USD"), doc, [make_bill()])
    assert outcome.match is not None
    assert outcome.match.rule is MatchRule.BL_AND_CHARGE


def test_rule2_requires_currency_match():
    doc = make_doc(reference_no="DN-OTHER")
    outcome = match_charge(charge("Ocean Freight", 2750.0, "CNY"), doc, [make_bill()])
    assert outcome.match is None


def test_rule3_booking():
    doc = make_doc(reference_no="DN-OTHER", references={"booking_no": ["BK001"]})
    outcome = match_charge(charge("Ocean Freight", 2750.0, "USD"), doc, [make_bill()])
    assert outcome.match is not None
    assert outcome.match.rule is MatchRule.BOOKING_AND_CHARGE


def test_rule4_container_parties_date_amount():
    doc = make_doc(reference_no="DN-OTHER", references={"container_no": ["CSLU1234567"]})
    outcome = match_charge(charge("Ocean Freight", 2750.0, "USD"), doc, [make_bill()])
    assert outcome.match is not None
    assert outcome.match.rule is MatchRule.CONTAINER_PARTY_DATE_AMOUNT


def test_no_match_returns_empty_outcome():
    doc = make_doc(reference_no="DN-NONE", references={})
    outcome = match_charge(charge("Ocean Freight", 1.0, "USD"), doc, [make_bill()])
    assert outcome.match is None
    assert outcome.notes


def test_ambiguous_match_is_flagged():
    a = make_bill(bill_id="B-1")
    b = make_bill(bill_id="B-2", lines=[BillLine(line_id="x", charge_item="Ocean Freight", amount=999.0, currency="USD")])
    outcome = match_charge(charge("Ocean Freight", 2750.0), make_doc(), [a, b])
    assert outcome.ambiguous
    assert len(outcome.candidates) == 2


def test_same_amount_across_bills_is_not_ambiguous():
    a = make_bill(bill_id="B-1")
    b = make_bill(bill_id="B-2")
    outcome = match_charge(charge("Ocean Freight", 2750.0), make_doc(), [a, b])
    assert not outcome.ambiguous
    assert outcome.match is not None
