"""增量重导入的接口（K11）：建一次导入、看工作项和冲突、接受选定字段、拒绝、人工对齐、显式恢复。

**认证主体只从服务端身份取。** 发起人、接受人一律 `current_principal()`（开登录时是认证用户名，关登录时是自报署名）；
请求体里写 actor 一概不看。这几个接口不登记进 `auth/keys.KEY_ROUTES`（访问密钥不开放绕过），路由挂在搭建者那一道门上
（main.py 的 `_BUILDER`，和 /api/ledger 下其余写接口一致）。

**R 只是候选，不改 L。** 建一次导入只在后台抽候选、算逐字段三方；真正落到图上的是「接受选定字段」——它走现有版本 /
CAS / 操作语义（另见 app/ledger/reimport.py 的 accept_fields），并按被接受的那几格推进字段基线 B。冲突没被接受之前，
L 和 B 一个字节都不动。

接口形状：
- POST /api/ledger/drafts/{draft_id}/imports          建一次导入（可带 request_id 幂等），后台抽候选
- GET  /api/ledger/drafts/{draft_id}/imports          这次草稿的导入列表
- GET  /api/ledger/imports/{run_id}                   一次导入的工作项（含逐字段三方、逐格 acceptable、align_key）、冲突
- POST /api/ledger/imports/{run_id}/items/{item_id}/align   人工把一条工作项对到图上某条指标
- POST /api/ledger/imports/{run_id}/accept            接受选定字段（定位按 item_id；落到 L + 按字段推进 B，原子）
- POST /api/ledger/imports/{run_id}/reject            拒绝选定字段 / 整条（保持 L 不动）
- POST /api/ledger/imports/{run_id}/refresh           按现在的 L / B 重算三方（import_stale_l 之后的恢复动作）
- POST /api/ledger/imports/{run_id}/resume            显式恢复未完成的工作项（按持久对齐键认项，不按位置）
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.coded import CodedHTTPException
from app.db.base import get_session
from app.db.models import CaliberDraft, CaliberImportItem, CaliberImportRun, Workflow
from app.ledger import fieldbaseline, reimport

router = APIRouter(prefix="/api/ledger", tags=["ledger"])

#: 接口自己的机器码（reimport 里的那几个之外）
IMPORT_DRAFT_UNAVAILABLE = "import_draft_unavailable"
IMPORT_CARD_GONE = "card_not_found"


def _refused(exc: reimport.ImportRefused) -> CodedHTTPException:
    return CodedHTTPException(exc.status_code, exc.message, exc.code, extra=exc.extra or None)


async def _draft_or_404(session: AsyncSession, draft_id: str) -> CaliberDraft:
    draft = await session.get(CaliberDraft, draft_id)
    if draft is None:
        raise HTTPException(404, "口径草稿不存在，可能已被删除")
    return draft


async def _run_or_404(session: AsyncSession, run_id: str) -> CaliberImportRun:
    run = await session.get(CaliberImportRun, run_id)
    if run is None:
        raise HTTPException(404, "这次导入不存在，可能已被删除")
    return run


async def _items(session: AsyncSession, run_id: str) -> list[CaliberImportItem]:
    return list((await session.execute(select(CaliberImportItem).where(CaliberImportItem.run_id == run_id)
                                       .order_by(CaliberImportItem.seq))).scalars())


async def _write_target(session: AsyncSession, run: CaliberImportRun) -> tuple[CaliberDraft, Workflow, str]:
    """这次导入要落到的（草稿、工作流、口径卡）：卡必须是可写的源头口径卡，认不出就拒。"""
    from app.api.ledger import draft_card

    draft = await _draft_or_404(session, run.draft_id)
    workflow = await session.get(Workflow, draft.workflow_id) if draft.workflow_id else None
    if workflow is None or not workflow.graph:
        raise CodedHTTPException(409, "这份草稿还没接管到工作流上（没有可改的图）：导入落不到图上，请先起草或接上一次运行",
                                 IMPORT_DRAFT_UNAVAILABLE)
    card_id, _cards = draft_card(workflow.graph)
    if not card_id:
        raise CodedHTTPException(409, "当前画布认不出唯一的口径卡：请在目标卡上明确要改哪一张", IMPORT_CARD_GONE)
    return draft, workflow, str(card_id)


def _field_view(path: str, cell: Any) -> dict[str, Any]:
    cell = cell if isinstance(cell, dict) else {}
    state = str(cell.get("state") or "missing")
    acceptable = bool(cell.get("acceptable")) if "acceptable" in cell else state in reimport.ACCEPT_STATES
    return {"path": path, "state": state, "b": cell.get("b"), "l": cell.get("l"), "r": cell.get("r"),
            "b_present": bool(cell.get("b_present")), "l_present": bool(cell.get("l_present")),
            "r_present": bool(cell.get("r_present")), "decided": cell.get("decided"),
            "acceptable": acceptable}


def _item_view(item: CaliberImportItem) -> dict[str, Any]:
    fields = item.fields if isinstance(item.fields, dict) else {}
    order = list(fieldbaseline.field_paths())
    ordered = [path for path in order if path in fields] + [path for path in fields if path not in order]
    failed = item.status == "failed"
    return {
        "item_id": item.id, "seq": item.seq, "metric_id": item.metric_id or "",
        "candidate_id": item.candidate_id or "", "align_key": item.align_key or "",
        "name": item.name or "", "status": item.status, "align": dict(item.align or {}),
        "error": item.error or "", "failed": failed,
        "accepted": list(item.accepted or []), "rejected": list(item.rejected or []),
        "applied_revision": item.applied_revision,
        "fields": [_field_view(path, fields.get(path)) for path in ordered],
    }


def _run_view(run: CaliberImportRun, items: list[CaliberImportItem]) -> dict[str, Any]:
    views = [_item_view(item) for item in items]
    conflicts: list[dict[str, Any]] = []
    for view in views:
        for cell in view["fields"]:
            if cell["state"] in ("conflict", "manual", "missing"):
                conflicts.append({"metric_id": view["metric_id"], "item_id": view["item_id"],
                                  "seq": view["seq"], "name": view["name"],
                                  "path": cell["path"], "state": cell["state"],
                                  "b": cell["b"], "l": cell["l"], "r": cell["r"]})
    return {
        "run_id": run.id, "draft_id": run.draft_id, "workflow_id": run.workflow_id or "", "status": run.status,
        "error": run.error or "", "actor": run.actor or "", "request_id": run.request_id or "",
        "source_snapshot": dict(run.source_snapshot or {}), "visible_chunk_ids": list(run.visible_chunk_ids or []),
        "prompt_version": run.prompt_version, "parse_version": run.parse_version, "model_id": run.model_id or "",
        "items_total": run.items_total, "items_decided": run.items_decided,
        "created_at": run.created_at.isoformat() if run.created_at else "",
        "items": views, "conflicts": conflicts,
    }


class ImportCreateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: 客户端幂等键：同一份草稿 + 同一个 request_id 只建一次 run（重放交回上一次那一次）
    request_id: str | None = Field(default=None, max_length=64)


class ImportAcceptItemIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: 首选：工作项的稳定主键。旧调用只给 metric_id 时要求它在这条导入里唯一对得上一条工作项
    item_id: str | None = Field(default=None, max_length=32)
    metric_id: str | None = Field(default=None, max_length=64)
    #: 要接受的字段路径；auto=true 时不用给（**只**收还没决定的「无冲突更新 + 新增候选」）
    fields: list[str] = Field(default_factory=list)
    auto: bool = False


class ImportAcceptIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[ImportAcceptItemIn] = Field(min_length=1)
    expected_revision: int | None = None
    request_id: str | None = Field(default=None, max_length=64)


class ImportRejectItemIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item_id: str | None = Field(default=None, max_length=32)
    metric_id: str | None = Field(default=None, max_length=64)
    seq: int | None = None
    #: 空 = 整条拒绝（还没决定的格子都拒）；没对齐上的工作项优先按 item_id 指
    fields: list[str] = Field(default_factory=list)


class ImportRejectIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[ImportRejectItemIn] = Field(min_length=1)
    note: str | None = Field(default=None, max_length=500)


class ImportAlignIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: 对齐到图上哪条指标；空串 = 取消对齐（当作新指标）
    metric_id: str = Field(default="", max_length=64)


@router.post("/drafts/{draft_id}/imports", status_code=201)
async def create_import(draft_id: str, payload: ImportCreateIn, background: BackgroundTasks,
                        session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """建一次增量导入：后台按这份草稿的来源文档重新抽候选 R，和当前图 L、字段基线 B 逐字段比对。不改 L、不改 B。

    带 request_id 的：同一份草稿 + 同一个 request_id 已经建过就交回那一次（200 的语义，仍回 201 由前端按 run_id 认），
    不重复抽、不重复建工作项。
    """
    from app.api.deps import current_principal, ensure_role

    ensure_role("builder")
    actor = current_principal().username or ""
    draft = await _draft_or_404(session, draft_id)
    existing = await _by_request_id(session, draft_id, payload.request_id)
    if existing is not None:
        return _run_view(existing, await _items(session, existing.id))
    run = CaliberImportRun(draft_id=draft.id, workflow_id=draft.workflow_id, status="queued", actor=actor,
                           request_id=payload.request_id or None, checkpoint={"stage": "queued"})
    session.add(run)
    try:
        await session.flush()
        await session.commit()
    except IntegrityError:
        # 两路真并发带同一个 request_id：后到的那一路撞唯一键，重查一次交回赢的那一次（不重复抽、不重复建工作项）
        await session.rollback()
        existing = await _by_request_id(session, draft_id, payload.request_id)
        if existing is None:
            raise
        return _run_view(existing, await _items(session, existing.id))
    background.add_task(reimport.start, run.id)
    return _run_view(run, [])


async def _by_request_id(session: AsyncSession, draft_id: str, request_id: str | None) -> CaliberImportRun | None:
    if not request_id:
        return None
    return (await session.execute(select(CaliberImportRun).where(
        CaliberImportRun.draft_id == draft_id, CaliberImportRun.request_id == request_id,
        CaliberImportRun.status != "cancelled").order_by(CaliberImportRun.created_at.desc()))).scalars().first()


@router.get("/drafts/{draft_id}/imports")
async def list_imports(draft_id: str, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    from app.api.deps import ensure_role

    ensure_role("builder")
    await _draft_or_404(session, draft_id)
    rows = (await session.execute(select(CaliberImportRun).where(CaliberImportRun.draft_id == draft_id)
                                 .order_by(CaliberImportRun.created_at.desc()))).scalars()
    return {"runs": [_run_view(run, await _items(session, run.id)) for run in rows]}


@router.get("/imports/{run_id}")
async def get_import(run_id: str, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    from app.api.deps import ensure_role

    ensure_role("builder")
    run = await _run_or_404(session, run_id)
    return _run_view(run, await _items(session, run_id))


@router.post("/imports/{run_id}/items/{item_id}/align")
async def align_import_item(run_id: str, item_id: str, payload: ImportAlignIn,
                            session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """人工把一条工作项对到图上某条指标上（或取消对齐）。「baseline_unknown 必须人工对齐」和「同名不同范围」都走这里。

    重新对齐会清掉旧的决定（换了对哪一条，决定就换了对象），只重算三方、不改 L、不改 B。
    """
    from app.api.deps import ensure_role

    ensure_role("builder")
    run = await _run_or_404(session, run_id)
    item = await session.get(CaliberImportItem, item_id)
    if item is None or item.run_id != run.id:
        raise CodedHTTPException(404, "这条工作项不存在", reimport.IMPORT_ITEM_UNKNOWN)
    draft, workflow, _card_id = await _write_target(session, run)
    try:
        await reimport.realign_item(session, run=run, draft=draft, workflow=workflow, item=item,
                                    metric_id=payload.metric_id)
    except reimport.ImportRefused as exc:
        raise _refused(exc) from None
    return _run_view(run, await _items(session, run.id))


@router.post("/imports/{run_id}/accept")
async def accept_import(run_id: str, payload: ImportAcceptIn,
                        session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """接受选定字段：**一次性**落到 L（新增一版图），并按被接受的那几格推进 B。

    原子性：先把推进后的不可变 B 工件写出来（写不出来就整次失败，L 不落；失败可能留下没人引用的孤儿工件），
    再在**同一个数据库事务**里提交 L 的新版本、工作项的决定、草稿上的新 B 引用与历史。任一步失败一起回滚，
    新 L 不可见；不会出现「L 提交了、B 永远没推进」的空档。

    并发与幂等：expected_revision 是读到的 Workflow.version（对不上回 409 stale_revision）；带 request_id 的按
    （主体 + 工作流 + 卡 + request_id）认这一次，重放交回已提交的结果（B 读得回来），同键不同内容回 409
    idempotency_conflict。
    """
    from app.api.deps import current_principal, ensure_role

    ensure_role("builder")
    actor = current_principal().username or ""
    run = await _run_or_404(session, run_id)
    draft, workflow, card_id = await _write_target(session, run)
    try:
        result = await reimport.accept_fields(
            session, run=run, draft=draft, workflow=workflow, card_id=card_id,
            selections=[spec.model_dump() for spec in payload.items], actor=actor,
            request_id=payload.request_id, expected_revision=payload.expected_revision)
    except reimport.ImportRefused as exc:
        raise _refused(exc) from None
    view = _run_view(run, await _items(session, run.id))
    return {**view, **result}


@router.post("/imports/{run_id}/reject")
async def reject_import(run_id: str, payload: ImportRejectIn,
                        session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """拒绝选定字段 / 整条工作项：保持 L 不动，也不推进 B。"""
    from app.api.deps import ensure_role

    ensure_role("builder")
    run = await _run_or_404(session, run_id)
    try:
        await reimport.reject_fields(session, run=run,
                                     selections=[spec.model_dump(exclude_unset=True) for spec in payload.items],
                                     note=payload.note)
    except reimport.ImportRefused as exc:
        raise _refused(exc) from None
    return _run_view(run, await _items(session, run.id))


@router.post("/imports/{run_id}/refresh")
async def refresh_import(run_id: str, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """按**现在的** L / B 重算这次导入还没决定的三方，并刷新每项的比较锚点。

    这是 409 `import_stale_l` 之后的恢复动作：L 在候选之后被人改过（或另一次接受动过），三方要按现在的口径重算，
    人看过新的三方再确认、再接受。工作项、人的决定、每项自己的来源依据都不动。
    """
    from app.api.deps import ensure_role

    ensure_role("builder")
    run = await _run_or_404(session, run_id)
    draft, workflow, _card_id = await _write_target(session, run)
    await reimport.refresh(session, run=run, draft=draft, workflow=workflow)
    return _run_view(run, await _items(session, run.id))


@router.post("/imports/{run_id}/resume")
async def resume_import(run_id: str, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """显式恢复：把这次导入重新排进后台，续做还没完成的工作项。已接受 / 拒绝的不动，不重复建版。

    工作项按持久对齐键认（稳定 metric 身份 + 来源证据），顺序 / 条数变了也不串项。真正的排队在 reimport.resume 里
    （它改状态、再排进后台）；这次响应是「已排队」的确认，接着轮询详情看结果。"""
    from app.api.deps import ensure_role

    ensure_role("builder")
    run = await _run_or_404(session, run_id)
    try:
        await reimport.resume(session, run)
    except reimport.ImportRefused as exc:
        raise _refused(exc) from None
    await session.refresh(run)
    return _run_view(run, await _items(session, run.id))


__all__ = ["router"]
