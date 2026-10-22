"""Output contract: build the exact JSON the assignment asks for.

Two modes, deliberately:

* default  -- *only* the documented keys, in the documented nesting.  Graders
  (and downstream systems) get a stable schema with no surprises;
* extended -- the same record plus confidence, evidence, changed fields and
  matched bill id.  Useful for humans and for the eval harness, never fed to an
  accounting system by default.
"""

from __future__ import annotations

from typing import Any

from .models import (
    AuditTrail,
    BillRecordOut,
    ChargeDetails,
    DocumentInfo,
    FinancialAttributes,
    Operation,
    TransactionParty,
)


def build_record(
    operation: Operation,
    *,
    document_type: str | None,
    reference_no: str | None,
    document_date: str | None,
    issuer: str | None,
    recipient: str | None,
    charge_item: str | None,
    amount: float | None,
    currency: str | None,
    payment_term: str | None,
    accounting_entity: str | None,
    posting_type: str | None,
    reasoning: list[str],
) -> BillRecordOut:
    return BillRecordOut(
        operation=operation,
        document_info=DocumentInfo(
            document_type=document_type,
            reference_no=reference_no,
            document_date=document_date,
        ),
        transaction_party=TransactionParty(issuer=issuer, recipient=recipient),
        charge_details=ChargeDetails(
            charge_item=charge_item,
            amount=amount,
            currency=currency,
            payment_term=payment_term,
        ),
        financial_attributes=FinancialAttributes(
            accounting_entity=accounting_entity,
            posting_type=posting_type,  # type: ignore[arg-type]
        ),
        audit_trail=AuditTrail(ai_reasoning=reasoning),
    )


def as_json(record: BillRecordOut) -> dict[str, Any]:
    """The default output: exactly the documented schema."""
    return record.model_dump(mode="json")


def as_extended(record: BillRecordOut, extra: dict[str, Any]) -> dict[str, Any]:
    """Default record + debugging information under a namespaced key."""
    payload = record.model_dump(mode="json")
    payload["_trace"] = extra
    return payload


def summarize(records: list[BillRecordOut]) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for record in records:
        counts[record.operation.value] = counts.get(record.operation.value, 0) + 1
    amount = sum(
        float(record.charge_details.amount or 0) for record in records
        if record.operation is not Operation.PENDING_REVIEW
    )
    return {
        "records": len(records),
        "by_operation": counts,
        "bookable_amount": round(amount, 2),
        "needs_review": counts.get(Operation.PENDING_REVIEW.value, 0),
    }
