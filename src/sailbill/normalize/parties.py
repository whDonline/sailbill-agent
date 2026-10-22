"""Transaction party resolution.

Freight documents name the same company in a dozen ways (``ABC Logistics Co.,
Ltd`` / ``ABC LOGISTICS`` / ``ABC物流有限公司`` / ``ops@abc-logistics.com``).  A
model alone is unreliable and unexplainable here, so resolution is a dictionary
first, fuzzy second, and only then a model proposal (validated here again).

The dictionary is data, not code: ship ``data/parties.yaml`` next to your data
and the same resolver learns your counterparties.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

# role-style parties that appear in the assignment's sample output
DEFAULT_ALIASES: dict[str, list[str]] = {
    "Origin Agent": ["origin agent", "origin agency", "起运港代理", "始发港代理", "国内代理", "起运地代理"],
    "Destination Agent": [
        "destination agent", "destination agency", "目的港代理", "目的港代理行", "目的地代理", "卸货港代理",
    ],
    "Carrier": ["carrier", "船公司", "承运人", "ocean carrier", "shipping line", "line"],
    "Shipper": ["shipper", "发货人", "托运人", "shpr"],
    "Consignee": ["consignee", "收货人", "cnEE".lower()],
    "Notify Party": ["notify party", "通知人", "通知方"],
    "Broker": ["broker", "customs broker", "报关行"],
}

# corporate/legal suffixes dropped before comparison -- these carry no identity
_SUFFIXES = [
    "co ltd", "co.,ltd", "co., ltd", "co ltd.", "company limited", "limited", "ltd", "llc", "inc",
    "incorporated", "corp", "corporation", "gmbh", "sdn bhd", "pte ltd", "pty ltd",
    "agencies", "agency", "有限公司", "股份有限公司", "有限责任公司", "集团", "集团有限",
]

# industry words dropped only for a *loose* comparison, so that
# "ABC Logistics" and "ABC Shipping" stay distinguishable on the strict key
_INDUSTRY = [
    "logistics", "logistic", "shipping", "forwarding", "forwarder", "freight", "cargo",
    "transport", "transportation", "agent", "line", "marine", "int l", "international",
    "物流", "货代", "货运代理", "国际货运", "运输", "代理", "船务", "海运", "国际",
]

_PUNCT = re.compile(r"[^0-9a-z\u4e00-\u9fff]+")


def normalize_party_name(name: object, *, drop_suffixes: bool = True) -> str:
    text = unicodedata.normalize("NFKC", str(name or "")).lower()
    text = text.replace("&", " and ")
    text = _PUNCT.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if drop_suffixes:
        changed = True
        while changed:
            changed = False
            for suf in _SUFFIXES:
                if text.endswith(" " + suf):
                    text = text[: -(len(suf) + 1)].strip()
                    changed = True
                elif text == suf:
                    return ""
    return text


@dataclass
class PartyMatch:
    canonical: str
    raw: str
    method: str = "unmatched"  # exact | alias | fuzzy | email | unmatched
    score: float = 0.0
    warnings: list[str] = field(default_factory=list)


class PartyResolver:
    """Resolve raw party strings/emails onto canonical entities."""

    def __init__(
        self,
        aliases: dict[str, Iterable[str]] | None = None,
        *,
        domain_map: dict[str, str] | None = None,
        fuzzy_threshold: float = 0.9,
        use_defaults: bool = True,
    ) -> None:
        self.aliases: dict[str, list[str]] = {}
        if use_defaults:
            self.aliases.update({k: list(v) for k, v in DEFAULT_ALIASES.items()})
        for canon, names in (aliases or {}).items():
            self.aliases.setdefault(canon, [])
            self.aliases[canon].extend(list(names))
        self.domain_map = {k.lower().strip(): v for k, v in (domain_map or {}).items()}
        self.fuzzy_threshold = fuzzy_threshold
        self._index: dict[str, str] = {}
        for canon, names in self.aliases.items():
            self._index[self._norm(canon)] = canon
            for n in names:
                self._index[self._norm(n)] = canon
        # canonical entities coming from the domain map are resolvable by name too
        for canon in self.domain_map.values():
            self._index.setdefault(self._norm(canon), canon)
        # canonicals that are pure role words (not company names)
        self._roles = set(DEFAULT_ALIASES) | {
            canon for canon, names in (aliases or {}).items() if canon in DEFAULT_ALIASES
        }

    # -- construction -----------------------------------------------------
    @classmethod
    def from_file(cls, path: str | Path) -> PartyResolver:
        p = Path(path)
        if not p.exists():
            return cls()
        raw: Any
        text = p.read_text(encoding="utf-8")
        if p.suffix.lower() in {".yaml", ".yml"}:
            import yaml  # optional dependency

            raw = yaml.safe_load(text) or {}
        else:
            raw = json.loads(text)
        aliases = raw.get("aliases", {})
        domains = raw.get("domains", {})
        return cls(aliases=aliases, domain_map=domains)

    # -- api --------------------------------------------------------------
    def resolve(self, raw: object) -> PartyMatch | None:
        if raw is None:
            return None
        text = str(raw).strip()
        if not text:
            return None

        email = re.search(r"[\w.+-]+@([\w.-]+\.[a-z]{2,})", text, re.IGNORECASE)
        if email:
            domain = email.group(1).lower()
            for d, canon in self.domain_map.items():
                if domain == d or domain.endswith("." + d):
                    return PartyMatch(canonical=canon, raw=text, method="email", score=1.0)

        key = self._norm(text)
        if not key:
            return None
        if key in self._index:
            canon = self._index[key]
            # A legal entity ("Destination Agent Pte Ltd") must keep its own name:
            # collapsing it to the role word "Destination Agent" would lose the
            # counterparty.  Role aliases only win for bare role words.
            if canon in self._roles and key != self._norm(canon) and self._had_legal_suffix(text):
                return PartyMatch(canonical=text, raw=text, method="unmatched", score=0.0)
            method = "exact" if self._norm(canon) == key else "alias"
            return PartyMatch(canonical=canon, raw=text, method=method, score=1.0)

        best, score = None, 0.0
        for alias, canon in self._index.items():
            if not alias:
                continue
            s = SequenceMatcher(None, key, alias).ratio()
            if s > score:
                best, score = canon, s
        if best and score >= self.fuzzy_threshold:
            return PartyMatch(
                canonical=best,
                raw=text,
                method="fuzzy",
                score=round(score, 4),
                warnings=[f"主体 {text!r} 模糊匹配到 {best!r} ({score:.2f})"],
            )
        return PartyMatch(canonical=text, raw=text, method="unmatched", score=0.0)

    def same(self, a: object, b: object) -> tuple[bool, float]:
        if a is None or b is None:
            return False, 0.0
        ka, kb = self._norm(str(a)), self._norm(str(b))
        if not ka or not kb:
            return False, 0.0
        if ka == kb:
            return True, 1.0
        ra, rb = self.resolve(a), self.resolve(b)
        if ra and rb and ra.method != "unmatched" and ra.canonical == rb.canonical:
            return True, min(ra.score or 1.0, rb.score or 1.0)
        la, lb = loose_party_key(a), loose_party_key(b)
        if la and la == lb and min(len(la), len(lb)) >= 4:
            return True, 0.93
        s = SequenceMatcher(None, ka, kb).ratio()
        return s >= self.fuzzy_threshold, round(s, 4)

    # -- internals --------------------------------------------------------
    def _norm(self, text: str) -> str:
        key = normalize_party_name(text)
        if not key:
            # a suffix-only string such as "Logistics Ltd" -- keep something
            key = normalize_party_name(text, drop_suffixes=False)
        return key

    @staticmethod
    def _had_legal_suffix(text: str) -> bool:
        return normalize_party_name(text, drop_suffixes=False) != normalize_party_name(text)


def loose_party_key(name: object) -> str:
    """Identity key with industry words removed (``ABC Logistics`` -> ``abc``)."""
    key = normalize_party_name(name)
    tokens = [t for t in key.split() if t not in _INDUSTRY]
    joined = "".join(tokens)
    return joined or key


def load_domain_map(path: str | Path) -> dict[str, str]:
    p = Path(path)
    if not p.exists():
        return {}
    data = json.loads(p.read_text(encoding="utf-8"))
    return {str(k).lower(): str(v) for k, v in (data.get("domains") or data).items()}
