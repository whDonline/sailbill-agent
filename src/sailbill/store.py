"""The "already booked" side: a tiny bill store.

Deliberately boring (JSON in, JSON out, no database) so the whole demo runs from
a checkout with no infrastructure, while still being a clean seam to replace
with a real ERP/API client.  ``upsert`` gives the pipeline idempotency: running
the same file twice cannot create the same bill twice.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .models import Bill, BillLine
from .normalize.charges import standardize_charge
from .normalize.currency import is_valid_iso as is_valid_currency


class BillStore:
    def __init__(self, bills: Iterable[Bill] | None = None) -> None:
        self._bills: dict[str, Bill] = {}
        for bill in bills or []:
            self._bills[bill.bill_id] = bill

    # -- construction -----------------------------------------------------
    @classmethod
    def from_file(cls, path: str | Path | None) -> BillStore:
        if not path:
            return cls()
        p = Path(path)
        if not p.exists():
            return cls()
        raw: Any = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            raw = raw.get("bills") or raw.get("data") or []
        bills = [Bill.from_any(item, index=i, source=str(p)) for i, item in enumerate(raw) if isinstance(item, dict)]
        return cls(bills)

    # -- access -----------------------------------------------------------
    def all(self) -> list[Bill]:
        return list(self._bills.values())

    def get(self, bill_id: str) -> Bill | None:
        return self._bills.get(bill_id)

    def __len__(self) -> int:
        return len(self._bills)

    def __bool__(self) -> bool:
        # a store is a container, not a collection: an *empty* store is still a
        # usable store (without this, ``store or BillStore()`` silently replaces
        # the caller's instance and commits go to a throw-away object)
        return True

    def common_entity(self) -> str | None:
        """Most frequent accounting entity -- a sane default for the demo."""
        counts: dict[str, int] = {}
        for bill in self._bills.values():
            if bill.accounting_entity:
                counts[bill.accounting_entity] = counts.get(bill.accounting_entity, 0) + 1
        if not counts:
            return None
        return max(counts, key=lambda k: counts[k])

    # -- mutation ---------------------------------------------------------
    def upsert_line(
        self,
        bill_id: str,
        *,
        charge_item: str,
        amount: float,
        currency: str,
        payment_term: str | None = None,
        line_id: str | None = None,
    ) -> BillLine:
        """Insert or update one charge line.

        Identity rules, in order:

        1. ``line_id`` given (an UPDATE that matched a specific line) -> update it;
        2. same standardised name + currency + amount -> idempotent no-op, so
           running the same document twice cannot duplicate a charge;
        3. otherwise append -- a bill legitimately carries two lines with the
           same name and different amounts (two handling charges, for instance),
           so merging by name alone would lose money.
        """
        bill = self._bills.get(bill_id)
        if bill is None:
            raise KeyError(bill_id)
        standard = standardize_charge(charge_item).standard or charge_item

        if line_id:
            for line in bill.lines:
                if line.line_id == line_id:
                    line.amount = float(amount)
                    line.charge_item = standard
                    if payment_term:
                        line.payment_term = payment_term
                    return line

        for line in bill.lines:
            if (
                standardize_charge(line.charge_item).standard == standard
                and line.currency == currency
                and abs(float(line.amount) - float(amount)) <= 0.01
            ):
                if payment_term:
                    line.payment_term = payment_term
                return line

        line = BillLine(
            line_id=f"{bill_id}-L{len(bill.lines) + 1}",
            charge_item=standard,
            charge_item_raw=charge_item,
            amount=float(amount),
            currency=currency,
            payment_term=payment_term,
        )
        bill.lines.append(line)
        return line

    def save(self, path: str | Path) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        payload = [bill.model_dump(mode="json") for bill in self._bills.values()]
        p.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    # -- writing pipeline decisions back ----------------------------------
    def apply_record(
        self,
        record: Any,
        *,
        matched_bill_id: str | None = None,
        matched_line_id: str | None = None,
    ) -> str | None:
        """Persist one CREATE/UPDATE decision onto the book.

        ``NO_CHANGE`` and ``PENDING_REVIEW`` are deliberately not written: the
        first needs no write, the second must not be booked before a human
        confirms it.
        """
        operation = getattr(record.operation, "value", str(record.operation))
        if operation not in {"CREATE", "UPDATE"}:
            return None

        details = record.charge_details
        if details.charge_item is None or details.amount is None or not is_valid_currency(details.currency):
            return None

        if operation == "UPDATE" and matched_bill_id and matched_bill_id in self._bills:
            self.upsert_line(
                matched_bill_id,
                charge_item=details.charge_item,
                amount=float(details.amount),
                currency=str(details.currency).upper(),
                payment_term=details.payment_term,
                line_id=matched_line_id,
            )
            return matched_bill_id

        reference = record.document_info.reference_no or "NO-REF"
        bill_id = f"AUTO-{reference}"
        bill = self._bills.get(bill_id)
        if bill is None:
            posting = record.financial_attributes.posting_type
            bill = Bill(
                bill_id=bill_id,
                document_type=record.document_info.document_type,
                reference_no=record.document_info.reference_no,
                document_date=record.document_info.document_date,
                issuer=record.transaction_party.issuer,
                recipient=record.transaction_party.recipient,
                accounting_entity=record.financial_attributes.accounting_entity,
                posting_type=posting if posting in {"AR", "AP"} else None,
                currency=str(details.currency).upper() if details.currency else None,
                lines=[],
                source="pipeline",
            )
            self._bills[bill_id] = bill
        self.upsert_line(
            bill_id,
            charge_item=details.charge_item,
            amount=float(details.amount),
            currency=str(details.currency).upper(),
            payment_term=details.payment_term,
        )
        return bill_id

    def apply_records(self, records: Iterable[Any], extras: Iterable[dict] | None = None) -> list[str]:
        """Apply a batch of decisions; returns the bill ids that were touched."""
        extra_list = list(extras or [])
        touched: list[str] = []
        for i, record in enumerate(records):
            extra = extra_list[i] if i < len(extra_list) else {}
            bill_id = self.apply_record(
                record,
                matched_bill_id=extra.get("matched_bill"),
                matched_line_id=extra.get("matched_line"),
            )
            if bill_id and bill_id not in touched:
                touched.append(bill_id)
        return touched
