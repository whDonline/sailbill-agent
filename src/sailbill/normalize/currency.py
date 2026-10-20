"""ISO 4217 currency normalisation.

Three jobs:

* map symbols / words / misspellings (``US$``, ``RMB``, ``美元``, ``USO``) to a
  three letter ISO code;
* flag genuinely ambiguous symbols (``¥`` is CNY *or* JPY) instead of guessing;
* reject anything that is not a real ISO 4217 code, so a bogus currency can
  never reach the accounting system.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

# A pragmatic subset covering > 99% of freight documents, plus every code that
# appears in common freight rate sheets.  Anything not in here is rejected.
ISO_4217: dict[str, str] = {
    "AED": "UAE Dirham", "AFN": "Afghani", "ALL": "Lek", "AMD": "Armenian Dram",
    "ANG": "Netherlands Antillean Guilder", "AOA": "Kwanza", "ARS": "Argentine Peso",
    "AUD": "Australian Dollar", "AWG": "Aruban Florin", "AZN": "Azerbaijan Manat",
    "BAM": "Convertible Mark", "BBD": "Barbados Dollar", "BDT": "Taka",
    "BGN": "Bulgarian Lev", "BHD": "Bahraini Dinar", "BIF": "Burundi Franc",
    "BMD": "Bermudian Dollar", "BND": "Brunei Dollar", "BOB": "Boliviano",
    "BRL": "Brazilian Real", "BSD": "Bahamian Dollar", "BTN": "Ngultrum",
    "BWP": "Pula", "BYN": "Belarusian Ruble", "BZD": "Belize Dollar",
    "CAD": "Canadian Dollar", "CDF": "Congolese Franc", "CHF": "Swiss Franc",
    "CLP": "Chilean Peso", "CNY": "Yuan Renminbi", "COP": "Colombian Peso",
    "CRC": "Costa Rican Colon", "CUP": "Cuban Peso", "CVE": "Cabo Verde Escudo",
    "CZK": "Czech Koruna", "DJF": "Djibouti Franc", "DKK": "Danish Krone",
    "DOP": "Dominican Peso", "DZD": "Algerian Dinar", "EGP": "Egyptian Pound",
    "ERN": "Nakfa", "ETB": "Ethiopian Birr", "EUR": "Euro", "FJD": "Fiji Dollar",
    "FKP": "Falkland Islands Pound", "GBP": "Pound Sterling", "GEL": "Lari",
    "GHS": "Ghana Cedi", "GIP": "Gibraltar Pound", "GMD": "Dalasi",
    "GNF": "Guinean Franc", "GTQ": "Quetzal", "GYD": "Guyana Dollar",
    "HKD": "Hong Kong Dollar", "HNL": "Lempira", "HTG": "Gourde",
    "HUF": "Forint", "IDR": "Rupiah", "ILS": "New Israeli Sheqel",
    "INR": "Indian Rupee", "IQD": "Iraqi Dinar", "IRR": "Iranian Rial",
    "ISK": "Iceland Krona", "JMD": "Jamaican Dollar", "JOD": "Jordanian Dinar",
    "JPY": "Yen", "KES": "Kenyan Shilling", "KGS": "Som", "KHR": "Riel",
    "KMF": "Comorian Franc", "KPW": "North Korean Won", "KRW": "Won",
    "KWD": "Kuwaiti Dinar", "KYD": "Cayman Islands Dollar", "KZT": "Tenge",
    "LAK": "Lao Kip", "LBP": "Lebanese Pound", "LKR": "Sri Lanka Rupee",
    "LRD": "Liberian Dollar", "LSL": "Loti", "LYD": "Libyan Dinar",
    "MAD": "Moroccan Dirham", "MDL": "Moldovan Leu", "MGA": "Malagasy Ariary",
    "MKD": "Denar", "MMK": "Kyat", "MNT": "Tugrik", "MOP": "Pataca",
    "MRU": "Ouguiya", "MUR": "Mauritius Rupee", "MVR": "Rufiyaa",
    "MWK": "Malawi Kwacha", "MXN": "Mexican Peso", "MYR": "Malaysian Ringgit",
    "MZN": "Mozambique Metical", "NAD": "Namibia Dollar", "NGN": "Naira",
    "NIO": "Cordoba Oro", "NOK": "Norwegian Krone", "NPR": "Nepalese Rupee",
    "NZD": "New Zealand Dollar", "OMR": "Rial Omani", "PAB": "Balboa",
    "PEN": "Sol", "PGK": "Kina", "PHP": "Philippine Peso", "PKR": "Pakistan Rupee",
    "PLN": "Zloty", "PYG": "Guarani", "QAR": "Qatari Rial", "RON": "Romanian Leu",
    "RSD": "Serbian Dinar", "RUB": "Russian Ruble", "RWF": "Rwanda Franc",
    "SAR": "Saudi Riyal", "SBD": "Solomon Islands Dollar", "SCR": "Seychelles Rupee",
    "SDG": "Sudanese Pound", "SEK": "Swedish Krona", "SGD": "Singapore Dollar",
    "SHP": "Saint Helena Pound", "SLE": "Leone", "SOS": "Somali Shilling",
    "SRD": "Surinam Dollar", "SSP": "South Sudanese Pound", "STN": "Dobra",
    "SVC": "El Salvador Colon", "SYP": "Syrian Pound", "SZL": "Lilangeni",
    "THB": "Baht", "TJS": "Somoni", "TMT": "Turkmenistan New Manat",
    "TND": "Tunisian Dinar", "TOP": "Pa'anga", "TRY": "Turkish Lira",
    "TTD": "Trinidad and Tobago Dollar", "TWD": "New Taiwan Dollar",
    "TZS": "Tanzanian Shilling", "UAH": "Hryvnia", "UGX": "Uganda Shilling",
    "USD": "US Dollar", "UYU": "Peso Uruguayo", "UZS": "Uzbekistan Sum",
    "VES": "Bolivar Soberano", "VND": "Dong", "VUV": "Vatu",
    "WST": "Tala", "XAF": "CFA Franc BEAC", "XCD": "East Caribbean Dollar",
    "XOF": "CFA Franc BCEAO", "XPF": "CFP Franc", "YER": "Yemeni Rial",
    "ZAR": "Rand", "ZMW": "Zambian Kwacha", "ZWL": "Zimbabwe Dollar",
}

ALIASES: dict[str, str] = {
    "US$": "USD", "US DOLLAR": "USD", "USDOLLAR": "USD", "DOLLAR": "USD",
    "U.S.$": "USD", "USD$": "USD", "USO": "USD", "UDS": "USD", "UDD": "USD",
    "美元": "USD", "美金": "USD", "美刀": "USD",
    "RMB": "CNY", "人民币": "CNY", "元": "CNY", "RENMINBI": "CNY",
    "HK$": "HKD", "HKD$": "HKD", "H.K.$": "HKD", "港元": "HKD", "港币": "HKD",
    "S$": "SGD", "SGD$": "SGD", "新加坡元": "SGD", "新币": "SGD",
    "EUR": "EUR", "EURO": "EUR", "欧元": "EUR",
    "GBP": "GBP", "STERLING": "GBP", "POUND": "GBP", "英镑": "GBP",
    "JPY": "JPY", "YEN": "JPY", "日元": "JPY",
    "NT$": "TWD", "TWD$": "TWD", "新台币": "TWD",
    "AUD": "AUD", "AU$": "AUD", "澳元": "AUD",
    "CAD": "CAD", "CA$": "CAD", "加元": "CAD",
    "KRW": "KRW", "韩元": "KRW",
    "THB": "THB", "泰铢": "THB",
    "VND": "VND", "越南盾": "VND",
    "MYR": "MYR", "马币": "MYR",
    "AED": "AED", "迪拉姆": "AED",
}

# symbol -> (code, ambiguous)
SYMBOLS: dict[str, tuple[str, bool]] = {
    "$": ("USD", False),
    "US$": ("USD", False),
    "€": ("EUR", False),
    "£": ("GBP", False),
    "￥": ("CNY", False),
    "¥": ("CNY", True),  # CNY or JPY
    "元": ("CNY", True),
}

_AMBIGUOUS_HINTS = {
    "JPY": ("JPY", "YEN", "日元", "JAPAN"),
    "CNY": ("CNY", "RMB", "人民币", "CHINA", "CN"),
    "USD": ("USD", "US", "DOLLAR", "美国"),
}


@dataclass
class CurResult:
    code: Optional[str] = None
    source: str = "none"  # text | symbol | alias | provided | invalid
    ambiguous: bool = False
    raw: str = ""
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.code is not None and is_valid_iso(self.code)


def is_valid_iso(code: object) -> bool:
    return isinstance(code, str) and code.strip().upper() in ISO_4217


def normalize_currency(raw: object, *, context: str = "") -> CurResult:
    """Normalise a currency token; ``context`` resolves ambiguous symbols."""
    if raw is None:
        return CurResult(raw="", warnings=["币种为空"])
    text = str(raw).strip()
    if not text:
        return CurResult(raw=text, warnings=["币种为空"])

    upper = text.upper()
    compact = re.sub(r"[\s._-]+", "", upper)

    if compact in ISO_4217:
        return CurResult(code=compact, source="text", raw=text)

    if upper in ALIASES:
        return CurResult(code=ALIASES[upper], source="alias", raw=text)
    if compact in ALIASES:
        return CurResult(code=ALIASES[compact], source="alias", raw=text)

    if text in SYMBOLS:
        code, ambiguous = SYMBOLS[text]
        if ambiguous:
            resolved = _resolve_ambiguous(code, context)
            if resolved:
                return CurResult(code=resolved, source="symbol", raw=text)
            return CurResult(
                code=code,
                source="symbol",
                ambiguous=True,
                raw=text,
                warnings=[f"符号 {text!r} 有歧义（{code}/JPY），需人工确认"],
            )
        return CurResult(code=code, source="symbol", raw=text)

    # embedded code inside a longer string, e.g. "USD 1,234.00"
    m = re.search(r"\b([A-Z]{3})\b", upper)
    if m and m.group(1) in ISO_4217:
        return CurResult(code=m.group(1), source="text", raw=text)

    return CurResult(code=None, source="invalid", raw=text, warnings=[f"无法识别的币种: {text!r}"])


def _resolve_ambiguous(default: str, context: str) -> Optional[str]:
    if not context:
        return None
    up = context.upper()
    for code, hints in _AMBIGUOUS_HINTS.items():
        for h in hints:
            if h in up:
                return code
    return None
