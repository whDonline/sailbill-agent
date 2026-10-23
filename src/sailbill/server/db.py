"""SQLite 持久层：批次、单据记录、账单库。

单机零安装方案：仅用标准库 ``sqlite3``（WAL 模式），账单库复用
``BillStore`` 的内存幂等逻辑，只在写入点同步落库（整单 JSON 列存储），
进程重启后自动加载，财务数据不出本机。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..models import Bill
from ..store import BillStore

_SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
  id TEXT PRIMARY KEY,
  created_at TEXT NOT NULL,
  status TEXT NOT NULL,
  hitl INTEGER NOT NULL DEFAULT 0,
  commit_enabled INTEGER NOT NULL DEFAULT 0,
  summary_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS records (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  batch_id TEXT NOT NULL,
  filename TEXT NOT NULL,
  doc_id TEXT NOT NULL,
  status TEXT NOT NULL,
  decision TEXT,
  confidence REAL,
  record_json TEXT NOT NULL DEFAULT '{}',
  trace_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_records_batch ON records(batch_id);
CREATE INDEX IF NOT EXISTS idx_records_status ON records(status);
CREATE TABLE IF NOT EXISTS bills (
  bill_id TEXT PRIMARY KEY,
  data_json TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    """SQLite 连接管理：每次操作独立连接，天然线程安全。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as conn:
            conn.executescript(_SCHEMA)

    # -- 连接 --------------------------------------------------------------
    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    # -- 通用 --------------------------------------------------------------
    def execute(self, sql: str, params: tuple | list = ()) -> None:
        with self._conn() as conn:
            conn.execute(sql, params)

    def query(self, sql: str, params: tuple | list = ()) -> list[dict[str, Any]]:
        with self._conn() as conn:
            return [dict(row) for row in conn.execute(sql, params).fetchall()]

    # -- 账单库 -------------------------------------------------------------
    def save_bill(self, bill: Bill) -> None:
        payload = json.dumps(bill.model_dump(mode="json"), ensure_ascii=False)
        self.execute(
            "INSERT INTO bills(bill_id, data_json) VALUES(?,?)"
            " ON CONFLICT(bill_id) DO UPDATE SET data_json=excluded.data_json",
            (bill.bill_id, payload),
        )

    def load_bills(self) -> list[Bill]:
        rows = self.query("SELECT data_json FROM bills ORDER BY bill_id")
        return [
            Bill.from_any(json.loads(row["data_json"]), index=i, source=str(self.path))
            for i, row in enumerate(rows)
        ]

    # -- 批次 ---------------------------------------------------------------
    def insert_batch(self, batch_id: str, *, hitl: bool, commit: bool) -> None:
        self.execute(
            "INSERT INTO batches(id, created_at, status, hitl, commit_enabled) VALUES(?,?,?,?,?)",
            (batch_id, _now(), "queued", int(hitl), int(commit)),
        )

    def update_batch(self, batch_id: str, *, status: str, summary: dict[str, Any] | None = None) -> None:
        if summary is None:
            self.execute("UPDATE batches SET status=? WHERE id=?", (status, batch_id))
        else:
            self.execute(
                "UPDATE batches SET status=?, summary_json=? WHERE id=?",
                (status, json.dumps(summary, ensure_ascii=False), batch_id),
            )

    def get_batch(self, batch_id: str) -> dict[str, Any] | None:
        rows = self.query("SELECT * FROM batches WHERE id=?", (batch_id,))
        return rows[0] if rows else None

    def list_batches(self) -> list[dict[str, Any]]:
        return self.query("SELECT * FROM batches ORDER BY created_at DESC, id DESC")

    # -- 单据记录 ------------------------------------------------------------
    def insert_record(
        self,
        *,
        batch_id: str,
        filename: str,
        doc_id: str,
        status: str,
        decision: str | None,
        confidence: float | None,
        record_json: dict[str, Any],
        trace_json: dict[str, Any],
    ) -> int:
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO records(batch_id, filename, doc_id, status, decision,"
                " confidence, record_json, trace_json, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    batch_id, filename, doc_id, status, decision, confidence,
                    json.dumps(record_json, ensure_ascii=False),
                    json.dumps(trace_json, ensure_ascii=False), _now(),
                ),
            )
            return int(cur.lastrowid or 0)

    def update_record_status(self, record_id: int, *, status: str, decision: str | None = None) -> None:
        if decision is None:
            self.execute("UPDATE records SET status=? WHERE id=?", (status, record_id))
        else:
            self.execute("UPDATE records SET status=?, decision=? WHERE id=?", (status, decision, record_id))

    def get_record(self, record_id: int) -> dict[str, Any] | None:
        rows = self.query("SELECT * FROM records WHERE id=?", (record_id,))
        return rows[0] if rows else None

    def list_records(self, batch_id: str) -> list[dict[str, Any]]:
        return self.query("SELECT * FROM records WHERE batch_id=? ORDER BY id", (batch_id,))

    def list_review(self) -> list[dict[str, Any]]:
        """复核队列：暂停在写账前、或决策本身就是待复核的记录。"""
        return self.query(
            "SELECT * FROM records WHERE status IN ('paused','pending_review') ORDER BY id DESC"
        )

    def find_records(self, *, batch_id: str, doc_id: str) -> list[dict[str, Any]]:
        return self.query("SELECT * FROM records WHERE batch_id=? AND doc_id=? ORDER BY id", (batch_id, doc_id))

    def mark_stale_paused(self) -> int:
        """服务重启后 MemorySaver 里的暂停态已丢失，历史 paused 行标记 interrupted。"""
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE records SET status='interrupted' WHERE status='paused'"
            )
            return int(cur.rowcount or 0)


class SqliteBillStore(BillStore):
    """``BillStore`` 的 SQLite 落地版：对外接口完全一致。

    继承内存版的幂等写入逻辑（同名同币种同金额不重复入账、UPDATE 按
    line_id 精确改行），只在 ``upsert_line`` / ``apply_record`` 两个写入点
    同步落库，跑完即持久化，无需再手动 save。
    """

    def __init__(self, db: Database) -> None:
        super().__init__()
        self._db = db
        for bill in db.load_bills():
            self._bills[bill.bill_id] = bill

    # -- 落库 ----------------------------------------------------------------
    def _persist(self, bill_id: str) -> None:
        bill = self._bills.get(bill_id)
        if bill is not None:
            self._db.save_bill(bill)

    # -- 写入点重写：先走内存逻辑，再落库 ----------------------------------------
    def upsert_line(self, bill_id: str, **kwargs: Any) -> Any:
        line = super().upsert_line(bill_id, **kwargs)
        self._persist(bill_id)
        return line

    def apply_record(
        self, record: Any, *, matched_bill_id: str | None = None, matched_line_id: str | None = None
    ) -> str | None:
        bill_id = super().apply_record(record, matched_bill_id=matched_bill_id, matched_line_id=matched_line_id)
        if bill_id:
            self._persist(bill_id)
        return bill_id

    def save(self, path: str | Path) -> None:
        # 兼容父类签名：先全量刷库，再按原样导出 JSON 快照
        for bill_id in self._bills:
            self._persist(bill_id)
        super().save(path)
