"""本地 FastAPI 服务：上传批次、进度查询、复核、台账导出、设置向导。

只监听 127.0.0.1，单机单用户，因此不做认证；所有数据落本地 SQLite。
"""

from __future__ import annotations

import io
import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from ..models import Bill
from .db import Database
from .jobs import BatchRunner, new_batch_id
from .settings_store import data_dir, public_view, write_config

_STATIC_DIR = Path(__file__).parent / "static"
_UPLOAD_SUFFIXES = {
    ".eml", ".msg", ".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp",
    ".xlsx", ".xls", ".xlsm", ".csv", ".docx", ".doc", ".txt", ".json",
}


def create_app() -> FastAPI:
    db = Database(data_dir() / "sailbill.db")
    runner = BatchRunner(db)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        # 重启后进程内暂停态丢失，历史 paused 记录标记 interrupted
        runner.recover()
        yield

    app = FastAPI(title="SailBill 桌面版", lifespan=lifespan)

    # -- 批次 ------------------------------------------------------------------
    @app.post("/api/batches")
    async def upload_batch(
        files: Annotated[list[UploadFile], File()],
        hitl: Annotated[bool, Form()] = False,
        commit: Annotated[bool, Form()] = True,
    ) -> dict[str, Any]:
        batch_id = new_batch_id()
        batch_dir = data_dir() / "batches" / batch_id
        batch_dir.mkdir(parents=True, exist_ok=True)
        saved: list[Path] = []
        for upload in files:
            suffix = Path(upload.filename or "").suffix.lower()
            if suffix not in _UPLOAD_SUFFIXES:
                continue  # 忽略不支持的类型
            target = batch_dir / Path(upload.filename or "unnamed").name
            target.write_bytes(await upload.read())
            saved.append(target)
        if not saved:
            raise HTTPException(400, "没有可处理的支持文件（pdf/图片/邮件/表格/文档）")
        runner.submit_batch(batch_id, saved, hitl=hitl, commit=commit)
        return {"batch_id": batch_id, "files": len(saved)}

    @app.get("/api/batches")
    def list_batches() -> list[dict[str, Any]]:
        out = []
        for batch in db.list_batches():
            batch["summary"] = _loads(batch.pop("summary_json", "{}"))
            batch["records_count"] = len(db.list_records(batch["id"]))
            out.append(batch)
        return out

    @app.get("/api/batches/{batch_id}")
    def get_batch(batch_id: str) -> dict[str, Any]:
        batch = db.get_batch(batch_id)
        if batch is None:
            raise HTTPException(404, "批次不存在")
        batch["summary"] = _loads(batch.pop("summary_json", "{}"))
        records = []
        for row in db.list_records(batch_id):
            row.pop("record_json", None)
            row.pop("trace_json", None)
            records.append(row)
        batch["records"] = records
        return batch

    @app.get("/api/records/{record_id}")
    def get_record(record_id: int) -> dict[str, Any]:
        row = db.get_record(record_id)
        if row is None:
            raise HTTPException(404, "记录不存在")
        row["record"] = _loads(row.pop("record_json", "{}"))
        row["trace"] = _loads(row.pop("trace_json", "{}"))
        return row

    # -- 复核 ------------------------------------------------------------------
    @app.get("/api/review")
    def review_queue() -> list[dict[str, Any]]:
        rows = db.list_review()
        for row in rows:
            row.pop("trace_json", None)
            row["record"] = _loads(row.pop("record_json", "{}"))
        return rows

    @app.post("/api/review/{doc_id}/confirm")
    def review_confirm(doc_id: str) -> dict[str, Any]:
        # pending_review 是决策本身转人工（信息不足），不允许直接确认入账；
        # confirm 只服务于 HITL 暂停（paused）的续跑
        rows = db.query("SELECT status FROM records WHERE doc_id=? ORDER BY id DESC LIMIT 1", (doc_id,))
        if rows and rows[0]["status"] == "pending_review":
            raise HTTPException(
                409,
                "该记录因信息不足自动转人工（常见原因：未配置记账主体，AR/AP 无法判定）。"
                "请到「设置」页填写记账主体后重新上传识别。",
            )
        if not runner.confirm(doc_id):
            raise HTTPException(404, "该单据不在暂停队列中（可能服务已重启，请重跑批次）")
        return {"ok": True, "doc_id": doc_id}

    @app.post("/api/review/{doc_id}/reject")
    def review_reject(doc_id: str) -> dict[str, Any]:
        if not runner.reject(doc_id):
            raise HTTPException(404, "该单据不在暂停队列中")
        return {"ok": True, "doc_id": doc_id}

    # -- 台账导出 ---------------------------------------------------------------
    @app.get("/api/export.xlsx")
    def export_xlsx() -> Response:
        buffer = _build_xlsx(db.load_bills())
        return Response(
            content=buffer.getvalue(),
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": 'attachment; filename="sailbill-ledger.xlsx"'},
        )

    # -- 设置 ------------------------------------------------------------------
    @app.get("/api/settings")
    def get_settings() -> dict[str, Any]:
        return public_view()

    @app.put("/api/settings")
    def put_settings(payload: dict[str, Any]) -> dict[str, Any]:
        write_config(payload)
        return public_view()

    # -- 静态页 ------------------------------------------------------------------
    app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(_STATIC_DIR / "index.html")

    return app


def _loads(raw: str) -> Any:
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return {}


def _build_xlsx(bills: list[Bill]) -> io.BytesIO:
    """账单台账：每条费用行一行，账单级字段随之重复，方便筛选透视。"""
    from openpyxl import Workbook

    headers = ["账单ID", "单据类型", "参考号", "单据日期", "出票人", "收票人",
               "记账主体", "方向", "币种", "费用项", "金额", "付款条款"]
    wb = Workbook()
    ws = wb.active
    ws.title = "台账"
    ws.append(headers)
    for bill in bills:
        for line in bill.lines:
            ws.append([
                bill.bill_id, bill.document_type, bill.reference_no, bill.document_date,
                bill.issuer, bill.recipient, bill.accounting_entity, bill.posting_type,
                line.currency, line.charge_item, line.amount, line.payment_term,
            ])
    if not bills:
        ws.append(["（暂无账单）"])
    buffer = io.BytesIO()
    wb.save(buffer)
    buffer.seek(0)
    return buffer
