"""OCR adapter: tolerant parsing of the assigned service's structured payload.

The payload shape below mirrors the real service (``data.list[].chargeList[]``);
it is synthetic, so it is safe to commit.
"""

from __future__ import annotations

import json

from sailbill.extract.from_ocr import charges_from_ocr
from sailbill.extract.rules import extract_rules
from sailbill.ingest.ocr import find_charges, find_parties, find_references, render_ocr_text
from sailbill.models import NormalizedDocument, SourceKind

PAYLOAD = {
    "data": {
        "createDate": "2026-09-27 10:00:00",
        "fileName": "",
        "orderType": "None",
        "list": [
            {
                "fileType": "DEBIT NOTE",
                "mblNumber": "COSU9510258060",
                "bookingNo": "BK20260901",
                "shipper": "ABC Logistics (Shanghai) Co., Ltd",
                "consignee": "XYZ Freight Forwarding Ltd",
                "ctrList": [{"containerNo": "CSLU1234567", "pageType": "MAIN"}],
                "chargeList": [
                    {"chargeName": "Ocean Freight", "qty": 1, "unitPrice": "2,750.00", "amount": "2,750.00", "currency": "USD", "paymentTerm": "PP"},
                    {"chargeName": "THC (DEST)", "qty": 2, "unitPrice": "150.00", "amount": "3OO.OO", "currency": "USD", "paymentTerm": "CC"},
                    {"chargeName": "Doc Fee", "amount": "50.00", "currency": "USD"},
                ],
                "page_index": [0],
            }
        ],
    },
    "success": True,
    "message": "文档处理成功",
}


def test_find_charges_is_schema_tolerant():
    rows = find_charges(PAYLOAD)
    assert len(rows) == 3
    assert rows[1]["charge_item_raw"] == "THC (DEST)"


def test_references_and_parties_are_recovered():
    refs = find_references(PAYLOAD)
    assert refs["bl_no"] == ["COSU9510258060"]
    assert "BK20260901" in refs["booking_no"]
    assert "CSLU1234567" in refs["container_no"]
    parties = find_parties(PAYLOAD)
    assert parties["issuer"].startswith("ABC Logistics")
    assert parties["recipient"].startswith("XYZ")


def test_render_produces_citable_lines():
    lines = render_ocr_text(PAYLOAD)
    assert any(line.startswith("[OCR] charge#1") for line in lines)
    assert any("2,750.00" in line for line in lines)


def test_charges_from_ocr_normalises_everything():
    charges = charges_from_ocr(PAYLOAD, doc_currency="USD")
    assert [c.charge_item for c in charges] == ["Ocean Freight", "Terminal Handling Charge", "Documentation Fee"]
    assert [c.amount for c in charges] == [2750.0, 300.0, 50.0]  # "3OO.OO" repaired
    assert charges[0].payment_term == "PP"
    assert charges[1].payment_term == "CC"
    assert all(c.currency == "USD" for c in charges)


def test_ocr_only_document_runs_through_rules(settings):
    """A scanned PDF has no text; everything must come from the OCR payload."""
    doc = NormalizedDocument(
        doc_id="doc-ocr",
        filename="scan.pdf",
        kind=SourceKind.PDF,
        sha256="x" * 64,
        ocr=PAYLOAD,
    )
    doc.meta["needs_ocr"] = True
    doc.meta["ocr_refs"] = find_references(PAYLOAD)
    doc.meta["ocr_parties"] = find_parties(PAYLOAD)
    doc.meta["ocr_meta"] = {"document_type": "DEBIT NOTE"}
    for line in render_ocr_text(PAYLOAD):
        doc.add_block(line, kind="ocr")

    extraction = extract_rules([doc])
    assert extraction.document_type.value == "Debit Note"
    assert len(extraction.charges) == 3
    assert extraction.references["bl_no"] == ["COSU9510258060"]


def test_payload_is_json_serialisable():
    json.dumps(PAYLOAD)


def test_empty_charge_list_does_not_invent_charges():
    payload = {"data": {"list": [{"fileType": "BILL OF LADING", "chargeList": []}]}, "success": True}
    assert find_charges(payload) == []
    assert charges_from_ocr(payload) == []


def test_unknown_shape_falls_back_to_flattened_text():
    payload = {"data": {"foo": "bar", "mblNumber": "COSU123"}}
    lines = render_ocr_text(payload)
    assert any("COSU123" in line for line in lines)
