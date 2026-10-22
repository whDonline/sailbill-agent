"""EML parsing: headers, plain/html body and recursive attachments.

The body is kept both as text and as ``mail_header`` blocks so that provenance
("this reference number came from the Subject line") survives into the audit
trail.  Attachments are materialised into the cache directory and handed back to
the router for recursive parsing.
"""

from __future__ import annotations

import email
import hashlib
import shutil
from collections.abc import Iterator
from email import policy
from email.message import Message
from pathlib import Path

from ..config import Settings
from ..models import Attachment, NormalizedDocument, SourceKind, sha256_file
from ..normalize.html import html_to_text

MAX_ATTACHMENTS = 40
MAX_BODY_CHARS = 200_000


def _decode(part: Message) -> str:
    payload = part.get_payload(decode=True)
    if payload is None:
        return ""
    charset = part.get_content_charset() or "utf-8"
    for enc in (charset, "utf-8", "gb18030", "latin-1"):
        try:
            return payload.decode(enc, errors="strict")
        except (UnicodeDecodeError, LookupError):
            continue
    return payload.decode("utf-8", errors="replace")


def _walk(part: Message) -> Iterator[Message]:
    if part.is_multipart():
        for child in part.get_payload():
            if isinstance(child, Message):
                yield from _walk(child)
    else:
        yield part


def parse_eml(doc: NormalizedDocument, *, settings: Settings, depth: int = 0) -> None:
    raw = Path(doc.path).read_bytes() if doc.path else b""
    msg = email.message_from_bytes(raw, policy=policy.default)

    headers = {
        "subject": str(msg.get("Subject", "") or ""),
        "from": str(msg.get("From", "") or ""),
        "to": str(msg.get("To", "") or ""),
        "cc": str(msg.get("Cc", "") or ""),
        "date": str(msg.get("Date", "") or ""),
        "message_id": str(msg.get("Message-ID", "") or ""),
        "in_reply_to": str(msg.get("In-Reply-To", "") or ""),
        "references": str(msg.get("References", "") or ""),
        "reply_to": str(msg.get("Reply-To", "") or ""),
    }
    doc.meta["mail_headers"] = headers

    for key in ("subject", "from", "to", "cc", "date", "message_id", "in_reply_to"):
        if headers[key]:
            doc.add_block(f"{key}: {headers[key]}", kind="mail_header", source=doc.filename)

    bodies: list[str] = []
    html_bodies: list[str] = []
    attachment_dir = settings.cache_dir / "attachments" / doc.doc_id
    count = 0
    for part in _walk(msg):
        ctype = part.get_content_type()
        filename = part.get_filename()
        is_attachment = bool(filename) or part.get_content_disposition() == "attachment"

        if not is_attachment and ctype in {"text/plain", "text/html"}:
            text = _decode(part)
            if ctype == "text/html":
                text = html_to_text(text)
                if text.strip():
                    html_bodies.append(text)
            elif text.strip():
                bodies.append(text)
            continue

        if not is_attachment:
            continue
        if count >= MAX_ATTACHMENTS:
            doc.warnings.append(f"附件数量超过 {MAX_ATTACHMENTS}，已忽略其余附件")
            break
        count += 1
        payload = part.get_payload(decode=True)
        if not payload:
            continue
        safe_name = _safe_name(filename or f"attachment-{count}")
        attachment_dir.mkdir(parents=True, exist_ok=True)
        target = _materialise(attachment_dir, safe_name, payload)
        doc.attachments.append(
            Attachment(
                filename=safe_name,
                sha256=hashlib.sha256(payload).hexdigest(),
                size=len(payload),
                kind=_guess_kind(safe_name),
                path=str(target),
                depth=depth + 1,
            )
        )

    # multipart/alternative carries the same message twice: prefer text/plain so
    # the body (and any evidence quoted from it) is not duplicated
    body = "\n\n".join(bodies or html_bodies).strip()
    doc.text = body[:MAX_BODY_CHARS]
    if body:
        for i, line in enumerate(doc.text.splitlines()):
            if line.strip():
                doc.add_block(line.strip(), kind="paragraph", line=i, source="body")
    if not body and not doc.attachments:
        doc.warnings.append("邮件正文为空且无附件")


def _safe_name(name: str) -> str:
    keep = "".join(ch for ch in name.replace("\\", "_").replace("/", "_") if ch.isprintable())
    keep = keep.strip() or "attachment"
    return keep[:120]


def _materialise(directory: Path, name: str, payload: bytes) -> Path:
    """Write an attachment once, and reuse it on later runs.

    Using ``_unique`` unconditionally re-copied the same attachment on every run
    (``name-1.pdf``, ``name-2.pdf``, ...), which grew the cache and, worse, made
    the *filename* — and therefore the audit trail — non-deterministic.
    """
    target = directory / name
    if target.exists() and sha256_file(target) == hashlib.sha256(payload).hexdigest():
        return target
    target = _unique(target)
    target.write_bytes(payload)
    return target


def _unique(path: Path) -> Path:
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    for i in range(1, 1000):
        candidate = path.with_name(f"{stem}-{i}{suffix}")
        if not candidate.exists():
            return candidate
    return path


def _guess_kind(name: str) -> SourceKind:
    from .router import _EXT_KIND  # local import avoids a cycle

    return _EXT_KIND.get(Path(name).suffix.lower(), SourceKind.UNKNOWN)


def materialize(path: str, target_dir: Path) -> Path:
    """Copy a file into the cache (used by tests and URL delivery)."""
    target_dir.mkdir(parents=True, exist_ok=True)
    dst = target_dir / Path(path).name
    shutil.copy2(path, dst)
    return dst
