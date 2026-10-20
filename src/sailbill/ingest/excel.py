"""Excel / CSV ingestion.

Keeps *all* rows (header row included, plus its index) so the extraction layer
can re-detect the header itself -- bills love to put a title, an address block
and two blank rows above the actual table.
"""

from __future__ import annotations

import csv
from pathlib import Path

from ..models import NormalizedDocument, TableData

MAX_ROWS = 5000
MAX_COLS = 60


def _cell(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def parse_excel(doc: NormalizedDocument) -> None:
    path = Path(doc.path) if doc.path else None
    if path is None:
        return
    suffix = path.suffix.lower()
    if suffix == ".csv":
        _parse_csv(doc, path)
    elif suffix == ".xls":
        _parse_xls_legacy(doc, path)
    else:
        _parse_openpyxl(doc, path)


def _parse_openpyxl(doc: NormalizedDocument, path: Path) -> None:
    try:
        from openpyxl import load_workbook
    except ImportError:  # pragma: no cover
        doc.warnings.append("未安装 openpyxl，无法解析 Excel")
        return

    try:
        wb = load_workbook(path, read_only=True, data_only=True)
    except Exception as exc:
        doc.warnings.append(f"Excel 打开失败: {exc}")
        return

    doc.meta["sheet_names"] = list(wb.sheetnames)
    for sheet_name in wb.sheetnames:
        ws = wb[sheet_name]
        rows: list[list[str]] = []
        for r, row in enumerate(ws.iter_rows(values_only=True)):
            if r >= MAX_ROWS:
                doc.warnings.append(f"工作表 {sheet_name} 行数超过 {MAX_ROWS}，已截断")
                break
            values = [_cell(c) for c in row[:MAX_COLS]]
            if not any(values):
                continue
            rows.append(values)
        if not rows:
            continue
        table = TableData(
            table_id=f"{doc.doc_id}-{sheet_name}",
            rows=rows,
            sheet=sheet_name,
            source=doc.filename,
            header_row=_guess_header_row(rows),
        )
        if table.header_row is not None:
            table.headers = rows[table.header_row]
        doc.tables.append(table)
        for i, row in enumerate(rows):
            line = " | ".join(v for v in row if v)
            if line:
                doc.add_block(line, kind="table_cell", row=i, table_id=table.table_id, source=doc.filename)
    wb.close()
    if not doc.tables:
        doc.warnings.append("Excel 中没有可用的数据")


def _parse_csv(doc: NormalizedDocument, path: Path) -> None:
    for enc in ("utf-8-sig", "utf-8", "gb18030", "latin-1"):
        try:
            with path.open("r", encoding=enc, newline="") as fh:
                rows = [[c.strip() for c in row[:MAX_COLS]] for row in csv.reader(fh)][:MAX_ROWS]
            break
        except UnicodeDecodeError:
            continue
    else:  # pragma: no cover
        doc.warnings.append("CSV 编码无法识别")
        return

    rows = [r for r in rows if any(r)]
    if not rows:
        return
    table = TableData(
        table_id=f"{doc.doc_id}-csv",
        rows=rows,
        source=doc.filename,
        header_row=_guess_header_row(rows),
    )
    if table.header_row is not None:
        table.headers = rows[table.header_row]
    doc.tables.append(table)
    for i, row in enumerate(rows):
        doc.add_block(" | ".join(v for v in row if v), kind="table_cell", row=i, table_id=table.table_id)


def _parse_xls_legacy(doc: NormalizedDocument, path: Path) -> None:
    try:
        import xlrd  # optional dependency
    except ImportError:
        doc.warnings.append(
            "旧版 .xls 需要 xlrd 或先转换为 .xlsx（pip install xlrd，或 soffice --convert-to xlsx）"
        )
        doc.meta["needs_conversion"] = True
        return
    book = xlrd.open_workbook(str(path))
    for sheet in book.sheets():
        rows = [[_cell(sheet.cell_value(r, c)) for c in range(min(sheet.ncols, MAX_COLS))]
                for r in range(min(sheet.nrows, MAX_ROWS))]
        rows = [r for r in rows if any(r)]
        if not rows:
            continue
        table = TableData(
            table_id=f"{doc.doc_id}-{sheet.name}",
            rows=rows,
            sheet=sheet.name,
            source=doc.filename,
            header_row=_guess_header_row(rows),
        )
        if table.header_row is not None:
            table.headers = rows[table.header_row]
        doc.tables.append(table)
        for i, row in enumerate(rows):
            doc.add_block(" | ".join(v for v in row if v), kind="table_cell", row=i, table_id=table.table_id)


def _guess_header_row(rows: list[list[str]], window: int = 15) -> int | None:
    """Pick the most header-looking row: many short labels, few pure numbers."""
    best, best_score = None, 0.0
    for i, row in enumerate(rows[:window]):
        cells = [c for c in row if c]
        if len(cells) < 2:
            continue
        numeric = sum(1 for c in cells if _looks_numeric(c))
        words = len(cells) - numeric
        score = words - numeric * 1.5
        if words >= 2 and score > best_score:
            best, best_score = i, score
    return best


def _looks_numeric(text: str) -> bool:
    t = text.replace(",", "").replace(".", "").replace("-", "").replace(" ", "")
    return bool(t) and t.isdigit()
