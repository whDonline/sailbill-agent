"""HTML -> text.

Uses :mod:`bs4` when available (it is a declared dependency) because mail bodies
are full of nested tables, quoted replies and inline styles that a regex/hand
parser mangles.  The stdlib fallback is kept so an installation without bs4
still works instead of crashing.

Table rows are flattened to ``a | b | c`` lines rather than dropped: fee tables
do arrive inside HTML mails, and the extractor downstream knows how to read them.
"""

from __future__ import annotations

import re
from html import unescape
from html.parser import HTMLParser


def html_to_text(html: str) -> str:
    text = _with_bs4(html)
    if text is None:
        text = _fallback(html)
    return _tidy(text)


# --------------------------------------------------------------------------- #
# bs4 (preferred)
# --------------------------------------------------------------------------- #
def _with_bs4(html: str) -> str | None:
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return None

    soup = BeautifulSoup(html or "", "html.parser")
    for tag in soup(["script", "style", "head", "meta", "noscript"]):
        tag.decompose()

    # flatten tables first, so the generic get_text() below sees clean lines
    for table in soup.find_all("table"):
        rows: list[str] = []
        for tr in table.find_all("tr"):
            cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
            if any(cells):
                rows.append(" | ".join(cells))
        table.replace_with("\n" + "\n".join(rows) + "\n")

    for br in soup.find_all("br"):
        br.replace_with("\n")

    return soup.get_text("\n")


# --------------------------------------------------------------------------- #
# stdlib fallback
# --------------------------------------------------------------------------- #
class _TextExtractor(HTMLParser):
    BLOCK = {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6", "table"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs) -> None:  # type: ignore[override]
        if tag in {"script", "style", "head"}:
            self._skip += 1
        if tag in self.BLOCK:
            self.out.append("\n")
        if tag in {"td", "th"}:
            self.out.append(" | ")

    def handle_endtag(self, tag: str) -> None:  # type: ignore[override]
        if tag in {"script", "style", "head"} and self._skip:
            self._skip -= 1
        if tag in self.BLOCK:
            self.out.append("\n")

    def handle_data(self, data: str) -> None:  # type: ignore[override]
        if not self._skip and data.strip():
            self.out.append(data)


def _fallback(html: str) -> str:
    parser = _TextExtractor()
    try:
        parser.feed(html or "")
    except Exception:
        return re.sub(r"<[^>]+>", " ", unescape(html or ""))
    return "".join(parser.out)


# --------------------------------------------------------------------------- #
# shared tidy-up
# --------------------------------------------------------------------------- #
def _tidy(text: str) -> str:
    text = unescape(text or "")
    text = text.replace("\u00a0", " ").replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    lines = [ln.strip(" |") for ln in text.splitlines()]
    return "\n".join(ln for ln in lines if ln.strip())
