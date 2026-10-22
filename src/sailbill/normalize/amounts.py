"""Money string parsing.

Real bills mix ``1,234.56`` / ``1.234,56`` / ``(40.00)`` / ``40.00-`` / ``USD
1,234.56`` / full width digits, and OCR adds ``0/O`` ``1/I`` ``5/S`` noise.
Getting this layer wrong silently corrupts every downstream number, so it is
written defensively and covered by unit tests.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

_FULLWIDTH = str.maketrans("０１２３４５６７８９，．－＋￥＄　", "0123456789,.-+¥$ ")

# digit-confusable characters, applied only as a *repair* after a first parse
_OCR_FIX = str.maketrans(
    {
        "O": "0",
        "o": "0",
        "D": "0",
        "I": "1",
        "l": "1",
        "|": "1",
        "!": "1",
        "S": "5",
        "s": "5",
        "B": "8",
        "Z": "2",
        "z": "2",
        "G": "6",
    }
)

_CONFUSABLE = re.compile(r"[OoIl|!SsBbZzGD]")
_CONFUSABLE_CHARS = set("OoIl|!SsBbZzGD")
_NUMERIC_TOKEN = re.compile(r"[0-9OoIl|!SsBbZzGD.,()\-]+")

_CURRENCY_TOKEN = re.compile(
    r"(?P<code>US\$|HK\$|S\$|NT\$|RMB|CNY|USD|HKD|SGD|EUR|GBP|JPY|AUD|CAD|CHF|THB|MYR|KRW|TWD|"
    r"AED|SAR|INR|VND|PHP|IDR|BRL|MXN|ZAR|RUB|NZD|SEK|NOK|DKK|PLN|TRY|EGP|PKR|BDT|LKR|"
    r"EUR|€|£|¥|￥|\$)",
    re.IGNORECASE,
)


@dataclass
class AmountParse:
    """Result of parsing a money cell."""

    value: float | None
    raw: str = ""
    negative: bool = False
    repaired: bool = False
    currency_hint: str | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.value is not None


def to_decimal(text: str) -> tuple[Decimal | None, bool, list[str]]:
    """Parse one number, tolerating locale separators.

    Returns ``(value, negative, warnings)``.
    """
    warnings: list[str] = []
    s = (text or "").translate(_FULLWIDTH).strip()
    negative = False

    if s.startswith("(") and s.endswith(")"):
        negative = True
        s = s[1:-1]
    if s.startswith(("-", "−", "–")):
        negative = True
        s = s[1:]
    if s.endswith("-"):
        negative = True
        s = s[:-1]
    if s.endswith("CR") or s.endswith("Cr"):
        negative = True
        s = s[:-2]

    s = re.sub(r"[^0-9.,]", "", s)
    if not s or not re.search(r"\d", s):
        return None, negative, warnings

    if "." in s and "," in s:
        if s.rfind(".") > s.rfind(","):
            s = s.replace(",", "")
        else:
            s = s.replace(".", "").replace(",", ".")
    elif "," in s:
        parts = s.split(",")
        if len(parts) == 2 and 1 <= len(parts[1]) <= 2:
            s = s.replace(",", ".")
        elif all(len(p) == 3 for p in parts[1:]):
            s = s.replace(",", "")
        elif len(parts) == 2:
            s = s.replace(",", ".")
            warnings.append(f"逗号分隔歧义，按小数处理: {text!r}")
        else:
            s = s.replace(",", "")
    elif "." in s:
        parts = s.split(".")
        if len(parts) > 2 and all(len(p) == 3 for p in parts[1:]):
            s = s.replace(".", "")
        elif len(parts) == 2 and len(parts[1]) == 3 and parts[0] not in {"0", ""}:
            # "1.234" is far more likely EU thousands than three decimals
            s = s.replace(".", "")
            warnings.append(f"点号分隔歧义，按千分位处理: {text!r}")

    try:
        return Decimal(s), negative, warnings
    except InvalidOperation:
        return None, negative, warnings


def parse_amount(
    raw: object,
    *,
    default_currency: str | None = None,
    allow_negative: bool = True,
    repair_ocr: bool = True,
) -> AmountParse:
    """Parse an amount from a string/number, recovering common OCR noise."""
    if raw is None:
        return AmountParse(value=None, raw="", warnings=["空值"])
    if isinstance(raw, (int, float)):
        return AmountParse(value=float(raw), raw=str(raw))
    text = str(raw).strip()
    if not text:
        return AmountParse(value=None, raw=text, warnings=["空值"])

    warnings: list[str] = []
    hint: str | None = None
    m = _CURRENCY_TOKEN.search(text)
    if m:
        hint = m.group("code").upper()
    else:
        hint = _alphabetic_currency(text)
    if hint:
        from .currency import normalize_currency

        hint = normalize_currency(hint).code or hint

    # Strip the currency token first so its letters ("USD") are not mistaken
    # for OCR noise, then repair remaining confusables *before* parsing:
    # "4O.OO" must become 40.00, never 4.00.
    rest = (text[: m.start()] + text[m.end():]) if m else text

    # Guard against prose: "see attachment" must never become 5.00 because the
    # leading 's' looks like a five.  Only strings whose substantive content is
    # number-like may be parsed (letters other than confusables = prose).
    candidates = _numeric_candidates(rest)
    prose_letters = [ch for ch in re.findall(r"[A-Za-z]", rest) if ch not in _CONFUSABLE_CHARS]
    if prose_letters and not candidates:
        return AmountParse(value=None, raw=text, warnings=["不是数字内容（含字母描述）"])
    if prose_letters and not any(("." in c or "," in c) for c in candidates):
        # a bare integer inside prose ("page 2 of 3") is ambiguous, not money
        if not _has_currency_context(text):
            return AmountParse(value=None, raw=text, warnings=["疑似文字描述中的序号，未按金额处理"])

    value: Decimal | None = None
    negative = False
    repaired = False

    if repair_ocr and _CONFUSABLE.search(rest):
        fixed = _repair_ocr_digits(rest)
        value, negative, w = to_decimal(fixed)
        warnings.extend(w)
        if value is not None:
            repaired = True
            warnings.append(f"OCR 数字混淆已修正: {text!r} -> {fixed.strip()!r}")
        else:
            value, negative, w2 = to_decimal(rest)
            warnings.extend(w2)
    else:
        value, negative, w = to_decimal(rest)
        warnings.extend(w)

    if value is None:
        return AmountParse(value=None, raw=text, currency_hint=hint or default_currency, warnings=warnings)

    if negative and not allow_negative:
        warnings.append("出现负数金额，已取绝对值")
        negative = False

    amount = -value if negative else value
    return AmountParse(
        value=float(amount),
        raw=text,
        negative=negative,
        repaired=repaired,
        currency_hint=hint or default_currency,
        warnings=warnings,
    )


def _numeric_candidates(text: str) -> list[str]:
    """Tokens that could plausibly be a number (digits + separators only)."""
    out: list[str] = []
    for token in re.split(r"\s+", text or ""):
        token = token.strip("()[]{}<>")
        if token and re.search(r"\d", token) and _NUMERIC_TOKEN.fullmatch(token):
            out.append(token)
    return out


def _has_currency_context(text: str) -> bool:
    return bool(_CURRENCY_TOKEN.search(text)) or bool(_alphabetic_currency(text))


def _alphabetic_currency(text: str) -> str | None:
    """Detect a leading/trailing currency word such as ``USD`` or ``RMB``."""
    upper = re.sub(r"[^A-Z]", "", text.upper())
    for code in ("USD", "CNY", "RMB", "HKD", "EUR", "GBP", "JPY", "SGD", "AUD", "CAD", "THB", "MYR"):
        if code in upper:
            return "CNY" if code == "RMB" else code
    return None


def _repair_ocr_digits(text: str) -> str:
    """Turn O->0, I->1, S->5, B->8 ... only inside number-like tokens."""
    def fix(match: re.Match[str]) -> str:
        token = match.group(0)
        return token.translate(_OCR_FIX)

    return re.sub(r"[0-9OoIl|!SsBbZzGD.,]+", fix, text)


def round2(x: float) -> float:
    return float(Decimal(str(x)).quantize(Decimal("0.01")))


def close(a: float, b: float, tol: float = 0.01) -> bool:
    return abs(Decimal(str(a)) - Decimal(str(b))) <= Decimal(str(tol))
