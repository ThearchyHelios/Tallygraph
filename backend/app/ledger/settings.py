"""口径账本运行与治理设置：默认项、范围校验与持久化读取（供 ledger 领域层与设置接口共用）。"""
from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Setting

LEDGER_GROUP = "ledger"
LEDGER_DEFAULTS: dict[str, Any] = {
    "auto_issue": True,
    "issue_daily_limit": 20,
    "match_timeout_s": 8,
    "recall_limit": 50,
}
LEDGER_SETTING_LABEL = {
    "auto_issue": "自动按口径正式出具",
    "issue_daily_limit": "按口径正式出具每日上限",
    "match_timeout_s": "口径比对时限（秒）",
    "recall_limit": "口径升版后每次最多重算几组",
}
LEDGER_RECALL_MAX = 1000
LEDGER_TIMEOUT_RANGE = (1, 60)
LEDGER_DAILY_LIMIT_MAX = 100_000


def _daily_limit_ok(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= LEDGER_DAILY_LIMIT_MAX


def _timeout_ok(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) \
        and LEDGER_TIMEOUT_RANGE[0] <= value <= LEDGER_TIMEOUT_RANGE[1]


def _recall_limit_ok(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= LEDGER_RECALL_MAX


def ledger_problem(value: Any) -> str | None:
    """设置页写进来的 ledger 这一组有什么问题，没问题返回 None。"""
    if not isinstance(value, dict):
        return f"「口径账本」设置格式有误，需要是一个对象，当前为「{value}」"
    for key, item in value.items():
        if key not in LEDGER_DEFAULTS:
            return (f"「口径账本」设置中不支持「{key}」，可设置的项："
                    + "、".join(f"「{LEDGER_SETTING_LABEL[k]}」" for k in LEDGER_DEFAULTS))
        if key == "auto_issue" and not isinstance(item, bool):
            return f"「{LEDGER_SETTING_LABEL[key]}」只能是开或关，当前为「{item}」"
        if key == "issue_daily_limit" and not _daily_limit_ok(item):
            return (f"「{LEDGER_SETTING_LABEL[key]}」需要是 0 到 {LEDGER_DAILY_LIMIT_MAX:,} 之间的整数（0 表示不再按口径出具），"
                    f"当前为「{item}」")
        if key == "recall_limit" and not _recall_limit_ok(item):
            return (f"「{LEDGER_SETTING_LABEL[key]}」需要是 0 到 {LEDGER_RECALL_MAX:,} 之间的整数（0 表示发布后不自动重算），"
                    f"当前为「{item}」")
        if key == "match_timeout_s" and not _timeout_ok(item):
            return (f"「{LEDGER_SETTING_LABEL[key]}」需要是 {LEDGER_TIMEOUT_RANGE[0]} 到 {LEDGER_TIMEOUT_RANGE[1]} 之间的数，"
                    f"当前为「{item}」")
    return None


def ledger_values(stored: Any) -> dict[str, Any]:
    """库里存的（或刚写进来的）ledger 一组 → 补齐默认；存坏了的项按默认（自动出具的开关存坏了按关，宁可少出具）。"""
    stored = stored if isinstance(stored, dict) else {}
    auto, limit, timeout = stored.get("auto_issue"), stored.get("issue_daily_limit"), stored.get("match_timeout_s")
    recall = stored.get("recall_limit")
    return {
        "auto_issue": auto if isinstance(auto, bool) else (LEDGER_DEFAULTS["auto_issue"] if auto is None else False),
        "issue_daily_limit": limit if _daily_limit_ok(limit) else LEDGER_DEFAULTS["issue_daily_limit"],
        "match_timeout_s": timeout if _timeout_ok(timeout) else LEDGER_DEFAULTS["match_timeout_s"],
        "recall_limit": recall if _recall_limit_ok(recall) else LEDGER_DEFAULTS["recall_limit"],
    }


async def ledger_settings(session: AsyncSession) -> dict[str, Any]:
    """设置里的 ledger 一组，没存过的补默认。按口径出具、比对从这里读。"""
    row = await session.get(Setting, LEDGER_GROUP)
    return ledger_values(row.value if row else None)


__all__ = [
    "LEDGER_DAILY_LIMIT_MAX",
    "LEDGER_DEFAULTS",
    "LEDGER_GROUP",
    "LEDGER_RECALL_MAX",
    "LEDGER_SETTING_LABEL",
    "LEDGER_TIMEOUT_RANGE",
    "ledger_problem",
    "ledger_settings",
    "ledger_values",
]
