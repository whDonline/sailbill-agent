"""Extraction orchestration: rules first, model second, conflict-aware merge.

The merge policy is deliberate and asymmetric:

* **amounts and charge rows** found by the deterministic parser (real table
  cells / real OCR fields) always beat the model -- they are transcripts, not
  interpretations;
* **document-level facts** the rules could not find are taken from the model,
  but only after grounding them back into the source text;
* when both sides disagree the rule value wins and a ``[CONFLICT]`` warning is
  recorded, which later lowers confidence and can push the record to
  ``PENDING_REVIEW``.  Disagreement is information, so it is never dropped.
"""

from __future__ import annotations

from ..config import SETTINGS, Settings
from ..models import DocExtraction, NormalizedDocument, Status, Tri
from ..normalize.parties import PartyResolver
from .grounding import apply_grounding
from .llm import LLMExtractor
from .rules import extract_rules

__all__ = ["extract_document", "extract_rules", "merge", "build_hints"]

_FIELDS = (
    "document_type",
    "reference_no",
    "document_date",
    "issuer",
    "recipient",
    "currency",
    "subtotal_amount",
    "tax_amount",
    "total_amount",
    "payment_term",
)


def build_hints(rules: DocExtraction) -> str:
    """Compact rule-engine output used as *locating* context for the model."""
    parts: list[str] = []
    for name in _FIELDS:
        field: Tri = getattr(rules, name)
        if field.is_present:
            parts.append(f"- {name}: {field.value}")
    if rules.references:
        parts.append("- references: " + ", ".join(f"{k}={v}" for k, v in rules.references.items()))
    if rules.charges:
        parts.append(f"- 规则引擎已识别 {len(rules.charges)} 条费用行")
    return "\n".join(parts)


def extract_document(
    docs: list[NormalizedDocument],
    *,
    settings: Settings | None = None,
    resolver: PartyResolver | None = None,
    use_llm: bool = True,
) -> DocExtraction:
    settings = settings or SETTINGS
    resolver = resolver or PartyResolver()

    result = extract_rules(docs, resolver=resolver)
    source_text = "\n".join(d.flatten_text() for d in docs if d.flatten_text())

    if not use_llm or not settings.llm_enabled:
        apply_grounding(result, source_text)
        return result

    try:
        llm_result = LLMExtractor(settings).extract(source_text, hints=build_hints(result))
    except Exception as exc:  # never let the model break the run
        result.warnings.append(f"LLM 抽取不可用，已回退到规则模式: {str(exc)[:200]}")
        apply_grounding(result, source_text)
        return result

    merged = merge(result, llm_result.doc)
    apply_grounding(merged, source_text)
    return merged


def merge(rules: DocExtraction, llm: DocExtraction) -> DocExtraction:
    merged = DocExtraction(extractor="hybrid")
    merged.warnings = list(rules.warnings)
    merged.references = dict(rules.references)

    for name in _FIELDS:
        rule_field: Tri = getattr(rules, name)
        llm_field: Tri = getattr(llm, name)

        if rule_field.is_present and llm_field.is_present:
            if _same(rule_field.value, llm_field.value):
                setattr(merged, name, rule_field)
            else:
                setattr(merged, name, rule_field)
                merged.warnings.append(
                    f"[CONFLICT] {name}: 规则={rule_field.value!r} 模型={llm_field.value!r}（采用规则值）"
                )
        elif rule_field.is_present:
            setattr(merged, name, rule_field)
        elif llm_field.is_present:
            setattr(merged, name, Tri(llm_field.value, llm_field.status, llm_field.evidence))
            merged.warnings.append(f"字段 {name} 由模型补充: {llm_field.value!r}")
        else:
            # preserve the *reason* for emptiness: absent beats unknown
            setattr(merged, name, rule_field if rule_field.status is Status.ABSENT else llm_field)

    if rules.charges:
        merged.charges = rules.charges
        if llm.charges and len(llm.charges) != len(rules.charges):
            merged.warnings.append(
                f"[CONFLICT] 费用行数不一致: 规则={len(rules.charges)} 模型={len(llm.charges)}（采用规则结果）"
            )
        elif llm.charges:
            for rule_charge, llm_charge in zip(rules.charges, llm.charges, strict=True):
                if rule_charge.amount is not None and llm_charge.amount is not None:
                    if abs(float(rule_charge.amount) - float(llm_charge.amount)) > 0.01:
                        merged.warnings.append(
                            f"[CONFLICT] 费用 {rule_charge.charge_item_raw}: "
                            f"规则={rule_charge.amount} 模型={llm_charge.amount}（采用规则值）"
                        )
    else:
        merged.charges = llm.charges
        if llm.charges:
            merged.warnings.append(f"费用明细由模型补充 {len(llm.charges)} 条")

    merged.warnings.extend(llm.warnings)
    return merged


def _same(a: object, b: object, tolerance: float = 0.01) -> bool:
    if a is None or b is None:
        return a is b
    try:
        return abs(float(a) - float(b)) <= tolerance  # type: ignore[arg-type]
    except (TypeError, ValueError):
        pass
    return str(a).strip().lower() == str(b).strip().lower()
