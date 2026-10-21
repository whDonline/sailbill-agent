"""Outlook ``.msg`` support, via the mature ``extract-msg`` library.

``.msg`` is an OLE compound file, *not* an RFC-822 message -- routing it to the
``email`` module (as the first version of the router did) simply fails.  This
module reuses ``extract-msg`` for the container and then produces exactly the
same ``NormalizedDocument`` shape as the EML path, so attachments recurse and
the downstream stages do not care which mail format they came from.

``extract-msg`` is an optional dependency::

    pip install -e ".[msg]"
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from pathlib import Path
from typing import Any

from ..config import Settings
from ..models import Attachment, NormalizedDocument, SourceKind
from ..normalize.html import html_to_text

MAX_ATTACHMENTS = 40


def _guess_kind(name: str) -> SourceKind:
    from .router import _EXT_KIND

    return _EXT_KIND.get(Path(name).suffix.lower(), SourceKind.UNKNOWN)


def _fmt(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value or "").strip()


def parse_msg(doc: NormalizedDocument, *, settings: Settings, depth: int = 0) -> None:
    try:
        import extract_msg
    except ImportError:
        doc.warnings.append(
            '解析 Outlook .msg 需要 extract-msg：pip install -e ".[msg]"'
        )
        doc.meta["needs_dependency"] = "extract-msg"
        return

    path = Path(doc.path) if doc.path else None
    if path is None:
        return

    try:
        message = extract_msg.Message(str(path))
    except Exception as exc:  # corrupt / password protected / not a MSG
        doc.warnings.append(f".msg 打开失败: {exc}")
        return

    headers = {
        "subject": _fmt(getattr(message, "subject", "")),
        "from": _fmt(getattr(message, "sender", "")),
        "to": _fmt(getattr(message, "to", "")),
        "cc": _fmt(getattr(message, "cc", "")),
        "date": _fmt(getattr(message, "date", "")),
        "message_id": _fmt(getattr(message, "messageId", "")),
        "in_reply_to": "",
    }
    doc.meta["mail_headers"] = headers
    for key in ("subject", "from", "to", "cc", "date"):
        if headers[key]:
            doc.add_block(f"{key}: {headers[key]}", kind="mail_header", source=doc.filename)

    body = _fmt(getattr(message, "body", "")) or html_to_text(_fmt(getattr(message, "htmlBody", "")))
    doc.text = body[:200_000]
    for i, line in enumerate(doc.text.splitlines()):
        if line.strip():
            doc.add_block(line.strip(), kind="paragraph", line=i, source="body")

    attachment_dir = settings.cache_dir / "attachments" / doc.doc_id
    count = 0
    for item in getattr(message, "attachments", []) or []:
        if count >= MAX_ATTACHMENTS:
            doc.warnings.append(f"附件数量超过 {MAX_ATTACHMENTS}，已忽略其余附件")
            break
        data = getattr(item, "data", None)
        if not isinstance(data, (bytes, bytearray)) or not data:
            continue
        count += 1
        name = (
            getattr(item, "longFilename", None)
            or getattr(item, "shortFilename", None)
            or f"attachment-{count}"
        )
        safe = _safe_name(str(name))
        attachment_dir.mkdir(parents=True, exist_ok=True)
        target = _unique(attachment_dir / safe)
        target.write_bytes(bytes(data))
        doc.attachments.append(
            Attachment(
                filename=safe,
                sha256=hashlib.sha256(bytes(data)).hexdigest(),
                size=len(data),
                kind=_guess_kind(safe),
                path=str(target),
                depth=depth + 1,
            )
        )

    try:
        message.close()
    except Exception:  # pragma: no cover - best effort
        pass

    if not body and not doc.attachments:
        doc.warnings.append(".msg 正文为空且无附件")


def _safe_name(name: str) -> str:
    keep = "".join(ch for ch in name.replace("\\", "_").replace("/", "_") if ch.isprintable())
    return (keep.strip() or "attachment")[:120]


def _unique(path: Path) -> Path:
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    for i in range(1, 1000):
        candidate = path.with_name(f"{stem}-{i}{suffix}")
        if not candidate.exists():
            return candidate
    return path


def is_available() -> bool:
    try:
        import extract_msg  # noqa: F401
    except ImportError:
        return False
    return True
