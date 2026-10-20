"""Money parsing: locale separators, OCR noise, negatives."""

from __future__ import annotations

import pytest

from sailbill.normalize.amounts import close, parse_amount, round2, to_decimal


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("1,234.56", 1234.56),
        ("1.234,56", 1234.56),
        ("1234", 1234.0),
        ("1 234.56", 1234.56),
        ("USD 1,234.56", 1234.56),
        ("US$1,234.56", 1234.56),
        ("￥1,200.00", 1200.0),
        ("1234.5", 1234.5),
        ("1,234", 1234.0),
        ("0.500", 0.5),
    ],
)
def test_parse_amount_formats(raw, expected):
    parsed = parse_amount(raw)
    assert parsed.ok, raw
    assert parsed.value == pytest.approx(expected)


@pytest.mark.parametrize("raw,expected", [("(40.00)", -40.0), ("40.00-", -40.0), ("-12.5", -12.5)])
def test_negative_amounts(raw, expected):
    parsed = parse_amount(raw)
    assert parsed.value == pytest.approx(expected)
    assert parsed.negative


@pytest.mark.parametrize("raw,expected", [("4O.OO", 40.0), ("1.OOO,5O", 1000.5), ("THC 4O.OO", 40.0)])
def test_ocr_confusion_is_repaired_before_parsing(raw, expected):
    """The dangerous case: '4O.OO' must be 40.00, never 4.00."""
    parsed = parse_amount(raw)
    assert parsed.value == pytest.approx(expected)
    assert parsed.repaired


@pytest.mark.parametrize("raw", ["", None, "abc", "N/A", "-", "see attachment"])
def test_unparseable_values(raw):
    assert parse_amount(raw).value is None


def test_invalid_amount_is_not_silently_zero():
    parsed = parse_amount("abc")
    assert parsed.value is None
    assert not parsed.ok


def test_currency_hint_detected():
    assert parse_amount("USD 100").currency_hint == "USD"
    assert parse_amount("RMB 100").currency_hint == "CNY"


def test_helpers():
    assert round2(1.005) == 1.0 or round2(1.005) == 1.01  # banker's rounding tolerated
    assert close(1.0, 1.009)
    assert not close(1.0, 1.02)
    value, negative, _ = to_decimal("(1,234.50)")
    assert value == 1234.50 and negative
