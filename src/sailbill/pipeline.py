"""The pipeline: file -> records.

One pass per *bill unit* (a standalone document, or each attachment of an email),
so an email carrying two different notes yields two sets of records instead of a
merged mess.  Every stage is recorded in a trace, and every decision carries its
reasoning -- which is what the audit trail is built from.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

from .config import SETTINGS, Settings
from .extract import extract_document
from .finance import PostingDecision, determine_posting
from .ingest import load_tree
from .matching import MatchOutcome, match_charge
from .models import (
    ChargeExtraction,
    DocExtraction,
    Issue,
    NormalizedDocument,
    Operation,
    SourceKind,
    Tri,
)
from .normalize.charges import standardize_charge
from .normalize.dates import format_date
from .normalize.parties import PartyResolver
from .output import build_record, summarize
from .store import BillStore
from .validate import has_blocking_issue, score_charge, validate_extraction

MAX_REASONING = 10

_SUPPORTED_SUFFIXES = {
    ".eml", ".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp",
    ".xlsx", ".xls", ".xlsm", ".csv", ".docx", ".doc", ".txt", ".json",
}


def _expand_paths(paths: Iterable[str | Path]) -> list[Path]:
    """Accept files *and* directories (one level, sorted, supported types only)."""
    out: list[Path] = []
    for item in paths:
        path = Path(item)
        if path.is_dir():
            out.extend(sorted(p for p in path.iterdir() if p.is_file() and p.suffix.lower() in _SUPPORTED_SUFFIXES))
        else:
            out.append(path)
    return out


def _needs_ocr(doc: NormalizedDocument, settings: Settings) -> bool:
    if doc.kind is SourceKind.IMAGE:
        return True
    if doc.meta.get("needs_ocr"):
        return True
    return bool(settings.ocr_force_for_pdf and doc.kind is SourceKind.PDF)


def _ensure_ocr(docs: list[NormalizedDocument], settings: Settings, *, ocr_holder: dict) -> None:
    """Attach OCR results to every document that needs them.

    A text-layer PDF is normally *not* sent to OCR: the deterministic text is
    cheaper and more faithful.  Set ``SAILBILL_OCR_FOR_PDF=true`` to force a
    second, independent reading for cross-checking.
    """
    from .ingest.ocr import OcrClient, attach_ocr

    for doc in docs:
        if not _needs_ocr(doc, settings):
            continue
        if not settings.ocr_enabled or settings.file_delivery == "none":
            doc.warnings.append("需要 OCR，但 OCR 未启用（离线模式或未配置文件投递方式），该文件无法解析")
            continue
        client = ocr_holder.get("client")
        if client is None:
            client = OcrClient(settings)
            ocr_holder["client"] = client
        attach_ocr(doc, client)


@dataclass
class UnitOutcome:
    filename: str
    doc_id: str
    extraction: DocExtraction
    records: list = field(default_factory=list)
    extras: list[dict[str, Any]] = field(default_factory=list)
    trace: dict[str, Any] = field(default_factory=dict)


@dataclass
class RunResult:
    units: list[UnitOutcome] = field(default_factory=list)
    summary: dict[str, Any] = field(default_factory=dict)

    @property
    def records(self) -> list:
        return [record for unit in self.units for record in unit.records]

    @property
    def extras(self) -> list[dict[str, Any]]:
        return [extra for unit in self.units for extra in unit.extras]

    @property
    def traces(self) -> list[dict[str, Any]]:
        return [unit.trace for unit in self.units]


# --------------------------------------------------------------------------- #
# document grouping
# --------------------------------------------------------------------------- #
def split_units(docs: list[NormalizedDocument]) -> list[tuple[list[NormalizedDocument], NormalizedDocument]]:
    """Leaf documents become bill units; their ancestors become context."""
    by_id = {doc.doc_id: doc for doc in docs}
    children: dict[str, list[NormalizedDocument]] = {}
    for doc in docs:
        parent = doc.meta.get("parent_id")
        if parent:
            children.setdefault(parent, []).append(doc)

    units: list[tuple[list[NormalizedDocument], NormalizedDocument]] = []
    for doc in docs:
        if children.get(doc.doc_id):
            continue  # container: its children are processed instead
        context: list[NormalizedDocument] = []
        cursor = doc.meta.get("parent_id")
        guard = 0
        while cursor and cursor in by_id and guard < 10:
            context.append(by_id[cursor])
            cursor = by_id[cursor].meta.get("parent_id")
            guard += 1
        units.append((context, doc))
    return units


# --------------------------------------------------------------------------- #
# main entry
# --------------------------------------------------------------------------- #
def run_paths(
    paths: Iterable[str | Path],
    *,
    settings: Settings | None = None,
    store: BillStore | None = None,
    accounting_entity: Optional[str] = None,
    use_llm: bool = True,
    include_context_docs: bool = True,
) -> RunResult:
    settings = settings or SETTINGS
    store = store or BillStore()
    resolver = PartyResolver()
    result = RunResult()

    for path in _expand_paths(paths):
        started = time.perf_counter()
        docs = load_tree(path, settings=settings)
        _ensure_ocr(docs, settings, ocr_holder={})
        for context, primary in split_units(docs):
            unit_context = context if include_context_docs else []
            unit = _process_unit(
                primary,
                unit_context,
                settings=settings,
                store=store,
                resolver=resolver,
                accounting_entity=accounting_entity,
                use_llm=use_llm,
            )
            unit.trace["elapsed_s"] = round(time.perf_counter() - started, 3)
            result.units.append(unit)

    result.summary = summarize(result.records)
    result.summary["files"] = len(list(paths))
    return result


def _process_unit(
    primary: NormalizedDocument,
    context: list[NormalizedDocument],
    *,
    settings: Settings,
    store: BillStore,
    resolver: PartyResolver,
    accounting_entity: Optional[str],
    use_llm: bool,
) -> UnitOutcome:
    started = time.perf_counter()
    docs = [primary, *context]
    extraction = extract_document(docs, settings=settings, resolver=resolver, use_llm=use_llm)
    issues = validate_extraction(extraction, settings=settings)

    entity = accounting_entity or settings.accounting_entity or _infer_entity(extraction, store, resolver)
    posting = determine_posting(extraction, accounting_entity=entity, resolver=resolver)

    outcome = UnitOutcome(
        filename=primary.filename,
        doc_id=primary.doc_id,
        extraction=extraction,
    )

    if not extraction.charges:
        outcome.records.append(
            _pending_record(extraction, posting, issues, reason="未能从单据中提取到任何费用明细")
        )
        outcome.extras.append(
            {
                "source_file": primary.filename,
                "confidence": 0.0,
                "issues": [issue.model_dump() for issue in issues],
                "warnings": extraction.warnings,
                "extractor": extraction.extractor,
            }
        )
    else:
        for charge in extraction.charges:
            match_outcome = match_charge(charge, extraction, store.all(), resolver=resolver)
            confidence = score_charge(
                extraction,
                charge,
                issues=issues,
                match=match_outcome.match,
                ambiguous_match=match_outcome.ambiguous,
                settings=settings,
            )
            record, extra = _decide_and_build(
                charge,
                extraction,
                match_outcome,
                posting,
                issues,
                confidence,
                settings=settings,
                primary=primary,
            )
            outcome.records.append(record)
            outcome.extras.append(extra)

    outcome.trace = {
        "file": primary.filename,
        "doc_id": primary.doc_id,
        "kind": primary.kind.value,
        "context_docs": [doc.filename for doc in context],
        "extractor": extraction.extractor,
        "sha256": primary.sha256,
        "charges": len(extraction.charges),
        "issues": len(issues),
        "errors": sum(1 for issue in issues if issue.severity == "error"),
        "operations": _operation_counts(outcome.records),
        "accounting_entity": entity,
        "posting_type": posting.posting_type,
        "ocr_cached": primary.meta.get("ocr_cached"),
        "warnings": extraction.warnings[:10],
        "elapsed_s": round(time.perf_counter() - started, 3),
    }
    return outcome


def _operation_counts(records: list) -> dict[str, int]:
    counts: dict[str, int] = {}
    for record in records:
        counts[record.operation.value] = counts.get(record.operation.value, 0) + 1
    return counts


# --------------------------------------------------------------------------- #
# decision
# --------------------------------------------------------------------------- #
def _decide_and_build(
    charge: ChargeExtraction,
    ext: DocExtraction,
    match: MatchOutcome,
    posting: PostingDecision,
    issues: list[Issue],
    confidence,
    *,
    settings: Settings,
    primary: NormalizedDocument,
):
    reasoning: list[str] = []
    changed_fields: list[str] = []
    blocking = has_blocking_issue(issues)

    reasoning.append(
        f"来源文件 {primary.filename}（{primary.kind.value}），"
        f"共识别 {len(ext.charges)} 条费用，本记录对应「{charge.charge_item_raw or charge.charge_item}」"
    )
    if charge.evidence:
        reasoning.append(f"费用行原文：{charge.evidence[:160]}")

    bill = match.match.bill if match.match else None
    bill_line = match.match.line if match.match else None

    # ---- decide the operation -------------------------------------------
    if blocking:
        operation = Operation.PENDING_REVIEW
        first_error = next((i for i in issues if i.severity == "error"), None)
        if first_error:
            reasoning.append(f"存在阻断性校验问题，转人工复核：[{first_error.code}] {first_error.message}")
    elif match.ambiguous:
        operation = Operation.PENDING_REVIEW
        reasoning.append("；".join(match.notes))
    elif bill is None:
        operation = Operation.CREATE
        reasoning.append("未匹配到任何已有账单，且金额与币种通过校验，按新增账单处理")
    else:
        changed_fields = _changed_fields(charge, bill_line)
        if changed_fields:
            operation = Operation.UPDATE
            reasoning.append("；".join(match.match.reasons))
            reasoning.append(
                f"匹配到账单 {bill.bill_id}，需变更字段：{', '.join(changed_fields)}"
                + _change_summary(charge, bill_line, changed_fields)
            )
        else:
            operation = Operation.NO_CHANGE
            reasoning.append("；".join(match.match.reasons))
            reasoning.append("输入材料与原账单完全一致，无需变更")

    if operation in {Operation.CREATE, Operation.UPDATE} and confidence.score < settings.review_threshold:
        operation = Operation.PENDING_REVIEW
        reasoning.append(
            f"置信度 {confidence.score:.2f} 低于阈值 {settings.review_threshold:.2f}，转人工复核"
        )

    # An entry without a posting type cannot be booked: AR and AP are different
    # ledgers.  Not knowing the accounting entity is a reason to ask, never a
    # reason to default to AP.
    if operation in {Operation.CREATE, Operation.UPDATE} and posting.posting_type is None:
        operation = Operation.PENDING_REVIEW
        reasoning.append("无法确定应收/应付方向（记账主体未指定或与单据双方均不匹配），转人工复核")

    # ---- merged values (UPDATE inherits what was not explicitly changed) --
    doc_type = _inherit(ext.document_type, bill.document_type if bill else None)
    reference_no = _inherit(ext.reference_no, bill.reference_no if bill else None)
    document_date = format_date(ext.document_date.value) if ext.document_date.is_present else (
        bill.document_date if bill else None
    )
    issuer = _inherit(ext.issuer, bill.issuer if bill else None)
    recipient = _inherit(ext.recipient, bill.recipient if bill else None)

    charge_item = charge.charge_item or charge.charge_item_raw
    if bill_line is not None and standardize_charge(charge_item or "").method == "unmapped":
        charge_item = bill_line.charge_item  # keep the already-standardised name

    amount = charge.amount
    currency = charge.currency or (bill_line.currency if bill_line else None)
    payment_term = charge.payment_term or (bill_line.payment_term if bill_line else None)

    entity = posting.accounting_entity or (bill.accounting_entity if bill else None)
    posting_type = posting.posting_type or (bill.posting_type if bill else None)

    # ---- reasoning tail ---------------------------------------------------
    for reason in posting.reasons:
        reasoning.append(reason)
    reasoning.append(f"置信度 {confidence.score:.2f}" + (
        f"，扣分项：{'；'.join(confidence.reasons[:3])}" if confidence.reasons else ""
    ))
    for issue in issues:
        if issue.code == "DUPLICATE_CHARGE":
            reasoning.append(f"校验提示：[{issue.code}] {issue.message}")
    if ext.warnings:
        interesting = [w for w in ext.warnings if w.startswith("[CONFLICT]") or "OCR" in w][:2]
        for warning in interesting:
            reasoning.append(f"处理依据：{warning}")

    record = build_record(
        operation,
        document_type=doc_type,
        reference_no=reference_no,
        document_date=document_date,
        issuer=issuer,
        recipient=recipient,
        charge_item=charge_item,
        amount=round(float(amount), 2) if amount is not None else None,
        currency=currency,
        payment_term=payment_term,
        accounting_entity=entity,
        posting_type=posting_type,
        reasoning=reasoning[:MAX_REASONING],
    )

    extra = {
        "source_file": primary.filename,
        "doc_id": primary.doc_id,
        "operation": operation.value,
        "confidence": confidence.score,
        "confidence_reasons": confidence.reasons,
        "evidence": charge.evidence,
        "charge_item_raw": charge.charge_item_raw,
        "matched_bill": bill.bill_id if bill else None,
        "matched_line": bill_line.line_id if bill_line else None,
        "match_rule": match.match.rule.value if match.match and match.match.rule else None,
        "match_score": match.match.score if match.match else None,
        "ambiguous": match.ambiguous,
        "changed_fields": changed_fields,
        "issues": [issue.model_dump() for issue in issues],
        "warnings": ext.warnings,
        "extractor": ext.extractor,
        "references": ext.references,
    }
    return record, extra


def _pending_record(
    ext: DocExtraction,
    posting: PostingDecision,
    issues: list[Issue],
    *,
    reason: str,
):
    reasoning = [reason]
    for issue in issues[:4]:
        reasoning.append(f"[{issue.code}] {issue.message}")
    reasoning.extend(posting.reasons[:2])
    if ext.warnings:
        reasoning.append(f"解析提示：{ext.warnings[0][:160]}")
    return build_record(
        Operation.PENDING_REVIEW,
        document_type=_inherit(ext.document_type, None),
        reference_no=_inherit(ext.reference_no, None),
        document_date=format_date(ext.document_date.value) if ext.document_date.is_present else None,
        issuer=_inherit(ext.issuer, None),
        recipient=_inherit(ext.recipient, None),
        charge_item=None,
        amount=None,
        currency=_inherit(ext.currency, None),
        payment_term=_inherit(ext.payment_term, None),
        accounting_entity=posting.accounting_entity,
        posting_type=posting.posting_type,
        reasoning=reasoning[:MAX_REASONING],
    )


def _inherit(field: Tri, fallback: Optional[str]) -> Optional[str]:
    if field.is_present and field.value is not None:
        value = field.value
        return value.isoformat() if hasattr(value, "isoformat") else str(value)
    return fallback


def _changed_fields(charge: ChargeExtraction, line) -> list[str]:
    if line is None:
        return []
    changed: list[str] = []
    if charge.amount is not None and abs(float(charge.amount) - float(line.amount)) > 0.01:
        changed.append("amount")
    if charge.currency and line.currency and charge.currency.upper() != line.currency.upper():
        changed.append("currency")
    if charge.payment_term and line.payment_term and charge.payment_term != line.payment_term:
        changed.append("payment_term")
    if charge.charge_item and line.charge_item:
        if standardize_charge(charge.charge_item).standard != standardize_charge(line.charge_item).standard:
            changed.append("charge_item")
    if charge.quantity is not None and line.quantity is not None and abs(charge.quantity - line.quantity) > 1e-6:
        changed.append("quantity")
    if charge.unit_price is not None and line.unit_price is not None:
        if abs(charge.unit_price - line.unit_price) > 0.01:
            changed.append("unit_price")
    return changed


def _change_summary(charge: ChargeExtraction, line, changed: list[str]) -> str:
    if line is None:
        return ""
    parts: list[str] = []
    if "amount" in changed:
        parts.append(f"金额 {line.amount} -> {charge.amount}")
    if "currency" in changed:
        parts.append(f"币种 {line.currency} -> {charge.currency}")
    if "payment_term" in changed:
        parts.append(f"付款方式 {line.payment_term} -> {charge.payment_term}")
    if "charge_item" in changed:
        parts.append(f"费用名称 {line.charge_item} -> {charge.charge_item}")
    return f"（{', '.join(parts)}）" if parts else ""


def _infer_entity(ext: DocExtraction, store: BillStore, resolver: PartyResolver) -> Optional[str]:
    """Use the accounting entity of the bill we would be updating, else the
    most common one in the store (demo-friendly default, overridable via CLI)."""
    for charge in ext.charges:
        outcome = match_charge(charge, ext, store.all(), resolver=resolver)
        if outcome.match and outcome.match.bill.accounting_entity:
            return outcome.match.bill.accounting_entity
    return store.common_entity()
