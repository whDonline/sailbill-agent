"""Currency, charge name, payment term and party normalisation."""

from __future__ import annotations

import pytest

from sailbill.normalize.charges import known_charges, same_charge, standardize_charge
from sailbill.normalize.currency import is_valid_iso, normalize_currency
from sailbill.normalize.parties import PartyResolver, normalize_party_name
from sailbill.normalize.payment import normalize_payment_term


# --------------------------------------------------------------------------- #
# currency
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "raw,expected",
    [("usd", "USD"), ("US$", "USD"), ("RMB", "CNY"), ("美元", "USD"), ("USO", "USD"), ("hkd", "HKD")],
)
def test_currency_aliases(raw, expected):
    assert normalize_currency(raw).code == expected


@pytest.mark.parametrize("raw", ["XYZ", "US0", "DOLLARS!"])
def test_currency_rejects_unknown_codes(raw):
    result = normalize_currency(raw)
    assert result.code is None
    assert not is_valid_iso(result.code)


def test_ambiguous_yen_symbol_is_flagged():
    result = normalize_currency("¥")
    assert result.code == "CNY"
    assert result.ambiguous
    resolved = normalize_currency("¥", context="JPY invoice from Tokyo")
    assert resolved.code == "JPY"


def test_currency_from_longer_text():
    assert normalize_currency("Amount in USD").code == "USD"


# --------------------------------------------------------------------------- #
# charges -- the six mappings required by the assignment
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Handling Fee", "Handling Charge"),
        ("H/D Fee", "Handling Charge"),
        ("Doc Fee", "Documentation Fee"),
        ("THC", "Terminal Handling Charge"),
        ("Trucking Fee", "Trucking Charge"),
        ("Customs Fee", "Customs Clearance Fee"),
        ("Storage Fee", "Storage Charge"),
    ],
)
def test_required_charge_mappings(raw, expected):
    assert standardize_charge(raw).standard == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("THC (DEST)", "Terminal Handling Charge"),
        ("THC DESTINATION", "Terminal Handling Charge"),
        ("码头操作费", "Terminal Handling Charge"),
        ("海运费", "Ocean Freight"),
        ("B/L FEE", "Documentation Fee"),
        ("文件费", "Documentation Fee"),
        ("拖车费", "Trucking Charge"),
        ("报关费", "Customs Clearance Fee"),
    ],
)
def test_charge_variants(raw, expected):
    assert standardize_charge(raw).standard == expected


def test_unknown_charge_keeps_original_name():
    result = standardize_charge("Quantum Foam Surcharge")
    assert result.standard == "Quantum Foam Surcharge"
    assert result.method == "unmapped"
    assert result.needs_review


def test_fuzzy_charge_is_flagged_for_review():
    result = standardize_charge("Terminal Handing Charge")  # missing 'l'
    assert result.standard == "Terminal Handling Charge"
    assert result.method == "fuzzy"
    assert result.needs_review


def test_same_charge_across_spellings():
    assert same_charge("THC", "Terminal Handling Charge")
    assert not same_charge("THC", "Ocean Freight")


def test_vocabulary_is_not_empty():
    assert len(known_charges()) >= 30


# --------------------------------------------------------------------------- #
# payment terms
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "raw,expected",
    [("PP", "PP"), ("prepaid", "PP"), ("Freight Prepaid", "PP"), ("预付", "PP"),
     ("CC", "CC"), ("Collect", "CC"), ("到付", "CC"), ("P/P.", "PP")],
)
def test_payment_terms(raw, expected):
    assert normalize_payment_term(raw).term == expected


@pytest.mark.parametrize("raw", ["", None, "unknown", "T/T 30 days"])
def test_unknown_payment_terms_are_not_guessed(raw):
    assert normalize_payment_term(raw).term is None


# --------------------------------------------------------------------------- #
# parties
# --------------------------------------------------------------------------- #
def test_party_suffixes_do_not_change_identity():
    resolver = PartyResolver()
    same, score = resolver.same("ABC Logistics Co., Ltd", "ABC LOGISTICS")
    assert same and score > 0.9


def test_industry_words_keep_companies_distinct():
    resolver = PartyResolver()
    same, _ = resolver.same("ABC Logistics Ltd", "ABC Shipping Ltd")
    assert not same


def test_party_role_aliases():
    resolver = PartyResolver()
    assert resolver.resolve("目的港代理").canonical == "Destination Agent"
    assert resolver.resolve("origin agent").canonical == "Origin Agent"


def test_party_email_domain_mapping():
    resolver = PartyResolver(domain_map={"abc-logistics.example": "ABC Logistics"})
    match = resolver.resolve("billing@abc-logistics.example")
    assert match is not None and match.canonical == "ABC Logistics"
    assert match.method == "email"


def test_plain_company_name_is_not_truncated():
    """parseaddr() truncates at the first space -- this used to lose 'Ltd'."""
    from sailbill.extract.rules import _display_name

    assert _display_name("XYZ Freight Forwarding Ltd") == "XYZ Freight Forwarding Ltd"
    assert _display_name("Billing <billing@abc.example>") == "Billing"


def test_normalize_party_name_strips_legal_suffix():
    # legal suffixes go, industry words stay (they distinguish companies)
    assert normalize_party_name("Shanghai Xinda Logistics Co., Ltd") == "shanghai xinda logistics"
