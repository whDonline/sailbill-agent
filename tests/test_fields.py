"""Business-field extraction: the eight items the assignment asks for.

Each test here pins one requirement (题面 四.2) to a behavioural guarantee,
including the cases where the right answer is "no value".
"""

from __future__ import annotations

from dataclasses import replace

from sailbill.config import SETTINGS
from sailbill.extract.rules import extract_rules
from sailbill.models import NormalizedDocument, SourceKind, Status


def make_doc(*lines: str) -> NormalizedDocument:
    doc = NormalizedDocument(
        doc_id="doc-fields", filename="fields.txt", kind=SourceKind.TEXT, sha256="x" * 64
    )
    for i, line in enumerate(lines):
        doc.add_block(line, kind="paragraph", line=i)
    return doc


# --------------------------------------------------------------------------- #
# 单据类型
# --------------------------------------------------------------------------- #
def test_document_type_from_title_line():
    extraction = extract_rules([make_doc("DEBIT NOTE", "Debit Note No.: DN-1")])
    assert extraction.document_type.value == "Debit Note"


def test_credit_note_wins_over_debit_note_mention():
    extraction = extract_rules([make_doc("CREDIT NOTE", "Reference: original debit note DN-9")])
    assert extraction.document_type.value == "Credit Note"


# --------------------------------------------------------------------------- #
# 单据编号与日期
# --------------------------------------------------------------------------- #
def test_reference_and_date():
    extraction = extract_rules([make_doc("Debit Note No.: DN-202609-001", "Date: 2026-09-20")])
    assert extraction.reference_no.value == "DN-202609-001"
    assert extraction.document_date.value.isoformat() == "2026-09-20"


def test_reference_that_is_really_a_label_is_rejected():
    """"DOCUMENT NO. B/L NUMBER" used to yield reference_no == "B/L"."""
    extraction = extract_rules([make_doc("DOCUMENT NO. B/L NUMBER", "SHIPPER: ACME")])
    assert not extraction.reference_no.is_present


def test_us_style_reference_and_date():
    extraction = extract_rules([make_doc("D/C No. : DC-108478", "D/C DATE 10-24-2026")])
    assert extraction.reference_no.value == "DC-108478"
    assert extraction.document_date.value.isoformat() == "2026-10-24"


# --------------------------------------------------------------------------- #
# 签发方 / 接收方
# --------------------------------------------------------------------------- #
def test_issuer_and_recipient_from_labels():
    extraction = extract_rules(
        [make_doc("Issued By: Neptune Shipping Limited", "Bill To: AEBO TCO Import LLC")]
    )
    assert extraction.issuer.value == "Neptune Shipping Limited"
    assert extraction.recipient.value == "AEBO TCO Import LLC"


def test_party_table_header_is_not_a_party():
    extraction = extract_rules(
        [make_doc("SHIPPER NOTIFY", "CONSIGNEE", "FREE BUSINESS", "DAYS AT PORT")]
    )
    assert not extraction.recipient.is_present


def test_sentence_is_not_a_party():
    extraction = extract_rules([make_doc("BILL TO: the account of the cargo")])
    assert not extraction.recipient.is_present


# --------------------------------------------------------------------------- #
# 费用名称 / 数量 / 单价 / 金额
# --------------------------------------------------------------------------- #
def test_charge_name_quantity_unit_price_amount():
    extraction = extract_rules(
        [make_doc("Charge Description   Qty   Unit Price   Amount   Currency   PP/CC",
                  "Handling Fee   2   20.00   40.00   USD   PP")]
    )
    charge = extraction.charges[0]
    assert charge.charge_item_raw == "Handling Fee"
    assert charge.charge_item == "Handling Charge"
    assert charge.quantity == 2.0
    assert charge.unit_price == 20.0
    assert charge.amount == 40.0
    assert charge.currency == "USD"


def test_amount_derived_from_quantity_times_unit_price():
    extraction = extract_rules(
        [make_doc("Charge Description | Qty | Unit Price | Amount | Currency",
                  "Ocean Freight | 2 | 1,000.00 |  | USD")]
    )
    charge = extraction.charges[0]
    assert charge.amount == 2000.0
    assert any("数量×单价" in w for w in extraction.warnings)


# --------------------------------------------------------------------------- #
# 币种
# --------------------------------------------------------------------------- #
def test_currency_is_detected_and_normalised():
    assert extract_rules([make_doc("Total Due: 100.00 USD")]).currency.value == "USD"
    assert extract_rules([make_doc("Total Due: 100.00 RMB")]).currency.value == "CNY"


def test_prose_word_is_not_a_currency():
    """"ALL" and "COD" are valid ISO 4217 codes but not currencies here."""
    extraction = extract_rules([make_doc("ALL CHARGES TO BE PAID BY SHIPPER", "TERMS COD")])
    assert not extraction.currency.is_present


# --------------------------------------------------------------------------- #
# PP / CC
# --------------------------------------------------------------------------- #
def test_payment_term_per_charge():
    extraction = extract_rules(
        [make_doc("Charge Description | Amount | Currency | PP/CC",
                  "Ocean Freight | 100.00 | USD | PP",
                  "Terminal Handling Charge | 50.00 | USD | CC")]
    )
    assert [c.payment_term for c in extraction.charges] == ["PP", "CC"]


def test_payment_term_from_pc_column():
    extraction = extract_rules([make_doc("HANDLING CHARGE - 65.00 1.00 C 65.00")])
    assert extraction.charges[0].payment_term == "CC"


def test_document_payment_term():
    extraction = extract_rules([make_doc("TERMS PP", "Total Due: 1.00 USD")])
    assert extraction.payment_term.value == "PP"


def test_non_ppcc_term_is_absent_not_unknown():
    extraction = extract_rules([make_doc("TERMS COD", "Total Due: 1.00 USD")])
    assert extraction.payment_term.status is Status.ABSENT


# --------------------------------------------------------------------------- #
# 小计 / 税额 / 总计
# --------------------------------------------------------------------------- #
def test_subtotal_tax_total():
    extraction = extract_rules(
        [make_doc("Charges: 100.00", "Subtotal: 1,000.00", "VAT (10%): 100.00",
                  "Total Due: 1,100.00 USD")]
    )
    assert extraction.subtotal_amount.value == 1000.0
    assert extraction.tax_amount.value == 100.0
    assert extraction.total_amount.value == 1100.0


def test_total_with_amount_in_words_is_not_parsed_as_money():
    """"TOTAL: SAY ONE (40HQx1) CONTAINER" used to yield 40.0."""
    extraction = extract_rules([make_doc("TOTAL: SAY ONE (40HQx1) CONTAINER ONLY")])
    assert not extraction.total_amount.is_present


def test_missing_tax_row_is_absent_when_subtotal_equals_total():
    extraction = extract_rules([make_doc("Subtotal: 175.00", "Total: 175.00 USD")])
    assert extraction.tax_amount.status is Status.ABSENT


# --------------------------------------------------------------------------- #
# 输出侧
# --------------------------------------------------------------------------- #
def test_quantity_and_unit_price_are_exposed_in_extended_trace(samples_dir):
    from sailbill.pipeline import run_paths
    from sailbill.store import BillStore

    result = run_paths(
        [samples_dir / "debit_note.xlsx"],
        settings=replace(SETTINGS, offline=True),
        store=BillStore(),
        use_llm=False,
    )
    trace = result.extras[0]
    assert trace["quantity"] == 1.0
    assert trace["unit_price"] == 3100.0
    # the documented contract must stay clean
    assert set(result.records[0].model_dump()) == {
        "operation", "document_info", "transaction_party", "charge_details",
        "financial_attributes", "audit_trail",
    }
