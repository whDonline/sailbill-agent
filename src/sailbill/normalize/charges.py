"""Charge name standardisation.

The assignment demands one canonical name per fee while the source documents
carry every possible spelling (``THC`` / ``THC (DEST)`` / ``码头操作费``).  The
policy implemented here is:

* exact / alias hit  -> standard name, high confidence;
* fuzzy hit (>= 0.88) -> standard name, medium confidence, warning attached;
* no hit             -> **keep the original name** as the standard name and flag
  it for review.  Silently inventing or force-mapping a name would corrupt the
  ledger, which the assignment explicitly forbids.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Iterable, Optional

# canonical name -> accepted spellings (matched after normalisation)
STANDARD_CHARGES: dict[str, list[str]] = {
    "Ocean Freight": [
        "ocean freight", "oceanfreight", "o/f", "of", "o f", "seafreight", "sea freight",
        "basic ocean freight", "ocean freight charge", "freight", "海运费", "海运运费", "基本海运费",
    ],
    "Air Freight": ["air freight", "airfreight", "a/f", "空运费", "航空运费"],
    "Handling Charge": [
        "handling charge", "handling fee", "handling", "h/d fee", "hd fee", "h/d", "hd",
        "h d fee", "操作费", "手续费", "代理操作费", "手續費", "操作費",
    ],
    "Documentation Fee": [
        "documentation fee", "document fee", "doc fee", "docfee", "doc", "documentation",
        "b/l fee", "bl fee", "bill of lading fee", "文件费", "单证费", "单证文件费", "文件費",
    ],
    "Terminal Handling Charge": [
        "terminal handling charge", "thc", "thc dest", "thc origin", "thc(origin)", "thc(dest)",
        "terminal handling", "terminal fee", "码头操作费", "码头费", "港杂费", "码头作业费",
    ],
    "Trucking Charge": [
        "trucking charge", "trucking fee", "trucking", "truck fee", "truckage", "haulage",
        "inland trucking", "拖车费", "内陆运费", "内陆运输费", "卡车费", "拖车",
    ],
    "On Carriage": [
        "on carriage", "on-carriage", "oncarriage", "on carriage additional", "on carriage charge",
        "oncarriage additional", "second leg freight", "二程运费", "内陆转运费", "转关运输费",
    ],
    "Customs Clearance Fee": [
        "customs clearance fee", "customs fee", "customs clearance", "customs",
        "customs brokerage", "brokerage", "报关费", "清关费", "报关", "清關費",
    ],
    "Storage Charge": [
        "storage charge", "storage fee", "storage", "warehouse storage", "堆存费", "仓储费", "仓租",
    ],
    "CFS Charge": ["cfs charge", "cfs fee", "cfs", "拼箱费", "场站费", "装箱费"],
    "LCL Handling": ["lcl handling", "lcl fee", "拼箱操作费"],
    "BAF": ["baf", "bunker adjustment factor", "bunker surcharge", "燃油附加费", "燃油费"],
    "FAF": ["faf", "fuel adjustment factor"],
    "CAF": ["caf", "currency adjustment factor", "汇率附加费"],
    "PSS": ["pss", "peak season surcharge", "旺季附加费"],
    "GRI": ["gri", "general rate increase", "运价上调"],
    "ISPS": ["isps", "isps charge", "港口安保费", "安保附加费"],
    "AMS": ["ams", "ams fee", "automated manifest system", "美国舱单费", "舱单费"],
    "ENS": ["ens", "entry summary declaration", "欧盟舱单费"],
    "VGM Fee": ["vgm", "vgm fee", "称重费", "重量申报费"],
    "Seal Fee": ["seal fee", "seal", "铅封费", "封条费"],
    "D/O Fee": ["d/o fee", "do fee", "d/o", "delivery order fee", "换单费", "提货单费"],
    "Telex Release Fee": ["telex release", "telex release fee", "电放费", "电放"],
    "Insurance": ["insurance", "insurance fee", "insurance premium", "保险费", "保费"],
    "Inspection Fee": ["inspection fee", "inspection", "查验费", "查验服务费"],
    "Port Surcharge": ["port surcharge", "port charge", "港建费", "港口附加费"],
    "Wharfage": ["wharfage", "wharfage charge"],
    "Demurrage": ["demurrage", "demurrage charge", "滞期费", "超期用箱费"],
    "Detention": ["detention", "detention charge", "滞箱费", "柜租"],
    "Detention Free Time Charge": [
        "detention free time charge", "additional detention free time charge", "add'l detention free time",
        "add'l detention free time charge", "additional detention free time", "detention free time",
        "extra free time", "用箱免柜期延长费", "免箱期延长费",
    ],
    "Amendment Fee": ["amendment fee", "amendment", "改单费", "改单"],
    "Booking Fee": ["booking fee", "booking charge", "订舱费"],
    "Pickup Charge": ["pickup charge", "pick up charge", "提箱费", "提柜费"],
    "Return Charge": ["return charge", "还箱费", "还柜费"],
    "Overnight Fee": ["overnight fee", "压夜费", "过夜费"],
    "Data Entry Fee": ["data entry fee", "data fee", "打单费", "录入费"],
}

_BRACKETS = re.compile(r"[\(（][^\)）]*[\)）]")
_NON_ALNUM = re.compile(r"[^0-9a-z\u4e00-\u9fff]+")

# qualifiers that do not change the identity of a charge ("THC DEST" == "THC")
_QUALIFIERS = {
    "dest", "destination", "origin", "orig", "port", "loading", "discharge",
    "inbound", "outbound", "import", "export", "目的港", "起运港", "卸货港", "装货港", "进口", "出口",
}


def _fold(text: str) -> str:
    """Unicode/case/punctuation folding used for every comparison."""
    t = unicodedata.normalize("NFKC", str(text or "")).lower()
    t = t.replace("&", " and ")
    t = _NON_ALNUM.sub(" ", t)
    return re.sub(r"\s+", " ", t).strip()


def _compact(text: str) -> str:
    return _fold(text).replace(" ", "")


@dataclass
class ChargeStandard:
    standard: Optional[str]
    raw: str
    method: str = "none"  # exact | alias | fuzzy | unmapped
    score: float = 0.0
    candidates: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def needs_review(self) -> bool:
        return self.method in {"fuzzy", "unmapped", "prefix"}


def _build_index(extra: dict[str, Iterable[str]] | None = None) -> dict[str, str]:
    index: dict[str, str] = {}
    table = {k: list(v) for k, v in STANDARD_CHARGES.items()}
    if extra:
        for canon, variants in extra.items():
            table.setdefault(canon, [])
            table[canon].extend(variants)
    for canon, variants in table.items():
        index[_compact(canon)] = canon
        for v in variants:
            index[_compact(v)] = canon
    return index


_INDEX = _build_index()


def standardize_charge(
    raw: object,
    *,
    custom: dict[str, Iterable[str]] | None = None,
    fuzzy_threshold: float = 0.88,
    allow_prefix: bool = True,
) -> ChargeStandard:
    """Map a raw fee name onto the canonical vocabulary."""
    text = str(raw or "").strip()
    if not text:
        return ChargeStandard(standard=None, raw=text, warnings=["费用名称为空"])

    index = _INDEX if not custom else _build_index(custom)
    key = _compact(text)

    if key in index:
        canon = index[key]
        return ChargeStandard(
            standard=canon, raw=text, method="exact" if _compact(canon) == key else "alias", score=1.0
        )

    # "THC (DEST)" -> "THC"; "FREIGHT - 40HQ" -> "FREIGHT"
    stripped = _compact(_BRACKETS.sub(" ", text))
    if stripped and stripped in index:
        return ChargeStandard(standard=index[stripped], raw=text, method="alias", score=0.97)

    for sep in (" - ", " – ", "/", "—"):
        if sep in text:
            head = _compact(text.split(sep)[0])
            if head in index:
                return ChargeStandard(standard=index[head], raw=text, method="alias", score=0.95)

    # drop shipping qualifiers: "THC DESTINATION" -> "THC"
    tokens = _fold(text).split()
    while tokens and tokens[-1] in _QUALIFIERS:
        tokens.pop()
    if tokens and len(tokens) < len(_fold(text).split()):
        candidate = "".join(tokens)
        if candidate in index:
            return ChargeStandard(standard=index[candidate], raw=text, method="alias", score=0.95)

    # longest known prefix: real documents append references and surcharge words,
    # e.g. "On Carriage Additional - US EmOCA17 USD" is just "On Carriage"
    if allow_prefix:
        words = _fold(text).split()
        for cut in range(len(words) - 1, 1, -1):
            candidate = "".join(words[:cut])
            if candidate in index:
                return ChargeStandard(
                    standard=index[candidate],
                    raw=text,
                    method="prefix",
                    score=0.9,
                    warnings=[f"费用名 {text!r} 含附加描述，按前缀映射为 {index[candidate]!r}"],
                )

    if len(key) < 4:
        return ChargeStandard(
            standard=text, raw=text, method="unmapped", score=0.0, warnings=[f"费用名 {text!r} 过短，未映射"]
        )

    best, best_score = None, 0.0
    for alias, canon in index.items():
        if abs(len(alias) - len(key)) > max(4, len(key) // 2):
            continue
        score = SequenceMatcher(None, key, alias).ratio()
        if score > best_score:
            best, best_score = canon, score

    if best and best_score >= fuzzy_threshold:
        return ChargeStandard(
            standard=best,
            raw=text,
            method="fuzzy",
            score=round(best_score, 4),
            warnings=[f"费用名 {text!r} 模糊匹配到 {best!r} (score={best_score:.2f})，建议人工确认"],
        )

    return ChargeStandard(
        standard=text,
        raw=text,
        method="unmapped",
        score=0.0,
        warnings=[f"费用名 {text!r} 不在标准词表中，保留原名"],
    )


def same_charge(a: object, b: object) -> bool:
    sa = standardize_charge(a).standard
    sb = standardize_charge(b).standard
    return bool(sa and sb and _compact(sa) == _compact(sb))


def known_charges() -> list[str]:
    return sorted(STANDARD_CHARGES)
