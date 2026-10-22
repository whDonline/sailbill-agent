"""Evidence grounding: every extracted value must be findable in the source.

This is the cheapest and most effective anti-hallucination control in the whole
pipeline -- cheaper than a better prompt, and *verifiable* by a reviewer in one
line of code.  Values that cannot be grounded are downgraded to ``unknown``
(never silently kept, never fabricated).
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from ..models import DocExtraction, Tri

_NON_KEY = re.compile(r"[^0-9a-z\u4e00-\u9fff]+")


def normalize_for_match(text: Any) -> str:
    return _NON_KEY.sub("", str(text or "").lower())


def ground_string(value: Any, haystack_norm: str) -> bool:
    key = normalize_for_match(value)
    if not key:
        return True
    if key in haystack_norm:
        return True
    # tolerate a truncated rendering ("ABC Logistics (Shanghai)" vs full legal name)
    if len(key) >= 8 and key[:8] in haystack_norm:
        return True
    return False


def number_variants(value: float) -> Iterable[str]:
    """Renderings a number may take in a document."""
    text = f"{value:.2f}"
    plain = text.rstrip("0").rstrip(".") if "." in text else text
    return {
        text,
        f"{value:,.2f}",
        plain,
        plain.replace(".00", ""),
        f"{int(value)}" if float(value).is_integer() else text,
    }


def ground_number(value: float, haystack: str) -> bool:
    compact = haystack.replace(" ", "").replace("\u00a0", "")
    for variant in number_variants(value):
        if variant and variant in compact:
            return True
    return False


def apply_grounding(doc: DocExtraction, source_text: str) -> list[str]:
    """Downgrade ungrounded values; returns the list of downgrades."""
    haystack_norm = normalize_for_match(source_text)
    downgrades: list[str] = []

    for name in ("document_type", "reference_no", "issuer", "recipient"):
        field: Tri = getattr(doc, name)
        if field.is_present and not ground_string(field.value, haystack_norm):
            setattr(doc, name, Tri.unknown(f"证据未能定位（原值 {field.value!r}）"))
            downgrades.append(name)

    for name in ("subtotal_amount", "tax_amount", "total_amount"):
        field = getattr(doc, name)
        if field.is_present and not ground_number(float(field.value), source_text):
            setattr(doc, name, Tri.unknown(f"金额未在原文出现（原值 {field.value}）"))
            downgrades.append(name)

    kept: list = []
    for i, charge in enumerate(doc.charges):
        if charge.amount is None:
            continue
        if not ground_number(float(charge.amount), source_text):
            downgrades.append(f"charges[{i}].amount")
            doc.warnings.append(
                f"费用金额 {charge.amount}（{charge.charge_item_raw}）未能在原文定位，已丢弃该行"
            )
            continue
        kept.append(charge)
    doc.charges = kept

    if downgrades:
        doc.warnings.append(f"证据锚定校验：{len(downgrades)} 个字段因原文无法定位被降级/丢弃")
    return downgrades
