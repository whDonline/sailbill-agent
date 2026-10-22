"""Bill matching: the four rules from the assignment, in priority order.

    1. reference_no + charge item
    2. B/L no        + charge item + currency
    3. booking no    + charge item + currency
    4. container no  + parties + date + amount

Two behaviours matter more than the rules themselves:

* **priority is absolute** -- a weaker rule never overrules a stronger one, so a
  recycled B/L number cannot hijack a DN that matches by its own number;
* **ambiguity is surfaced, not resolved by coin flip** -- if two bills match at
  the same level with different amounts, the record goes to review.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date

from .models import (
    Bill,
    BillLine,
    ChargeExtraction,
    DocExtraction,
    MatchResult,
    MatchRule,
)
from .normalize.charges import standardize_charge
from .normalize.currency import is_valid_iso
from .normalize.dates import parse_date
from .normalize.parties import PartyResolver

_DATE_WINDOW_DAYS = 120


@dataclass
class MatchOutcome:
    match: MatchResult | None = None
    candidates: list[MatchResult] = field(default_factory=list)
    ambiguous: bool = False
    notes: list[str] = field(default_factory=list)

    @property
    def rule(self) -> MatchRule | None:
        return self.match.rule if self.match else None


def _norm_ref(value: object) -> str:
    return "".join(ch for ch in str(value or "").upper() if ch.isalnum())


def _same_charge(a: object, b: object) -> bool:
    sa = standardize_charge(a).standard or ""
    sb = standardize_charge(b).standard or ""
    if not sa or not sb:
        return False
    return _norm_ref(sa) == _norm_ref(sb)


def _refs_match(doc_refs: dict[str, list[str]], bill_refs: dict[str, list[str]], key: str) -> list[str]:
    doc_values = {_norm_ref(v) for v in doc_refs.get(key, []) if _norm_ref(v)}
    bill_values = {_norm_ref(v) for v in bill_refs.get(key, []) if _norm_ref(v)}
    if not doc_values or not bill_values:
        return []
    return sorted(doc_values & bill_values)


def _lines_for(bill: Bill, charge: ChargeExtraction) -> list[BillLine]:
    return [line for line in bill.lines if _same_charge(line.charge_item, charge.charge_item or charge.charge_item_raw)]


def _best_line(lines: list[BillLine], charge: ChargeExtraction) -> BillLine | None:
    if not lines:
        return None
    amount = charge.amount
    if amount is None:
        return lines[0]
    return min(lines, key=lambda line: abs(float(line.amount) - float(amount)))


def match_charge(
    charge: ChargeExtraction,
    doc: DocExtraction,
    bills: Iterable[Bill],
    *,
    resolver: PartyResolver | None = None,
) -> MatchOutcome:
    """Try the four rules in order and return the winning match."""
    resolver = resolver or PartyResolver()
    bills = list(bills)
    outcome = MatchOutcome()

    # ---- rule 1: reference_no + charge --------------------------------
    ref_no = _norm_ref(doc.reference_no.value) if doc.reference_no.is_present else ""
    if ref_no:
        candidates = []
        for bill in bills:
            if _norm_ref(bill.reference_no) != ref_no:
                continue
            lines = _lines_for(bill, charge)
            if not lines:
                continue
            line = _best_line(lines, charge)
            candidates.append(
                MatchResult(
                    bill=bill,
                    line=line,
                    rule=MatchRule.REF_AND_CHARGE,
                    score=_score_amount(line, charge, base=1.0),
                    reasons=[f"单据编号一致: {doc.reference_no.value}", f"费用名称一致: {line.charge_item}"],
                )
            )
        if candidates:
            return _decide(outcome, candidates, "第 1 级规则（单据编号+费用名称）")

    # ---- rule 2/3: BL / booking + charge + currency ---------------------
    for rule, key, label in (
        (MatchRule.BL_AND_CHARGE, "bl_no", "提单号"),
        (MatchRule.BOOKING_AND_CHARGE, "booking_no", "订舱号"),
    ):
        candidates = []
        for bill in bills:
            shared = _refs_match(doc.references, bill.references, key)
            if not shared:
                continue
            lines = _lines_for(bill, charge)
            if not lines:
                continue
            currency_ok = _currency_matches(charge, lines)
            if not currency_ok:
                continue
            line = _best_line(currency_ok, charge)
            candidates.append(
                MatchResult(
                    bill=bill,
                    line=line,
                    rule=rule,
                    score=_score_amount(line, charge, base=0.95),
                    reasons=[f"{label}一致: {', '.join(shared)}", f"费用名称一致: {line.charge_item}"],
                )
            )
        if candidates:
            return _decide(outcome, candidates, f"{label}+费用名称+币种")

    # ---- rule 4: container + parties + date + amount ---------------------
    containers: set[str] = {_norm_ref(v) for v in doc.references.get("container_no", []) if _norm_ref(v)}
    if containers:
        candidates = []
        for bill in bills:
            shared = containers & {_norm_ref(v) for v in bill.references.get("container_no", [])}
            if not shared:
                continue
            party_ok, party_score = _parties_match(doc, bill, resolver)
            if not party_ok:
                continue
            date_ok = _dates_close(doc, bill)
            if not date_ok:
                continue
            lines = [line for line in bill.lines if _amount_close(line.amount, charge.amount)]
            if not lines:
                continue
            line = lines[0]
            candidates.append(
                MatchResult(
                    bill=bill,
                    line=line,
                    rule=MatchRule.CONTAINER_PARTY_DATE_AMOUNT,
                    score=round(0.6 * party_score + 0.2, 3),
                    reasons=[
                        f"箱号一致: {', '.join(sorted(shared))}",
                        "交易双方一致",
                        "单据日期接近",
                        f"金额一致: {line.amount}",
                    ],
                )
            )
        if candidates:
            return _decide(outcome, candidates, "第 4 级规则（箱号+交易方+日期+金额）")

    outcome.notes.append("未找到可匹配的已有账单")
    return outcome


def _decide(outcome: MatchOutcome, candidates: list[MatchResult], rule_label: str) -> MatchOutcome:
    candidates.sort(key=lambda c: c.score, reverse=True)
    outcome.candidates = candidates
    best = candidates[0]
    outcome.match = best

    distinct_bills = {c.bill.bill_id for c in candidates}
    if len(distinct_bills) > 1:
        # same rule, different bills: only ambiguous if the amounts disagree
        amounts = {round(float(c.line.amount), 2) for c in candidates if c.line}
        if len(amounts) > 1 or len(distinct_bills) > 2:
            outcome.ambiguous = True
            outcome.notes.append(
                f"{rule_label} 命中 {len(distinct_bills)} 张账单且金额不一致，需人工确认"
            )
        else:
            outcome.notes.append(f"{rule_label} 命中多张账单但金额一致，取编号 {best.bill.bill_id}")

    outcome.notes.append(f"命中 {rule_label}，账单 {best.bill.bill_id}")
    return outcome


def _score_amount(line: BillLine, charge: ChargeExtraction, *, base: float) -> float:
    if line is None or charge.amount is None:
        return base
    if abs(float(line.amount) - float(charge.amount)) <= 0.01:
        return base
    return round(base - 0.1, 3)


def _currency_matches(charge: ChargeExtraction, lines: list[BillLine]) -> list[BillLine]:
    currency = (charge.currency or "").upper()
    if not is_valid_iso(currency):
        return lines  # cannot filter, but the match is weakened by confidence
    matched = [line for line in lines if (line.currency or "").upper() == currency]
    return matched


def _parties_match(doc: DocExtraction, bill: Bill, resolver: PartyResolver) -> tuple[bool, float]:
    scores: list[float] = []
    for doc_side, bill_side in (
        (doc.issuer.value, bill.issuer),
        (doc.recipient.value, bill.recipient),
    ):
        if not doc_side or not bill_side:
            continue
        same, score = resolver.same(doc_side, bill_side)
        scores.append(score if same else 0.0)
    if not scores:
        return False, 0.0
    if 0.0 in scores:
        return False, 0.0
    return True, sum(scores) / len(scores)


def _dates_close(doc: DocExtraction, bill: Bill, window: int = _DATE_WINDOW_DAYS) -> bool:
    if not doc.document_date.is_present or not bill.document_date:
        return False
    doc_date = doc.document_date.value
    bill_date = parse_date(bill.document_date)
    if not isinstance(doc_date, date) or bill_date is None:
        return False
    return abs((doc_date - bill_date).days) <= window


def _amount_close(a: float | None, b: float | None, tol: float = 0.01) -> bool:
    if a is None or b is None:
        return False
    return abs(float(a) - float(b)) <= tol
