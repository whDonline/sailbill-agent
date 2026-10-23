"""批次执行器：把上传的文件排队送进现有流水线，并把结果落库。

单机串行（max_workers=1）：LLM/OCR 有速率限制，串行最稳。HITL 暂停的
``UnitOutcome`` 持有编译图与 thread config（见 ``graph.run_unit_graph``），
保存在进程内 registry 中，复核确认后 ``invoke(None, config)`` 续跑 commit；
服务重启后暂停态丢失，历史 paused 行统一标 ``interrupted``。
"""

from __future__ import annotations

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any

from ..config import Settings
from ..output import summarize
from ..pipeline import RunResult, UnitOutcome, run_paths
from .db import Database, SqliteBillStore
from .settings_store import get_effective_settings


def new_batch_id() -> str:
    return datetime.now().strftime("%Y%m%d%H%M%S") + "-" + uuid.uuid4().hex[:6]


class BatchRunner:
    """单机批次执行器：提交批次、跟踪进度、复核 resume。"""

    def __init__(self, db: Database) -> None:
        self._db = db
        self._executor = ThreadPoolExecutor(max_workers=1)
        self._lock = threading.Lock()
        self._paused: dict[str, UnitOutcome] = {}

    # -- 生命周期 -------------------------------------------------------------
    def recover(self) -> int:
        """服务启动时调用：重启后进程内暂停态已丢失，标记历史 paused 行。"""
        return self._db.mark_stale_paused()

    # -- 提交 ------------------------------------------------------------------
    def submit_batch(
        self,
        batch_id: str,
        files: list[Path],
        *,
        hitl: bool,
        commit: bool,
        settings: Settings | None = None,
        accounting_entity: str | None = None,
    ) -> None:
        effective = settings or get_effective_settings()
        self._db.insert_batch(batch_id, hitl=hitl, commit=commit)
        self._executor.submit(self._run_batch, batch_id, files, hitl, commit, effective, accounting_entity)

    # -- 批次执行 ----------------------------------------------------------------
    def _run_batch(
        self,
        batch_id: str,
        files: list[Path],
        hitl: bool,
        commit: bool,
        settings: Settings,
        accounting_entity: str | None,
    ) -> None:
        self._db.update_batch(batch_id, status="running")
        store = SqliteBillStore(self._db)
        summary: dict[str, Any] = {"files": len(files), "records": 0, "by_operation": {}, "bookable_amount": 0.0}
        for path in files:
            try:
                result = run_paths(
                    [path],
                    settings=settings,
                    store=store,
                    accounting_entity=accounting_entity,
                    use_llm=settings.llm_enabled,
                    hitl=hitl,
                    commit=commit,
                )
                self._store_units(batch_id, path.name, result, commit=commit)
                _merge_summary(summary, result)
            except Exception as exc:  # 单文件失败不影响批次其余文件
                self._db.insert_record(
                    batch_id=batch_id, filename=path.name, doc_id="", status="failed",
                    decision=None, confidence=None,
                    record_json={"error": f"{type(exc).__name__}: {exc}"}, trace_json={},
                )
        self._db.update_batch(batch_id, status="done", summary=summary)

    def _store_units(self, batch_id: str, filename: str, result: RunResult, *, commit: bool) -> None:
        for unit in result.units:
            paused = bool(unit.trace.get("paused_before_commit"))
            if paused:
                with self._lock:
                    self._paused[unit.doc_id] = unit
            for record, record_extra in zip(unit.records, unit.extras, strict=True):
                if paused:
                    status = "paused"
                elif record.operation.value == "PENDING_REVIEW":
                    status = "pending_review"
                else:
                    status = "committed" if commit else "preview"
                payload = record.model_dump(mode="json")
                payload["extra"] = record_extra
                self._db.insert_record(
                    batch_id=batch_id,
                    filename=filename,
                    doc_id=unit.doc_id,
                    status=status,
                    decision=record.operation.value,
                    confidence=_confidence(record_extra),
                    record_json=payload,
                    trace_json=unit.trace,
                )

    # -- 复核 resume -----------------------------------------------------------
    def confirm(self, doc_id: str) -> bool:
        """复核确认：续跑 commit 节点，把暂停记录落账。"""
        with self._lock:
            unit = self._paused.pop(doc_id, None)
        if unit is None or getattr(unit, "graph", None) is None:
            return False
        unit.graph.invoke(None, config=unit.graph_config)
        rows = self._db.query(
            "SELECT id FROM records WHERE doc_id=? AND status='paused'", (doc_id,)
        )
        for row in rows:
            self._db.update_record_status(int(row["id"]), status="committed")
        return True

    def reject(self, doc_id: str) -> bool:
        """复核驳回：不写账，标记 rejected 留痕。"""
        with self._lock:
            unit = self._paused.pop(doc_id, None)
        if unit is None:
            return False
        rows = self._db.query(
            "SELECT id FROM records WHERE doc_id=? AND status='paused'", (doc_id,)
        )
        for row in rows:
            self._db.update_record_status(int(row["id"]), status="rejected")
        return True


def _confidence(extra: dict[str, Any]) -> float | None:
    value = extra.get("confidence")
    try:
        return round(float(value), 4) if value is not None else None
    except (TypeError, ValueError):
        return None


def _merge_summary(summary: dict[str, Any], result: RunResult) -> None:
    part = summarize(result.records)
    summary["records"] += part["records"]
    summary["bookable_amount"] = round(summary["bookable_amount"] + part["bookable_amount"], 2)
    for key, count in part["by_operation"].items():
        summary["by_operation"][key] = summary["by_operation"].get(key, 0) + count
