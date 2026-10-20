"""AR/AP matrix and business validation."""

from __future__ import annotations

from sailbill.finance import determine_posting
from sailbill.models import ChargeExtraction, DocExtraction, Tri
from sailbill.validate.business import has_blocking_issue, validate_extraction


def doc(doc_type="Debit Note", issuer="ABC Logistics", recipient="XYZ Forwarding") -> DocExtraction:
    ext = DocExtraction()
    ext.document_type = Tri.present(doc_type)
    ext.issuer = Tri.present(issuer)
    ext.recipient = Tri.present(recipient)
    ext.reference_no = Tri.present("DN-001")
    return ext


def charge(name="Ocean Freight", amount=100.0, currency="USD") -> ChargeExtraction:
    return ChargeExtraction(charge_item_raw=name, charge_item=name, amount=amount, currency=currency)


# --------------------------------------------------------------------------- #
# AR / AP
# --------------------------------------------------------------------------- #
def test_entity_as_issuer_is_ar():
    decision = determine_posting(doc(), accounting_entity="ABC Logistics")
    assert decision.posting_type == "AR"
    assert decision.resolved


def test_entity_as_recipient_is_ap():
    decision = determine_posting(doc(), accounting_entity="XYZ Forwarding")
    assert decision.posting_type == "AP"


def test_document_type_alone_never_decides():
    """Same DN, opposite posting, purely because the entity differs."""
    assert determine_posting(doc(), accounting_entity="ABC Logistics").posting_type == "AR"
    assert determine_posting(doc(), accounting_entity="XYZ Forwarding").posting_type == "AP"


def test_unknown_entity_is_not_guessed():
    assert determine_posting(doc(), accounting_entity=None).posting_type is None
    assert determine_posting(doc(), accounting_entity="Someone Else Ltd").posting_type is None


def test_credit_note_reverses_direction_but_keeps_side():
    decision = determine_posting(doc("Credit Note"), accounting_entity="ABC Logistics")
    assert decision.posting_type == "AR"
    assert decision.direction == -1
    assert any("贷项" in reason for reason in decision.reasons)


def test_debit_note_direction_is_positive():
    assert determine_posting(doc(), accounting_entity="ABC Logistics").direction == 1


def test_fuzzy_party_match_still_resolves():
    decision = determine_posting(doc(issuer="ABC Logistics Co., Ltd"), accounting_entity="ABC LOGISTICS")
    assert decision.posting_type == "AR"


def test_missing_parties_do_not_resolve():
    ext = DocExtraction()
    ext.document_type = Tri.present("Debit Note")
    assert determine_posting(ext, accounting_entity="ABC Logistics").posting_type is None


# --------------------------------------------------------------------------- #
# validation
# --------------------------------------------------------------------------- #
def test_valid_extraction_has_no_blocking_issue():
    ext = doc()
    ext.currency = Tri.present("USD")
    ext.charges = [charge(), charge("THC", 50.0)]
    ext.subtotal_amount = Tri.present(150.0)
    ext.total_amount = Tri.present(150.0)
    issues = validate_extraction(ext)
    assert not has_blocking_issue(issues)


def test_total_mismatch_is_blocking():
    ext = doc()
    ext.charges = [charge(), charge("THC", 50.0)]  # 150
    ext.subtotal_amount = Tri.present(150.0)
    ext.tax_amount = Tri.present(0.0)
    ext.total_amount = Tri.present(420.0)
    issues = validate_extraction(ext)
    assert has_blocking_issue(issues)
    assert any(i.code == "TOTAL_MISMATCH" for i in issues)


def test_small_mismatch_is_only_a_warning():
    ext = doc()
    ext.charges = [charge(amount=100.0)]
    ext.subtotal_amount = Tri.present(100.0)
    ext.total_amount = Tri.present(100.7)
    issues = validate_extraction(ext)
    assert not has_blocking_issue(issues)
    assert any(i.code == "TOTAL_MISMATCH" and i.severity == "warn" for i in issues)


def test_invalid_currency_is_blocking():
    ext = doc()
    ext.charges = [charge(currency="US0")]
    issues = validate_extraction(ext)
    assert has_blocking_issue(issues)
    assert any(i.code == "CURRENCY_INVALID" for i in issues)


def test_missing_currency_is_blocking():
    ext = doc()
    ext.charges = [charge(currency=None)]
    issues = validate_extraction(ext)
    assert any(i.code == "CURRENCY_MISSING" for i in issues)


def test_no_charges_is_blocking():
    issues = validate_extraction(doc())
    assert any(i.code == "NO_CHARGE" and i.severity == "error" for i in issues)


def test_duplicate_charge_is_reported():
    ext = doc()
    ext.charges = [charge("Ocean Freight", 100.0), charge("Ocean Freight", 100.0)]
    issues = validate_extraction(ext)
    assert any(i.code == "DUPLICATE_CHARGE" for i in issues)


def test_unknown_charge_name_lowers_but_does_not_block():
    ext = doc()
    ext.charges = [charge("Quantum Foam Surcharge", 10.0)]
    issues = validate_extraction(ext)
    assert any(i.code == "CHARGE_UNMAPPED" for i in issues)
    assert not has_blocking_issue(issues)


def test_amount_must_be_numeric():
    ext = doc()
    bad = charge()
    bad.amount = "100"  # type: ignore[assignment]
    ext.charges = [bad]
    issues = validate_extraction(ext)
    assert any(i.code == "AMOUNT_TYPE" for i in issues)
