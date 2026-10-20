"""Payment term normalisation (PP / CC and friends)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

PREPAID = "PP"
COLLECT = "CC"

_TERMS: dict[str, tuple[str, float]] = {
    "pp": (PREPAID, 1.0),
    "p/p": (PREPAID, 1.0),
    "p p": (PREPAID, 1.0),
    "prepaid": (PREPAID, 1.0),
    "pre paid": (PREPAID, 1.0),
    "freight prepaid": (PREPAID, 1.0),
    "prepayment": (PREPAID, 0.95),
    "paid": (PREPAID, 0.85),
    "预付": (PREPAID, 1.0),
    "预付运费": (PREPAID, 1.0),
    "预付(pp)": (PREPAID, 1.0),
    "cc": (COLLECT, 1.0),
    "c/c": (COLLECT, 1.0),
    "c c": (COLLECT, 1.0),
    "collect": (COLLECT, 1.0),
    "freight collect": (COLLECT, 1.0),
    "到付": (COLLECT, 1.0),
    "到付运费": (COLLECT, 1.0),
    "对方付": (COLLECT, 0.9),
    "third party": (COLLECT, 0.75),
    "third party pay": (COLLECT, 0.75),
    "第三方付": (COLLECT, 0.75),
}

_FOLD = re.compile(r"[^0-9a-z\u4e00-\u9fff/()]+")


@dataclass
class PaymentTerm:
    term: Optional[str]
    raw: str = ""
    score: float = 0.0
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.term in {PREPAID, COLLECT}


def normalize_payment_term(raw: object) -> PaymentTerm:
    """Return ``PP``, ``CC`` or ``None`` (never a guess)."""
    if raw is None:
        return PaymentTerm(term=None, raw="", warnings=["付款方式为空"])
    text = str(raw).strip()
    if not text:
        return PaymentTerm(term=None, raw=text, warnings=["付款方式为空"])

    key = _FOLD.sub(" ", text.lower()).strip()
    key = re.sub(r"\s+", " ", key)
    if key in _TERMS:
        term, score = _TERMS[key]
        return PaymentTerm(term=term, raw=text, score=score)

    # "FREIGHT PREPAID / PP", "P.P."
    compact = key.replace(" ", "").replace(".", "")
    for candidate, (term, score) in _TERMS.items():
        c = candidate.replace(" ", "")
        if c and c in compact:
            return PaymentTerm(term=term, raw=text, score=score * 0.9)

    return PaymentTerm(term=None, raw=text, warnings=[f"无法识别的付款方式: {text!r}"])
