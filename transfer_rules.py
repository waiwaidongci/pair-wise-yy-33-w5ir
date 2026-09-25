"""转供判定：备用线路容量余量与重要用户优先级校核。

纯函数模块，不接触数据库与 HTTP，便于单独测试与复用。
"""
from __future__ import annotations


def remaining_capacity(capacity_mw: float, committed_mw: float, reserve_mw: float) -> float:
    """扣除原有承接与恢复预留后的剩余容量。"""
    return round(float(capacity_mw) - float(committed_mw) - float(reserve_mw), 6)


def evaluate_transfer(*, capacity_mw: float, committed_mw: float, reserve_mw: float,
                      requested_load_mw: float, required_user_ids: set[int],
                      carried_users: list[dict]) -> dict:
    """判定一笔转供单是否准许执行。

    - 容量：扣除原有承接(committed)与恢复预留(reserve)后仍需容得下申请负荷；
    - 重要用户：源线路一级重要用户（如医院）必须全部纳入转供，
      且所带重要用户的保电需求之和不得超过转供负荷。
    """
    reasons: list[str] = []
    remaining = remaining_capacity(capacity_mw, committed_mw, reserve_mw)
    if requested_load_mw <= 0:
        reasons.append("转供负荷必须为正数")
    if requested_load_mw > remaining:
        reasons.append(
            f"容量不足：目标线路剩余 {remaining:.2f}MW（已扣原有承接 {committed_mw:.2f}MW、"
            f"恢复预留 {reserve_mw:.2f}MW），申请 {requested_load_mw:.2f}MW")
    carried_ids = {int(u["id"]) for u in carried_users}
    missing = sorted(set(required_user_ids) - carried_ids)
    if missing:
        reasons.append(f"一级重要用户未全部纳入转供：{missing}")
    backup_mw = round(sum(float(u["backup_power_mw"]) for u in carried_users), 6)
    if backup_mw > requested_load_mw:
        reasons.append(f"重要用户保电需求 {backup_mw:.2f}MW 超过转供负荷 {requested_load_mw:.2f}MW")
    return {"approved": not reasons, "remaining_mw": remaining,
            "committed_mw": round(committed_mw, 6), "reserve_mw": round(reserve_mw, 6),
            "requested_load_mw": round(requested_load_mw, 6), "backup_required_mw": backup_mw,
            "missing_critical_users": missing, "reasons": reasons}
