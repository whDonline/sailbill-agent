"""The "already booked" side: a tiny bill store.

Deliberately boring (JSON in, JSON out, no database) so the whole demo runs from
a checkout with no infrastructure, while still being a clean seam to replace
with a real ERP/API client.  ``upsert`` gives the pipeline idempotency: running
the same file twice cannot create the same bill twice.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable, Optional

from .models import Bill, BillLine
from .normalize.charges import standardize_charge


class BillStore:
    def __init__(self, bills: Optional[Iterable[Bill]] = None) -> None:
        self._bills: dict[str, Bill] = {}
        for bill in bills or []:
            self._bills[bill.bill_id] = bill

    # -- construction -----------------------------------------------------
    @classmethod
    def from_file(cls, path: str | Path | None) -> "BillStore":
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

    def get(self, bill_id: str) -> Optional[Bill]:
        return self._bills.get(bill_id)

    def __len__(self) -> int:
        return len(self._bills)

    def common_entity(self) -> Optional[str]:
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
        payment_term: Optional[str] = None,
    ) -> BillLine:
        bill = self._bills.get(bill_id)
        if bill is None:
            raise KeyError(bill_id)
        standard = standardize_charge(charge_item).standard or charge_item
        for line in bill.lines:
            if standardize_charge(line.charge_item).standard == standard and line.currency == currency:
                line.amount = float(amount)
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
