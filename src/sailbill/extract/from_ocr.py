"""Turn a raw OCR payload into charge rows.

The OCR service returns *domain structured* JSON (``data.list[].chargeList[]``)
rather than free text, and its schema is undocumented and varies per document
type.  ``ingest.ocr`` already knows how to find the charge list tolerantly; this
module turns those dicts into validated :class:`ChargeExtraction` rows, applying
exactly the same deterministic normalisation the table/text paths use.

Keeping it here (not in ``ingest``) means the ingester stays a pure parser.
"""

from __future__ import annotations

from typing import Any

from ..ingest.ocr import find_charges
from ..models import ChargeExtraction
from ..normalize.amounts import parse_amount, round2
from ..normalize.charges import standardize_charge
from ..normalize.currency import normalize_currency
from ..normalize.payment import normalize_payment_term


def charges_from_ocr(payload: Any, *, doc_currency: str | None = None) -> list[ChargeExtraction]:
    out: list[ChargeExtraction] = []
    for row in find_charges(payload):
        raw_name = row.get("charge_item_raw") or row.get("description")
        amount_parse = parse_amount(row.get("amount"))
        quantity = parse_amount(row.get("quantity")) if row.get("quantity") is not None else None
        unit_price = parse_amount(row.get("unit_price")) if row.get("unit_price") is not None else None

        amount = amount_parse.value
        if amount is None and quantity and quantity.value is not None and unit_price and unit_price.value is not None:
            amount = round2(quantity.value * unit_price.value)
        if amount is None:
            continue

        currency = amount_parse.currency_hint
        if row.get("currency"):
            currency = normalize_currency(row["currency"], context=str(raw_name or "")).code or currency
        currency = currency or doc_currency

        raw_text = str(raw_name or "").strip()
        payment = normalize_payment_term(row.get("payment_term")).term if row.get("payment_term") else None

        out.append(
            ChargeExtraction(
                charge_item_raw=raw_text or None,
                charge_item=standardize_charge(raw_text).standard if raw_text else None,
                description=str(row.get("description") or "") or None,
                quantity=quantity.value if quantity else None,
                unit_price=unit_price.value if unit_price else None,
                amount=round2(amount),
                currency=currency,
                payment_term=payment,
                evidence=_evidence(row),
            )
        )
    return out


def _evidence(row: dict[str, Any]) -> str | None:
    parts = [
        f"{key}={row.get(key)}"
        for key in ("charge_item_raw", "quantity", "unit_price", "amount", "currency", "payment_term")
        if row.get(key) not in (None, "")
    ]
    return "OCR: " + " | ".join(parts) if parts else None
