"""AR / AP determination.

The assignment is explicit: *do not* map Debit Note -> AP.  The posting type is a
function of **who the accounting entity is relative to the two parties**, and
the document type only decides the *direction* of the entry (a credit note
reverses the same side).  When the entity cannot be tied to either party the
record must go to review rather than default to AP -- a wrong AP entry posts
money that is not owed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Optional

from .models import DocExtraction
from .normalize.parties import PartyResolver

PostingType = Literal["AR", "AP"]


@dataclass
class PostingDecision:
    accounting_entity: Optional[str]
    posting_type: Optional[PostingType] = None
    direction: int = 1  # +1 debit the side, -1 credit (credit note)
    confidence: float = 0.0
    reasons: list[str] = field(default_factory=list)

    @property
    def resolved(self) -> bool:
        return self.posting_type is not None


def determine_posting(
    ext: DocExtraction,
    *,
    accounting_entity: Optional[str],
    resolver: PartyResolver | None = None,
) -> PostingDecision:
    resolver = resolver or PartyResolver()
    entity = (accounting_entity or "").strip() or None
    doc_type = ext.document_type.value if ext.document_type.is_present else None
    issuer = ext.issuer.value if ext.issuer.is_present else None
    recipient = ext.recipient.value if ext.recipient.is_present else None

    decision = PostingDecision(accounting_entity=entity)
    decision.direction = -1 if doc_type == "Credit Note" else 1

    if entity is None:
        decision.reasons.append("未指定记账主体，无法判断 AR/AP（可用 --entity 或账单库中的 accounting_entity 指定）")
        return decision

    issuer_same, issuer_score = resolver.same(entity, issuer) if issuer else (False, 0.0)
    recipient_same, recipient_score = resolver.same(entity, recipient) if recipient else (False, 0.0)

    if issuer_same and recipient_same:
        decision.reasons.append(
            f"记账主体 {entity} 同时匹配签发方与接收方，单据双方无法区分，需人工确认"
        )
        return decision

    if issuer_same:
        decision.posting_type = "AR"
        decision.confidence = round(issuer_score, 3)
        decision.reasons.append(
            f"记账主体是签发方 {issuer}，该笔费用由其向接收方 {recipient or '对方'} 收取 → 应收（AR）"
        )
    elif recipient_same:
        decision.posting_type = "AP"
        decision.confidence = round(recipient_score, 3)
        decision.reasons.append(
            f"记账主体是接收方 {recipient}，该笔费用由签发方 {issuer or '对方'} 向其收取 → 应付（AP）"
        )
    else:
        decision.reasons.append(
            f"记账主体 {entity} 与签发方 {issuer!r}、接收方 {recipient!r} 均不匹配，"
            "不能仅凭单据类型判定 AR/AP"
        )
        return decision

    if doc_type == "Credit Note":
        decision.reasons.append(
            f"贷项通知（Credit Note）：方向为冲减，posting_type 仍记 {decision.posting_type}，"
            "入账时按贷方冲减处理"
        )
    elif doc_type in {"Debit Note", "Invoice"}:
        decision.reasons.append(f"单据类型 {doc_type}：确认方向为借方（正常计入 {decision.posting_type}）")

    return decision
