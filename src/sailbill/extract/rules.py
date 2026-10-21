"""Deterministic extraction: the path that always runs, with or without LLM.

Design rule: **if a value can be read by code, code reads it.**  The model is
only asked to handle what this module explicitly reports as unresolved, which
makes the whole system debuggable, cheap and reproducible.  It also means the
project is fully usable without any API key, which matters for reviewers.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Iterable, Optional

from ..models import ChargeExtraction, DocExtraction, NormalizedDocument, Tri
from ..normalize.amounts import parse_amount, round2
from ..normalize.charges import standardize_charge
from ..normalize.currency import ISO_4217
from ..normalize.dates import parse_date
from ..normalize.parties import PartyResolver
from ..normalize.payment import normalize_payment_term
from . import lexicon
from .tables import table_to_charges

_CJK = "\u4e00-\u9fff"
_NUM_TOKEN = re.compile(r"[-−(]?\d[\d,\s]*\.?\d*\)?")
# note: [ \t] -- never \s.  A separator that can eat a newline lets an empty
# label ("CONSIGNEE", a table header with no value) capture the *next* line as
# its value, which silently invents a counterparty.
_LABEL_SEP = r"[ \t]*(?:no\.?|number|#|:|-|：)?[ \t]*"


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #
def _lines(text: str) -> list[str]:
    return [ln.strip() for ln in (text or "").splitlines() if ln.strip()]


def _first_line_with(lines: Iterable[str], needle: str) -> Optional[str]:
    n = needle.lower()
    for line in lines:
        if n in line.lower():
            return line
    return None


def _label_regex(label: str) -> str:
    parts = [re.escape(p) for p in re.split(r"\s+", label.strip())]
    return r"\s+".join(parts)


def _label_pattern(label: str, *, strict: bool) -> re.Pattern[str]:
    """Build a label matcher.

    ``(?![A-Za-z])`` after the label is not cosmetic: without it the label
    ``to`` matches inside ``TOTAL`` and a document's own total line becomes the
    "recipient". Likewise ``from`` matches prose ("FROM THE INTENDED VESSEL").
    Strict mode additionally requires the label to start the line, which is how
    real documents render ``SHIPPER :`` / ``Bill To:``.
    """
    head = r"^[\s\W]*" if strict else r"(?<![A-Za-z])"
    return re.compile(
        rf"{head}(?P<label>{_label_regex(label)})(?![A-Za-z]){_LABEL_SEP}(?P<val>[^\n]{{1,90}})",
        re.IGNORECASE | re.MULTILINE,
    )


def labeled_value(
    text: str,
    labels: Iterable[str],
    *,
    valid: Optional[re.Pattern[str]] = None,
    strict_only: bool = False,
) -> tuple[Optional[str], Optional[str], bool]:
    """Find ``label: value``; returns (value, evidence, label_seen).

    Two passes: line-start labels first (high precision), then inline labels
    (needed because PDF extraction puts ``Date:`` in the middle of a line).
    """
    text = text or ""
    alias = labels if isinstance(labels, (list, tuple)) else list(labels)
    for strict in (True, False) if not strict_only else (True,):
        for label in alias:
            for match in _label_pattern(label, strict=strict).finditer(text):
                evidence = match.group(0).strip()
                raw = match.group("val").strip(" \t:：-—|.")
                parts = [p.strip(" |") for p in re.split(r"\s*\|\s*|\s{2,}", raw) if p.strip(" |")]
                candidate = parts[0] if parts else ""
                if not candidate:
                    return None, evidence, True
                if valid and not valid.fullmatch(candidate.split()[0]):
                    return None, evidence, True
                if valid:
                    candidate = candidate.split()[0]
                return candidate, evidence, True
    return None, None, False


def amount_in_line(line: str) -> Optional[float]:
    """Rightmost parseable number in a line (charge amounts are right aligned)."""
    best: Optional[float] = None
    for m in _NUM_TOKEN.finditer(line):
        parsed = parse_amount(m.group(0))
        if parsed.ok:
            best = parsed.value
    return best


def first_amount_in_line(line: str, *, after: int = 0) -> Optional[float]:
    """Leftmost parseable number after ``after`` (totals: "TOTAL 1,410.40 0")."""
    for m in _NUM_TOKEN.finditer(line, after):
        parsed = parse_amount(m.group(0))
        if parsed.ok:
            return parsed.value
    return None


def detect_doc_type(text: str) -> tuple[Optional[str], Optional[str]]:
    """Document type from the *title area* first, then the whole document."""
    head = "\n".join(_lines(text)[:12])
    for region in (head, text):
        low = region.lower()
        # Credit Note before Debit Note: "CREDIT NOTE" titles often mention DN too
        order = (
            "Credit Note",
            "Debit Note",
            "Arrival Notice",
            "Statement of Account",
            "Invoice",
            "Bill of Lading",
        )
        for doc_type in order:
            for pattern in lexicon.DOC_TYPE_PATTERNS[doc_type]:
                if pattern in {"dn", "cn", "soa", "b/l"}:
                    continue  # bare abbreviations are handled below
                if re.search(rf"(?<![a-z]){re.escape(pattern)}(?![a-z])", low):
                    line = _first_line_with(_lines(region), pattern)
                    return doc_type, line
        for doc_type, abbr in (("Debit Note", "dn"), ("Credit Note", "cn")):
            if re.search(rf"\b{abbr}\s*(?:no|number|#|：|:)", region, re.IGNORECASE):
                return doc_type, _first_line_with(_lines(region), abbr.upper())
    return None, None


def _pick_currency(text: str) -> tuple[Optional[str], Optional[str], bool]:
    """Most likely invoice currency.

    A bare three-letter token is *not* enough: ``ALL``, ``COD``, ``THE`` and
    friends are all valid ISO 4217 codes and appear in ordinary English prose.
    A code only counts when it sits in a currency context (next to an amount, or
    near the word "currency") and is not a common word.
    """
    counts: dict[str, int] = {}
    evidence: dict[str, str] = {}
    for m in re.finditer(r"\b([A-Z]{3})\b", text):
        code = m.group(1)
        if code not in ISO_4217 or code in _NOT_CURRENCY_WORDS:
            continue
        if not _currency_context(text, m.start(), m.end()):
            continue
        counts[code] = counts.get(code, 0) + 1
        evidence.setdefault(code, m.group(0))
    for symbol, code in (("US$", "USD"), ("￥", "CNY"), ("€", "EUR"), ("£", "GBP"), ("HK$", "HKD")):
        if symbol in text:
            counts[code] = counts.get(code, 0) + 1
            evidence.setdefault(code, symbol)
    if not counts:
        return None, None, False
    code = max(counts, key=lambda c: (counts[c], c in _COMMON_FREIGHT_CURRENCIES))
    return code, evidence[code], True


def _currency_context(text: str, start: int, end: int) -> bool:
    before = text[max(0, start - 30):start]
    after = text[end:end + 30]
    if re.search(r"(?i)currency|币种|结算币|计价", before + after):
        return True
    if re.search(r"\d", after[:10]) or re.search(r"\d", before[-10:]):
        return True
    return bool(re.search(r"[$€£¥￥]", before[-3:] + after[:3]))


_NOT_CURRENCY_WORDS = {
    "ALL", "THE", "AND", "FOR", "NOT", "ANY", "NEW", "MAX", "INC", "LTD", "CO", "PL", "REF",
    "COD", "CIF", "FOB", "CFR", "FCL", "LCL", "CFS", "CY", "SO", "PO", "NO", "TO", "OF", "IN",
    "ON", "AT", "BY", "AS", "IS", "IT", "EB", "GST", "VAT", "AWB", "MBL", "HBL", "THC",
    "AMS", "ENS", "BAF", "CAF", "PSS", "GRI", "ETD", "ETA", "ATD", "ATA", "POD", "POL",
    "POR", "DEL", "KGS", "LBS", "CBM", "CFT", "PKG", "PKGS", "CNTR", "SEAL", "TYPE", "TEU",
}

_COMMON_FREIGHT_CURRENCIES = {
    "USD", "CNY", "EUR", "HKD", "GBP", "JPY", "SGD", "AUD", "CAD", "THB", "MYR", "TWD", "KRW", "VND",
}


def _totals(text: str, currency: Optional[str]) -> dict[str, tuple[Optional[float], Optional[str], bool]]:
    out: dict[str, tuple[Optional[float], Optional[str], bool]] = {}
    lines = _lines(text)
    for field, labels in lexicon.TOTAL_LABELS.items():
        hit_value: Optional[float] = None
        hit_line: Optional[str] = None
        seen = False
        for label in labels:
            pattern = re.compile(rf"(?<![A-Za-z]){_label_regex(label)}(?![A-Za-z])", re.IGNORECASE)
            for line in lines:
                match = pattern.search(line)
                if not match:
                    continue
                seen = True
                # skip the subtotal row when looking for the grand total
                if field == "total_amount" and re.search(r"sub\s*-?\s*total", line, re.IGNORECASE):
                    continue
                # FIRST number after the label: "TOTAL 1,410.40 0" -> 1410.40,
                # not the trailing zero of the credit column
                value = first_amount_in_line(line, after=match.end())
                if value is None:
                    value = first_amount_in_line(line)
                if value is not None:
                    hit_value, hit_line = round2(value), line
                    break
            if hit_value is not None:
                break
        out[field] = (hit_value, hit_line, seen)
    return out


# --------------------------------------------------------------------------- #
# charges from free text
# --------------------------------------------------------------------------- #
_TEXT_CHARGE = re.compile(
    rf"^(?P<name>[A-Za-z{_CJK}][A-Za-z0-9{_CJK}()/&.\- ]{{2,45}}?)\s{{2,}}(?P<rest>.*\d.*)$"
)

# space-aligned US-style charge tables:
#   DESCRIPTION  UNIT RATE  QTY  P/C  DEBIT(+)  CREDIT(-)
#   HANDLING CHARGE -  65.00  1.00  C  65.00
_TAIL_PC = re.compile(
    r"(?P<rate>\d[\d,]*\.?\d*)\s+(?P<qty>\d[\d,]*\.?\d*)\s+(?P<pc>[PpCc])\s+"
    r"(?P<amount>-?\d[\d,]*\.?\d*)\s*$"
)
_LINE_NUMBER = re.compile(r"^\s*\d{1,3}\s+(?=[A-Za-z])")

# a line only counts as a fee row if its name looks like a fee; otherwise free
# text ("Remark: 3 copies") would be booked as charges
_FEE_WORD = re.compile(
    r"(fee|charge|surcharge|cost|rate|detention|demurrage|freight|carriage|haulage|handling"
    r"|terminal|wharfage|inland|pickup|delivery|service|admin|toll|费|金)",
    re.IGNORECASE,
)

# currency codes used to split "<description> USD 245.00" style lines
_CCY_INLINE = re.compile(
    r"\b(USD|CNY|RMB|EUR|HKD|GBP|JPY|SGD|AUD|CAD|THB|MYR|TWD|KRW|VND|AED|CHF|NZD)\b", re.IGNORECASE
)


def _split_by_currency(line: str) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """``On Carriage Additional - US EmOCA17 USD  245.0000`` -> (name, rest, USD).

    Splitting on the last currency token is far more reliable than splitting on
    whitespace: it keeps reference codes out of the fee name.
    """
    matches = list(_CCY_INLINE.finditer(line))
    if not matches:
        return None, None, None
    last = matches[-1]
    rest = line[last.end():]
    if amount_in_line(rest) is None:
        return None, None, None
    return line[: last.start()].strip(" -—|:."), rest, last.group(1).upper()


def charges_from_text(text: str, *, doc_currency: Optional[str]) -> list[ChargeExtraction]:
    """Last-resort parser for fee rows that survive only as text lines.

    Two shapes are handled: the space-aligned ``rate qty P/C amount`` table and
    the simpler ``<fee name>    <amount>`` line.  Unknown names are **kept**
    (flagged as unmapped, which lowers confidence) rather than dropped: silently
    losing a fee understates the bill, which is worse than asking a human.
    """
    out: list[ChargeExtraction] = []
    for raw_line in _lines(text):
        low = raw_line.lower()
        if any(lbl in low for lbl in ("total", "subtotal", "sub total", "小计", "合计", "总计",
                                      "amount due", "paid amount", "balance")):
            continue
        line = _LINE_NUMBER.sub("", raw_line)

        # --- shape 1: rate / qty / P|C / amount ---------------------------
        tail = _TAIL_PC.search(line)
        if tail:
            name = line[: tail.start()].strip(" -—|.\t")
            if len(name) >= 3 and (standardize_charge(name).method != "unmapped" or _FEE_WORD.search(name)):
                amount = parse_amount(tail.group("amount"))
                rate = parse_amount(tail.group("rate"))
                qty = parse_amount(tail.group("qty"))
                if amount.ok:
                    out.append(
                        ChargeExtraction(
                            charge_item_raw=name,
                            charge_item=standardize_charge(name).standard,
                            quantity=qty.value,
                            unit_price=rate.value,
                            amount=round2(amount.value or 0.0),
                            currency=amount.currency_hint or doc_currency,
                            payment_term="PP" if tail.group("pc").upper() == "P" else "CC",
                            evidence=raw_line,
                        )
                    )
                    continue

        # --- shape 2: <name> [CUR]   <amount> -----------------------------
        name_part, rest, inline_currency = _split_by_currency(line)
        if name_part is None:
            m = _TEXT_CHARGE.match(line)
            if not m:
                continue
            name_part = m.group("name")
            rest = m.group("rest")
            inline_currency = None

        name = name_part.strip(" .:：-")
        if len(name) < 3 or len(name) > 60 or len(name.split()) > 8:
            continue
        std = standardize_charge(name)
        amount = amount_in_line(rest)
        if amount is None:
            continue
        looks_like_fee = (
            std.method != "unmapped"
            or bool(_FEE_WORD.search(name))
            or inline_currency is not None
        )
        if not looks_like_fee:
            continue
        parsed = parse_amount(rest)
        term_match = re.search(r"\b(PP|CC|P/P|PREPAID|COLLECT)\b", line, re.IGNORECASE)
        term = normalize_payment_term(term_match.group(1)).term if term_match else None
        out.append(
            ChargeExtraction(
                charge_item_raw=name,
                charge_item=std.standard,
                amount=round2(amount),
                currency=inline_currency or (parsed.currency_hint if parsed else None) or doc_currency,
                payment_term=term,
                evidence=raw_line,
            )
        )
    return out


def _dedupe_charges(charges: list[ChargeExtraction]) -> tuple[list[ChargeExtraction], list[str]]:
    seen: dict[tuple[str, float, Optional[str]], ChargeExtraction] = {}
    warnings: list[str] = []
    for charge in charges:
        key = (str(charge.charge_item), float(charge.amount or 0.0), charge.currency)
        if key in seen:
            warnings.append(
                f"检测到重复费用行，已去重: {charge.charge_item_raw} {charge.amount} {charge.currency or ''}"
            )
            continue
        seen[key] = charge
    return list(seen.values()), warnings


def _supplement_from_text(
    charges: list[ChargeExtraction],
    primary_text: str,
    doc_currency: Optional[str],
    result: DocExtraction,
) -> list[ChargeExtraction]:
    """Use text lines to fill gaps when a table extraction is only partial.

    Table parsing is precise but can miss rows (merged cells, pdfplumber row
    collapsing).  When the extracted rows do not add up to the document total,
    the free-text parser is given a chance -- and only rows that are genuinely
    new are added, so nothing is double counted.
    """
    if not result.total_amount.is_present:
        return charges
    total = float(result.total_amount.value or 0)
    current = round(sum(float(c.amount or 0) for c in charges), 2)
    if abs(current - total) <= 0.01:
        return charges

    extra = charges_from_text(primary_text, doc_currency=doc_currency)
    if not extra:
        return charges

    known = {(str(c.charge_item), round(float(c.amount or 0), 2), c.currency) for c in charges}
    added: list[ChargeExtraction] = []
    for candidate in extra:
        key = (str(candidate.charge_item), round(float(candidate.amount or 0), 2), candidate.currency)
        if key in known:
            continue
        known.add(key)
        added.append(candidate)

    if not added:
        return charges
    merged = charges + added
    merged_sum = round(sum(float(c.amount or 0) for c in merged), 2)
    if abs(merged_sum - total) < abs(current - total):
        result.warnings.append(
            f"表格仅抽出 {len(charges)} 条（合计 {current:.2f} ≠ 总额 {total:.2f}），"
            f"已用文本行补齐 {len(added)} 条"
        )
        return merged
    return charges


# --------------------------------------------------------------------------- #
# main entry
# --------------------------------------------------------------------------- #
def extract_rules(
    docs: list[NormalizedDocument],
    *,
    resolver: PartyResolver | None = None,
    extractor_name: str = "rules",
) -> DocExtraction:
    """Extract everything we can without a model."""
    resolver = resolver or PartyResolver()
    result = DocExtraction(extractor=extractor_name)

    # the most informative document in the group first (attachment over email body)
    ordered = sorted(docs, key=lambda d: (d.kind.value == "eml", -len(d.flatten_text())))
    primary = ordered[0] if ordered else None
    text = "\n".join(d.flatten_text() for d in ordered if d.flatten_text())
    result.warnings.extend(w for d in ordered for w in d.warnings)

    # ---- document type -------------------------------------------------
    doc_type, type_evidence = detect_doc_type(text)
    if doc_type:
        result.document_type = Tri.present(doc_type, type_evidence)
    else:
        result.document_type = Tri.absent("未在标题区域识别到单据类型")
        result.warnings.append("未能识别单据类型")

    # ---- reference number ----------------------------------------------
    value, evidence, seen = labeled_value(
        text,
        lexicon.REFERENCE_LABELS["reference_no"],
        valid=re.compile(r"[A-Za-z0-9][A-Za-z0-9\-_/]{2,}"),
    )
    ocr_meta = {}
    for d in ordered:
        if isinstance(d.meta.get("ocr_meta"), dict):
            ocr_meta.update(d.meta["ocr_meta"])
    if not value and ocr_meta.get("invoice_no"):
        value, evidence = str(ocr_meta["invoice_no"]), "OCR: invoiceNumber"
        seen = True
    result.reference_no = Tri.present(value, evidence) if value else (
        Tri.absent(evidence) if seen else Tri.unknown("未找到单据编号标签")
    )

    # ---- date -----------------------------------------------------------
    value, evidence, seen = labeled_value(
        text, ["date", "dated", "invoice date", "issue date", "document date", "日期", "开票日期"],
        valid=re.compile(r"[0-9A-Za-z]"),
    )
    parsed_date: Optional[date] = parse_date(value) if value else None
    if parsed_date is None:
        for line in _lines(text)[:40]:
            d = parse_date(line)
            if d:
                parsed_date, evidence, seen = d, line, True
                break
    result.document_date = (
        Tri.present(parsed_date, evidence) if parsed_date else (Tri.absent(evidence) if seen else Tri.unknown("未找到日期"))
    )

    # ---- parties --------------------------------------------------------
    headers = {}
    for d in ordered:
        if isinstance(d.meta.get("mail_headers"), dict):
            headers = d.meta["mail_headers"]
    issuer_raw, issuer_ev = _party_from_headers_or_labels(headers, text, role="issuer")
    recipient_raw, recipient_ev = _party_from_headers_or_labels(headers, text, role="recipient")
    if not issuer_raw:
        for d in ordered:
            parties = d.meta.get("ocr_parties") or {}
            if parties.get("issuer"):
                issuer_raw, issuer_ev = parties["issuer"], "OCR: shipper"
    if not recipient_raw:
        for d in ordered:
            parties = d.meta.get("ocr_parties") or {}
            if parties.get("recipient"):
                recipient_raw, recipient_ev = parties["recipient"], "OCR: consignee"

    result.issuer = _resolve_party(issuer_raw, issuer_ev, resolver)
    result.recipient = _resolve_party(recipient_raw, recipient_ev, resolver)

    # ---- currency -------------------------------------------------------
    code, evidence, seen = _pick_currency(text)
    result.currency = Tri.present(code, evidence) if code else (Tri.absent("未发现币种标识") if seen else Tri.unknown())
    doc_currency = code

    # ---- totals ---------------------------------------------------------
    totals = _totals(text, doc_currency)
    for field in ("subtotal_amount", "tax_amount", "total_amount"):
        value, evidence, seen = totals[field]
        if value is not None:
            setattr(result, field, Tri.present(value, evidence))
        elif seen:
            setattr(result, field, Tri.absent(evidence))
        else:
            setattr(result, field, Tri.unknown(f"未找到 {field} 标签"))

    # a totals block without a tax row means the bill is not taxed -- "absent",
    # not "unreadable", which matters for update semantics
    if (
        result.tax_amount.is_unknown
        and result.subtotal_amount.is_present
        and result.total_amount.is_present
        and abs(float(result.subtotal_amount.value or 0) - float(result.total_amount.value or 0)) <= 0.01
    ):
        result.tax_amount = Tri.absent("小计与合计相等，单据无税额行")

    # ---- charges --------------------------------------------------------
    # Only the most informative document contributes charge rows: an email that
    # carries both a PDF and an XLSX must not mix two versions of the same bill.
    charges: list[ChargeExtraction] = []
    best_charges: list[ChargeExtraction] = []
    best_totals: dict[str, float] = {}
    charge_warnings: list[str] = []
    for d in ordered:
        local: list[ChargeExtraction] = []
        local_totals: dict[str, float] = {}
        local_warnings: list[str] = []
        for table in d.tables:
            found, table_totals, warnings = table_to_charges(
                table, doc_currency=doc_currency, context=text[:4000]
            )
            local.extend(found)
            local_warnings.extend(warnings)
            for field, amount in table_totals.items():
                local_totals.setdefault(field, amount)
        if len(local) > len(best_charges):
            best_charges, best_totals, charge_warnings = local, local_totals, local_warnings

    charges = list(best_charges)
    for field, amount in best_totals.items():
        current = getattr(result, field)
        if not current.is_present:
            setattr(result, field, Tri.present(amount, "表格合计行"))

    if charges and primary is not None:
        charges = _supplement_from_text(charges, primary.flatten_text(), doc_currency, result)

    if not charges:
        for d in ordered:
            if d.ocr:
                from .from_ocr import charges_from_ocr

                charges.extend(charges_from_ocr(d.ocr, doc_currency=doc_currency))
                if charges:
                    result.warnings.append("费用明细来自 OCR 结构化结果")
                    break

    if not charges:
        # free-text fallback uses the primary document only: prose in an email
        # body ("the handling fee became USD 45") must not become a charge row
        primary_text = primary.flatten_text() if primary else text
        charges = charges_from_text(primary_text, doc_currency=doc_currency)
        if charges:
            result.warnings.append("费用明细来自文本行解析（未发现结构化表格）")

    charges, dup_warnings = _dedupe_charges(charges)
    result.warnings.extend(charge_warnings)
    result.warnings.extend(dup_warnings)
    result.charges = charges

    # ---- references (bl / booking / container) --------------------------
    refs: dict[str, list[str]] = {}
    for field in ("bl_no", "hbl_no", "booking_no", "container_no", "voyage_no"):
        value, evidence, _ = labeled_value(
            text, lexicon.REFERENCE_LABELS[field], valid=re.compile(r"[A-Za-z0-9][A-Za-z0-9\-/]{2,}")
        )
        if value:
            refs.setdefault(field.replace("_no", "_no"), []).append(value.strip())
    for d in ordered:
        for field, values in (d.meta.get("ocr_refs") or {}).items():
            bucket = refs.setdefault(field, [])
            for v in values:
                if v not in bucket:
                    bucket.append(v)
    result.references = {k: v for k, v in refs.items() if v}
    return result


def _party_from_headers_or_labels(headers: dict, text: str, *, role: str) -> tuple[Optional[str], Optional[str]]:
    """Document labels win over mail headers.

    A bill's own "Issued By"/"Bill To" is authoritative; the envelope From/To is
    only a fallback (a mail may be forwarded by a colleague).  This ordering is
    also what keeps ``issuer`` from becoming ``Billing <billing@...>``.
    """
    labels = lexicon.PARTY_LABELS["issuer" if role == "issuer" else "recipient"]
    header_keys = ("from", "sender") if role == "issuer" else ("to", "recipient")

    value, evidence, _ = labeled_value(text, labels)
    if value and not _bad_party(value) and not _looks_like_table_header(evidence):
        return value, evidence

    for key in header_keys:
        raw = str(headers.get(key) or "").strip()
        if raw:
            return _display_name(raw), f"邮件头 {key}: {raw}"
    return None, None


def _bad_party(value: str) -> bool:
    text = (value or "").strip()
    low = text.lower()
    if len(text) > 80 or len(text) < 3:
        return True
    if text.isdigit():
        return True
    # placeholders and table artifacts, not names
    if text.startswith("(") or text.startswith("["):
        return True
    # a company name is not a sentence: no leading article, no all-lowercase run
    if re.match(r"^(the|a|an|this|that|our|your|its)\s", low):
        return True
    words = text.split()  # case matters here, so use the original text
    if len(words) >= 3 and all(word.islower() for word in words):
        return True
    # a sentence fragment can carry a digit ("has signed 3 bills of lading")
    if len(words) >= 3 and not re.search(r"[A-Z\u4e00-\u9fff]", text):
        return True
    if len(words) == 1 and text.islower() and not re.search(r"[\u4e00-\u9fff]", text):
        return True  # a bare lowercase token ("pltplt") is a domain, not a party
    return any(low.startswith(bad) for bad in lexicon.BAD_PARTY_VALUES)


_HEADER_TOKENS = re.compile(
    r"\b(SHIPPER|CONSIGNEE|NOTIFY|BILL\s+TO|SOLD\s+TO|DELIVERY\s+AGENT|FORWARDING\s+AGENT)\b",
    re.IGNORECASE,
)


def _looks_like_table_header(evidence: Optional[str]) -> bool:
    """Arrival notices put ``SHIPPER NOTIFY | CONSIGNEE`` in a header row.

    Taking the cell after such a header yields company names that belong to a
    different column ("FREE BUSINESS"), so two or more party labels on one line
    mean "this is a table header, not a label/value pair".
    """
    if not evidence:
        return False
    return len(set(m.group(1).upper() for m in _HEADER_TOKENS.finditer(evidence))) >= 2


def _display_name(raw: str) -> str:
    """``Billing <billing@abc.example>`` -> ``Billing``.

    ``parseaddr`` must only be used when the string really is an address: on a
    plain company name it truncates at the first space (``XYZ Freight
    Forwarding Ltd`` -> ``XYZ``), which is a nasty silent data loss.
    """
    text = (raw or "").strip()
    if "@" not in text:
        return text
    from email.utils import parseaddr

    display, addr = parseaddr(text)
    if display.strip():
        return display.strip()
    if addr:
        # no display name: keep the address itself.  Deriving a company name
        # from the domain root invented identities ("pltplt"); the raw address
        # is at least traceable.
        return addr.strip()
    return text


def _resolve_party(raw: Optional[str], evidence: Optional[str], resolver: PartyResolver) -> Tri[str]:
    if not raw:
        return Tri.unknown("未找到交易方信息")
    cleaned = _display_name(raw)
    match = resolver.resolve(cleaned)
    if match is None:
        return Tri.unknown("交易方无法解析")
    return Tri.present(match.canonical, evidence)
