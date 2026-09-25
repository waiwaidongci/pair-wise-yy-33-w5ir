"""转供单存档：登记、判定落档、待执行单唯一约束、许可失效重算与剩余容量视图。

判定逻辑见 transfer_rules.py，界面见 static/transfer.html。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from transfer_rules import TransferError, content_key, evaluate_transfer, normalize_users


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _j(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


SCHEMA = """
CREATE TABLE IF NOT EXISTS transfer_orders (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source_line_id INTEGER NOT NULL REFERENCES assets(id),
  target_line_id INTEGER NOT NULL REFERENCES assets(id),
  load_mw REAL NOT NULL,
  important_users_json TEXT NOT NULL,
  state TEXT NOT NULL CHECK(state IN ('pending','executed','rejected','invalidated','cancelled')),
  decision_json TEXT NOT NULL,
  content_key TEXT NOT NULL,
  capacity_snapshot REAL NOT NULL,
  revision INTEGER NOT NULL DEFAULT 1,
  supersedes_id INTEGER REFERENCES transfer_orders(id),
  note TEXT,
  created_by TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  executed_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_transfer_orders_pending ON transfer_orders(target_line_id) WHERE state='pending';
"""

SELECT = """SELECT o.*, s.code AS source_code, s.name AS source_name, t.code AS target_code, t.name AS target_name
            FROM transfer_orders o JOIN assets s ON s.id=o.source_line_id JOIN assets t ON t.id=o.target_line_id"""


class TransferStore:
    """SQLite 存档：转供单的写入、状态流转与查询。"""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def insert(self, *, source_line_id: int, target_line_id: int, load_mw: float, users: list[dict],
               state: str, decision: dict, key: str, capacity_snapshot: float, revision: int,
               supersedes_id: int | None, actor: str) -> int:
        try:
            cur = self.conn.execute(
                """INSERT INTO transfer_orders(source_line_id,target_line_id,load_mw,important_users_json,state,decision_json,
                                             content_key,capacity_snapshot,revision,supersedes_id,created_by,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (source_line_id, target_line_id, load_mw, _j(users), state, _j(decision), key,
                 capacity_snapshot, revision, supersedes_id, actor, _now(), _now()))
        except sqlite3.IntegrityError as exc:
            raise TransferError(409, "该目标线路已存在待执行单") from exc
        return int(cur.lastrowid)

    def mark(self, order_id: int, state: str, *, note: str | None = None) -> None:
        self.conn.execute("UPDATE transfer_orders SET state=?, note=COALESCE(?,note), updated_at=? WHERE id=?",
                          (state, note, _now(), order_id))

    def mark_executed(self, order_id: int) -> None:
        self.conn.execute("UPDATE transfer_orders SET state='executed', updated_at=?, executed_at=? WHERE id=?",
                          (_now(), _now(), order_id))

    def get(self, order_id: int) -> sqlite3.Row:
        row = self.conn.execute(SELECT + " WHERE o.id=?", (order_id,)).fetchone()
        if not row:
            raise TransferError(404, "转供单不存在")
        return row

    def pending_for(self, target_line_id: int) -> sqlite3.Row | None:
        return self.conn.execute(SELECT + " WHERE o.target_line_id=? AND o.state='pending'", (target_line_id,)).fetchone()

    def committed_load(self, target_line_id: int) -> float:
        row = self.conn.execute("SELECT COALESCE(SUM(load_mw),0) FROM transfer_orders WHERE target_line_id=? AND state IN ('pending','executed')",
                                (target_line_id,)).fetchone()
        return round(float(row[0]), 3)

    def list_orders(self, limit: int = 50) -> list[sqlite3.Row]:
        return self.conn.execute(SELECT + " ORDER BY o.id DESC LIMIT ?", (limit,)).fetchall()


class TransferService:
    """转供单业务：登记去重、判定落档、许可失效重算、执行取消与容量视图。"""

    def __init__(self, store):  # store 为 app.Store，共用同一 SQLite 连接与审计
        self.store, self.conn = store, store.conn
        self.archive = TransferStore(store.conn)

    @staticmethod
    def _actor(actor: str | None, role: str | None, allowed: set[str]) -> str:
        if not actor:
            raise TransferError(401, "缺少身份")
        if role not in allowed:
            raise TransferError(403, "角色无权执行此操作")
        return actor

    def _line(self, asset_id: object, label: str) -> sqlite3.Row:
        try:
            identity = int(asset_id)
        except (TypeError, ValueError) as exc:
            raise TransferError(400, f"{label}编号不合法") from exc
        row = self.conn.execute("SELECT * FROM assets WHERE id=?", (identity,)).fetchone()
        if not row:
            raise TransferError(404, f"{label}不存在")
        if row["asset_type"] != "line":
            raise TransferError(400, f"{label}必须是线路资产")
        return row

    def _required_users(self, source_line_id: int) -> list[dict]:
        rows = self.conn.execute("SELECT id,name,priority FROM facilities WHERE asset_id=? AND priority=1 ORDER BY id", (source_line_id,))
        return [dict(row) for row in rows]

    def _recovery_reserve(self, line_code: str) -> float:
        total = 0.0
        for row in self.conn.execute("SELECT steps_json FROM plans WHERE state='active'"):
            total += sum(float(step["required_mw"]) for step in json.loads(row["steps_json"]) if step["asset"] == line_code)
        return round(total, 3)

    def submit(self, actor: str | None, role: str | None, source_line_id: object, target_line_id: object,
               load_mw: object, important_users: object) -> dict:
        actor = self._actor(actor, role, {"dispatcher", "field"})
        source, target = self._line(source_line_id, "源线路"), self._line(target_line_id, "目标线路")
        if source["id"] == target["id"]:
            raise TransferError(400, "源线路与目标线路不能相同")
        try:
            load = float(load_mw)
        except (TypeError, ValueError) as exc:
            raise TransferError(400, "转供负荷不合法") from exc
        if load <= 0:
            raise TransferError(400, "转供负荷必须为正数")
        users = normalize_users(important_users)
        key = content_key(source["id"], load, users)
        capacity = float(target["capacity_mw"])
        with self.conn:
            pending = self.archive.pending_for(target["id"])
            if pending and pending["content_key"] == key and float(pending["capacity_snapshot"]) == capacity:
                return self._dict(pending, reused=True)  # 重复提交沿用首次结果
            revision, supersedes_id = 1, None
            if pending:  # 转供内容或线路容量变了：原许可失效并重算
                self.archive.mark(pending["id"], "invalidated", note="转供内容或线路容量变更，原许可失效")
                self.store.audit(actor, "transfer.invalidate", "transfer_order", pending["id"], {"reason": "转供内容或线路容量变更"})
                revision, supersedes_id = int(pending["revision"]) + 1, pending["id"]
            order_id = self._judge_and_insert(source=source, target=target, load_mw=load, users=users, key=key,
                                              actor=actor, revision=revision, supersedes_id=supersedes_id, action="transfer.submit")
            return self._dict(self.archive.get(order_id), reused=False)

    def execute(self, actor: str | None, role: str | None, order_id: int) -> dict:
        actor = self._actor(actor, role, {"dispatcher"})
        order = self.archive.get(order_id)
        if order["state"] != "pending":
            raise TransferError(409, "只有待执行单可以执行")
        with self.conn:
            self.archive.mark_executed(order["id"])
            self.store.audit(actor, "transfer.execute", "transfer_order", order["id"],
                             {"target": order["target_code"], "load_mw": order["load_mw"]})
        return self._dict(self.archive.get(order["id"]))

    def cancel(self, actor: str | None, role: str | None, order_id: int, reason: str = "") -> dict:
        actor = self._actor(actor, role, {"dispatcher"})
        order = self.archive.get(order_id)
        if order["state"] != "pending":
            raise TransferError(409, "只有待执行单可以取消")
        with self.conn:
            self.archive.mark(order["id"], "cancelled", note=reason or "班组取消")
            self.store.audit(actor, "transfer.cancel", "transfer_order", order["id"], {"reason": reason})
        return self._dict(self.archive.get(order["id"]))

    def recalculate_line(self, line_id: int, actor: str) -> list[dict]:
        """线路容量变更后：该线路待执行许可失效，按原登记内容重新判定。"""
        with self.conn:
            pending = self.archive.pending_for(line_id)
            if not pending:
                return []
            self.archive.mark(pending["id"], "invalidated", note="线路容量变更，原许可失效并重算")
            self.store.audit(actor, "transfer.invalidate", "transfer_order", pending["id"], {"reason": "线路容量变更"})
            source, target = self._line(pending["source_line_id"], "源线路"), self._line(line_id, "目标线路")
            order_id = self._judge_and_insert(source=source, target=target, load_mw=float(pending["load_mw"]),
                                              users=json.loads(pending["important_users_json"]), key=pending["content_key"],
                                              actor=actor, revision=int(pending["revision"]) + 1,
                                              supersedes_id=pending["id"], action="transfer.recalculate")
            return [self._dict(self.archive.get(order_id))]

    def overview(self) -> dict:
        """各线路剩余容量与待执行单，供界面展示。"""
        lines = []
        for row in self.conn.execute("SELECT * FROM assets WHERE asset_type='line' ORDER BY id"):
            committed, reserve = self.archive.committed_load(row["id"]), self._recovery_reserve(row["code"])
            pending = self.archive.pending_for(row["id"])
            lines.append({"line_id": row["id"], "code": row["code"], "name": row["name"], "region": row["region"],
                          "capacity_mw": row["capacity_mw"], "committed_mw": committed, "reserve_mw": reserve,
                          "remaining_mw": round(float(row["capacity_mw"]) - committed - reserve, 3),
                          "pending_order": self._dict(pending) if pending else None})
        return {"lines": lines, "pending_orders": [line["pending_order"] for line in lines if line["pending_order"]]}

    def list_orders(self, limit: int = 50) -> list[dict]:
        return [self._dict(row) for row in self.archive.list_orders(limit)]

    def _judge_and_insert(self, *, source: sqlite3.Row, target: sqlite3.Row, load_mw: float, users: list[dict],
                          key: str, actor: str, revision: int, supersedes_id: int | None, action: str) -> int:
        capacity = float(target["capacity_mw"])
        decision = evaluate_transfer(capacity_mw=capacity, committed_mw=self.archive.committed_load(target["id"]),
                                     reserve_mw=self._recovery_reserve(target["code"]), load_mw=load_mw,
                                     required_users=self._required_users(source["id"]), registered_users=users)
        state = "pending" if decision["approved"] else "rejected"
        order_id = self.archive.insert(source_line_id=source["id"], target_line_id=target["id"], load_mw=load_mw, users=users,
                                       state=state, decision=decision, key=key, capacity_snapshot=capacity,
                                       revision=revision, supersedes_id=supersedes_id, actor=actor)
        self.store.audit(actor, action, "transfer_order", order_id,
                         {"target": target["code"], "state": state, "revision": revision, "reasons": decision["reasons"]})
        return order_id

    @staticmethod
    def _dict(row: sqlite3.Row, *, reused: bool = False) -> dict:
        return {"id": row["id"], "source_line_id": row["source_line_id"], "target_line_id": row["target_line_id"],
                "source_code": row["source_code"], "source_name": row["source_name"],
                "target_code": row["target_code"], "target_name": row["target_name"],
                "load_mw": row["load_mw"], "important_users": json.loads(row["important_users_json"]),
                "state": row["state"], "decision": json.loads(row["decision_json"]),
                "capacity_snapshot": row["capacity_snapshot"], "revision": row["revision"],
                "supersedes_id": row["supersedes_id"], "note": row["note"], "reused": reused,
                "created_by": row["created_by"], "created_at": row["created_at"],
                "updated_at": row["updated_at"], "executed_at": row["executed_at"]}
