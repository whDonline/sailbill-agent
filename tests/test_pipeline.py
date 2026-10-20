"""End-to-end pipeline behaviour on the synthetic corpus."""

from __future__ import annotations

import json

from sailbill.output import as_extended, as_json, summarize
from sailbill.pipeline import run_paths, split_units


def run(paths, store, settings, **kwargs):
    return run_paths(paths, settings=settings, store=store, use_llm=False, **kwargs)


def test_pdf_produces_expected_operations(samples_dir, store, settings):
    result = run([samples_dir / "debit_note.pdf"], store, settings)
    operations = [record.operation.value for record in result.records]
    assert operations == ["NO_CHANGE", "UPDATE", "NO_CHANGE", "CREATE", "CREATE"]

    update = next(r for r in result.records if r.operation.value == "UPDATE")
    assert update.charge_details.charge_item == "Terminal Handling Charge"
    assert update.charge_details.amount == 300.0
    assert update.document_info.reference_no == "DN-202609-001"


def test_update_inherits_unchanged_fields(samples_dir, store, settings):
    result = run([samples_dir / "debit_note.pdf"], store, settings)
    update = next(r for r in result.records if r.operation.value == "UPDATE")
    # payment term is inherited from the existing bill line
    assert update.charge_details.payment_term == "CC"
    assert update.financial_attributes.accounting_entity == "XYZ Freight Forwarding Ltd"
    assert update.financial_attributes.posting_type == "AP"


def test_ar_and_ap_are_both_produced(samples_dir, store, settings):
    ap = run([samples_dir / "debit_note.pdf"], store, settings)
    ar = run([samples_dir / "ar_debit_note.pdf"], store, settings)
    assert {r.financial_attributes.posting_type for r in ap.records} == {"AP"}
    assert {r.financial_attributes.posting_type for r in ar.records} == {"AR"}


def test_accounting_entity_override_changes_posting(samples_dir, store, settings):
    result = run([samples_dir / "ar_debit_note.pdf"], store, settings, accounting_entity="Golden Dragon Trading Co., Ltd")
    assert {r.financial_attributes.posting_type for r in result.records} == {"AP"}


def test_messy_document_goes_to_review(samples_dir, store, settings):
    result = run([samples_dir / "pending_debit_note.pdf"], store, settings)
    assert result.records
    assert all(record.operation.value == "PENDING_REVIEW" for record in result.records)
    reasoning = " ".join(result.records[0].audit_trail.ai_reasoning)
    assert "校验" in reasoning or "复核" in reasoning


def test_email_yields_one_bill_from_its_attachment(samples_dir, store, settings):
    result = run([samples_dir / "booking_email.eml"], store, settings)
    assert len(result.units) == 1  # the mail itself is context, the PDF is the bill
    assert [r.operation.value for r in result.records] == ["NO_CHANGE", "UPDATE", "NO_CHANGE", "CREATE", "CREATE"]
    assert all(e["source_file"].endswith(".pdf") for e in result.extras)


def test_split_units_keeps_context(samples_dir):
    from sailbill.ingest import load_tree

    docs = load_tree(samples_dir / "booking_email.eml")
    units = split_units(docs)
    assert len(units) == 1
    context, primary = units[0]
    assert primary.kind.value == "pdf"
    assert len(context) == 1 and context[0].kind.value == "eml"


def test_every_amount_is_a_json_number(samples_dir, store, settings):
    result = run([samples_dir], store, settings)
    payload = [as_json(record) for record in result.records]
    json.dumps(payload)
    for record in payload:
        amount = record["charge_details"]["amount"]
        if record["operation"] != "PENDING_REVIEW":
            assert isinstance(amount, float)
        assert isinstance(record["charge_details"]["currency"], (str, type(None)))


def test_output_schema_is_exactly_the_contract(samples_dir, store, settings):
    result = run([samples_dir / "debit_note.pdf"], store, settings)
    allowed = {"operation", "document_info", "transaction_party", "charge_details",
               "financial_attributes", "audit_trail"}
    for record in result.records:
        payload = as_json(record)
        assert set(payload) == allowed
        assert set(payload["document_info"]) == {"document_type", "reference_no", "document_date"}
        assert set(payload["transaction_party"]) == {"issuer", "recipient"}
        assert set(payload["charge_details"]) == {"charge_item", "amount", "currency", "payment_term"}
        assert set(payload["financial_attributes"]) == {"accounting_entity", "posting_type"}
        assert payload["audit_trail"]["ai_reasoning"]


def test_extended_mode_adds_trace_without_touching_contract(samples_dir, store, settings):
    result = run([samples_dir / "debit_note.pdf"], store, settings)
    payload = as_extended(result.records[0], result.extras[0])
    assert "_trace" in payload
    assert payload["_trace"]["confidence"] > 0
    payload.pop("_trace")
    assert set(payload) == {
        "operation", "document_info", "transaction_party", "charge_details",
        "financial_attributes", "audit_trail",
    }


def test_runs_are_deterministic(samples_dir, store, settings):
    first = [as_json(r) for r in run([samples_dir / "debit_note.pdf"], store, settings).records]
    second = [as_json(r) for r in run([samples_dir / "debit_note.pdf"], store, settings).records]
    assert first == second


def test_summary_counts(samples_dir, store, settings):
    result = run([samples_dir / "debit_note.pdf"], store, settings)
    summary = summarize(result.records)
    assert summary["records"] == 5
    assert summary["by_operation"]["UPDATE"] == 1
    assert summary["bookable_amount"] > 0


def test_reference_numbers_are_not_polluted(samples_dir, store, settings):
    """Regression: '.: DN-202609-001' used to leak the separator into the value."""
    result = run([samples_dir / "debit_note.pdf"], store, settings)
    for record in result.records:
        reference = record.document_info.reference_no or ""
        assert not reference.startswith(".:")
        assert reference == "DN-202609-001"
