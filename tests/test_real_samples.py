"""Optional checks against the *real* assignment samples.

Those files live in ``TestSamples/`` (git-ignored, because they are real
customer documents).  When they are not present -- on CI, or on a fresh clone --
every test here is skipped, so the suite stays runnable everywhere.

What the real corpus covers:

* ``PDF 示例账单2.pdf`` -- US-style Debit Note with a ``rate qty P/C amount``
  table; three charges must add up to the printed ``TOTAL AMOUNT``;
* ``PDF示例账单1.pdf`` -- CMA CGM Arrival Notice with a single
  ``On Carriage Additional ... USD 245.0000`` line;
* the EML -- 3 attachments (JPG + MBL + HBL); the JPG needs OCR, the two PDFs
  are bills of lading and carry no charges.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from sailbill.config import SETTINGS
from sailbill.ingest import load_tree
from sailbill.pipeline import run_paths
from sailbill.store import BillStore

ROOT = Path(__file__).resolve().parents[1]
REAL = ROOT / "TestSamples"

pytestmark = pytest.mark.skipif(
    not REAL.exists() or not any(REAL.iterdir()),
    reason="TestSamples/ 不存在（真实样本不随仓库分发）",
)


def _find(needle: str) -> Path:
    for path in REAL.iterdir():
        if needle in path.name:
            return path
    pytest.skip(f"未找到 {needle}")


def _offline(settings=None):
    return replace(settings or SETTINGS, offline=True)


# --------------------------------------------------------------------------- #
# debit note
# --------------------------------------------------------------------------- #
def test_real_debit_note_arithmetic_closes(settings):
    result = run_paths(
        [_find("示例账单2")],
        settings=_offline(settings),
        store=BillStore(),
        use_llm=False,
        accounting_entity="AEBO TCO IMPORT LLC",
    )
    amounts = [record.charge_details.amount for record in result.records]
    assert len(amounts) == 3
    assert round(sum(amounts), 2) == 1410.40
    assert {r.charge_details.currency for r in result.records} == {"USD"}
    assert {r.financial_attributes.posting_type for r in result.records} == {"AP"}
    assert {r.operation.value for r in result.records} == {"CREATE"}
    assert all(r.document_info.reference_no == "DC-108478" for r in result.records)
    assert all(r.document_info.document_date == "2026-10-24" for r in result.records)


def test_real_debit_note_without_entity_is_not_booked(settings):
    """No accounting entity -> no AR/AP -> review, never a silent AP default."""
    result = run_paths(
        [_find("示例账单2")], settings=_offline(settings), store=BillStore(), use_llm=False
    )
    assert all(record.operation.value == "PENDING_REVIEW" for record in result.records)
    assert all(record.financial_attributes.posting_type is None for record in result.records)
    assert any(
        "应收/应付" in reason for record in result.records for reason in record.audit_trail.ai_reasoning
    )


def test_real_debit_note_parties_are_correct(settings):
    result = run_paths(
        [_find("示例账单2")],
        settings=_offline(settings),
        store=BillStore(),
        use_llm=False,
        accounting_entity="AEBO TCO IMPORT LLC",
    )
    first = result.records[0]
    assert first.transaction_party.issuer == "NEPTUNE SHIPPING LIMITED"
    assert first.transaction_party.recipient == "AEBO TCO IMPORT LLC"
    assert first.document_info.document_type == "Debit Note"


def test_fresh_document_is_inserted_into_the_store(settings):
    """Same document twice: the second run must not create duplicates."""
    from sailbill.store import BillStore as Store

    store = Store.from_file(REAL.parent / "samples" / "existing_bills.json")
    kwargs = dict(settings=_offline(settings), use_llm=False, accounting_entity="AEBO TCO IMPORT LLC")
    first = run_paths([_find("示例账单2")], store=store, **kwargs)
    second = run_paths([_find("示例账单2")], store=store, **kwargs)
    assert len(first.records) == len(second.records) == 3
    assert {r.operation.value for r in second.records} == {"CREATE"}


# --------------------------------------------------------------------------- #
# arrival notice
# --------------------------------------------------------------------------- #
def test_real_arrival_notice_extracts_on_carriage(settings):
    """The charge and the total are read correctly; the parties are a table
    header on this document, so AR/AP stays unresolved and the record is sent to
    review instead of being booked against a guessed counterparty."""
    result = run_paths(
        [_find("示例账单1")],
        settings=_offline(settings),
        store=BillStore(),
        use_llm=False,
        accounting_entity="AEBO TCO IMPORT LLC",
    )
    assert len(result.records) == 1
    record = result.records[0]
    assert record.document_info.document_type == "Arrival Notice"
    assert record.charge_details.charge_item == "On Carriage"
    assert record.charge_details.amount == 245.0
    assert record.charge_details.currency == "USD"
    # parties cannot be trusted on this layout -> must not be invented
    assert record.transaction_party.recipient is None
    assert record.financial_attributes.posting_type is None
    assert record.operation.value == "PENDING_REVIEW"
    assert any(
        "应收/应付" in reason for reason in record.audit_trail.ai_reasoning
    )


def test_arrival_notice_total_matches_its_single_charge(settings):
    from sailbill.extract import extract_document

    docs = load_tree(_find("示例账单1"), settings=_offline(settings))
    extraction = extract_document(docs, settings=_offline(settings), use_llm=False)
    total = sum(charge.amount for charge in extraction.charges)
    assert round(total, 2) == round(extraction.total_amount.value, 2)


# --------------------------------------------------------------------------- #
# email with attachments
# --------------------------------------------------------------------------- #
def test_real_email_attachments_are_enumerated(settings):
    eml = next(p for p in REAL.iterdir() if p.suffix.lower() == ".eml")
    docs = load_tree(eml, settings=_offline(settings))
    assert docs[0].kind.value == "eml"
    assert len(docs[0].attachments) == 3
    kinds = sorted(doc.kind.value for doc in docs[1:])
    assert kinds == ["image", "pdf", "pdf"]


def test_real_bills_of_lading_carry_no_charges(settings):
    eml = next(p for p in REAL.iterdir() if p.suffix.lower() == ".eml")
    docs = load_tree(eml, settings=_offline(settings))
    pdfs = [doc for doc in docs if doc.kind.value == "pdf"]
    assert pdfs
    from sailbill.extract import extract_rules

    for doc in pdfs:
        assert extract_rules([doc]).charges == []
