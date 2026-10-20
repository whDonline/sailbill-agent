"""Extraction merge policy and grounding (runs without any API key)."""

from __future__ import annotations

from sailbill.extract import merge
from sailbill.extract.grounding import apply_grounding, ground_number, normalize_for_match
from sailbill.models import ChargeExtraction, DocExtraction, Tri


def rules_doc() -> DocExtraction:
    ext = DocExtraction(extractor="rules")
    ext.document_type = Tri.present("Debit Note")
    ext.reference_no = Tri.present("DN-001")
    ext.total_amount = Tri.present(3200.0)
    ext.charges = [ChargeExtraction(charge_item_raw="THC", charge_item="Terminal Handling Charge", amount=300.0, currency="USD")]
    return ext


def llm_doc() -> DocExtraction:
    ext = DocExtraction(extractor="llm")
    ext.document_type = Tri.present("Debit Note")
    ext.reference_no = Tri.present("DN-001")
    ext.total_amount = Tri.present(9999.0)          # wrong on purpose
    ext.issuer = Tri.present("ABC Logistics")        # rule engine missed it
    ext.charges = [ChargeExtraction(charge_item_raw="THC", charge_item="Terminal Handling Charge", amount=310.0, currency="USD")]
    return ext


def test_rules_win_on_numeric_conflict():
    merged = merge(rules_doc(), llm_doc())
    assert merged.total_amount.value == 3200.0
    assert any("[CONFLICT]" in warning for warning in merged.warnings)


def test_model_fills_what_rules_missed():
    merged = merge(rules_doc(), llm_doc())
    assert merged.issuer.value == "ABC Logistics"
    assert merged.extractor == "hybrid"


def test_rules_charges_are_authoritative():
    merged = merge(rules_doc(), llm_doc())
    assert len(merged.charges) == 1
    assert merged.charges[0].amount == 300.0


def test_charge_count_mismatch_is_reported():
    llm = llm_doc()
    llm.charges.append(ChargeExtraction(charge_item_raw="Doc Fee", charge_item="Documentation Fee", amount=50.0, currency="USD"))
    merged = merge(rules_doc(), llm)
    assert any("费用行数不一致" in warning for warning in merged.warnings)


def test_llm_charges_used_when_rules_found_none():
    rules = DocExtraction(extractor="rules")
    merged = merge(rules, llm_doc())
    assert len(merged.charges) == 1
    assert any("费用明细由模型补充" in warning for warning in merged.warnings)


# --------------------------------------------------------------------------- #
# grounding
# --------------------------------------------------------------------------- #
def test_grounding_downgrades_invented_values():
    ext = rules_doc()
    ext.total_amount = Tri.present(9999.0)
    ext.reference_no = Tri.present("DN-FAKE-999")
    apply_grounding(ext, "Debit Note No.: DN-001\nTotal Due: 3,200.00 USD")
    # the real total is grounded and kept; the invented one is dropped, not guessed
    assert ext.total_amount.status.value == "unknown"
    assert ext.reference_no.status.value == "unknown"
    assert ext.charges == []


def test_grounding_drops_rows_whose_amount_is_absent():
    ext = rules_doc()
    ext.charges = [
        ChargeExtraction(charge_item_raw="THC", amount=300.0, currency="USD"),
        ChargeExtraction(charge_item_raw="Ghost Fee", amount=777.0, currency="USD"),
    ]
    apply_grounding(ext, "THC 300.00")
    assert [c.charge_item_raw for c in ext.charges] == ["THC"]


def test_number_grounding_tolerates_separators():
    assert ground_number(3220.0, "Total Due: 3,220.00 USD")
    assert ground_number(3220.0, "TOTAL 3220.00")
    assert not ground_number(3220.0, "Total Due: 3200.00 USD")


def test_normalize_for_match():
    assert normalize_for_match("ABC Logistics (Shanghai) Co., Ltd") == "abclogisticsshanghaicoltd"
