"""转供单存档：SQLite 持久化。

同一目标线路最多保留一笔待执行(pending)单，由部分唯一索引硬性保证；
转供内容或线路容量变化时旧许可置为 invalidated，重算结果另写一笔或原位更新。
事务由调用方（业务服务）统一管理，本模块只负责读写。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _j(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


class TransferStore:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.init_schema()

    def init_schema(self) -> None:
        self.conn.executescript("""
        CREATE TABLE IF NOT EXISTS transfer_orders (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          source_line_id INTEGER NOT NULL REFERENCES assets(id),
          target_line_id INTEGER NOT NULL REFERENCES assets(id),
          load_mw REAL NOT NULL,
          important_users_json TEXT NOT NULL,
          signature_json TEXT NOT NULL,
          state TEXT NOT NULL CHECK(state IN ('pending','executed','invalidated')),
          decision_json TEXT NOT NULL,
          snapshot_json TEXT NOT NULL,
          note TEXT,
          invalidated_reason TEXT,
          revision INTEGER NOT NULL DEFAULT 1,
          created_by TEXT NOT NULL,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_transfer_pending_target
          ON transfer_orders(target_line_id) WHERE state='pending';
        """)
        self.conn.commit()

    def _dict(self, row: sqlite3.Row) -> dict:
        return {"id": row["id"], "source_line_id": row["source_line_id"], "target_line_id": row["target_line_id"],
                "load_mw": row["load_mw"], "important_users": json.loads(row["important_users_json"]),
                "signature": json.loads(row["signature_json"]), "state": row["state"],
                "decision": json.loads(row["decision_json"]), "snapshot": json.loads(row["snapshot_json"]),
                "note": row["note"], "invalidated_reason": row["invalidated_reason"], "revision": row["revision"],
                "created_by": row["created_by"], "created_at": row["created_at"], "updated_at": row["updated_at"]}

    def get(self, order_id: int) -> dict | None:
        row = self.conn.execute("SELECT * FROM transfer_orders WHERE id=?", (order_id,)).fetchone()
        return self._dict(row) if row else None

    def pending_for_target(self, target_line_id: int) -> dict | None:
        row = self.conn.execute("SELECT * FROM transfer_orders WHERE target_line_id=? AND state='pending'", (target_line_id,)).fetchone()
        return self._dict(row) if row else None

    def pending_for_line(self, line_id: int) -> list[dict]:
        return [self._dict(r) for r in self.conn.execute(
            "SELECT * FROM transfer_orders WHERE state='pending' AND (source_line_id=? OR target_line_id=?) ORDER BY id",
            (line_id, line_id))]

    def create(self, *, source_line_id: int, target_line_id: int, load_mw: float, important_users: list[dict],
               signature: dict, decision: dict, snapshot: dict, note: str, actor: str) -> dict:
        cur = self.conn.execute(
            """INSERT INTO transfer_orders(source_line_id,target_line_id,load_mw,important_users_json,signature_json,
                                         state,decision_json,snapshot_json,note,created_by,created_at,updated_at)
               VALUES(?,?,?,?,?, 'pending',?,?,?,?,?,?)""",
            (source_line_id, target_line_id, load_mw, _j(important_users), _j(signature),
             _j(decision), _j(snapshot), note, actor, _now(), _now()))
        return self.get(cur.lastrowid)

    def replace_decision(self, order_id: int, decision: dict, snapshot: dict) -> dict:
        self.conn.execute("UPDATE transfer_orders SET decision_json=?,snapshot_json=?,revision=revision+1,updated_at=? WHERE id=?",
                          (_j(decision), _j(snapshot), _now(), order_id))
        return self.get(order_id)

    def invalidate(self, order_id: int, reason: str) -> dict:
        self.conn.execute("UPDATE transfer_orders SET state='invalidated',invalidated_reason=?,revision=revision+1,updated_at=? WHERE id=?",
                          (reason, _now(), order_id))
        return self.get(order_id)

    def mark_executed(self, order_id: int) -> dict:
        self.conn.execute("UPDATE transfer_orders SET state='executed',revision=revision+1,updated_at=? WHERE id=? AND state='pending'",
                          (_now(), order_id))
        return self.get(order_id)

    def committed_load(self, target_line_id: int) -> float:
        """目标线路已被已执行转供单占用的承接容量。"""
        return float(self.conn.execute(
            "SELECT COALESCE(SUM(load_mw),0) FROM transfer_orders WHERE target_line_id=? AND state='executed'",
            (target_line_id,)).fetchone()[0])

    def list_pending(self) -> list[dict]:
        return [self._dict(r) for r in self.conn.execute("SELECT * FROM transfer_orders WHERE state='pending' ORDER BY id")]

    def list_orders(self, limit: int = 50) -> list[dict]:
        return [self._dict(r) for r in self.conn.execute("SELECT * FROM transfer_orders ORDER BY id DESC LIMIT ?", (limit,))]
