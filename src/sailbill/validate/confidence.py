"""Confidence scoring.

The number itself matters less than the fact that it is **explainable**: every
deduction is recorded with a human-readable reason which is written straight
into ``audit_trail.ai_reasoning``.  A reviewer can therefore see exactly why a
record was sent to ``PENDING_REVIEW`` instead of being booked.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Optional

from ..config import SETTINGS, Settings
from ..models import ChargeExtraction, DocExtraction, Issue, MatchResult
from ..normalize.charges import standardize_charge


@dataclass
class Confidence:
    score: float = 1.0
    reasons: list[str] = field(default_factory=list)

    def deduct(self, amount: float, reason: str) -> None:
        if amount <= 0:
            if reason:
                self.reasons.append(f"  {reason}")
            return
        self.score = max(0.0, self.score - amount)
        self.reasons.append(f"-{amount:.2f} {reason}")

    def as_json(self) -> dict:
        return {"score": round(self.score, 3), "reasons": self.reasons}


def score_charge(
    ext: DocExtraction,
    charge: ChargeExtraction,
    *,
    issues: Iterable[Issue] = (),
    match: Optional[MatchResult] = None,
    ambiguous_match: bool = False,
    settings: Settings | None = None,
) -> Confidence:
    settings = settings or SETTINGS
    conf = Confidence()

    # ---- extraction completeness ---------------------------------------
    if ext.document_type.is_absent:
        conf.deduct(0.15, "单据类型未识别")
    if ext.reference_no.is_absent:
        conf.deduct(0.10, "单据编号未识别")
    if ext.issuer.is_unknown:
        conf.deduct(0.08, "签发方未识别")
    if ext.recipient.is_unknown:
        conf.deduct(0.08, "接收方未识别")
    if ext.document_date.is_unknown:
        conf.deduct(0.06, "单据日期未识别")

    # ---- the charge line itself -----------------------------------------
    std = standardize_charge(charge.charge_item_raw or charge.charge_item)
    if std.method == "unmapped":
        conf.deduct(0.12, f"费用名 {charge.charge_item_raw!r} 未在标准词表")
    elif std.method == "fuzzy":
        conf.deduct(0.06, f"费用名 {charge.charge_item_raw!r} 为模糊匹配（{std.score:.2f}）")
    if charge.currency is None:
        conf.deduct(0.15, "币种缺失")
    if charge.amount is None:
        conf.deduct(0.30, "金额缺失")
    if not charge.evidence:
        conf.deduct(0.05, "缺少原文依据片段")

    # ---- document-level issues -------------------------------------------
    for issue in issues:
        if issue.severity == "error":
            conf.deduct(0.25, f"[{issue.code}] {issue.message}")
        elif issue.severity == "warn":
            conf.deduct(0.05, f"[{issue.code}] {issue.message}")

    # ---- matching ---------------------------------------------------------
    if match is not None:
        if match.rule is not None and match.rule.value.startswith("4."):
            conf.deduct(0.10, "仅通过第 4 级规则（箱号+交易方+日期+金额）匹配，可靠性较低")
        if match.score < 1.0:
            conf.deduct((1.0 - match.score) * 0.2, f"匹配得分 {match.score:.2f}")
    if ambiguous_match:
        conf.deduct(0.30, "存在多个候选账单，匹配不唯一")

    if any("[CONFLICT]" in w for w in ext.warnings):
        conf.deduct(0.10, "规则引擎与模型结果存在冲突")
    if any("OCR" in w and "修正" in w for w in ext.warnings):
        conf.deduct(0.05, "OCR 数字混淆已自动修正")

    conf.score = round(min(1.0, max(0.0, conf.score)), 3)
    return conf
