"""PDF ingestion.

Strategy: read the text layer first and only pay for OCR when it is missing or
obviously unusable (scan).  This is both cheaper and *more accurate* for born
digital documents, and it gives a second, independent reading of the numbers
that the OCR path can be cross-checked against.
"""

from __future__ import annotations

from pathlib import Path

from ..models import NormalizedDocument, TableData
from .excel import guess_header_row

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

    _extract_bordered_tables(doc, path)


# --------------------------------------------------------------------------- #
# tables (pdfplumber, only where it helps)
# --------------------------------------------------------------------------- #
MIN_RULE_ITEMS = 8  # ruling lines / rects needed before a page counts as "ruled"


def _extract_bordered_tables(doc: NormalizedDocument, path: Path) -> None:
    """Extract tables from PDFs that actually have ruling lines.

    pdfplumber's geometry engine is excellent for ruled tables and *harmful* for
    whitespace-aligned bills: given a borderless table it splits cells mid-word
    (``DU`` / ``E DATE``) and merges data rows.  Freight debit notes are mostly
    borderless, so this only runs on pages that carry enough lines/rects, and
    the line-oriented text parser remains the primary path for the rest.
    """
    try:
        import pdfplumber
    except ImportError:
        doc.meta["pdfplumber"] = False
        return

    pages_with_tables = 0
    try:
        with pdfplumber.open(str(path)) as pdf:
            for i, page in enumerate(pdf.pages[:MAX_PAGES]):
                rules = len(getattr(page, "lines", []) or []) + len(getattr(page, "rects", []) or [])
                if rules < MIN_RULE_ITEMS:
                    continue
                for t_index, table in enumerate(page.extract_tables() or []):
                    rows = _explode_multiline_rows(_clean_rows(table))
                    rows = [row for row in rows if any(row)]
                    if len(rows) < 2:
                        continue
                    header_row = guess_header_row(rows)
                    doc.tables.append(
                        TableData(
                            table_id=f"{doc.doc_id}-p{i + 1}-t{t_index + 1}",
                            rows=rows,
                            header_row=header_row,
                            headers=rows[header_row] if header_row is not None else [],
                            page=i + 1,
                            source=doc.filename,
                        )
                    )
                    pages_with_tables += 1
    except Exception as exc:  # malformed PDF: never fail the whole document
        doc.warnings.append(f"pdfplumber 表格提取失败: {exc}")
        return

    doc.meta["pdfplumber"] = True
    doc.meta["bordered_tables"] = pages_with_tables
    if pages_with_tables:
        doc.warnings.append(f"pdfplumber 提取到 {pages_with_tables} 个带框线表格")


def _clean_rows(table: list[list[str | None]]) -> list[list[str]]:
    return [[(cell or "").strip() for cell in row] for row in table if row]


def _explode_multiline_rows(rows: list[list[str]]) -> list[list[str]]:
    """Split a row whose cells each hold N lines into N rows.

    pdfplumber merges vertically stacked cells into one when a table has no
    horizontal rules; every cell then carries the same number of lines.  That is
    recoverable information, so recover it instead of discarding the row.
    """
    out: list[list[str]] = []
    for row in rows:
        counts = {len(cell.splitlines()) for cell in row if cell}
        if len(counts) == 1:
            line_count = counts.pop()
            if line_count > 1:
                columns = [cell.splitlines() for cell in row]
                for k in range(line_count):
                    out.append([col[k].strip() if k < len(col) else "" for col in columns])
                continue
        out.append(row)
    return out
