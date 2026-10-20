"""Format detection and dispatch."""

from __future__ import annotations

import mimetypes
from pathlib import Path
from typing import Optional

from ..config import SETTINGS, Settings
from ..models import NormalizedDocument, SourceKind, sha256_file, stable_doc_id

_EXT_KIND = {
    ".eml": SourceKind.EML,
    ".msg": SourceKind.EML,
    ".pdf": SourceKind.PDF,
    ".png": SourceKind.IMAGE,
    ".jpg": SourceKind.IMAGE,
    ".jpeg": SourceKind.IMAGE,
    ".tif": SourceKind.IMAGE,
    ".tiff": SourceKind.IMAGE,
    ".bmp": SourceKind.IMAGE,
    ".webp": SourceKind.IMAGE,
    ".xls": SourceKind.EXCEL,
    ".xlsx": SourceKind.EXCEL,
    ".xlsm": SourceKind.EXCEL,
    ".csv": SourceKind.EXCEL,
    ".doc": SourceKind.WORD,
    ".docx": SourceKind.WORD,
    ".txt": SourceKind.TEXT,
    ".json": SourceKind.TEXT,
}

_MAGIC = [
    (b"%PDF", SourceKind.PDF),
    (b"\x89PNG\r\n\x1a\n", SourceKind.IMAGE),
    (b"\xff\xd8\xff", SourceKind.IMAGE),
    (b"II*\x00", SourceKind.IMAGE),
    (b"MM\x00*", SourceKind.IMAGE),
    (b"PK\x03\x04", SourceKind.UNKNOWN),  # zip container: docx/xlsx/eml-embedded
    (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", SourceKind.EXCEL),  # legacy OLE: xls/doc
]


def sniff_kind(path: str | Path, filename: str | None = None) -> SourceKind:
    """Prefer the extension, verify with magic bytes when they disagree."""
    p = Path(path)
    name = (filename or p.name).lower()
    by_ext = _EXT_KIND.get(Path(name).suffix)
    try:
        head = p.open("rb").read(16)
    except OSError:
        return by_ext or SourceKind.UNKNOWN

    for magic, kind in _MAGIC:
        if head.startswith(magic):
            if kind is SourceKind.UNKNOWN:  # OOXML: decide by extension
                return by_ext if by_ext in {SourceKind.WORD, SourceKind.EXCEL} else SourceKind.UNKNOWN
            if by_ext and kind is SourceKind.EXCEL and by_ext is SourceKind.WORD:
                return by_ext  # .doc reported as OLE -- trust the name
            return kind
    return by_ext or SourceKind.UNKNOWN


def load_document(
    path: str | Path,
    *,
    settings: Settings | None = None,
    depth: int = 0,
    parent_id: Optional[str] = None,
) -> NormalizedDocument:
    """Parse one file into a NormalizedDocument (no OCR here)."""
    settings = settings or SETTINGS
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(p)
    kind = sniff_kind(p)
    sha = sha256_file(p)
    doc = NormalizedDocument(
        doc_id=stable_doc_id(sha, p.name),
        filename=p.name,
        kind=kind,
        sha256=sha,
        size=p.stat().st_size,
        path=str(p),
    )
    doc.meta.update(
        {
            "root_id": parent_id or doc.doc_id,
            "parent_id": parent_id,
            "mime": mimetypes.guess_type(p.name)[0] or "",
            "depth": depth,
        }
    )

    if kind is SourceKind.EML:
        from .eml import parse_eml

        parse_eml(doc, settings=settings, depth=depth)
    elif kind is SourceKind.EXCEL:
        from .excel import parse_excel

        parse_excel(doc)
    elif kind is SourceKind.WORD:
        from .word import parse_word

        parse_word(doc)
    elif kind is SourceKind.PDF:
        from .pdf import parse_pdf

        parse_pdf(doc)
    elif kind is SourceKind.IMAGE:
        doc.meta["needs_ocr"] = True
        doc.warnings.append("图片文件，需 OCR 识别")
    elif kind is SourceKind.UNKNOWN:
        doc.warnings.append(f"无法识别的文件类型: {p.suffix or '(无扩展名)'}")
        doc.meta["needs_review"] = True
    else:
        try:
            doc.text = p.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:  # pragma: no cover
            doc.warnings.append(f"无法读取文件: {exc}")
        doc.add_block(doc.text)
    return doc


def load_tree(
    path: str | Path,
    *,
    settings: Settings | None = None,
    _depth: int = 0,
    _parent: Optional[str] = None,
    _seen: Optional[set[str]] = None,
) -> list[NormalizedDocument]:
    """Parse a file and every EML attachment beneath it (bounded depth)."""
    settings = settings or SETTINGS
    seen = _seen if _seen is not None else set()
    doc = load_document(path, settings=settings, depth=_depth, parent_id=_parent)
    if doc.sha256 in seen:
        return []
    seen.add(doc.sha256)
    docs = [doc]

    if doc.kind is SourceKind.EML and _depth < settings.max_attachment_depth:
        for att in doc.attachments:
            if not att.path:
                continue
            try:
                docs.extend(
                    load_tree(
                        att.path,
                        settings=settings,
                        _depth=_depth + 1,
                        _parent=doc.doc_id,
                        _seen=seen,
                    )
                )
            except Exception as exc:  # one broken attachment must not kill the run
                doc.warnings.append(f"附件解析失败 {att.filename}: {exc}")
    return docs
