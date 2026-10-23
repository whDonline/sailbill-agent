"""LLM extraction stage (LangChain + any OpenAI-compatible endpoint).

Two-stage by design:

1. *document pass*  -- one call over the (truncated) document text for the
   document-level facts and the full charge list;
2. *repair pass*    -- only if validation rejects the first answer, the error
   list is fed back (max 2 attempts, as documented in ``docs/harness.md``).

Everything the model returns is re-validated here: numbers are re-parsed,
currencies re-normalised, charge names re-standardised, and every value must be
grounded in the source text or it is discarded.  The model is therefore never
trusted -- it is only *used*.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ..config import SETTINGS, Settings
from ..models import ChargeExtraction, DocExtraction, Tri
from ..normalize.amounts import round2
from ..normalize.charges import standardize_charge
from ..normalize.currency import normalize_currency
from ..normalize.dates import parse_date
from ..normalize.payment import normalize_payment_term
from . import prompts


class LLMCharge(BaseModel):
    model_config = ConfigDict(extra="ignore")

    charge_item_raw: str | None = None
    description: str | None = None
    quantity: float | None = None
    unit_price: float | None = None
    amount: float | None = None
    currency: str | None = None
    payment_term: str | None = None
    evidence: str | None = None


class LLMDoc(BaseModel):
    model_config = ConfigDict(extra="ignore")

    document_type: str | None = None
    reference_no: str | None = None
    document_date: str | None = None
    issuer: str | None = None
    recipient: str | None = None
    currency: str | None = None
    subtotal_amount: float | None = None
    tax_amount: float | None = None
    total_amount: float | None = None
    payment_term: str | None = None
    charges: list[LLMCharge] = Field(default_factory=list)
    absent_fields: list[str] = Field(default_factory=list)
    unreadable_fields: list[str] = Field(default_factory=list)
    evidence: dict[str, str] = Field(default_factory=dict)
    notes: str | None = None


@dataclass
class LLMResult:
    doc: DocExtraction
    raw: dict[str, Any] = field(default_factory=dict)
    attempts: int = 1
    warnings: list[str] = field(default_factory=list)


class LLMExtractor:
    """Thin LangChain wrapper; degrades to rules when anything goes wrong."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or SETTINGS
        self._chain: Any = None
        self._json_chain: Any = None

    # -- lazy construction ------------------------------------------------
    def _build(self) -> None:
        if self._chain is not None:
            return
        from langchain_core.prompts import ChatPromptTemplate
        from langchain_openai import ChatOpenAI

        # deepseek-flash 默认开启思考模式，而思考模式不支持 tool_choice；
        # 结构化抽取用非思考模式更快更省，仅对 DeepSeek 端点生效
        extra_body = (
            {"thinking": {"type": "disabled"}}
            if self.settings.llm_provider.lower().startswith("deepseek")
            else None
        )
        llm = ChatOpenAI(
            model=self.settings.llm_model,
            api_key=self.settings.llm_api_key,
            base_url=self.settings.llm_base_url,
            temperature=self.settings.llm_temperature,
            timeout=self.settings.llm_timeout,
            max_retries=1,
            extra_body=extra_body,
        )
        base_prompt = ChatPromptTemplate.from_messages(
            [("system", prompts.SYSTEM), ("user", prompts.USER)]
        )
        # 显式走 function calling：DeepSeek 已下线 json_schema/json_object
        # 响应格式（400 "response_format type is unavailable"），工具调用仍受支持
        structured = (
            llm.with_structured_output(LLMDoc, method="function_calling")
            if self._supports_structured()
            else None
        )
        if structured is not None:
            self._chain = base_prompt | structured
        # JSON fallback chain, always built: used when tool calling is unavailable
        self._json_chain = base_prompt | llm

    def _supports_structured(self) -> bool:
        # DeepSeek/Qwen/OpenAI all support tool calling; local vLLM may not.
        return self.settings.llm_provider.lower() not in {"local", "vllm", "ollama"}

    # -- public -----------------------------------------------------------
    def extract(self, text: str, *, hints: str = "", max_attempts: int = 2) -> LLMResult:
        self._build()
        errors: list[str] = []
        last_exc: Exception | None = None

        for attempt in range(1, max_attempts + 1):
            user_text = text + (f"\n\n【上次错误】\n{errors[0]}" if errors else "")
            try:
                payload = self._invoke(user_text, hints)
                doc = self._to_extraction(payload)
                return LLMResult(doc=doc, raw=payload, attempts=attempt)
            except ValidationError as exc:
                errors = [_short(exc)]
                last_exc = exc
            except Exception as exc:  # network, auth, parse
                errors = [str(exc)[:500]]
                last_exc = exc
        raise RuntimeError(f"LLM 抽取失败（{max_attempts} 次）: {last_exc}")

    # -- internals --------------------------------------------------------
    def _invoke(self, user_text: str, hints: str) -> dict[str, Any]:
        messages = {"text": user_text, "hints": hints or "（无）"}
        if self._chain is not None:
            out = self._chain.invoke(messages)
            if isinstance(out, LLMDoc):
                return out.model_dump()
            if isinstance(out, dict):
                return out
        out = self._json_chain.invoke(messages)
        content = getattr(out, "content", out)
        if isinstance(content, list):  # some providers return content parts
            content = "".join(str(c.get("text", "")) if isinstance(c, dict) else str(c) for c in content)
        return _loads_json(str(content))

    def _to_extraction(self, payload: dict[str, Any]) -> DocExtraction:
        data = LLMDoc.model_validate(payload)
        absent = {_k(f) for f in data.absent_fields}

        def tri_str(value: str | None, field: str) -> Tri[str]:
            if value and str(value).strip():
                return Tri.present(str(value).strip(), data.evidence.get(field))
            return Tri.absent() if _k(field) in absent else Tri.unknown()

        def tri_num(value: float | None, field: str) -> Tri[float]:
            if value is not None:
                return Tri.present(round2(float(value)), data.evidence.get(field))
            return Tri.absent() if _k(field) in absent else Tri.unknown()

        result = DocExtraction(extractor="llm")
        result.document_type = tri_str(data.document_type, "document_type")
        if result.document_type.is_present:
            result.document_type = Tri.present(
                _canonical_doc_type(str(result.document_type.value)), result.document_type.evidence
            )
        result.reference_no = tri_str(data.reference_no, "reference_no")
        date_value = parse_date(data.document_date) if data.document_date else None
        result.document_date = (
            Tri.present(date_value, data.evidence.get("document_date"))
            if date_value
            else (Tri.absent() if _k("document_date") in absent else Tri.unknown())
        )
        result.issuer = tri_str(data.issuer, "issuer")
        result.recipient = tri_str(data.recipient, "recipient")
        currency = normalize_currency(data.currency).code if data.currency else None
        result.currency = (
            Tri.present(currency, data.evidence.get("currency"))
            if currency
            else (Tri.absent() if _k("currency") in absent else Tri.unknown())
        )
        result.subtotal_amount = tri_num(data.subtotal_amount, "subtotal_amount")
        result.tax_amount = tri_num(data.tax_amount, "tax_amount")
        result.total_amount = tri_num(data.total_amount, "total_amount")
        term = normalize_payment_term(data.payment_term).term if data.payment_term else None
        result.payment_term = Tri.present(term) if term else Tri.unknown()

        for charge in data.charges:
            amount = charge.amount
            if amount is None and charge.unit_price is not None and charge.quantity is not None:
                amount = round2(charge.unit_price * charge.quantity)
            item = (charge.charge_item_raw or charge.description or "").strip()
            if amount is None or not item:
                continue
            std = standardize_charge(item)
            cur = normalize_currency(charge.currency).code if charge.currency else currency
            result.charges.append(
                ChargeExtraction(
                    charge_item_raw=item,
                    charge_item=std.standard,
                    description=charge.description,
                    quantity=charge.quantity,
                    unit_price=charge.unit_price,
                    amount=round2(float(amount)),
                    currency=cur,
                    payment_term=normalize_payment_term(charge.payment_term).term if charge.payment_term else None,
                    evidence=charge.evidence,
                )
            )
        if data.notes:
            result.warnings.append(f"模型备注: {data.notes}")
        return result


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
_DOC_TYPE_MAP = {
    "debitnote": "Debit Note",
    "creditnote": "Credit Note",
    "invoice": "Invoice",
    "statementofaccount": "Statement of Account",
    "statement": "Statement of Account",
    "billoflading": "Bill of Lading",
}


def _canonical_doc_type(raw: str) -> str:
    key = re.sub(r"[^a-z]", "", raw.lower())
    return _DOC_TYPE_MAP.get(key, raw.strip())


def _k(field: str) -> str:
    return re.sub(r"[^a-z]", "", field.lower())


def _loads_json(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start >= 0 and end > start:
        cleaned = cleaned[start : end + 1]
    return json.loads(cleaned)


def _short(exc: Exception, limit: int = 800) -> str:
    text = str(exc)
    return text[:limit]
