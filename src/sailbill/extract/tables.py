"""Charge-table understanding.

Bills in the wild put the fee table at row 3, or 9, or inside a Word table with
a two-row header and merged cells.  This module finds the header, maps columns
onto semantic fields and turns data rows into candidate charges.  Everything
here is deterministic and unit-tested, so a table-driven document never depends
on the model at all.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

from ..models import ChargeExtraction, TableData
from ..normalize.amounts import parse_amount, round2
from ..normalize.charges import standardize_charge
from ..normalize.currency import normalize_currency
from . import lexicon

_CJK = "\u4e00-\u9fff"


def _norm(text: object) -> str:
    t = str(text or "").lower().replace("&", " and ")
    t = re.sub(r"[^0-9a-z" + _CJK + r"]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _label_score(cell: str, labels: Iterable[str]) -> bool:
    """Cell starts with one of the labels (never the reverse: 'Amount' must not
    match the label 'Amount Due')."""
    c = _norm(cell)
    if not c:
        return False
    for label in labels:
        ln = _norm(label)
        if not ln:
            continue
        if c == ln:
            return True
        if c.startswith(ln) and len(ln) >= 3:
            return True
    return False


def _label_exact(cell: str, labels: Iterable[str]) -> bool:
    c = _norm(cell)
    return bool(c) and any(_norm(label) == c for label in labels if _norm(label))


@dataclass
class ColumnMap:
    charge: int | None = None
    quantity: int | None = None
    unit_price: int | None = None
    amount: int | None = None
    currency: int | None = None
    payment_term: int | None = None
    tax: int | None = None
    subtotal: int | None = None
    total: int | None = None

    @property
    def has_charge(self) -> bool:
        return self.charge is not None

    @property
    def has_amount(self) -> bool:
        return self.amount is not None or self.unit_price is not None

    @property
    def usable(self) -> bool:
        return self.has_charge and self.has_amount


# most specific first: the generic "amount" column must be tried last so that
# "Total Amount" becomes a total, not an amount
_FIELD_PRIORITY = (
    "quantity",
    "unit_price",
    "currency",
    "payment_term",
    "tax",
    "subtotal",
    "total",
    "charge",
    "amount",
)


def detect_columns(header: list[str]) -> ColumnMap:
    """Map header cells onto semantic columns.

    Two passes: exact label matches win over prefix matches, and fields are
    assigned in priority order, so "Amount" never lands on ``total``.
    """
    mapping = ColumnMap()
    for exact in (True, False):
        matcher = _label_exact if exact else _label_score
        for idx, cell in enumerate(header):
            if not str(cell or "").strip():
                continue
            for field in _FIELD_PRIORITY:
                if getattr(mapping, field) is not None:
                    continue
                if matcher(cell, lexicon.COLUMN_LABELS[field]):
                    setattr(mapping, field, idx)
                    break
    return mapping


def find_header(table: TableData, window: int = 15) -> tuple[int | None, ColumnMap]:
    """Locate the header row of a charge table inside a parsed table."""
    start = table.header_row if table.header_row is not None else 0
    candidates = list(range(max(0, start - 2), min(len(table.rows), max(start, 0) + window)))
    for idx in candidates:
        columns = detect_columns(table.rows[idx])
        if columns.charge is not None and (
            columns.amount is not None or columns.unit_price is not None or columns.currency is not None
        ):
            return idx, columns
    for idx in candidates:
        columns = detect_columns(table.rows[idx])
        if columns.charge is not None and sum(1 for c in table.rows[idx] if str(c).strip()) >= 2:
            return idx, columns
    return None, ColumnMap()


def is_footer(row: list[str]) -> bool:
    text = " ".join(_norm(c) for c in row if c)
    if not text:
        return False
    for label in lexicon.FOOTER_LABELS:
        ln = _norm(label)
        if text.strip().startswith(ln) or f" {ln}" == text[-len(ln) - 1:]:
            return True
    return False


def _cell(row: list[str], idx: int | None) -> str:
    if idx is None or idx >= len(row):
        return ""
    return str(row[idx] or "").strip()


def table_to_charges(
    table: TableData,
    *,
    doc_currency: str | None = None,
    context: str = "",
) -> tuple[list[ChargeExtraction], dict[str, float], list[str]]:
    """Extract charges plus any totals found in the table."""
    charges: list[ChargeExtraction] = []
    totals: dict[str, float] = {}
    warnings: list[str] = []

    header_idx, columns = find_header(table)
    if header_idx is None:
        return _fallback_rows(table, doc_currency=doc_currency, context=context)

    for r in range(header_idx + 1, len(table.rows)):
        row = table.rows[r]
        if not any(str(c).strip() for c in row):
            continue

        if is_footer(row):
            _harvest_total(row, columns, totals)
            continue

        name = _cell(row, columns.charge)
        if not name or _norm(name) in {"total", "subtotal"}:
            continue

        amount_cell = _cell(row, columns.amount)
        amount_parse = parse_amount(amount_cell) if amount_cell else None
        qty = parse_amount(_cell(row, columns.quantity)) if columns.quantity is not None else None
        unit = parse_amount(_cell(row, columns.unit_price)) if columns.unit_price is not None else None

        amount = amount_parse.value if amount_parse else None
        if amount is None and unit and unit.value is not None and qty and qty.value is not None:
            amount = round2(unit.value * qty.value)
            warnings.append(f"行 {r} 缺少金额，由 数量×单价 推导: {name} = {amount}")
        if amount is None:
            warnings.append(f"行 {r} 无法解析金额，已跳过: {' | '.join(row)}")
            continue

        currency_cell = _cell(row, columns.currency)
        currency = None
        if currency_cell:
            cur = normalize_currency(currency_cell, context=context or name)
            currency = cur.code
        if not currency:
            currency = (amount_parse.currency_hint if amount_parse else None) or doc_currency

        term_cell = _cell(row, columns.payment_term)
        charge = ChargeExtraction(
            charge_item_raw=name,
            charge_item=standardize_charge(name).standard,
            quantity=qty.value if qty else None,
            unit_price=unit.value if unit else None,
            amount=round2(amount),
            currency=currency,
            payment_term=term_cell or None,
            evidence=" | ".join(c for c in row if c),
            source_row=r,
        )
        for w in (amount_parse.warnings if amount_parse else []):
            warnings.append(f"行 {r}: {w}")
        charges.append(charge)

    if not charges:
        return _fallback_rows(table, doc_currency=doc_currency, context=context)
    return charges, totals, warnings


def _harvest_total(row: list[str], columns: ColumnMap, totals: dict[str, float]) -> None:
    text = _norm(" ".join(c for c in row if c))
    value = None
    for cell in reversed(row):
        parsed = parse_amount(cell)
        if parsed.ok:
            value = parsed.value
            break
    if value is None:
        return
    if "subtotal" in text or "sub total" in text or "小计" in text:
        totals.setdefault("subtotal_amount", value)
    elif "tax" in text or "vat" in text or "gst" in text or "税" in text:
        totals.setdefault("tax_amount", value)
    elif any(k in text for k in ("total", "合计", "总计", "amount due")):
        totals.setdefault("total_amount", value)


def _fallback_rows(
    table: TableData, *, doc_currency: str | None, context: str
) -> tuple[list[ChargeExtraction], dict[str, float], list[str]]:
    """Handle header-less tables: ``<charge name> | <amount>`` rows."""
    charges: list[ChargeExtraction] = []
    warnings: list[str] = []
    totals: dict[str, float] = {}
    for r, row in enumerate(table.rows):
        cells = [str(c).strip() for c in row if str(c).strip()]
        if len(cells) < 2:
            continue
        if is_footer(row):
            _harvest_total(row, ColumnMap(), totals)
            continue
        name = cells[0]
        parsed = parse_amount(cells[-1])
        if not parsed.ok:
            continue
        std = standardize_charge(name)
        if std.method == "unmapped":
            continue
        charges.append(
            ChargeExtraction(
                charge_item_raw=name,
                charge_item=std.standard,
                amount=round2(parsed.value or 0.0),
                currency=parsed.currency_hint or doc_currency,
                evidence=" | ".join(cells),
                source_row=r,
            )
        )
    if charges:
        warnings.append(f"表格 {table.table_id} 未识别到表头，按两列格式解析 {len(charges)} 条")
    return charges, totals, warnings
