"""PDF ingestion.

Strategy: read the text layer first and only pay for OCR when it is missing or
obviously unusable (scan).  This is both cheaper and *more accurate* for born
digital documents, and it gives a second, independent reading of the numbers
that the OCR path can be cross-checked against.
"""

from __future__ import annotations

from pathlib import Path

from ..models import NormalizedDocument

MAX_PAGES = 30
# below this many characters per page a PDF is treated as a scan
OCR_TEXT_THRESHOLD = 60


def parse_pdf(doc: NormalizedDocument) -> None:
    path = Path(doc.path) if doc.path else None
    if path is None:
        return
    try:
        from pypdf import PdfReader
    except ImportError:  # pragma: no cover
        doc.warnings.append("未安装 pypdf，无法解析 PDF")
        doc.meta["needs_ocr"] = True
        return

    try:
        reader = PdfReader(str(path))
        if reader.is_encrypted:
            try:
                reader.decrypt("")
            except Exception:
                doc.warnings.append("PDF 已加密，无法读取")
                doc.meta["needs_ocr"] = True
                return
        pages = len(reader.pages)
    except Exception as exc:
        doc.warnings.append(f"PDF 打开失败: {exc}")
        doc.meta["needs_ocr"] = True
        return

    chunks: list[str] = []
    for i, page in enumerate(reader.pages[:MAX_PAGES]):
        try:
            text = page.extract_text() or ""
        except Exception as exc:  # pragma: no cover - malformed page
            doc.warnings.append(f"第 {i + 1} 页文本提取失败: {exc}")
            text = ""
        chunks.append(text)
        for j, line in enumerate(text.splitlines()):
            doc.add_block(line, kind="paragraph", page=i + 1, line=j, source=doc.filename)

    doc.text = "\n".join(chunks).strip()
    chars = len(doc.text)
    per_page = chars / max(pages, 1)
    doc.meta.update({"pages": pages, "text_chars": chars, "chars_per_page": round(per_page, 1)})

    if pages > MAX_PAGES:
        doc.warnings.append(f"PDF 超过 {MAX_PAGES} 页，仅解析前 {MAX_PAGES} 页")

    if chars == 0 or per_page < OCR_TEXT_THRESHOLD:
        doc.meta["needs_ocr"] = True
        doc.warnings.append(
            f"PDF 文本层不足（{per_page:.0f} 字符/页 < {OCR_TEXT_THRESHOLD}），判定为扫描件，转 OCR"
        )
