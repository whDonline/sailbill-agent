"""Business validation: the arithmetic and sanity gates before anything is booked.

Each check returns an :class:`Issue` with a severity.  ``error`` blocks booking
(forces ``PENDING_REVIEW``), ``warn`` lowers confidence, ``info`` is recorded in
the audit trail only.  Checks are pure functions of the extraction, so they are
cheap to unit test and impossible to "prompt away".
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Iterable

from ..config import SETTINGS, Settings
from ..models import DocExtraction, Issue
from ..normalize.charges import standardize_charge
from ..normalize.currency import is_valid_iso


def validate_extraction(ext: DocExtraction, *, settings: Settings | None = None) -> list[Issue]:
    settings = settings or SETTINGS
    issues: list[Issue] = []

    # ---- amounts --------------------------------------------------------
    if not ext.charges:
        issues.append(
            Issue(code="NO_CHARGE", message="单据中未提取到任何费用明细", severity="error", field="charges")
        )
    for i, charge in enumerate(ext.charges):
        if charge.amount is None:
            issues.append(
                Issue(code="AMOUNT_MISSING", message=f"第 {i + 1} 条费用缺少金额", severity="error",
                      field="amount")
            )
            continue
        if not isinstance(charge.amount, (int, float)):
            issues.append(
                Issue(code="AMOUNT_TYPE", message=f"第 {i + 1} 条费用金额不是数字类型", severity="error",
                      field="amount")
            )
            continue
        if charge.amount < 0:
            issues.append(
                Issue(code="AMOUNT_NEGATIVE", message=f"第 {i + 1} 条费用金额为负数: {charge.amount}",
                      severity="warn", field="amount")
            )
        if charge.amount == 0:
            issues.append(
                Issue(code="AMOUNT_ZERO", message=f"第 {i + 1} 条费用金额为 0，需确认是否为免收",
                      severity="warn", field="amount")
            )

    # ---- currency -------------------------------------------------------
    for i, charge in enumerate(ext.charges):
        if charge.currency is None:
            issues.append(
                Issue(code="CURRENCY_MISSING", message=f"第 {i + 1} 条费用缺少币种", severity="error",
                      field="currency")
            )
        elif not is_valid_iso(charge.currency):
            issues.append(
                Issue(code="CURRENCY_INVALID", message=f"第 {i + 1} 条费用币种不符合 ISO 4217: "
                                                      f"{charge.currency}", severity="error", field="currency")
            )
    currencies = {c.currency for c in ext.charges if c.currency}
    if len(currencies) > 1 and ext.total_amount.is_present:
        issues.append(
            Issue(code="MULTI_CURRENCY", message=f"单据含多种币种 {sorted(currencies)}，合计金额无法直接校验",
                  severity="warn", field="currency")
        )

    # ---- arithmetic -----------------------------------------------------
    tol = settings.amount_tolerance
    single_currency = len(currencies) <= 1
    if ext.charges and single_currency:
        charge_sum = round(sum(float(c.amount or 0) for c in ext.charges), 2)

        if ext.subtotal_amount.is_present:
            subtotal = float(ext.subtotal_amount.value or 0)
            if abs(charge_sum - subtotal) > max(tol, tol * len(ext.charges)):
                issues.append(
                    Issue(
                        code="SUBTOTAL_MISMATCH",
                        message=f"费用明细合计 {charge_sum:.2f} 与小计 {subtotal:.2f} 不一致"
                                f"（差额 {charge_sum - subtotal:+.2f}）",
                        severity="warn" if abs(charge_sum - subtotal) <= 1.0 else "error",
                        field="subtotal_amount",
                    )
                )
        elif ext.total_amount.is_present and not ext.tax_amount.is_present:
            total = float(ext.total_amount.value or 0)
            if abs(charge_sum - total) > max(tol, tol * len(ext.charges)):
                issues.append(
                    Issue(
                        code="TOTAL_MISMATCH",
                        message=f"费用明细合计 {charge_sum:.2f} 与总额 {total:.2f} 不一致",
                        severity="warn" if abs(charge_sum - total) <= 1.0 else "error",
                        field="total_amount",
                    )
                )

    if ext.subtotal_amount.is_present and ext.total_amount.is_present:
        subtotal = float(ext.subtotal_amount.value or 0)
        total = float(ext.total_amount.value or 0)
        tax = float(ext.tax_amount.value or 0) if ext.tax_amount.is_present else 0.0
        diff = abs(subtotal + tax - total)
        if diff > max(tol, 0.05):
            issues.append(
                Issue(
                    code="TOTAL_MISMATCH",
                    message=f"小计 {subtotal:.2f} + 税额 {tax:.2f} ≠ 总额 {total:.2f}"
                            f"（差额 {diff:.2f}）",
                    severity="error" if diff > 1.0 else "warn",
                    field="total_amount",
                )
            )

    # ---- duplicates -----------------------------------------------------
    seen: dict[tuple, int] = {}
    for i, charge in enumerate(ext.charges):
        key = (str(charge.charge_item or charge.charge_item_raw), round(float(charge.amount or 0), 2),
               charge.currency)
        if key in seen:
            issues.append(
                Issue(
                    code="DUPLICATE_CHARGE",
                    message=f"疑似重复提取同一费用: {charge.charge_item_raw} {charge.amount} "
                            f"{charge.currency or ''}（与第 {seen[key] + 1} 条相同）",
                    severity="warn",
                    field="charges",
                )
            )
        else:
            seen[key] = i

    # ---- charge name standardisation ------------------------------------
    for i, charge in enumerate(ext.charges):
        std = standardize_charge(charge.charge_item_raw or charge.charge_item)
        if std.method == "unmapped":
            issues.append(
                Issue(
                    code="CHARGE_UNMAPPED",
                    message=f"费用名 {charge.charge_item_raw!r} 不在标准词表，已保留原名",
                    severity="warn",
                    field="charge_item",
                )
            )
        elif std.method == "fuzzy":
            issues.append(
                Issue(
                    code="CHARGE_FUZZY",
                    message=f"费用名 {charge.charge_item_raw!r} 模糊匹配到 {std.standard!r}，建议确认",
                    severity="warn",
                    field="charge_item",
                )
            )

    # ---- document level --------------------------------------------------
    if ext.document_type.is_present and ext.document_type.value not in {
        "Debit Note", "Credit Note", "Invoice", "Statement of Account",
    }:
        issues.append(
            Issue(code="DOC_TYPE_UNSUPPORTED", message=f"单据类型 {ext.document_type.value} 不是账单类单据",
                  severity="warn", field="document_type")
        )
    if ext.document_type.is_absent:
        issues.append(Issue(code="DOC_TYPE_MISSING", message="未识别到单据类型", severity="warn",
                            field="document_type"))
    if ext.reference_no.is_absent:
        issues.append(Issue(code="REF_MISSING", message="未识别到单据编号", severity="warn",
                            field="reference_no"))
    if ext.document_date.is_present:
        d = ext.document_date.value
        if isinstance(d, date):
            if d > date.today() + timedelta(days=7):
                issues.append(
                    Issue(code="DATE_FUTURE", message=f"单据日期 {d} 晚于今天，疑似年份识别错误",
                          severity="warn", field="document_date")
                )
            if d < date(2000, 1, 1):
                issues.append(
                    Issue(code="DATE_IMPLAUSIBLE", message=f"单据日期 {d} 早于 2000 年",
                          severity="warn", field="document_date")
                )
    elif ext.document_date.is_unknown:
        issues.append(Issue(code="DATE_MISSING", message="未能识别单据日期", severity="warn",
                            field="document_date"))

    return issues


def has_blocking_issue(issues: Iterable[Issue]) -> bool:
    return any(i.severity == "error" for i in issues)


def issues_by_code(issues: Iterable[Issue]) -> dict[str, list[Issue]]:
    out: dict[str, list[Issue]] = {}
    for issue in issues:
        out.setdefault(issue.code, []).append(issue)
    return out
