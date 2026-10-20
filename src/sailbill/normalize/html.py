"""HTML -> text, with the table structure kept as best as stdlib allows."""

from __future__ import annotations

import re
from html import unescape
from html.parser import HTMLParser


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


def html_to_text(html: str) -> str:
    parser = _TextExtractor()
    try:
        parser.feed(html or "")
    except Exception:
        return re.sub(r"<[^>]+>", " ", unescape(html or ""))
    text = "".join(parser.out)
    text = unescape(text)
    text = re.sub(r"[ \t\u00a0]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    lines = [ln.strip(" |") for ln in text.splitlines()]
    return "\n".join(ln for ln in lines if ln.strip())
