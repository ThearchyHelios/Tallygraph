"""最小审阅（K05）：对一条验证记录的 confirmed / rejected 决定，**只追加**，绑定认证主体、确认范围和所审依据。

**为什么要有它。** 「样例对得上」和「这条口径可以正式用」是两件事：前者是机器/夹具能判的（validation 里的 matched），
后者要一个**认证主体**看着定义和依据说一句。没有这一层，自动匹配就成了变相的批准；有了它，界面上的「验证通过」才有
明确的责任人。

**模型不能写审批人。** 这里没有从请求体或模型回复取 actor 的路径：主体一律来自服务端身份（app.api.deps.current_principal），
连「关登录时自报的署名」和「认证过的用户名」也分开记在一个 verified 标志里。决定的内容只有 confirmed / rejected
（不建多级审批，也不做「样例匹配自动变成 confirmed」）。

**审的是依据，不是这张卡现在的样子。** 每条决定都记下**所审记录**的定义指纹、绑定指纹、取数语义指纹和方法版本
（basis）。定义或绑定改过之后，旧决定仍在（历史可追），但不算覆盖新版本（covers_current=False）——所以「删掉一段
没验证过的业务描述」不会让旧的审阅自动升级成对新定义的批准（E15/R07）。三样指纹里**任一侧缺失**（旧行没有、
算不出来）同样算不覆盖：缺证据不得升级。

**确认范围要说清。** metrics 不写 = 确认这份记录的全部指标；写一个子集 = 只确认那几条；写空列表直接拒
（空范围不是「确认全部」，是「什么也没确认」）。只确认一部分时，草稿摘要不会给卡级 verified——它把还没确认的
指标列在 `unconfirmed` 里（卡级通过要求确认范围盖住这份记录的全部指标）。
"""
from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import Any

logger = logging.getLogger(__name__)

CONFIRMED = "confirmed"
REJECTED = "rejected"
DECISIONS = (CONFIRMED, REJECTED)


class ReviewRefused(ValueError):
    """这次审阅不合规（接口回 422）：message 给人看，code 是机器码。"""

    def __init__(self, message: str, *, code: str = "review_invalid") -> None:
        super().__init__(message)
        self.message = message
        self.code = code


def basis_of(record: Any) -> dict[str, Any]:
    """所审记录的依据版本：审的是这一份依据，不是「这张卡现在长什么样」。"""
    return {"version": int(getattr(record, "version", 0) or 0),
            "definition_hash": str(getattr(record, "definition_hash", "") or ""),
            "binding_fingerprints": dict(getattr(record, "binding_fingerprints", None) or {}),
            "semantic_hash": str(getattr(record, "semantic_hash", "") or ""),
            "method_version": int(getattr(record, "method_version", 0) or 0)}


def scope_of(record: Any, wanted: Sequence[str] | None = None) -> list[str]:
    """确认范围：这份记录覆盖的指标；给了 wanted 就按那个子集，但**不能是空的、也不能超出这份记录**。

    wanted 不给 = 确认这份记录覆盖的全部指标。写着一个空列表不算「确认全部」——那是「什么也没确认」，
    卡级通过要求确认范围盖住记录的全部指标，所以空范围在这里直接拒（要确认全部就别写这一项）。
    """
    covered = [str(mid) for mid in getattr(record, "metric_ids", None) or []]
    if wanted is None:
        return covered
    asked = [str(mid) for mid in wanted]
    if not asked:
        raise ReviewRefused("确认范围不能是空的：要确认这份记录全部指标就别写 metrics 这一项；"
                            "只确认其中几条就写那几条", code="review_scope")
    unknown = [mid for mid in asked if mid not in covered]
    if unknown:
        raise ReviewRefused(f"这份验证记录没有覆盖这些指标：{'、'.join(unknown)}", code="review_scope")
    return list(dict.fromkeys(asked))


def check(decision: Any) -> str:
    """决定只有 confirmed / rejected；别的写法一律拒（不给「默认确认」这种方便）。"""
    text = str(decision or "").strip()
    if text not in DECISIONS:
        raise ReviewRefused(f"审阅决定只能是 confirmed（确认）或 rejected（驳回），现在是「{text or '（空）'}」",
                            code="review_decision")
    return text


async def decide(session: Any, *, draft_id: str, record: Any, decision: str, actor: str | None,
                 verified: bool = False, scope: Sequence[str] | None = None, note: str | None = None
                 ) -> Any:
    """追加一条审阅决定，绑定认证主体、确认范围和所审记录的依据。不提交：和这次请求同一个事务。"""
    from app.db.models import CaliberReviewDecision

    text = check(decision)
    row = CaliberReviewDecision(
        draft_id=draft_id, record_id=str(getattr(record, "id", "")),
        workflow_id=str(getattr(record, "workflow_id", "") or "") or None, decision=text, actor=actor,
        verified=bool(verified), scope=scope_of(record, scope), basis=basis_of(record),
        note=(str(note).strip() or None) if note is not None else None)
    session.add(row)
    await session.flush()
    return row


def covers_current(decision: Any, *, current: Mapping[str, Any] | None) -> bool:
    """这条决定还算不算覆盖**当前**定义和绑定：按它记下的 basis 和当前依据比，**任一侧缺失都算不覆盖**。

    定义指纹、取数语义指纹少一个（旧行没有、算不出来）时一律 False：缺证据不得自动升级成「已确认当前版本」。
    """
    if not isinstance(current, Mapping):
        return False
    basis = getattr(decision, "basis", None) or {}
    if not str(basis.get("definition_hash") or "") or not str(getattr(decision, "workflow_id", "") or ""):
        return False
    if str(getattr(decision, "workflow_id", "")) != str(current.get("workflow_id") or ""):
        return False
    if str(basis.get("definition_hash")) != str(current.get("definition_hash") or ""):
        return False
    if dict(basis.get("binding_fingerprints") or {}) != dict(current.get("binding_fingerprints") or {}):
        return False
    recorded, live = str(basis.get("semantic_hash") or ""), str(current.get("semantic_hash") or "")
    if not recorded or not live or recorded != live:
        return False
    return True


def as_dict(row: Any, *, current: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """一条审阅决定的对外形状。covers_current 说明它还算不算覆盖当前定义/绑定。"""
    return {"id": row.id, "draft_id": row.draft_id, "record_id": row.record_id,
            "workflow_id": row.workflow_id, "decision": row.decision, "actor": row.actor,
            "verified": bool(row.verified), "scope": [str(mid) for mid in row.scope or []],
            "basis": dict(row.basis or {}), "note": row.note,
            "created_at": row.created_at.isoformat() if getattr(row, "created_at", None) else None,
            "covers_current": covers_current(row, current=current) if isinstance(current, Mapping) else None}


async def latest_by_record(session: Any, record_ids: Sequence[str], *, current: Mapping[str, Any] | None = None
                           ) -> dict[str, dict[str, Any]]:
    """这些验证记录上最新的审阅决定：record_id → 决定（对外形状）。没有决定的记录不出现在结果里。"""
    from sqlalchemy import select

    from app.db.models import CaliberReviewDecision

    wanted = [str(rid) for rid in record_ids if rid]
    if not wanted:
        return {}
    rows = (await session.execute(
        select(CaliberReviewDecision).where(CaliberReviewDecision.record_id.in_(wanted))
        .order_by(CaliberReviewDecision.created_at.asc(), CaliberReviewDecision.id.asc()))).scalars()
    out: dict[str, dict[str, Any]] = {}
    for row in rows:  # 按时间升序扫，后写的覆盖先写的：显示用最新那一条（旧的一条都没删）
        out[str(row.record_id)] = as_dict(row, current=current)
    return out


__all__ = ["CONFIRMED", "DECISIONS", "REJECTED", "ReviewRefused", "as_dict", "basis_of", "check",
           "covers_current", "decide", "latest_by_record", "scope_of"]
