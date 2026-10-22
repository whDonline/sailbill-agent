"""Date parsing for bills, emails and OCR payloads."""

from __future__ import annotations

import re
from datetime import date, datetime
from email.utils import parsedate_to_datetime

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}

_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})\b"), "ymd"),
    (re.compile(r"\b(\d{4})年(\d{1,2})月(\d{1,2})日"), "ymd"),
    (re.compile(r"\b(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})\b"), "dmy_or_mdy"),
    (re.compile(r"\b(\d{1,2})\s+([A-Za-z]{3,9})\.?,?\s+(\d{4})\b"), "dMy"),
    (re.compile(r"\b([A-Za-z]{3,9})\.?\s+(\d{1,2}),?\s+(\d{4})\b"), "Mdy"),
    (re.compile(r"\b(\d{1,2})年(\d{1,2})月"), "ym"),
]


def parse_date(raw: object, *, default_year: int | None = None) -> date | None:
    """Best-effort, but *strict*: an unparseable string yields ``None``."""
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw
    text = str(raw).strip()
    if not text:
        return None

    iso = re.match(r"^(\d{4})-(\d{2})-(\d{2})", text)
    if iso:
        try:
            return date(int(iso.group(1)), int(iso.group(2)), int(iso.group(3)))
        except ValueError:
            pass

    for pattern, kind in _PATTERNS:
        m = pattern.search(text)
        if not m:
            continue
        try:
            if kind == "ymd":
                return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            if kind == "dmy_or_mdy":
                a, b, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
                if a > 12 and b <= 12:
                    return date(y, b, a)
                if b > 12 and a <= 12:
                    return date(y, a, b)
                # ambiguous: freight documents in Asia are overwhelmingly D/M/Y
                return date(y, b, a)
            if kind == "dMy":
                month = _month(m.group(2))
                if month:
                    return date(int(m.group(3)), month, int(m.group(1)))
            if kind == "Mdy":
                month = _month(m.group(1))
                if month:
                    return date(int(m.group(3)), month, int(m.group(2)))
            if kind == "ym":
                year = default_year or date.today().year
                return date(year, int(m.group(2)), 1)
        except ValueError:
            continue

    try:  # RFC 2822 email dates
        return parsedate_to_datetime(text).date()
    except (TypeError, ValueError):
        return None


def _month(token: str) -> int | None:
    return _MONTHS.get(token.strip().lower()[:4]) or _MONTHS.get(token.strip().lower()[:3])


def format_date(d: date | None) -> str | None:
    return d.isoformat() if d else None
