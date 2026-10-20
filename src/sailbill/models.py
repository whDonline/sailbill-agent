"""Data contracts shared by every stage of the pipeline.

The pipeline is built around three ideas that drive most of the robustness the
assignment asks for:

1. ``Tri`` -- a *three state* field.  ``present`` means "the document says it",
   ``absent`` means "the document provably does not carry this field" (so an
   UPDATE may inherit the old value) and ``unknown`` means "it is there but we
   could not read it reliably" (so an UPDATE must NOT inherit and must not be
   written, the record goes to review).  Collapsing the last two into a single
   ``None`` is the single most common bug in this class of system.
2. ``Block`` / ``TableData`` -- every piece of text keeps its provenance
   (file, page, line) so that any extracted value can be traced back, which is
   what "evidence anchoring" and the audit trail are built on.
3. ``BillRecordOut`` -- the *exact* JSON contract of the assignment.  Nothing
   else is allowed to leak into the default output; richer debugging
   information lives in ``--extended`` mode only.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Any, Generic, Literal, Optional, TypeVar

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")


# --------------------------------------------------------------------------- #
# three-state values
# --------------------------------------------------------------------------- #
class Status(str, Enum):
    PRESENT = "present"
    ABSENT = "absent"
    UNKNOWN = "unknown"


@dataclass
class Tri(Generic[T]):
    """A field that knows *why* it has no value."""

    value: Optional[T] = None
    status: Status = Status.UNKNOWN
    evidence: Optional[str] = None

    @classmethod
    def present(cls, value: T, evidence: Optional[str] = None) -> "Tri[T]":
        return cls(value=value, status=Status.PRESENT, evidence=evidence)

    @classmethod
    def absent(cls, evidence: Optional[str] = None) -> "Tri[T]":
        return cls(value=None, status=Status.ABSENT, evidence=evidence)

    @classmethod
    def unknown(cls, evidence: Optional[str] = None) -> "Tri[T]":
        return cls(value=None, status=Status.UNKNOWN, evidence=evidence)

    @property
    def is_present(self) -> bool:
        return self.status is Status.PRESENT and self.value is not None

    @property
    def is_absent(self) -> bool:
        return self.status is Status.ABSENT

    @property
    def is_unknown(self) -> bool:
        return self.status is Status.UNKNOWN

    def as_json(self) -> dict[str, Any]:
        return {"value": self.value, "status": self.status.value, "evidence": self.evidence}


# --------------------------------------------------------------------------- #
# ingestion
# --------------------------------------------------------------------------- #
class SourceKind(str, Enum):
    EML = "eml"
    PDF = "pdf"
    IMAGE = "image"
    EXCEL = "excel"
    WORD = "word"
    TEXT = "text"
    UNKNOWN = "unknown"


class Block(BaseModel):
    """One addressable piece of text, with provenance."""

    model_config = ConfigDict(extra="forbid")

    text: str
    kind: Literal["heading", "paragraph", "kv", "table_cell", "ocr", "mail_header"] = "paragraph"
    page: Optional[int] = None
    line: Optional[int] = None
    table_id: Optional[str] = None
    row: Optional[int] = None
    col: Optional[int] = None
    source: Optional[str] = None  # file name the block came from (attachments)


class TableData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    table_id: str
    headers: list[str] = Field(default_factory=list)
    rows: list[list[str]] = Field(default_factory=list)
    header_row: Optional[int] = None
    page: Optional[int] = None
    title: Optional[str] = None
    source: Optional[str] = None
    sheet: Optional[str] = None


class Attachment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    filename: str
    sha256: str
    size: int
    kind: SourceKind = SourceKind.UNKNOWN
    path: Optional[str] = None  # materialised path for recursive parsing
    depth: int = 0


class NormalizedDocument(BaseModel):
    """Everything we know about one input file *before* semantic extraction."""

    model_config = ConfigDict(extra="forbid")

    doc_id: str
    filename: str
    kind: SourceKind
    sha256: str
    size: int = 0
    text: str = ""
    blocks: list[Block] = Field(default_factory=list)
    tables: list[TableData] = Field(default_factory=list)
    meta: dict[str, Any] = Field(default_factory=dict)
    attachments: list[Attachment] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    ocr: Optional[dict[str, Any]] = None
    path: Optional[str] = None

    def add_block(self, text: str, **kw: Any) -> None:
        if text is None:
            return
        text = str(text).strip()
        if not text:
            return
        self.blocks.append(Block(text=text, **kw))

    def flatten_text(self, limit: int | None = None) -> str:
        """Rebuild a plain text view (used for evidence checks and prompts)."""
        parts = [b.text for b in self.blocks]
        if self.text and not parts:
            parts = [self.text]
        text = "\n".join(parts)
        return text[:limit] if limit else text


# --------------------------------------------------------------------------- #
# extraction
# --------------------------------------------------------------------------- #
class ChargeExtraction(BaseModel):
    """One fee line as read from a document, before standardisation."""

    model_config = ConfigDict(extra="forbid")

    charge_item_raw: Optional[str] = None
    charge_item: Optional[str] = None  # standardised name
    description: Optional[str] = None
    quantity: Optional[float] = None
    unit_price: Optional[float] = None
    amount: Optional[float] = None
    currency: Optional[str] = None
    payment_term: Optional[str] = None
    evidence: Optional[str] = None
    page: Optional[int] = None
    source_row: Optional[int] = None


class DocExtraction(BaseModel):
    """Document-level facts, each one three-state."""

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    document_type: Tri[str] = Field(default_factory=Tri)
    reference_no: Tri[str] = Field(default_factory=Tri)
    document_date: Tri[date] = Field(default_factory=Tri)
    issuer: Tri[str] = Field(default_factory=Tri)
    recipient: Tri[str] = Field(default_factory=Tri)
    currency: Tri[str] = Field(default_factory=Tri)
    subtotal_amount: Tri[float] = Field(default_factory=Tri)
    tax_amount: Tri[float] = Field(default_factory=Tri)
    total_amount: Tri[float] = Field(default_factory=Tri)
    payment_term: Tri[str] = Field(default_factory=Tri)
    references: dict[str, list[str]] = Field(default_factory=dict)  # mbl/bl/booking/container
    charges: list[ChargeExtraction] = Field(default_factory=list)
    extractor: str = "rules"  # rules | llm | hybrid
    warnings: list[str] = Field(default_factory=list)

    def field_map(self) -> dict[str, Tri[Any]]:
        return {
            "document_type": self.document_type,
            "reference_no": self.reference_no,
            "document_date": self.document_date,
            "issuer": self.issuer,
            "recipient": self.recipient,
            "currency": self.currency,
            "subtotal_amount": self.subtotal_amount,
            "tax_amount": self.tax_amount,
            "total_amount": self.total_amount,
            "payment_term": self.payment_term,
        }


# --------------------------------------------------------------------------- #
# bills (the "already exists" side)
# --------------------------------------------------------------------------- #
class BillLine(BaseModel):
    model_config = ConfigDict(extra="forbid")

    line_id: str
    charge_item: str
    charge_item_raw: Optional[str] = None
    amount: float
    currency: str
    payment_term: Optional[str] = None
    quantity: Optional[float] = None
    unit_price: Optional[float] = None


class Bill(BaseModel):
    """A bill already booked in the system (input for matching/update)."""

    model_config = ConfigDict(extra="forbid")

    bill_id: str
    document_type: Optional[str] = None
    reference_no: Optional[str] = None
    document_date: Optional[str] = None
    issuer: Optional[str] = None
    recipient: Optional[str] = None
    accounting_entity: Optional[str] = None
    posting_type: Optional[Literal["AR", "AP"]] = None
    currency: Optional[str] = None
    references: dict[str, list[str]] = Field(default_factory=dict)
    lines: list[BillLine] = Field(default_factory=list)
    source: Optional[str] = None

    # ---- flexible construction -------------------------------------------------
    @classmethod
    def from_any(cls, payload: dict[str, Any], *, index: int = 0, source: str | None = None) -> "Bill":
        """Accept our own schema, the assignment's output schema, or a loose bill.

        The assignment hands over "existing bills" as JSON whose exact shape is
        not specified, so the loader is deliberately forgiving: it accepts the
        documented output contract (``document_info`` / ``transaction_party`` /
        ``charge_details`` / ``financial_attributes``) as well as flat records.
        """
        p = payload or {}

        def pick(*keys: str, default: Any = None) -> Any:
            for k in keys:
                cur: Any = p
                ok = True
                for part in k.split("."):
                    if isinstance(cur, dict) and part in cur:
                        cur = cur[part]
                    else:
                        ok = False
                        break
                if ok and cur not in (None, ""):
                    return cur
            return default

        doc_info = p.get("document_info") if isinstance(p.get("document_info"), dict) else {}
        party = p.get("transaction_party") if isinstance(p.get("transaction_party"), dict) else {}
        charge = p.get("charge_details") if isinstance(p.get("charge_details"), dict) else {}
        fin = p.get("financial_attributes") if isinstance(p.get("financial_attributes"), dict) else {}

        reference_no = pick("reference_no", "document_info.reference_no", "doc_no", "bill_no")
        bill_id = pick("bill_id", "id") or f"bill-{index:04d}"

        lines: list[BillLine] = []
        raw_lines = []
        if isinstance(p.get("lines"), list):
            raw_lines = p["lines"]
        elif isinstance(p.get("charge_details"), list):
            raw_lines = p["charge_details"]
        elif charge:
            raw_lines = [charge]

        for i, line in enumerate(raw_lines):
            if not isinstance(line, dict):
                continue
            amount = line.get("amount")
            currency = line.get("currency")
            item = line.get("charge_item") or line.get("charge_name") or line.get("item")
            if amount is None or item is None:
                continue
            try:
                amount_f = float(amount)
            except (TypeError, ValueError):
                continue
            lines.append(
                BillLine(
                    line_id=str(line.get("line_id") or f"{bill_id}-L{i + 1}"),
                    charge_item=str(item),
                    charge_item_raw=line.get("charge_item_raw"),
                    amount=amount_f,
                    currency=str(currency or pick("currency") or "USD").upper(),
                    payment_term=line.get("payment_term") or line.get("payment_terms"),
                    quantity=_f(line.get("quantity")),
                    unit_price=_f(line.get("unit_price")),
                )
            )

        refs: dict[str, list[str]] = {}
        for key in ("mbl_no", "hbl_no", "bl_no", "booking_no", "container_no", "container_nos", "voyage_no"):
            val = pick(key, f"references.{key}")
            if val:
                vals = val if isinstance(val, list) else [val]
                refs[key.replace("_nos", "_no")] = [str(v).strip() for v in vals if v]

        return cls(
            bill_id=str(bill_id),
            document_type=pick("document_type", "document_info.document_type"),
            reference_no=str(reference_no) if reference_no else None,
            document_date=_s(pick("document_date", "document_info.document_date")),
            issuer=pick("issuer", "transaction_party.issuer"),
            recipient=pick("recipient", "transaction_party.recipient"),
            accounting_entity=pick("accounting_entity", "financial_attributes.accounting_entity"),
            posting_type=_posting(pick("posting_type", "financial_attributes.posting_type")),
            currency=pick("currency"),
            references=refs,
            lines=lines,
            source=source,
        )

    def line_key(self) -> str:
        return f"{self.bill_id}"


def _f(v: Any) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _s(v: Any) -> Optional[str]:
    return None if v is None else str(v)


def _posting(v: Any) -> Optional[Literal["AR", "AP"]]:
    if isinstance(v, str) and v.strip().upper() in {"AR", "AP"}:
        return v.strip().upper()  # type: ignore[return-value]
    return None


# --------------------------------------------------------------------------- #
# matching / decision
# --------------------------------------------------------------------------- #
class MatchRule(str, Enum):
    REF_AND_CHARGE = "1.reference_no+charge_item"
    BL_AND_CHARGE = "2.bl_no+charge_item+currency"
    BOOKING_AND_CHARGE = "3.booking_no+charge_item+currency"
    CONTAINER_PARTY_DATE_AMOUNT = "4.container+parties+date+amount"


class MatchResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bill: Bill
    line: Optional[BillLine] = None
    rule: Optional[MatchRule] = None
    score: float = 0.0
    reasons: list[str] = Field(default_factory=list)


class Operation(str, Enum):
    CREATE = "CREATE"
    UPDATE = "UPDATE"
    NO_CHANGE = "NO_CHANGE"
    PENDING_REVIEW = "PENDING_REVIEW"


class Issue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    message: str
    severity: Literal["info", "warn", "error"] = "warn"
    field: Optional[str] = None


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation: Operation
    confidence: float = 0.0
    reasons: list[str] = Field(default_factory=list)
    issues: list[Issue] = Field(default_factory=list)
    match: Optional[MatchResult] = None
    changed_fields: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# output contract (exact assignment schema)
# --------------------------------------------------------------------------- #
class DocumentInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_type: Optional[str] = None
    reference_no: Optional[str] = None
    document_date: Optional[str] = None


class TransactionParty(BaseModel):
    model_config = ConfigDict(extra="forbid")

    issuer: Optional[str] = None
    recipient: Optional[str] = None


class ChargeDetails(BaseModel):
    model_config = ConfigDict(extra="forbid")

    charge_item: Optional[str] = None
    amount: Optional[float] = None
    currency: Optional[str] = None
    payment_term: Optional[str] = None


class FinancialAttributes(BaseModel):
    model_config = ConfigDict(extra="forbid")

    accounting_entity: Optional[str] = None
    posting_type: Optional[Literal["AR", "AP"]] = None


class AuditTrail(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ai_reasoning: list[str] = Field(default_factory=list)


class BillRecordOut(BaseModel):
    """The one and only default output shape."""

    model_config = ConfigDict(extra="forbid")

    operation: Operation
    document_info: DocumentInfo
    transaction_party: TransactionParty
    charge_details: ChargeDetails
    financial_attributes: FinancialAttributes
    audit_trail: AuditTrail


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: str | Any, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            b = fh.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def stable_doc_id(sha: str, name: str = "") -> str:
    base = f"{sha}:{name}"
    return "doc-" + hashlib.sha1(base.encode("utf-8")).hexdigest()[:12]


def json_default(obj: Any) -> Any:
    if isinstance(obj, (date, datetime)):
        return obj.isoformat()
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, BaseModel):
        return obj.model_dump(mode="json")
    if isinstance(obj, Tri):
        return obj.as_json()
    raise TypeError(f"not JSON serialisable: {type(obj)!r}")


def dumps(obj: Any, *, indent: int = 2) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=indent, default=json_default)
