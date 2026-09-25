"""转供判定：容量余量与重要用户优先级的纯函数校核，不接触数据库。

判定规则（存档见 transfer_store.py，界面见 static/transfer.html）：
1. 扣除原有承接及恢复预留后仍有余量：load <= capacity - committed - reserve；
2. 重要用户优先级达标：源线路上所有一级重要用户（医院等）必须纳入转供单。
"""
from __future__ import annotations

import json

PRIORITIES = (1, 2, 3)  # 1 为最高优先级（医院等重要负荷）


class TransferError(Exception):
    """转供业务错误，status 为 HTTP 状态码。"""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


def normalize_users(users: object) -> list[dict]:
    """校验并规范化转供单登记的重要用户，按优先级、名称排序并去重。"""
    if users is None:
        return []
    if not isinstance(users, list):
        raise TransferError(400, "重要用户必须是列表")
    normalized, seen = [], set()
    for raw in users:
        if not isinstance(raw, dict):
            raise TransferError(400, "重要用户条目不合法")
        try:
            name, priority = str(raw["name"]).strip(), int(raw["priority"])
        except (KeyError, TypeError, ValueError) as exc:
            raise TransferError(400, "重要用户需包含名称和优先级") from exc
        facility_id = raw.get("facility_id")
        if not name or priority not in PRIORITIES:
            raise TransferError(400, "重要用户名称或优先级不合法")
        key = (facility_id, name)
        if key in seen:
            continue
        seen.add(key)
        normalized.append({"facility_id": int(facility_id) if facility_id else None, "name": name, "priority": priority})
    return sorted(normalized, key=lambda u: (u["priority"], u["name"]))


def content_key(source_line_id: int, load_mw: float, users: list[dict]) -> str:
    """转供内容指纹：源线路、负荷和重要用户一致才视为同一笔转供。"""
    payload = {"source": int(source_line_id), "load": round(float(load_mw), 3),
               "users": [[u["facility_id"] or 0, u["name"], u["priority"]] for u in users]}
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def missing_critical_users(required_users: list[dict], registered_users: list[dict]) -> list[str]:
    """源线路一级重要用户中未纳入转供单的名称列表。"""
    missing = []
    for req in required_users:
        covered = any((u["facility_id"] and u["facility_id"] == req["id"]) or u["name"] == req["name"] for u in registered_users)
        if not covered:
            missing.append(req["name"])
    return missing


def evaluate_transfer(*, capacity_mw: float, committed_mw: float, reserve_mw: float, load_mw: float,
                      required_users: list[dict], registered_users: list[dict]) -> dict:
    """判定是否准许执行，返回可存档的判定结果。"""
    remaining = round(float(capacity_mw) - float(committed_mw) - float(reserve_mw), 3)
    missing = missing_critical_users(required_users, registered_users)
    reasons = []
    if float(load_mw) > remaining:
        reasons.append(f"扣除原有承接{committed_mw}MW及恢复预留{reserve_mw}MW后剩余{remaining}MW，不足转供负荷{load_mw}MW")
    if missing:
        reasons.append("重要用户优先级不达标：一级重要用户 " + "、".join(missing) + " 未纳入转供单")
    return {"approved": not reasons, "reasons": reasons, "capacity_mw": float(capacity_mw),
            "committed_mw": float(committed_mw), "reserve_mw": float(reserve_mw),
            "remaining_mw": remaining, "missing_critical_users": missing}
