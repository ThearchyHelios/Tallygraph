"""账本（BE-3，只读）：工作流版本被哪些应用钉住、生效计数；生效记录查询。设计文档 §2.9 L4。

不另建存储：ActionRecord 本身带着工作流、版本、graph_hash、能力版本、payload_hash、回执；「口径 → 应用」就是固定这一版的
应用版本。用在发布弹窗、退役确认、版本列表三处。

口径账本（docs/design/next-round/02-口径账本.md）在这里加的接口：
- GET /api/runs/{run_id}/ledger（runs_router，和证据同一道门）：一次运行里每个数的口径标签、整题判断、每条查询的比对、能做的
  动作、出具过的正式运行、对过的账（app/ledger/view.py）；
- GET /api/ledger/stats（router，搭建者）：账本自身给出的统计 E1–E4，从比对工件、运行、审计现算（app/ledger/stats.py）；
- POST /api/ledger/answers/{run_id}/issue（answers_router，登录即可，第 3 波）：按口径正式出具（app/ledger/issue.py，拍板
  L1、L5：参数只从比对工件取，使用者只能按他有权使用的应用固定着的版本出具）；
- POST /api/ledger/answers/{run_id}/reconcile（同上）：口径对账，点开才跑一次确定性派生的只读图（app/ledger/reconcile.py）。
  它派生出来的图实际执行的就是点名的这一版口径：**普通使用者先要有这一版的资产授权**（K14，`ensure_caliber_use`：缺了在
  派生、发起之前就 403 querycore_not_authorized，一条运行都不起，也不换路、不退底表）；builder / admin / 关登录照旧放行；
- POST /api/ledger/answers/{run_id}/reconcile/{reconcile_id}/feedback（同上，第 4 波）：对账结果上的「说明有用 / 看不懂」
  （app/ledger/feedback.py，统计 E2）；
- GET /api/ledger/certify-queue、POST /api/ledger/certify-queue/dismiss（router，搭建者，第 3 波）：可沉淀的问题、「不是口径」
  （app/ledger/certify.py）。
- GET /api/ledger/recall/{workflow_id}/{version}、POST …/continue（router，搭建者，第 4 波）：口径升到这一版之后的召回批次——
  每组的结论、「另有 M 组未重算」、手动接着算（app/ledger/recall.py）。版本用量（workflow-versions）另加问数回答、导出、召回。
- GET /api/ledger/calibers、GET /api/ledger/calibers/{workflow_id}/{version}/{node_id}（router，搭建者）：口径可调用描述
  （app/ledger/descriptor.py）：入口字段的 JSON Schema、依赖、能不能组合和出具；不返回 SQL 和参数值。
- POST /api/ledger/drafts/extract、GET /api/ledger/drafts、GET/PATCH /api/ledger/drafts/{id}、POST …/{id}/dismiss、
  POST …/{id}/attach、GET …/{id}/trial（router，搭建者）：从业务文档起草口径。主路径一次到位：模型读文档和数据目录，
  产出定义 + 取数 SQL + 入口字段 + 指标表达式（app/ledger/draftplan.py），服务端校验收成草稿图、建草稿工作流和 v1，
  后台自动只读试跑；草稿页给「定义 + 数 + 原文引用」。模型一轮只起草一组指标，一轮覆盖不完就带着 uncovered 续跑，
  一组一个取数节点挂到同一张口径卡上（见 _extract_in_background）。attach 保留为辅助入口：已经有跑对的答案时让它认
  这份定义（app/ledger/docattach.py）。
  读草稿给出这份草稿的权威口径卡（card_id，多张时是下游契约声明的那张）和基准版本（revision = Workflow.version）；
  改草稿（PATCH）对已经接管到工作流上的这类草稿按 K02 的写入保护走：要带 expected_revision、目标卡要明确、带
  request_id 的按主体 + 工作流 + 目标卡幂等（见 patch_draft 和 app/apps/versions.py 的写入幂等一节）。
  PATCH 的 operations 是 K03 的字段变更：按稳定 metric_id + 字段路径改一处（app/ledger/changes.py 的白名单和语义），
  和整份换指标（metrics）互斥；改在图的深拷贝上，回执里带变更分类、影响指标和失效说明。
- GET /api/ledger/drafts/{id}/validations、POST …/validations/{record_id}/review（router，搭建者，K05）：验证记录
  （追加存储，app/ledger/validation.py）和最小审阅（app/ledger/review.py）。每条记录绑着这次试跑的定义/绑定/取数语义
  依据，covers_current 读的时候现算；没有独立期望只记 observed，样例匹配不等于批准。审阅的审批人只来自服务端身份。
  草稿详情另给 field_baseline（K05 的字段基线 B：首次接受导入时按 metric_id + field_path 存下的不可变工件引用，
  app/ledger/fieldbaseline.py）和 validation（验证总情况：覆盖判断、最新一条、审阅结论）。
- POST /api/ledger/querycore（router，搭建者，K09）：进程内 QueryCore 固定执行（app/ledger/querycore.py）——按一份
  已发布的固定口径（点名哪一版、或读一次推荐版并当场固定）跑一次：K08 闭包选指标、K01 参数语义、只读边界，交回
  类型化指标、发布/manifest 引用、闭包身份、参数解释与哈希、数据时点与质量、每条查询的证据引用。用途是宿主常量
  （这个入口固定 compose/探索，客户端传不了 purpose / role / actor），要正式身份的是 K10 父 Run 的宿主入口
  （querycore.execute_formal，同一套 judge）。
- POST /api/ledger/drafts/{id}/change-proposal、POST …/nl-change（router，搭建者，K12）：受限的自然语言修改
  （app/ledger/nlchange.py）。前者**只读**：一句话 → K03 同一形状的 operations + 影响摘要 + 澄清项；后者默认只预览，
  confirm=true 才按同一批操作落一版，且只是一层薄适配——翻成一次 DraftPatchIn 交给现有变更服务（patch_draft）。
  说不清（含糊业务条件、没匹配能力）回 409 并带澄清项，一条操作都不落。
- POST /api/ledger/ask（router，搭建者，K12）：Ask Data 的固定请求路由。已经明确 workflow/card/metric_ids/inputs 的
  请求直接进 QueryCore（路由只把版本固定一次，不重复解析/规划，内部 LLM 仍为 0）；没有匹配口径就进探索、含糊业务
  条件先澄清、业务条件和匹配到的口径对不上就明确说「先改口径」，绝不静默换计算规则。
这几个新接口都不放给访问密钥（auth/keys.KEY_ROUTES 不登记）。
"""
from __future__ import annotations

import asyncio
import copy
import logging
import re
from collections.abc import Iterable, Mapping
from typing import Any

from datetime import datetime

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.coded import CodedHTTPException
from app.db.base import SessionLocal, get_session
from app.db.models import CaliberDraft, Chunk, DataSource, Document, Run, Workflow, WorkflowVersion
from app.db.models_process import ActionRecord

logger = logging.getLogger(__name__)

from app.ledger.authoring import (
    require_authoring_source, _can_repair, _columns_digest, _draft_payload, _entry_notes, _focus_tables, _fresh_metrics, _merge_questions, _merged_fields, _problems_text, _published_metrics, _question_count, _question_key, _schema_material, _source_tables, _status_for
)

router = APIRouter(prefix="/api/ledger", tags=["ledger"])
#: 口径账本里按运行读的那几个（一个答案里每个数的口径标签等，docs/design/next-round/02-口径账本.md）：挂在
#: /api/runs/{run_id}/ledger… 下，和证据接口同一道门（main.py 挂 evidence_scope：使用者只读、而且要能读这次运行）
runs_router = APIRouter(prefix="/api/runs", tags=["ledger"])
#: 按口径正式出具、口径对账这类要写的（docs/design/next-round/02-口径账本.md 做法 5、6）：使用者也能发起（总方案拍板
#: 账本-L5：对账都可以，正式出具只限有权使用的应用固定着的口径版本），所以 main.py 只挂登录即可，接口里按运行和应用授权再判
answers_router = APIRouter(prefix="/api/ledger/answers", tags=["ledger"])


async def ensure_caliber_use(session: AsyncSession, actor: Any, targets: Iterable[tuple[str, Any]], *,
                             what: str) -> None:
    """普通使用者按口径执行之前的**共享那一道**（K14）：这次实际要执行的每一版口径都要有资产授权到 use 及以上。

    - 只对「普通使用者」成立（`auth/chat_policy.restricted_actor`：开登录后不是 builder / admin 的那一档；actor 可以是当前
      Principal，也可以是用户名）。builder / admin / 关登录在这里**显式放行**——这几档本来就能按口径执行，它们的角色不写进
      只认授权的 `apps/access.caliber_role`；
    - 缺一个就 403 `querycore_not_authorized`（`what` 说明是哪次操作），**在这个入口发起任何运行之前**：不换路、也不退回
      探索底表；problems 里逐条写清缺的是哪一版；
    - targets 是「这次真正会执行的那几版」`(workflow_id, version)`：调用方按自己派生出来的图给，别把没执行的版本列进来
      （召回那一批同时执行新旧两版，两版都要过）；
    - 一处判定：各入口（对账、以后别的派生执行）都调这一个函数，不各写一套角色逻辑。**只读地看历史**（账本、对过账的运行、
      证据、运行记录）不走这一道（下面那个 `restricted_actor` 之外还有 `can_read_run` 那条线管读）；按口径**正式出具**仍走它
      自己那一套（`ledger/issue`：资产授权 + `publications.judge`），不在这里重判。
    """
    from app.apps.access import at_least, caliber_role
    from app.auth.chat_policy import effective_actor, restricted_actor
    from app.ledger import querycore

    wanted: list[tuple[str, int]] = []
    for workflow_id, version in targets:
        try:
            wanted.append((str(workflow_id), int(version)))
        except (TypeError, ValueError):        # 版本认不成整数：交给那个入口自己的说法（这里不替它猜）
            continue
    if not wanted:
        return
    who = effective_actor(actor)
    if not await restricted_actor(session, who):
        return
    missing: list[dict[str, Any]] = []
    for workflow_id, version in wanted:
        role = await caliber_role(session, who, workflow_id, version)
        if not at_least(role, "use"):
            missing.append({"code": querycore.NOT_AUTHORIZED, "workflow_id": workflow_id, "version": version,
                            "role": role or ""})
    if not missing:
        return
    names = dict((await session.execute(select(Workflow.id, Workflow.name).where(
        Workflow.id.in_({m["workflow_id"] for m in missing})))).all())
    listed = "、".join(f"「{names.get(m['workflow_id']) or m['workflow_id']}」v{m['version']}" for m in missing[:4])
    raise CodedHTTPException(
        403,
        f"没有权限：{what}要执行的这几版口径没有授权给你使用：{listed}"
        + (f" 等 {len(missing)} 版" if len(missing) > 4 else "")
        + "。用一份已发布的口径要有应用授权（让搭建者把固定着这一版口径的应用授权给你，至少「使用」）",
        querycore.NOT_AUTHORIZED, extra={"problems": missing})


@router.get("/workflow-versions/{workflow_id}/{version}")
async def workflow_version(workflow_id: str, version: int, session: AsyncSession = Depends(get_session)
                           ) -> dict[str, Any]:
    """一个工作流版本被哪些应用固定、生效记录数、正式运行数；另加口径账本的用量（next-round 02 做法 8）：被几次问数回答用过
    （answers：比对里等价已证明的探索运行）、导出过几次（exports：这些答案和这一版的正式运行上的报告导出审计）、定时任务几个、
    召回（recall：以这一版为新版本召回过几组、其中数变了几组，作为旧版本被召回过几组）。"""
    from app.apps.versions import usage
    from app.ledger.uses import version_usage

    if await session.get(Workflow, workflow_id) is None:
        raise HTTPException(404, "工作流不存在，可能已被删除")
    base = await usage(session, workflow_id, version)
    extra = await version_usage(session, workflow_id, version)
    return {**base, "answers": extra["answers"], "exports": extra["exports"], "schedules": extra["schedules"],
            "recall": extra["recall"]}


def effect_row(r: ActionRecord, *, receipt: bool = True) -> dict[str, Any]:
    return {"id": r.id, "run_id": r.run_id, "node_id": r.node_id, "app_id": r.app_id, "workflow_id": r.workflow_id,
            "workflow_version": r.workflow_version, "graph_hash": r.graph_hash, "capability": r.capability,
            "capability_version": r.capability_version, "environment": r.environment, "state": r.state,
            "attempt": r.attempt, "payload_hash": r.payload_hash, "preview": r.preview,
            "receipt": r.receipt if receipt else None, "error": r.error, "resolution": r.resolution,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "updated_at": r.updated_at.isoformat() if r.updated_at else None}


@router.get("/effects")
async def effects(workflow_id: str | None = None, version: int | None = None, app_id: str | None = None,
                  state: str | None = None, capability: str | None = None,
                  limit: int = Query(default=100, ge=1, le=500), session: AsyncSession = Depends(get_session)
                  ) -> dict[str, Any]:
    """生效记录，新的在前。演练（simulated）照样列出，状态写明是演练。"""
    query = select(ActionRecord)
    if workflow_id:
        query = query.where(ActionRecord.workflow_id == workflow_id)
    if version is not None:
        query = query.where(ActionRecord.workflow_version == version)
    if app_id:
        query = query.where(ActionRecord.app_id == app_id)
    if state:
        query = query.where(ActionRecord.state == state)
    if capability:
        query = query.where(ActionRecord.capability == capability)
    rows = (await session.execute(query.order_by(ActionRecord.created_at.desc(), ActionRecord.id.desc()).limit(limit))).scalars()
    return {"items": [effect_row(r) for r in rows]}


# --------------------------------------------------------------------------
# 口径账本（next-round 02）
# --------------------------------------------------------------------------


@runs_router.get("/{run_id}/ledger")
async def run_ledger(run_id: str) -> dict[str, Any]:
    """一次运行的口径账本：每个数来自哪一份口径（等价已证明 / 助手选用）、相关口径差在哪、这次的推理、没比对的原因；整题判断；
    每条查询和哪几份口径比过。比对在封存之后做（note 写明不在封存范围内）；没比对过、能比的运行这时排上按需比对，答复
    state=pending。绑定的值是这次运行查到过的遮罩值时不给值，差异条目里的 SQL 片段按同一套遮罩换掉。"""
    from app.ledger.view import run_view

    return await run_view(run_id)


@router.get("/stats")
async def ledger_stats(since: datetime | None = Query(default=None, description="只算这之后发起的运行（ISO 时间）"),
                       until: datetime | None = Query(default=None, description="只算这之前发起的运行"),
                       origin: str | None = Query(default=None, description="逗号分隔的运行来源，缺省是探索运行的几种"),
                       session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """账本自身给出的统计 E1–E4（app/ledger/stats.compute_stats）：口径命中、对账、重复度、召回。全部现算，不另存；评测页
    「口径命中与重复度」直接调同一个函数。"""
    from app.api.deps import current_principal
    from app.ledger.stats import compute_stats

    origins = [o.strip() for o in (origin or "").split(",") if o.strip()] or None
    return await compute_stats(session, since=since, until=until, origins=origins, principal=current_principal())


class IssueIn(BaseModel):
    """按哪份口径出具：只能选口径，参数由服务端从比对工件里取（不收客户端传的参数）。"""

    workflow_id: str = Field(min_length=1, max_length=32)
    version: int = Field(ge=1)


@answers_router.post("/{run_id}/issue")
async def issue_answer(run_id: str, payload: IssueIn) -> dict[str, Any]:
    """按口径正式出具这次问数据的答案（app/ledger/issue.py）：经 launch_run 发起正式运行，来源 ledger_issue。同一运行、同一
    口径、同一组参数只发起一次，再点交回已经发起的那一次（created 为假）。不能出具时回 409（整题不等价、版本退役、有人工
    节点或写入、缺入口字段……）、403（权限不够：使用者只能按他有权使用的应用固定着的版本出具）、429（超过每日上限），
    detail 写明原因。"""
    from app.api.coded import CodedHTTPException
    from app.api.deps import current_principal
    from app.ledger import issue as ledger_issue

    try:
        run, created = await ledger_issue.issue(run_id, payload.workflow_id, payload.version,
                                                principal=current_principal())
    except ledger_issue.IssueRefused as e:
        raise CodedHTTPException(e.status_code, e.message, e.code) from e
    return {"run_id": run.id, "created": created, "issued": ledger_issue.issued_view(run)}


class ReconcileIn(BaseModel):
    """对哪条查询、和哪份相关口径对账。inputs + confirm：参数要核对时（动作里 needs_check）人核对、改过的参数；pairs：认不出
    哪一列对哪个指标时人选的 [{ours: 这次的列, metric: 口径卡上的指标 id}]（动作里写明要不要选）。"""

    query: str = Field(min_length=1, max_length=64)
    workflow_id: str = Field(min_length=1, max_length=32)
    version: int = Field(ge=1)
    inputs: dict[str, Any] | None = None
    confirm: bool = False
    pairs: list[dict[str, Any]] | None = None


@answers_router.post("/{run_id}/reconcile", status_code=201)
async def reconcile_answer(run_id: str, payload: ReconcileIn,
                           session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """口径对账（app/ledger/reconcile.py）：按那一版口径确定性派生只读图，发起一次探索运行（来源 reconcile），差值在对账卡
    里算。参数是推断出来的、不全的要先核对（409，code ledger_reconcile_check_inputs）；认不出列怎么配的要人选（409，
    ledger_reconcile_needs_pairs）；多行时维度是遮罩列、键不唯一照实拒绝（409）。

    派生图实际执行的就是点名的这一版口径（`reconcile.reconcile` 按 workflow_id/version 读 pinned，内联进对账图的那一段）：
    **普通使用者**要先有这一版的资产授权（K14，`ensure_caliber_use`），缺了在派生、发起之前就 403
    `querycore_not_authorized`——一条运行都不起，也不换路、不退回探索底表；builder / admin / 关登录照旧放行。"""
    from app.api.coded import CodedHTTPException
    from app.api.deps import current_principal
    from app.ledger import reconcile as ledger_reconcile

    principal = current_principal()
    # 发起之前先过共享那一道（对账这一版实际会执行的口径版本）：缺授权当场 403，下面一行都不会走到
    await ensure_caliber_use(session, principal, [(payload.workflow_id, payload.version)], what="口径对账")
    try:
        run = await ledger_reconcile.reconcile(run_id, ledger_reconcile.Request(
            query=payload.query, workflow_id=payload.workflow_id, version=payload.version, inputs=payload.inputs,
            confirm=payload.confirm, pairs=payload.pairs), principal=principal)
    except ledger_reconcile.ReconcileRefused as e:
        raise CodedHTTPException(e.status_code, e.message, e.code, extra=e.extra) from e
    return {"run_id": run.id, "reconciled": ledger_reconcile.reconciled_view(run)}


class FeedbackIn(BaseModel):
    """对账结果上的反馈：说明有用（useful）还是看不懂（unclear）。"""

    verdict: str = Field(min_length=1, max_length=20)


@answers_router.post("/{run_id}/reconcile/{reconcile_id}/feedback")
async def reconcile_feedback(run_id: str, reconcile_id: str, payload: FeedbackIn) -> dict[str, Any]:
    """对账结果上的「说明有用 / 看不懂」（app/ledger/feedback.py，统计 E2）：记成对账运行上的封存后事件，同一个人改主意只算
    最后一次。交回这次对账现在的反馈 {mine, useful, unclear}。不认的反馈 422；不是这个答案的对账 404；读不了这个答案 403；
    对账还没跑完 409。"""
    from app.api.coded import CodedHTTPException
    from app.api.deps import current_principal
    from app.ledger import feedback

    try:
        out = await feedback.give(run_id, reconcile_id, payload.verdict, principal=current_principal())
    except feedback.FeedbackRefused as e:
        raise CodedHTTPException(e.status_code, e.message, e.code) from e
    return {"run_id": reconcile_id, "feedback": out}


@router.get("/recall/{workflow_id}/{version}")
async def recall_batches(workflow_id: str, version: int, session: AsyncSession = Depends(get_session)
                         ) -> dict[str, Any]:
    """口径升到这一版之后的召回（app/ledger/recall.batches）：每组（旧版本、参数）的结论——数变了 / 没变 / 无法重算：原因 /
    重算失败 / 还在算 / 未重算——和「另有 M 组未重算」、能不能接着算。计划从已有的召回运行现算，不另存。"""
    from app.ledger import recall

    if await session.get(Workflow, workflow_id) is None:
        raise HTTPException(404, "工作流不存在，可能已被删除")
    return await recall.batches(session, workflow_id, version)


@router.post("/recall/{workflow_id}/{version}/continue")
async def recall_continue(workflow_id: str, version: int, session: AsyncSession = Depends(get_session)
                          ) -> dict[str, Any]:
    """接着算：把还没重算的组再发起一批（每批 recall_limit 组，上限是 0 时按缺省的组数），交回发起了的召回运行和新的批次
    情况。都算过了的、这一版已经不是当前发布版的（之后又发布了更新的版本）回 409，detail 写为什么。

    这一组接缝是**搭建者专用**（main.py 给 ledger.router 挂的是 require_role("builder")，使用者到不了这里）；叫起的召回运行
    一次同时执行**新旧两版**口径（旧版那几组的卡 + 这一版，ledger/recall.build_graph），若以后开放给使用者，先按这两版过
    `ensure_caliber_use`（对账那条路就是这么做的：缺任何一版都在发起前 403 querycore_not_authorized）。"""
    from app.api.coded import CodedHTTPException
    from app.api.deps import current_principal
    from app.ledger import recall

    if await session.get(Workflow, workflow_id) is None:
        raise HTTPException(404, "工作流不存在，可能已被删除")
    current = await recall.batches(session, workflow_id, version)
    if not current["can_continue"]:
        raise CodedHTTPException(409, current.get("continue_reason") or "没有还没重算的组", recall.REFUSED)
    launched = await recall.launch(workflow_id, version, batch="manual", actor=current_principal().username)
    async with SessionLocal() as fresh:
        view = await recall.batches(fresh, workflow_id, version)
    return {"launched": launched, **view}


@router.get("/certify-queue")
async def certify_queue(session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """可沉淀的问题（认证队列，app/ledger/certify.py）：确定性重复的 SQL 形状、问题聚类、被反复用的答案。每项给代表问题、
    轮次、（形状的）参数候选和相关口径；点过「不是口径」的不再列。固化走 POST /api/copilot/from-run（freeze: true）。"""
    from app.api.deps import current_principal
    from app.ledger import certify

    return await certify.queue(session, principal=current_principal())


class DismissIn(BaseModel):
    item_id: str = Field(min_length=1, max_length=64)
    kind: str | None = Field(default=None, max_length=20)
    note: str | None = Field(default=None, max_length=500)


@router.post("/certify-queue/dismiss")
async def certify_dismiss(payload: DismissIn, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """「不是口径」：记一条审计（ledger.certify.dismiss），这一项之后不再进队。"""
    from app.ledger import certify

    from app.api.deps import current_principal

    if payload.kind is not None and payload.kind not in certify.KINDS:
        raise HTTPException(422, "可沉淀的问题只有三种：确定性重复、问题聚类、被反复用的答案")
    await certify.dismiss(session, payload.item_id, kind=payload.kind, note=payload.note,
                          principal=current_principal())
    return {"ok": True, "item_id": payload.item_id}


@router.get("/calibers")
async def calibers(workflow_id: str | None = None, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """口径可调用描述（app/ledger/descriptor.py）：每个工作流最新「已发布且未退役」版本里的源头口径卡一张一条，含入口字段的
    JSON Schema、指标、依赖的表和发布时钉住的数据目录/结构版本、能不能组合、出具策略。workflow_id 可只列一个工作流。

    不返回取数 SQL、不返回参数值（SQL 里可能写着遮罩值）。issuance 只描述这一版的策略（级别、有没有人工节点或写外部系统），
    不代表这次运行一定能出具：全局自动出具开关、每日上限这类运行时条件不在这里判。

    每一条另给 binding（K04 的业务绑定能力描述，和单条接口交同一份）：能按字段改哪些业务定义、允许值和说法、这一版
    现在的值、定义和取数实现的同步状态（aligned / needs_update / unsupported / unbound）、不支持的原因和该做什么。
    aligned 只说定义和实现同步，不等于业务验证通过；这里也不返回受控模板。"""
    from app.ledger.descriptor import list_calibers as list_descriptors

    made = await list_descriptors(session, workflow_id=workflow_id)
    return {"calibers": [await _with_binding(session, item) for item in made]}


async def _with_binding(session: AsyncSession, made: dict[str, Any] | None) -> dict[str, Any] | None:
    """给一条口径描述补上 K04 的绑定能力描述（列表和单条交同一份：读那一版的图现算）。

    **不给受控模板、不给 snippets、不给 SQL**：那几样是作者侧的实现细节，界面和外部集成只需要知道业务值和状态。"""
    if made is None:
        return None
    from app.apps.versions import version_row
    from app.ledger import bindings

    row = await version_row(session, str(made.get("workflow_id") or ""), int(made.get("version") or 0))
    graph = (row.graph if row is not None else None) or {}
    return {**made, "binding": bindings.capability(graph, str((made.get("card") or {}).get("node_id") or ""))}


@router.get("/calibers/{workflow_id}/{version}/{node_id}")
async def caliber_descriptor(workflow_id: str, version: int, node_id: str,
                             session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """一张源头口径卡的描述（形状同列表里的一条）。工作流不存在、这一版没发布过或已退役、节点不是源头口径卡（转抄卡也算）
    回 404。同样不返回 SQL、参数值；issuance 只描述版本策略，不代表这次运行一定能出具。

    另给 binding（K04 的服务端能力描述）：能按字段改的业务定义（时间口径、退款处理）、每个字段的允许值和说法、
    这一版绑定里现在的值、定义和取数实现的同步状态（aligned / needs_update / unsupported / unbound）、不支持时的
    原因、影响到的指标和取数节点、以及该做什么。**aligned 只说定义和实现同步，不等于业务验证通过**；这里也不返回
    受控模板和 SQL（那是作者侧的实现细节）。"""
    from app.ledger.descriptor import caliber_descriptor as descriptor_of

    made = await descriptor_of(session, workflow_id, version, node_id)
    if made is None:
        raise HTTPException(404, "没有这份口径：工作流不存在、这一版没发布过或已退役，或者节点不是源头口径卡")
    return await _with_binding(session, made)


class RuleCompileIn(BaseModel):
    """规则 DSL 编译：表 + 结构化条件 → 只读 SQL；fixtures 可选，用内存 SQLite 跑一遍。"""

    rule: dict[str, Any]
    table: str
    fixtures: list[dict[str, Any]] = Field(default_factory=list)


@router.post("/rules/compile")
async def compile_rule_endpoint(payload: RuleCompileIn) -> dict[str, Any]:
    """把结构化规则编译成 SQL，并（给了 fixtures 时）用样例行验证。只生成 SELECT，不碰业务库。"""
    from app.data.rule_compile import RuleInvalid, compile_rule, run_fixtures

    try:
        sql = compile_rule(payload.rule, payload.table)
        checks = run_fixtures(payload.rule, payload.table, payload.fixtures)
    except RuleInvalid as e:
        raise HTTPException(422, str(e)) from None
    return {"sql": sql, "fixtures": checks}


class TrialIn(BaseModel):
    """试跑只收入口字段的值；没写的按口径里的缺省值补。"""

    inputs: dict[str, Any] = Field(default_factory=dict)


@router.post("/calibers/{workflow_id}/{version}/{node_id}/trial")
async def trial_caliber(workflow_id: str, version: int, node_id: str, payload: TrialIn,
                        session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """按固定版本试跑一份口径：派生只读小图（取数 + 口径卡），跑一次探索运行，返回指标值和运行 id。

    试跑不是正式出具：来源记 trial，不进使用统计、不自动出具；参数按入口 Schema 校验，缺的补缺省值；参数完整性按
    ledger/params 同一套判断（缺参数、未声明参数、值承载不了），不合规在**发起运行之前**回结构化 422（code 是
    missing_parameter / undeclared_parameter / …），一条 SQL 都不会发出去。不能派生的口径（输入不是查库结果里的格、
    取数模板引用了运行时变量……）照实回 409。最多等 30 秒；还没跑完就回 running，前端按 run_id 去看运行。"""
    from app.api.deps import current_principal
    from app.apps.forms import input_schema, validate_form
    from app.engine.runner import run_manager
    from app.ledger import params
    from app.ledger.derive import UNDECLARED_PARAMETER as derive_undeclared_code
    from app.ledger.derive import NotDerivable, caliber_probe, load_pinned
    from app.tools.datasource import READ_ONLY_OPTION

    try:
        pinned = await load_pinned(session, workflow_id, int(version), node_id)
    except NotDerivable as e:
        # 取数模板引用了入口没声明的字段（undeclared_parameter）：这是参数契约的问题，按参数完整性回结构化 422，
        # 并把逐条问题原样交回（problems）；别的不能派生的原因照旧 409
        if e.code == derive_undeclared_code:
            raise CodedHTTPException(422, e.message, params.UNDECLARED,
                                     extra=({"problems": [p.as_dict() for p in e.problems]} if e.problems else None)
                                     ) from None
        raise HTTPException(409, e.message) from None
    if not pinned.published or pinned.retired:
        raise HTTPException(409, "这一版没有发布或已退役，不能试运行")
    fields = [dict(f) for f in pinned.entry_fields]
    known = {str(f.get("name")) for f in fields}
    unknown = sorted(str(k) for k in payload.inputs if str(k) not in known)
    if unknown:
        raise HTTPException(422, f"不认识的入口字段：{'、'.join(unknown)}")
    # 收值和组合式问数同一份（params.provided）：显式传的空串算没给——字段又没声明缺省值时就是缺参数（下面拒），
    # 声明了 default="" 的照缺省值走（作者写的就是「空值不过滤」）
    values = {str(f["name"]): f.get("default") for f in fields if f.get("default") not in (None, "")}
    values.update(params.provided(payload.inputs))
    if errors := validate_form(input_schema(fields), values):
        raise HTTPException(422, "；".join(str(e.get("message")) for e in errors[:5]))
    # 参数完整性：模板里的每个参数这次都要有值（可选、没有缺省值的也算缺），引用的字段入口都得声明过，
    # 承载不了的值明确拒——都在发起运行之前判，缺参/未声明不会走到「发出 SQL 才发现」
    problems = params.check_nodes(pinned.fetches, fields=pinned.entry_fields, values=values,
                                  where=f"口径「{pinned.workflow_name}」的入口")
    if problems:
        raise CodedHTTPException(422, params.describe(problems), problems[0].code,
                                 extra={"problems": [p.as_dict() for p in problems]})

    graph = caliber_probe(pinned).graph
    principal = current_principal()
    run = await run_manager.start(
        graph=graph, input_payload=values, run_class="exploratory", origin_kind="caliber_trial",
        origin={"kind": "caliber_trial", "workflow_id": workflow_id, "version": int(version),
                "node_id": node_id, "card": pinned.node_id},
        options={READ_ONLY_OPTION: True},
        started_by=principal.username)
    row = None
    for _ in range(300):
        async with SessionLocal() as poll:
            row = await poll.get(Run, run.id)
        if row is None or row.status in TERMINAL_RUN_STATUSES:
            break
        await asyncio.sleep(0.1)

    out: dict[str, Any] = {"run_id": run.id, "status": row.status if row is not None else "unknown",
                           "output": None,
                           "error": row.error if row is not None else None, "metrics": [],
                           "queries": []}
    if row is not None and row.status == "succeeded":
        out["metrics"] = await _trial_metrics(run.id)
        out["queries"] = await _trial_queries(run.id)
    out["validation"] = await _trial_validation(run.id, str(out["status"]), out["metrics"])
    return out


# --------------------------------------------------------------------------
# 进程内 QueryCore 固定执行（app/ledger/querycore.py，K09）
# --------------------------------------------------------------------------


class QueryCoreIn(BaseModel):
    """这一次固定执行要什么：口径身份（workflow + card）、要算的指标、类型化入口取值、预算与 trace。

    **不收 purpose / role / actor**：用途由宿主常量定（这个接口固定成 compose/探索），认证主体来自服务端身份。
    请求体里多传的字段一律不看（不认识的键被忽略），提不了权。"""

    workflow_id: str
    card_id: str
    metric_ids: list[str] = Field(min_length=1)
    inputs: dict[str, Any] = Field(default_factory=dict)
    version: int | None = None
    budget: dict[str, Any] | None = None
    trace: str | None = None


@router.post("/querycore")
async def querycore_execute(payload: QueryCoreIn, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """按一份**已发布的固定口径**进程内跑一次（app/ledger/querycore.py）：交回类型化指标、实际发布/manifest 引用、
    selection/closure 身份、参数解释与哈希、数据时点与质量状态、每条查询的执行/attempt/工件/来源证据引用。

    这是**交互入口**：`version` 点名就固定到它、不点名就读推荐版并当场固定（解析只发生一次）；用途固定成 compose
    （探索运行），不继承正式身份、也不拿它绕过 K07 的 formal 判定——要正式身份的是 K10 父 Run 的宿主入口
    （`querycore.execute_formal`，同一套 judge）。发布清单必须读得出来、就是这一版这一张图、约定与现在兼容；老资产
    （没有清单）明确拒。参数在发 SQL 之前就判完整性（缺参数/未声明/承载不了回 422，一条 SQL 都不发）；最多等 30 秒，
    没跑完回 `status="running"` + run_id，**超时不算成功**。"""
    from app.api.deps import current_principal
    from app.ledger import querycore

    principal = current_principal()
    try:
        outcome = await querycore.execute(session, workflow_id=payload.workflow_id, card_id=payload.card_id,
                                         metric_ids=payload.metric_ids, inputs=payload.inputs,
                                         version=payload.version, actor=principal.username,
                                         budget=payload.budget, trace=payload.trace)
    except querycore.QueryCoreRefused as e:
        raise CodedHTTPException(e.status_code, e.message, e.code,
                                 extra=({"problems": [dict(p) for p in e.problems]} if e.problems else None)
                                 ) from None
    return outcome.as_dict()


# --------------------------------------------------------------------------
# 受限的自然语言修改与固定请求路由（app/ledger/nlchange.py，K12）
#
# 自然语言只产出**一份 K03 提案**：operations + 影响摘要 + 澄清项，不落库、不新增版本，也没有第二条写路径。
# confirm=true 时才按同一批操作落一版，走的仍是现有变更服务（patch_draft 那条路：expected_revision / request_id /
# CAS / 绑定同步 / 校验一条不少）。Ask Data 的确定请求直接进 QueryCore，路由只把版本固定一次，不重复解析/规划。
# --------------------------------------------------------------------------


class NlChangeIn(BaseModel):
    """一句话 + 目标卡：默认只产出一份受限修改提案（confirm=false）；confirm=true 时按同一批操作落一版。"""

    instruction: str = Field(min_length=1, max_length=2000)
    #: 改哪张卡（图上有好几张可写的卡时必须给，不给则按草稿的权威卡）
    target: DraftTargetIn | None = None
    #: 显式点名要改哪几条指标（不给则按这句话、或声明过这个字段的指标）
    metrics: list[str] | None = None
    #: confirm=true 落版时要带的两样（K02）：读到的基准版本、幂等键
    expected_revision: int | None = Field(default=None, ge=1)
    request_id: str | None = Field(default=None, min_length=1, max_length=64)
    #: 客户端确认：false（缺省）只预览，不落版
    confirm: bool = False


async def _nl_proposal(session: AsyncSession, draft_id: str, payload: NlChangeIn) -> tuple[Any, Any, str, Any]:
    """读草稿、定目标卡、跑一遍 nlchange.propose。交回 (draft, workflow, card_id, proposal)。"""
    from app.ledger import changes as change_service
    from app.ledger import nlchange
    from app.ledger.repository import CaliberGone

    draft = await _draft_or_404(session, draft_id)
    workflow = await session.get(Workflow, draft.workflow_id) if draft.workflow_id else None
    if not _doc_draft_workflow(workflow):
        raise CodedHTTPException(409, "这份草稿还没接管到工作流上（没有可改的图）：字段操作落不到图上，"
                                      "请先起草或接上一次运行", CHANGE_UNAVAILABLE)
    card_id = _target_card(payload, workflow)
    try:
        proposal = nlchange.propose(workflow.graph or {}, str(card_id), payload.instruction,
                                    metrics=payload.metrics)
    except change_service.ChangeRefused as e:
        extra = {key: value for key, value in (("operation", e.operation), ("path", e.path)) if value is not None}
        raise CodedHTTPException(422, e.message, e.code, field="operations", extra=extra) from None
    except CaliberGone:  # 目标卡在这中间没了或者成了转抄卡：和字段变更那条路同一句话、同一个码
        raise CodedHTTPException(409, DRAFT_CARD_GONE, CARD_NOT_FOUND, field="target") from None
    return draft, workflow, str(card_id), proposal


def _proposal_refusal(proposal: Any) -> CodedHTTPException:
    """提案说不清（含糊业务条件、没匹配能力）：回 409，把澄清项原样带上，不落任何东西。"""
    from app.ledger import nlchange

    first = proposal.clarifications[0] if proposal.clarifications else None
    code = (nlchange.PROPOSAL_AMBIGUOUS
            if first is not None and first.kind == nlchange.KIND_AMBIGUOUS else nlchange.PROPOSAL_NONE)
    message = first.question if first is not None else "这句话没有能按字段改的已声明能力"
    return CodedHTTPException(409, message, code,
                              extra={"clarifications": [item.as_dict() for item in proposal.clarifications]})


@router.post("/drafts/{draft_id}/change-proposal")
async def change_proposal(draft_id: str, payload: NlChangeIn,
                          session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """一句话 → 受限修改提案（app/ledger/nlchange.py，K12）：**只读**，不落库、不新增版本。

    产出 K03 同一形状的 operations、这次的影响摘要（失效与绑定结论，由 changes.summary 在同一批操作上现算）和必须先
    问清的澄清项。识别只认已声明的能力（统计时间口径、退款处理这类业务字段）；「排除退款」这种说不清取哪个值的先澄清，
    一条操作都不产出，不替人猜、也不擅自改口径。真要用它，客户端把 operations 交给
    `PATCH /api/ledger/drafts/{id}`（要带 expected_revision / request_id / target）或 `POST …/nl-change`。"""
    from app.api.deps import ensure_role

    ensure_role("builder")
    draft, workflow, card_id, proposal = await _nl_proposal(session, draft_id, payload)
    return {"draft_id": draft.id, "workflow_id": workflow.id, "revision": workflow.version, "card": card_id,
            "proposal": proposal.as_dict()}


@router.post("/drafts/{draft_id}/nl-change")
async def nl_change(draft_id: str, payload: NlChangeIn,
                    session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """一句话改口径（K12）：默认只预览（confirm=false）；confirm=true 时按同一批操作落一版。

    **只是一层薄适配**：真落版时把它翻成一次 `DraftPatchIn(operations=…)`，交给现有变更服务（patch_draft）——K02 的
    expected_revision / request_id / CAS、K03 的深拷贝与整批原子、K04 的绑定同步、校验一条都不少。说不清（含糊业务条件、
    没匹配能力）回 409 并带上澄清项，**一条操作都不落**：失败不擅自改口径。"""
    from app.api.deps import ensure_role

    ensure_role("builder")
    draft, workflow, card_id, proposal = await _nl_proposal(session, draft_id, payload)
    view = {"draft_id": draft.id, "workflow_id": workflow.id, "revision": workflow.version, "card": card_id,
            "proposal": proposal.as_dict()}
    if not payload.confirm:
        # 默认不自动落版：只交回提案，客户端确认后带着它回来
        return {**view, "applied": False, "persisted": False}
    if not proposal.ready:
        raise _proposal_refusal(proposal)
    patch = DraftPatchIn(operations=[DraftOperationIn(**op) for op in proposal.operations],
                         expected_revision=payload.expected_revision, request_id=payload.request_id,
                         target=payload.target)
    return await patch_draft(draft_id, patch, session)


class FixedRequestIn(BaseModel):
    """Ask Data 的固定请求（K12）：口径身份 + 要算的指标 + 入口取值，可选一句业务条件。

    已经明确 workflow/card/metric_ids/inputs 的确定请求直接进 QueryCore；没有匹配口径就进探索、含糊业务条件先澄清、
    业务条件和匹配到的口径对不上就明确说「先改口径」，绝不静默换计算规则。purpose / actor 由服务端定，请求体不收。"""

    workflow_id: str | None = None
    card_id: str | None = None
    metric_ids: list[str] | None = None
    inputs: dict[str, Any] = Field(default_factory=dict)
    version: int | None = None
    #: 一句话里的业务条件（可选）：识别到含糊条件先澄清，识别到和这一版算法对不上的条件就进探索
    instruction: str | None = None


@router.post("/ask")
async def ask_fixed(payload: FixedRequestIn, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """固定请求路由（K12）：能确定的直接进 QueryCore，说不清/没匹配的进澄清或探索。

    路由只把版本固定一次并交给 QueryCore（点名就固定到它、不点名读推荐版并当场固定），QueryCore 不再另挑一次，
    执行路径内部 LLM 仍为 0。返回里 route.mode 是 querycore / clarify / explore：querycore 时另带 result
    （querycore.execute 的结果），其余两种只交回 route（澄清项或为什么进探索），**不发起任何运行、不改任何口径**。"""
    from app.api.deps import current_principal
    from app.ledger import nlchange, querycore

    principal = current_principal()
    routed = await nlchange.route(session, request=payload.model_dump(), purpose=querycore.DEFAULT_PURPOSE)
    out: dict[str, Any] = {"route": routed.as_dict(), "result": None}
    if routed.mode != nlchange.ROUTE_EXECUTE:
        return out
    request = {key: value for key, value in routed.request.items() if key != "purpose"}
    try:
        outcome = await querycore.execute(
            session, purpose=str(routed.request.get("purpose") or querycore.DEFAULT_PURPOSE),
            actor=principal.username, **request)
    except querycore.QueryCoreRefused as e:
        raise CodedHTTPException(e.status_code, e.message, e.code,
                                 extra=({"problems": [dict(p) for p in e.problems]} if e.problems else None)
                                 ) from None
    out["result"] = outcome.as_dict()
    return out


# --------------------------------------------------------------------------
# 从业务文档起草口径（app/ledger/docdraft.py；attach 是 app/ledger/docattach.py）
# --------------------------------------------------------------------------

#: 草稿被驳回的审计（和 certify.dismiss 同一个写法：subject 是草稿 id）
DRAFT_DISMISS_ACTION = "ledger.docdraft.dismiss"
DRAFT_SUBJECT_KIND = "ledger_draft"


#: 试跑最多等多久（秒）和轮询间隔：和 trial_caliber 的 300 × 0.1 一致
TRIAL_TIMEOUT_S = 30
TRIAL_POLL_S = 0.1
#: 试跑结果集最多给几个查询、每个查询前几行：一次试跑只取数不写报告，主要就是验证这几条 SQL
TRIAL_QUERY_LIMIT = 5
TRIAL_QUERY_ROWS = 20
#: 运行到这里就是终态了（interrupted 归到 failed，接口契约只有三种状态）
TERMINAL_RUN_STATUSES = ("succeeded", "failed", "cancelled", "interrupted")


def _run_status(status: Any) -> str:
    """运行状态 → 接口契约里的 succeeded / failed / running。"""
    if status == "succeeded":
        return "succeeded"
    if status in ("failed", "cancelled", "interrupted"):
        return "failed"
    return "running"


def _trial_binding(version: int, graph: dict[str, Any], values: dict[str, Any]) -> dict[str, Any]:
    from app.core.artifact_store import canonical_json, content_hash, execution_hash, graph_hash

    return {"version": version, "graph_hash": graph_hash(graph), "execution_hash": execution_hash(graph),
            "inputs_hash": content_hash(canonical_json(values))}


def _doc_draft_workflow(workflow: Workflow | None) -> bool:
    """这份草稿是不是已经接管到工作流上了（文档起草建出来的口径卡）：只有它对图写入要带基准版本（K02）。"""
    from app.apps.versions import taken_over

    return taken_over(workflow)


async def _trial_view(session: AsyncSession, trial: dict[str, Any], *, version: int,
                      graph: dict[str, Any]) -> dict[str, Any]:
    """历史试跑只能证明当时的版本和输入；运行超时返回 running 后，读时从 Run 刷新终态。"""
    from app.core.artifact_store import canonical_json, content_hash

    if not trial:
        return {}
    run = await session.get(Run, trial["run_id"]) if trial.get("run_id") else None
    if (not _same_graph(trial, graph, version)
            or (run is not None and trial.get("inputs_hash") != content_hash(canonical_json(run.input or {})))):
        return {**trial, "status": "stale", "error": "口径已修改或试运行缺少版本记录，请重新试运行"}
    if trial.get("run_id") and run is None:
        return {**trial, "status": "stale", "error": "试运行记录已不存在，请重新试运行"}
    if run is not None:
        return {**trial, "status": _run_status(run.status), "error": str(run.error or "")}
    return trial


def _same_graph(trial: Mapping[str, Any], graph: Mapping[str, Any], version: int) -> bool:
    """这次试跑记下的图指纹是不是就是 graph 这一版：执行哈希优先（忽略布局），旧试跑按版本 + 图哈希认。"""
    from app.core.artifact_store import execution_hash, graph_hash

    if not isinstance(graph, Mapping) or not graph:
        return False
    if trial.get("execution_hash"):
        return trial.get("execution_hash") == execution_hash(dict(graph))
    return trial.get("version") == version and trial.get("graph_hash") == graph_hash(dict(graph))


async def _save_trial(draft_id: str, trial: dict[str, Any], *, replace: bool = False,
                      expect: Mapping[str, Any] | None = None) -> bool:
    """保存这一次试跑，并把它**追加**成一条验证记录（K05）。

    旧版本或较早的试跑晚结束时，不能覆盖新的验证结果——但**记录照旧追加**：新旧两次试跑都留得下（各自的定义和
    绑定依据都记在记录里），读的时候按覆盖算谁对当前版本说话（见 _record_trial）。
    """
    ok = await _write_trial_pointer(draft_id, trial, replace=replace, expect=expect)
    await _record_trial(draft_id, trial, expect=expect)
    return ok


async def _write_trial_pointer(draft_id: str, trial: dict[str, Any], *, replace: bool = False,
                               expect: Mapping[str, Any] | None = None) -> bool:
    """旧版本或较早的试跑晚结束时，不能覆盖新的验证结果。"""
    async with SessionLocal() as fresh:
        row = await fresh.scalar(select(CaliberDraft).where(CaliberDraft.id == draft_id).with_for_update())
        if row is None or not isinstance(row.payload, dict):
            return False
        workflow = await fresh.get(Workflow, row.workflow_id) if row.workflow_id else None
        if _doc_draft_workflow(workflow):
            if not _same_graph(trial, workflow.graph or {}, workflow.version):
                return False
        body = dict(row.payload)
        current = body.get("trial") or {}
        if not replace and current.get("trial_id") and current.get("trial_id") != trial.get("trial_id"):
            return False
        if not replace and current.get("run_id") and current.get("run_id") != trial.get("run_id"):
            return False
        body["trial"] = trial
        if expect:
            # 这次试跑的固定期望跟着 payload 存一份（按 trial_id 认）：试跑超时、晚些才跑完时，读接口补记记录还用得上
            body[TRIAL_EXPECT_KEY] = {**dict(expect), "trial_id": str(trial.get("trial_id") or "")}
        # SQLite 忽略 FOR UPDATE；加上读时的修订时间，避免并发完成或编辑覆盖刚写入的新内容。
        from sqlalchemy import update

        saved = await fresh.execute(update(CaliberDraft).where(
            CaliberDraft.id == draft_id, CaliberDraft.updated_at == row.updated_at,
        ).values(payload=body).execution_options(synchronize_session=False))
        if saved.rowcount != 1:
            await fresh.rollback()
            return False
        await fresh.commit()
        return True


#: payload 里放「这次试跑的固定期望」的键（按 trial_id 认）：试跑超时、晚些才跑完时，读接口补记记录还用得上
TRIAL_EXPECT_KEY = "trial_expectation"


def _record_status(status: Any) -> str | None:
    """试跑状态 → 值得记一条验证记录的状态；还在跑（running / 空）时返回 None。"""
    text = str(status or "")
    return text if text in ("succeeded", "failed", "needs_input", "stale") else None


def _has_stable_trial_key(trial: Mapping[str, Any]) -> bool:
    """这次试运行有没有服务端给的稳定身份（trial_id，或老写法的 run_id）。"""
    trial = trial or {}
    return bool(str(trial.get("trial_id") or "") or str(trial.get("run_id") or ""))


def _stable_trial_key(trial: Mapping[str, Any]) -> str:
    """这次试运行在验证记录里的身份：trial_id 优先，老写法退到 run_id。

    两样都没有时给一个**由这次试运行本身推出来的确定键**（版本 + 图指纹 + 输入指纹 + 状态 + 错误），不是随机 id：
    同一份 trial 反复保存落到同一行，快照/读取也不会一次多出一行。新试运行都带 trial_id（_run_trial 建 binding 时写，
    authoring.extract 的失败分支同样写），这个兜底只给历史数据和不走 _run_trial 的调用方用。
    """
    trial = trial or {}
    for key in ("trial_id", "run_id"):
        value = str(trial.get(key) or "")
        if value:
            return value
    from app.core.artifact_store import canonical_json, content_hash

    digest = content_hash(canonical_json({
        "version": trial.get("version"), "graph_hash": str(trial.get("graph_hash") or ""),
        "execution_hash": str(trial.get("execution_hash") or ""),
        "inputs_hash": str(trial.get("inputs_hash") or ""),
        "status": str(trial.get("status") or ""), "error": str(trial.get("error") or "")}))
    return f"derived-{digest[:24]}"


async def _resolve_trial_graph(session: AsyncSession, workflow: Workflow | None, trial: Mapping[str, Any]
                               ) -> dict[str, Any]:
    """这次试跑对着的那张图：当前图就是它就用当前图；不是（晚结束的旧试跑）就到这一工作流的版本里按图指纹找。

    都找不到时交回空图：记录照旧追加（这次试跑确实发生过），但依据留空、covers_current 一律 False——不拿当前
    定义冒充它当时跑的那一份。
    """
    from app.core.artifact_store import execution_hash, graph_hash

    current = workflow.graph if workflow is not None and isinstance(workflow.graph, dict) else {}
    if current and _same_graph(trial, current, workflow.version):
        return current
    if workflow is None:
        return {}
    rows = (await session.execute(select(WorkflowVersion).where(
        WorkflowVersion.workflow_id == workflow.id))).scalars()
    wanted = str(trial.get("execution_hash") or "")
    digest = str(trial.get("graph_hash") or "")
    for row in rows:
        graph = row.graph if isinstance(row.graph, dict) else None
        if not graph:
            continue
        if wanted and execution_hash(graph) == wanted:
            return graph
        if not wanted and digest and graph_hash(graph) == digest:
            return graph
    return {}


async def _trial_metric_links(run_id: str) -> tuple[dict[str, str], list[dict[str, Any]]]:
    """这次运行的**指标 → 取数快照**的可靠关联（判形状用），以及每份快照的行数。

    指标记录本身不带取数节点（_trial_metrics 给界面看的那几个字段里没有），但封存的口径卡里有：每个指标的
    `inputs` 记着它取自哪份查询快照（`origin.artifact` / `query_artifact` / `artifact`，和引擎判 contract_findings
    时同一套认法），认不出工件时退回它记下的取数节点。返回
    `(metric_id → 查询快照的键, [{key, node_id, total_rows, truncated}])`。

    **不放进给用户看的响应**：这一份只喂给验证记录（app/ledger/validation.actual_of），响应里的指标键一个不变。
    读不出封存证据时给空（照实记「形状判不了」→ unverified），不猜。
    """
    from app.core.artifact_store import load
    from app.engine.evidence_reader import metric_cards, read_sealed

    try:
        sealed = await read_sealed(run_id)
        if not sealed.trusted:
            return {}, []
    except Exception:  # noqa: BLE001 - 证据读不出来只影响形状判断，记录照落
        logger.exception("试运行 %s 的封存证据读不出来，验证记录按「形状判不了」记", run_id)
        return {}, []
    snapshots: list[dict[str, Any]] = []
    artifacts: set[str] = set()
    for artifact, info in (sealed.queries or {}).items():
        try:
            snap = load(artifact)
        except Exception:  # noqa: BLE001 - 这一份读不出来就跳过它（还有别的快照）
            logger.warning("试运行 %s 的查询快照 %s 读不出来", run_id, artifact, exc_info=True)
            continue
        if not isinstance(snap, dict) or not isinstance(snap.get("rows"), list):
            continue
        artifacts.add(str(artifact))
        snapshots.append({"key": str(artifact), "node_id": str((info or {}).get("node_id") or ""),
                          "total_rows": len(snap["rows"]), "truncated": bool(snap.get("truncated"))})
    links: dict[str, str] = {}
    try:
        for _card_node, _card_artifact, card in metric_cards(sealed):
            for metric in card.get("metrics") or []:
                if not isinstance(metric, Mapping) or not metric.get("id"):
                    continue
                key = _metric_query_key(metric, artifacts)
                if key:
                    links.setdefault(str(metric["id"]), key)
    except Exception:  # noqa: BLE001 - 认不出关联时按判不了记，不让它把记录弄没
        logger.warning("试运行 %s 的指标与查询快照关联认不出来", run_id, exc_info=True)
    return links, snapshots


def _metric_query_key(metric: Mapping[str, Any], artifacts: set[str]) -> str:
    """这条指标读的是哪份查询快照：输入里认得出工件就用工件 id，认不出就退回它记下的取数节点。"""
    inputs = [item for item in metric.get("inputs") or [] if isinstance(item, Mapping)]
    for item in inputs:
        for candidate in ((item.get("origin") or {}).get("artifact") if isinstance(item.get("origin"), Mapping)
                          else None, item.get("query_artifact"), item.get("artifact")):
            if isinstance(candidate, str) and candidate and (not artifacts or candidate in artifacts):
                return candidate
    return next((str(item["node_id"]) for item in inputs if item.get("node_id")), "")


async def _record_trial(draft_id: str, trial: Mapping[str, Any], *, expect: Mapping[str, Any] | None = None,
                        origin: str = "trial") -> str | None:
    """把一次试跑追加成一条验证记录（K05）。晚结束的旧试跑也照记：指针可能不换，记录一定留。

    记的是这一件事：这次在**哪一份定义和绑定**上（卡、定义指纹、绑定指纹、取数语义指纹）、拿什么输入、和什么
    独立期望比、实际是什么、结论是什么。没有独立期望时只记 observed——不能算验证通过。
    """
    from app.ledger import validation

    status = _record_status(trial.get("status"))
    if status is None:
        return None
    run_id = str(trial.get("run_id") or "")
    # 这一次试运行的身份：trial_id 优先，老写法退到 run_id，两样都没有时按这次试运行的内容推一个确定键
    # （见 _stable_trial_key）——同一份 trial 反复保存只落一行，晚到的旧试运行也不会被当成另一次
    record_key = _stable_trial_key(trial)
    inputs: dict[str, Any] = {}
    actor: str | None = None
    recorded_metrics: list[dict[str, Any]] = []
    snapshots: list[dict[str, Any]] = []
    links: dict[str, str] = {}
    problems: list[str] = []
    async with SessionLocal() as session:
        draft = await session.get(CaliberDraft, draft_id)
        if draft is None or not isinstance(draft.payload, dict):
            return None
        workflow = await session.get(Workflow, draft.workflow_id) if draft.workflow_id else None
        graph = await _resolve_trial_graph(session, workflow, trial)
        resolved_card, cards = draft_card(graph) if graph else (None, [])
        card_id = str(resolved_card or (cards[0]["id"] if cards else "") or "")
        card_metrics = validation.card_metrics(graph, card_id) if card_id else []
        metric_ids = [str(metric["id"]) for metric in card_metrics]
        materials = (validation.materials(graph, card_id, workflow_id=str(draft.workflow_id or ""))
                     if graph and card_id else
                     {"workflow_id": str(draft.workflow_id or ""), "card_id": "", "definition_hash": "",
                      "binding_fingerprints": {}, "semantic_hash": ""})
        if not materials["definition_hash"]:
            problems.append("这次试运行找不到对应的定义版本：这条记录只作追踪，不能算覆盖任何一版定义")
        if status == "succeeded" and run_id:
            run = await session.get(Run, run_id)
            inputs = dict(run.input or {}) if run is not None and isinstance(run.input, dict) else {}
            actor = getattr(run, "started_by", None) if run is not None else None
            try:
                recorded_metrics = await _trial_metrics(run_id)
                # 指标 → 取数快照的关联从封存的口径卡里认（指标记录本身不带节点）：不认这一份，真实运行的形状
                # 一律判不了，matched 就永远到不了。这一份不进给用户看的响应。
                links, snapshots = await _trial_metric_links(run_id)
            except Exception:  # noqa: BLE001 - 结果读不出来照实记「没有可比较的实际值」，不让它把记录弄没
                logger.exception("试运行 %s 的结果读不出来，验证记录按「没有可比较的实际值」记", run_id)
                recorded_metrics, snapshots, links = [], [], {}
        elif run_id:
            # 没跑成、停在 needs_input 这类：没有可比较的实际值，但「这次拿什么输入去跑」照记
            run = await session.get(Run, run_id)
            inputs = dict(run.input or {}) if run is not None and isinstance(run.input, dict) else {}
            actor = getattr(run, "started_by", None) if run is not None else None
        stored = draft.payload.get(TRIAL_EXPECT_KEY)
        wanted = expect if expect else (
            stored if isinstance(stored, Mapping)
            and str(stored.get("trial_id") or "") == str(trial.get("trial_id") or "") else None)
        expectation = validation.expectation_of(wanted, card_metrics_=card_metrics)
    actual = validation.actual_of(metrics=recorded_metrics, queries=snapshots, card_metrics_=card_metrics,
                                  metric_ids=metric_ids, links=links)
    result = validation.compare(expected=(expectation or {}).get("metrics") if expectation else None,
                                actual=actual, metric_ids=metric_ids, card_metrics_=card_metrics,
                                run_status=("succeeded" if status == "succeeded"
                                            else "failed" if status == "failed" else "skipped"),
                                problems=problems)
    from app.core.artifact_store import execution_hash

    return await validation.append(
        draft_id=draft_id, workflow_id=str(materials.get("workflow_id") or "") or None,
        version=int(trial.get("version") or 1), card_id=str(materials.get("card_id") or ""),
        metric_ids=metric_ids, basis=materials, inputs=inputs, inputs_hash=str(trial.get("inputs_hash") or ""),
        trial_id=record_key, run_id=run_id, actor=actor, status=result["status"],
        problems=result["problems"], expectation=expectation, actual=result["actual"],
        execution_hash=str(trial.get("execution_hash") or "")
        or (execution_hash(graph) if graph else ""), origin=origin)


async def _ensure_trial_records(draft: CaliberDraft, trial: Mapping[str, Any], *, origin: str = "late"
                                ) -> str | None:
    """读的时候把已经到终态、还没有记录的试运行补记一条（超时后晚完成的那些，以及过期成 stale 的那些）。

    **只对有稳定身份（trial_id / run_id）的试运行追加**：老草稿 payload 里留下的那一条没有身份，认不出「是不是
    同一次」（补记一次、读十次就会多出十行），只读不改。记录按 (草稿, 身份, 状态) 去重；还在执行的什么都不做。
    """
    if _record_status((trial or {}).get("status")) is None or not _has_stable_trial_key(trial):
        return None
    return await _record_trial(draft.id, trial, origin=origin)


async def _trial_record(session: AsyncSession, draft_id: str, trial: Mapping[str, Any]) -> Any:
    """这次试运行对应的那条验证记录；没有就是 None。

    **不退回「最新的一条」**：那可能是另一次试运行的记录，拿它冒充本次记录会让页面显示错的依据。
    """
    from app.ledger import validation

    if _record_status((trial or {}).get("status")) is None:
        return None
    return await validation.find_record(session, draft_id, _stable_trial_key(trial))


async def _trial_record_brief(session: AsyncSession, record: Any, *,
                              current: Mapping[str, Any] | None) -> dict[str, Any]:
    """试运行响应里的那一段：本次试运行的验证记录 + 覆盖判断（**每个分支形状都一样**）。

    covers_current 是三态：True（还覆盖当前定义/绑定）、False（不覆盖）、None（这次没有可判的记录——还在执行，
    或没有稳定身份，或没有交回记录）。没有记录时也明说一句，不给一个空对象让人猜，也不拿别的记录顶上。
    """
    from app.ledger import review, validation

    if record is None:
        return {"record": None, "covers_current": None,
                "note": "这次试运行还没有留下可对应的验证记录：不能算验证通过"}
    reviews = await review.latest_by_record(session, [record.id], current=current)
    made = validation.as_dict(record, current=current, review=reviews.get(record.id))
    return {"record": made, "covers_current": made["covers_current"],
            "note": "这次试运行的结果只说明这一次执行成什么样；它还算不算覆盖当前的定义与绑定，"
                    "看下面「是否覆盖当前版本」这一项"}


async def _run_trial(*, draft_id: str, workflow_id: str, graph: dict[str, Any], values: dict[str, Any],
                     started_by: str | None, version: int = 1,
                     entry_fields: list[dict[str, Any]] | tuple[dict[str, Any], ...] = ()) -> dict[str, Any]:
    """后台试跑草稿的取数与口径卡，返回 payload["trial"] 的形状（run_id / status / error）。

    必填入口没有值时不跑：试跑是为了「看数对不对」，拿系统编的日期跑出来的数没有意义。这时状态记 needs_input，
    页面上让人把入口字段填了再点试运行（POST 同一个路径）。

    跑的是**探针图**（取数 + 口径卡，`derive.caliber_probe`），不是整张草稿图：草稿图里带着报告撰写节点，
    试跑只关心数对不对，不该为了一次试跑多花一次模型调用、也不该因为没有可用模型就判这份口径不行。

    不复用 trial_caliber：那里要求「已发布」，草稿本来就没发布。来源记 caliber_trial；起来之后先写一次 run_id，
    页面这时就能去看运行；轮询到终态或者等满 TRIAL_TIMEOUT_S，没跑完就照实答 running。
    """
    from app.engine.runner import run_manager
    from app.ledger.derive import NotDerivable, caliber_probe, load_pinned
    from app.tools.datasource import READ_ONLY_OPTION
    from uuid import uuid4

    binding = {**_trial_binding(version, graph, values), "trial_id": uuid4().hex}

    async def failed(status: str, error: str, *, reserve: bool = False) -> dict[str, Any]:
        trial = {**binding, "run_id": "", "status": status, "error": error}
        await _save_trial(draft_id, trial, replace=reserve)
        return trial

    missing = [str(field.get("name")) for field in entry_fields
               if field.get("required") and values.get(str(field.get("name"))) in (None, "")]
    if missing:
        return await failed("needs_input", f"缺少必填输入：{'、'.join(missing)}", reserve=True)

    pending = {**binding, "run_id": "", "status": "running", "error": ""}
    if not await _save_trial(draft_id, pending, replace=True):
        return {**pending, "status": "stale", "error": "口径已修改，请重新试运行"}

    card_id = next((n.get("id") for n in graph.get("nodes") or []
                    if isinstance(n, dict) and n.get("type") == "metrics"), None)
    if not card_id:
        return await failed("failed", "这份草稿没有口径卡，没有可试运行的指标")
    try:
        async with SessionLocal() as read:
            pinned = await load_pinned(read, workflow_id, version, str(card_id))
        target = caliber_probe(pinned).graph
    except NotDerivable as e:
        return await failed("failed", f"口径卡不能派生探针：{e.message}")
    # 参数完整性（ledger/params，和 trial_caliber、组合式问数同一套）：模板里的参数这次没有值（可选、没有缺省值的
    # 也算缺）、引用的字段入口没声明、值承载不了——都在发起运行之前停住，一条 SQL 都不发
    from app.ledger import params

    if problems := params.check_nodes(pinned.fetches, fields=pinned.entry_fields, values=values,
                                     where=f"口径「{pinned.workflow_name}」的入口"):
        return await failed("needs_input", params.describe(problems))

    try:
        run = await run_manager.start(
            graph=target, input_payload=values, run_class="exploratory", origin_kind="caliber_trial",
            origin={**binding, "kind": "caliber_trial", "draft_id": draft_id, "workflow_id": workflow_id,
                    "node_id": str(card_id)},
            options={READ_ONLY_OPTION: True},
            started_by=started_by)
    except Exception as e:  # noqa: BLE001 - 试跑发起失败不该把已经生成的草稿弄没
        logger.exception("草稿 %s 的试运行没有发起", draft_id)
        return await failed("failed", f"试运行没有发起：{e}")
    await _save_trial(draft_id, {**binding, "run_id": run.id, "status": "running", "error": ""})
    row: Run | None = None
    for _ in range(int(TRIAL_TIMEOUT_S / TRIAL_POLL_S)):
        async with SessionLocal() as poll:
            row = await poll.get(Run, run.id)
        if row is None or row.status in TERMINAL_RUN_STATUSES:
            break
        await asyncio.sleep(TRIAL_POLL_S)
    status = _run_status(row.status if row is not None else "running")
    error = str(row.error or "") if row is not None and status == "failed" else ""
    return {**binding, "run_id": run.id, "status": status, "error": error}


async def _trial_metrics(run_id: str) -> list[dict[str, Any]]:
    """试运行和证据面板共享封存核验、遮罩和质量判定。"""
    from app.engine.evidence_reader import read_sealed, metric_cards
    from app.engine.validation import metric_quality
    from app.api.evidence import _Masks, _metric_masks

    try:
        sealed = await read_sealed(run_id)
        if not sealed.trusted:
            return []
        masks = _Masks(sealed)
        out = []
        for node_id, artifact, card in metric_cards(sealed):
            for metric in card.get("metrics") or []:
                if not isinstance(metric, dict) or not metric.get("id"):
                    continue
                _, hidden = await _metric_masks(metric, node_id, artifact, sealed, masks, card=card)
                projected = {k: metric[k] for k in ("id", "name", "unit", "value", "rendered", "decimals", "format", "status")
                             if metric.get(k) is not None}
                if hidden:
                    projected.pop("value", None)
                    projected.update(rendered=hidden, masked=True, quality={"status": "unknown", "checks": [
                        {"code": "masked", "status": "unknown", "detail": "指标值已隐藏，不能在此验证"}]})
                else:
                    projected["quality"] = metric_quality(metric)
                    if card.get("validation_version") != 1:
                        projected["quality"]["checks"].append({"code": "legacy_validation", "status": "unknown",
                                                               "detail": "历史结果缺少完整质量记录，请重新试运行"})
                        if projected["quality"]["status"] == "pass":
                            projected["quality"]["status"] = "unknown"
                out.append(projected)
        return out
    except Exception:
        logger.exception("试运行 %s 的指标证据无法验证", run_id)
        return []


async def _trial_validation(run_id: str, status: str, metrics: list[dict[str, Any]]) -> dict[str, Any]:
    from app.engine.evidence_reader import read_sealed
    from app.engine.validation import validation_report

    trusted = False
    if run_id:
        try:
            trusted = (await read_sealed(run_id)).trusted
        except (LookupError, ValueError, OSError):
            pass
    return validation_report(metrics, execution=status, trusted=trusted)


async def _trial_queries(run_id: str) -> list[dict[str, Any]]:
    """试跑运行里取数节点交回的查询快照：页面据此直接看「这条 SQL 查回来什么」。

    只读封存范围内的查询快照（sealed.queries，和证据接口同一份认法），最多 TRIAL_QUERY_LIMIT 个、每个前 TRIAL_QUERY_ROWS
    行。遮罩列按 _Masks.sets(...).hidden 处理（数据源现在设的加查询当时记下的），格子换成「已遮罩」；masked 列出被遮的
    列名。**不返回 SQL**：SQL 里可能写着遮罩值，试跑页面只需要列和行。

    返回的每一项是 {node_id, tool, source, columns, rows, total_rows, truncated, masked}；读不出来的快照跳过，整个读不出
    封存时就照实给空数组，不编造数据。import 放在函数里：api/evidence 与 api/ledger 互相引用，模块顶层导入会成环。
    """
    from app.api.evidence import MASKED, _Masks
    from app.engine.evidence_reader import read_sealed
    from app.core.artifact_store import load

    try:
        sealed = await read_sealed(run_id)
        if not sealed.trusted:
            return []
        masks = _Masks(sealed)
    except Exception:  # noqa: BLE001 - 证据读不出来就只给状态，不编造结果集
        logger.exception("试跑 %s 的结果集读不出来", run_id)
        return []
    out: list[dict[str, Any]] = []
    for artifact, info in list(sealed.queries.items())[:TRIAL_QUERY_LIMIT]:
        try:
            snap = load(artifact)
            if not isinstance(snap, dict) or not isinstance(snap.get("rows"), list):
                continue
            source = (info or {}).get("source") or snap.get("source")
            columns = [str(column) for column in snap.get("columns") or []]
            if any(not isinstance(row, list) or len(row) != len(columns) for row in snap["rows"]):
                raise ValueError("查询行与列定义不一致，不能安全展示")
            hidden = (await masks.sets(artifact, source, snap)).hidden
            masked = {position for position, column in enumerate(columns) if column.lower() in hidden}
            # 只给前 TRIAL_QUERY_ROWS 行：查询本身被工具截断、或者这里只展示了前几行，都要在 truncated 里说清
            rows = [[MASKED if position in masked else value for position, value in enumerate(row)]
                    if isinstance(row, list) else row
                    for row in snap["rows"][:TRIAL_QUERY_ROWS]]
        except Exception:  # noqa: BLE001 - 这一份快照读不出来、认不出遮罩，跳过它，不影响别的查询
            logger.warning("试跑 %s 的查询快照 %s 读不出来", run_id, artifact, exc_info=True)
            continue
        out.append({
            "node_id": (info or {}).get("node_id"),
            "tool": (info or {}).get("tool"),
            "source": source,
            "columns": columns,
            "rows": rows,
            "total_rows": len(snap["rows"]),
            "truncated": bool(snap.get("truncated")) or len(snap["rows"]) > TRIAL_QUERY_ROWS,
            "source_truncated": bool(snap.get("truncated")),
            "preview_truncated": len(snap["rows"]) > TRIAL_QUERY_ROWS,
            "masked": [columns[position] for position in sorted(masked)],
        })
    return out


def _draft_summary(draft: CaliberDraft, *, graph: dict[str, Any] | None = None) -> dict[str, Any]:
    """列表和详情共用的一份摘要：细状态照实给，另给界面用的四阶段 stage。"""
    from app.apps.tree import node_map
    from app.ledger import docdraft

    payload = draft.payload if isinstance(draft.payload, dict) else {}
    if graph is not None:
        card_id, cards = draft_card(graph)
        card = node_map(graph).get(card_id or (cards[0]["id"] if cards else "")) or {}
        payload = {**payload, "metrics": (card.get("config") or {}).get("metrics") or []}
    metrics = payload.get("metrics")
    definitions = payload.get("semantic_inventory")
    return {"id": draft.id, "title": draft.title, "status": draft.status,
            "stage": docdraft.stage_of(draft.status),
            "metric_count": len(metrics) if isinstance(metrics, list) else 0,
            **({"definition_count": sum(item.get("kind") == "metric" for item in definitions.get("items", []))}
               if isinstance(definitions, dict) else {}),
            "question_count": _question_count(payload), "workflow_id": draft.workflow_id,
            "created_at": draft.created_at.isoformat() if draft.created_at else None, "error": draft.error}


async def _draft_view(session: AsyncSession, draft: CaliberDraft, *, ensure_records: bool = False
                      ) -> dict[str, Any]:
    """一份草稿的完整样子：payload 摊平在上层（界面直接读 metrics / open_questions），另加状态和关联工作流名。

    接管到工作流上的（文档起草建出来的、以及接上一次运行的 docattach 产物）图是唯一真相：画布上改过指标或取数
    SQL，这里从图里取最新的那几项覆盖 payload 里的起草快照。两类都按 taken_over 认（见 app/apps/versions.py），
    所以 docattach 的产物也是从图里读，不退回它接上那一刻的快照。

    另给写入要用的两样（K02）：card_id 是这份草稿的权威口径卡，revision 是客户端要带回来的基准版本
    （Workflow.version）；没有接管到工作流上的老草稿两样都是 None（照旧只改 payload）。

    来源基线（K05 的读取侧）现算现给，而且**会把快照工件真的读一遍**（load 复验哈希）：读得回来才是 known，没有
    这份引用（保存快照之前起草的老草稿、这次落库失败）或工件不在/被改过都是 baseline_unknown（readable=false，
    带上那个工件 id 和原因）。老草稿也照样给这一段——「不知道基线是什么」本身就是要说的话，比不写更安全；反过来，
    payload 里存过的状态不作数，工件坏了就是坏了，读草稿不会因此抛错。不另补、不伪造 B。

    字段基线 B（K05）另给一段 field_baseline：首次接受导入时按 metric_id + field_path 存下的那件工件（值是值、
    引用是引用，逐格带哈希），状态只有 known / baseline_unknown，另带 readable（工件读不读得回来）。人工改过 L 之后
    这一段不变——B 说的是「上次被接受的导入值」，不是「现在长什么样」；工件被删或被改过时照实说 baseline_unknown
    （readable=false），不报成 known，也不把读草稿抛崩。

    验证总情况另给一段 validation（K05）：covers_current（现有记录还覆不覆盖当前定义/绑定）、最新一条的状态、
    审阅结论和计数。verdict 才是结论：没有记录是 none、都不覆盖是 stale、只有独立期望对得上**并且**有认证审阅确认了
    这份记录**全部**指标才是 verified（confirmed / unconfirmed 分别列出确认到和还没确认的指标）。整条记录
    （期望/实际/问题/审阅）走 GET /api/ledger/drafts/{id}/validations。

    已接管的草稿另给 binding（K04 的服务端能力描述）：能按字段改的业务定义、允许值、现在的值、定义和取数实现的
    同步状态（aligned / needs_update / unsupported / unbound）、不支持时的原因和该做什么——工作台照它决定展示哪些
    业务修改。aligned 只说定义和实现同步，不等于验证通过。
    """
    from app.ledger import docdraft, fieldbaseline

    payload = draft.payload if isinstance(draft.payload, dict) else {}
    workflow = await session.get(Workflow, draft.workflow_id) if draft.workflow_id else None
    taken_over = _doc_draft_workflow(workflow)
    card_id, cards = draft_card(workflow.graph or {}) if taken_over else (None, [])
    view = {**payload, **_draft_summary(draft),
            "collection": draft.collection, "datasource_id": draft.datasource_id,
            "origin": draft.origin if isinstance(draft.origin, dict) else {},
            "model_id": draft.model_id or payload.get("model_id"), "created_by": draft.created_by,
            "updated_at": draft.updated_at.isoformat() if draft.updated_at else None,
            "card_id": card_id, "cards": cards, "revision": workflow.version if taken_over else None,
            "workflow_name": workflow.name if workflow is not None else None}
    # 来源基线现算现给（不拿 payload 里存过的那份）：有可还原的快照才是 known，快照被删被改照实说 baseline_unknown
    # （readable=false）。老草稿没有这份引用，同样如实标 unknown，不拿它现在的内容冒充「上次被接受的导入值」。
    view["source_baseline"] = docdraft.source_baseline(payload)
    if taken_over:
        # 这张卡上的业务绑定（K04）：能按字段改什么、允许哪些值、定义和取数实现现在同步到哪一步
        from app.ledger import bindings

        view["binding"] = bindings.capability(workflow.graph or {}, str(card_id))
    if taken_over:
        live = await _draft_graph_payload(session, workflow, workflow.version, draft=draft, payload=payload)
        if live is not None:
            view.update(live)
            view["metric_count"] = len(live["metrics"])
            view["question_count"] = _question_count(view)
    # K05 的两段：字段基线 B 的状态（这次导入被接受成什么、有没有），以及验证总情况（有没有覆盖当前版本的记录、
    # 最新一条、审阅结论）。两段都现算现给：B 不因为人工改过 L 而变，覆盖也不因为旧记录还在就成立。
    if ensure_records:
        # 已经到终态、还没记过的试跑（超时后晚完成的那些）补记一条。只在纯读的入口做（GET 草稿 / 试跑 /
        # 验证列表）：写路径上这会和请求自己的写事务抢锁。
        await _ensure_trial_records(draft, view.get("trial") or {})
    view["field_baseline"] = fieldbaseline.status_of(payload)
    view["validation"] = await _draft_validation_summary(session, draft, workflow=workflow)
    from app.db.models import CaliberDraftJob

    job = await session.get(CaliberDraftJob, draft.id)
    if job:
        view["job"] = {"status": job.status, "attempt": job.attempt, "error": job.error,
                       "retryable": draft.status == "failed" and (not draft.workflow_id or payload.get("finalization_pending", False))
                                    and job.status not in ("queued", "running")}
    return view


def _current_materials(workflow: Workflow | None) -> dict[str, Any] | None:
    """这份草稿**当前**的定义/绑定依据（覆盖判断拿它和记录里的依据比）。读不到图或认不出卡时给 None。

    三样指纹——定义（definition_hash）、绑定（binding_fingerprints）、取数语义（semantic_hash，含 SQL 和数据源）——
    就是「这一版是什么」。布局变更不在里面，所以移动画布不作废验证（4.1）。
    """
    from app.ledger import validation

    graph = workflow.graph if _doc_draft_workflow(workflow) and isinstance(workflow.graph, dict) else None
    if not graph:
        return None
    card_id, cards = draft_card(graph)
    card_id = str(card_id or (cards[0]["id"] if cards else "") or "")
    if not card_id:
        return None
    return validation.materials(graph, card_id, workflow_id=str(getattr(workflow, "id", "") or ""))


async def _draft_records(session: AsyncSession, draft: CaliberDraft, *, workflow: Workflow | None = None
                         ) -> dict[str, Any]:
    """这份草稿的验证记录（新的在前）+ 总情况：列表接口和详情共用同一份读法。"""
    from app.ledger import review, validation

    rows = await validation.list_for_draft(session, draft.id)
    current = _current_materials(workflow)
    reviews = await review.latest_by_record(session, [row.id for row in rows], current=current)
    return {"records": [validation.as_dict(row, current=current, review=reviews.get(row.id)) for row in rows],
            "summary": validation.summarize(rows, current=current, reviews=reviews),
            "method": {"version": validation.METHOD_VERSION, "note": validation.METHOD_NOTE}}


async def _draft_validation_summary(session: AsyncSession, draft: CaliberDraft, *,
                                    workflow: Workflow | None = None) -> dict[str, Any]:
    """草稿详情里那一段「验证总情况」：覆盖判断、最新一条记录、审阅结论（记录本身走验证列表接口）。"""
    return (await _draft_records(session, draft, workflow=workflow))["summary"]


def _graph_entry_fields(graph: dict[str, Any]) -> list[dict[str, Any]]:
    """图里入口节点（input）的字段；草稿页和试跑用它，画布改过的入口字段不会被 payload 的旧快照盖住。"""
    from app.apps.tree import node_map

    start = next((node for node in node_map(graph).values() if node.get("type") == "input"), None)
    return [dict(field) for field in ((start or {}).get("config") or {}).get("fields") or []
            if isinstance(field, dict)]


def _allowed_tables(source: DataSource | None) -> set[str]:
    """这个数据源目录里允许出现的表名；和 _sql_problem 判表在不在目录用的是同一套键。"""
    if source is None:
        return set()
    tables = _source_tables(source)
    allowed = {str(table) for table in tables}
    cache = source.schema_cache if isinstance(source.schema_cache, dict) else {}
    schema = cache.get("schema")
    if isinstance(schema, str) and schema:
        allowed |= {f"{schema}.{table}" for table in tables}
    return allowed


async def _draft_checks(session: AsyncSession, draft: CaliberDraft, payload: dict[str, Any], graph: dict[str, Any],
                        metrics: list[dict[str, Any]], groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """发布前检查：只读地算一份清单，不拦人，也不发起运行。

    五项：图能过发布校验、取数都是单条只读 SELECT 且表在目录里、入口字段与 SQL 占位符一致、试运行成功、
    每条指标都有引用原文。旧草稿没有图时不给 checks，页面整块不画。
    """
    from app.apps.validation import gate_issues
    from app.ledger import draftplan

    checks: list[dict[str, Any]] = []
    detail, graph_ok = "", True
    try:
        issues = await gate_issues(session, graph, "published")
        errors = [issue for issue in issues or [] if issue["level"] == "error"]
        if issues is None:
            raise ValueError("图结构无法解析")
    except Exception as e:  # noqa: BLE001 - 图读不出来也是一项没通过，照实说
        errors = []
        graph_ok = False
        detail = f"图读不出来：{e}"
    if errors:
        graph_ok = False
        detail = "；".join(str(issue["message"]) for issue in errors[:3])
        if len(errors) > 3:
            detail += f"；另有 {len(errors) - 3} 项"
    checks.append({"key": "graph", "label": "图能过发布校验", "ok": graph_ok, "detail": detail})

    source = await session.get(DataSource, draft.datasource_id) if draft.datasource_id else None
    allowed = _allowed_tables(source)
    sql_reasons: list[str] = []
    for group in groups:
        sql = str(group.get("sql") or "")
        if not sql.strip():
            sql_reasons.append("有一组没有取数 SQL")
            continue
        if reason := draftplan._sql_problem(sql, allowed):
            sql_reasons.append(reason)
    sql_detail = "；".join(dict.fromkeys(sql_reasons))
    checks.append({"key": "sql", "label": "取数都是单条只读 SELECT，表在数据目录里",
                   "ok": not sql_reasons, "detail": sql_detail})

    fields = _graph_entry_fields(graph)
    declared = {str(field.get("name")) for field in fields if field.get("name")}
    used: set[str] = set()
    for group in groups:
        used |= set(draftplan._INPUT.findall(str(group.get("sql") or "")))
    missing = sorted(used - declared)
    unused = sorted(declared - used)
    entry_detail: list[str] = []
    if missing:
        entry_detail.append(f"SQL 里用了没声明的入口：{'、'.join(missing)}")
    if unused:
        entry_detail.append(f"声明了但 SQL 没用到的入口：{'、'.join(unused)}")
    checks.append({"key": "entries", "label": "入口字段与 SQL 占位符一致", "ok": not entry_detail,
                   "detail": "；".join(entry_detail)})

    trial = payload.get("trial") if isinstance(payload.get("trial"), dict) else {}
    trial_status = str(trial.get("status") or "")
    trial_detail = ""
    if trial_status == "failed":
        trial_detail = str(trial.get("error") or "试运行没有成功")
    elif trial_status == "needs_input":
        trial_detail = "等补入口字段"
    elif trial_status == "running":
        trial_detail = "试运行还在进行"
    elif trial_status == "stale":
        trial_detail = "口径已修改，请重新试运行"
    elif not trial_status:
        trial_detail = "还没有试运行"
    run_id = str(trial.get("run_id") or "")
    trial_metrics = await _trial_metrics(run_id) if run_id and trial_status == "succeeded" else []
    validation = await _trial_validation(run_id, trial_status, trial_metrics)
    quality_details = "；".join(c["detail"] for c in validation["checks"][:5])
    checks.append({"key": "trial", "label": "试运行结果通过质量检查", "ok": validation["status"] == "pass",
                   "status": validation["status"], "detail": trial_detail or quality_details})

    missing_citations = [str(metric.get("name") or metric.get("id")) for metric in metrics
                         if not (metric.get("definition") or {}).get("citations")]
    checks.append({"key": "citations", "label": "每条指标都有引用原文", "ok": not missing_citations,
                   "detail": "；".join(f"「{name}」没有引用原文" for name in missing_citations[:5])})
    return checks


async def _draft_graph_payload(session: AsyncSession, workflow: Workflow, version: int, *,
                               draft: CaliberDraft | None = None,
                               payload: dict[str, Any] | None = None,
                               card_id: str | None = None) -> dict[str, Any] | None:
    """从草稿工作流的某一版图里取出草稿页要显示的那几项（指标、入口、SQL、groups）。

    图是唯一真相：画布上改了指标名、定义或取数 SQL，草稿页跟着图变，不显示 payload 里起草那一刻的快照。取数节点
    按 SQL 分组，指标按表达式里引用的节点归组；表达式不引用取数节点的（比如只读 vars）归到第一组。图里没有口径卡
    或没有指标时交回空指标列表，不能退回起草时的旧快照。

    card_id 是这次要读的那张口径卡（写入时由 target.card_id 定）；不给就按这张草稿的权威卡（draft_card）。
    """
    from app.apps.tree import node_map

    row = (await session.execute(select(WorkflowVersion).where(
        WorkflowVersion.workflow_id == workflow.id,
        WorkflowVersion.version == int(version)))).scalar_one_or_none()
    if row is None or not isinstance(row.graph, dict):
        return None
    nodes = node_map(row.graph)
    wanted, cards = draft_card(row.graph)
    # 读的时候认不出唯一的权威卡（多张、契约也没说是哪张）就照旧显示第一张：显示不改任何东西，写入那条路上会拒
    wanted = card_id or wanted or (cards[0]["id"] if cards else None)
    card = nodes.get(wanted) if wanted else None
    metrics = [metric for metric in ((card or {}).get("config") or {}).get("metrics") or []
               if isinstance(metric, dict) and metric.get("id")]
    fields = _graph_entry_fields(row.graph)
    fetches: list[tuple[str, str]] = []
    for node_id, node in nodes.items():
        config = node.get("config") if isinstance(node.get("config"), dict) else {}
        args = config.get("args") if isinstance(config.get("args"), dict) else {}
        sql = args.get("sql") if isinstance(args, dict) else None
        if node.get("type") == "tool" and isinstance(sql, str) and sql.strip():
            fetches.append((str(node_id), sql))
    groups: list[dict[str, Any]] = []
    for node_id, sql in fetches:
        pattern = re.compile(rf"nodes\.{re.escape(node_id)}(?![\w])")
        from app.look.calibers import _metric_paths

        ids = [str(metric["id"]) for metric in metrics
               if any(pattern.search(path) for path in _metric_paths(metric) or [])]
        groups.append({"sql": sql, "entry_fields": [dict(field) for field in fields], "metric_ids": ids})
    assigned = {metric_id for group in groups for metric_id in group["metric_ids"]}
    loose = [str(metric["id"]) for metric in metrics if str(metric["id"]) not in assigned]
    if loose:
        if groups:
            groups[0]["metric_ids"].extend(loose)
        else:
            groups = [{"sql": "", "entry_fields": [dict(field) for field in fields], "metric_ids": loose}]
    out = {
        "version": version,
        "metrics": metrics,
        "entry_fields": [dict(field) for field in fields],
        "sql": "\n\n".join(sql for _node, sql in fetches),
        "groups": groups,
        "blocks": len(fetches) or 1,
    }
    if draft is not None and payload is not None:
        out["trial"] = await _trial_view(session, payload.get("trial") or {}, version=version, graph=row.graph)
        out["checks"] = await _draft_checks(session, draft, {**payload, "trial": out["trial"]}, row.graph, metrics, groups)
    return out


async def _draft_or_404(session: AsyncSession, draft_id: str) -> CaliberDraft:
    draft = await session.get(CaliberDraft, draft_id)
    if draft is None:
        raise HTTPException(404, "口径草稿不存在，可能已被删除")
    return draft


# --------------------------------------------------------------------------
# 卡级身份与写入保护（K02）：卡是明确的，不按「第一张」猜；写要带基准版本和幂等键
# --------------------------------------------------------------------------

#: 卡级定位与幂等冲突的机器码（STALE_REVISION / REVISION_REQUIRED 在 apps/versions.py，和这里一起构成 K02 的契约）
CARD_REQUIRED = "card_required"
CARD_NOT_FOUND = "card_not_found"
TARGET_MISMATCH = "target_mismatch"
IDEMPOTENCY_CONFLICT = "idempotency_conflict"
#: 字段变更（K03）的机器码：同一次请求里既整份换指标、消问题，又按字段改（一次请求只落一版图）；这份草稿还没有图
CHANGE_CONFLICT = "change_conflict"
CHANGE_UNAVAILABLE = "change_unavailable"
#: 草稿改完之后对不上目标卡
DRAFT_CARD_GONE = "这份草稿的口径卡不见了（可能刚被删掉）：请刷新页面再看一次"


def editable_cards(graph: dict[str, Any]) -> list[dict[str, str]]:
    """图里能写的一张张口径卡：[{id, title}]，按节点出现的顺序。

    能写的是源头口径卡（metrics 节点、没有 caliber_from）：转抄别处的口径卡不是权威写入源（A01），改它等于在别处
    另存一份定义。
    """
    from app.apps.tree import node_map

    return [{"id": node_id, "title": str(node.get("title") or node_id)}
            for node_id, node in node_map(graph).items()
            if node.get("type") == "metrics" and not (node.get("config") or {}).get("caliber_from")]


def contract_card(graph: dict[str, Any]) -> str | None:
    """下游契约声明从哪张卡取指标（metrics_from 只写了一张时就是它）：多张卡时按它认这张草稿的权威卡。"""
    for node in graph.get("nodes") or []:
        config = ((node or {}).get("data") or {}).get("config") if isinstance(node, dict) else None
        contract = config.get("contract") if isinstance(config, dict) else None
        if not isinstance(contract, dict):
            continue
        sources = contract.get("metrics_from")
        ids = [sources] if isinstance(sources, str) else [s for s in sources or [] if isinstance(s, str)]
        if len(ids) == 1:
            return ids[0]
    return None


def draft_card(graph: dict[str, Any]) -> tuple[str | None, list[dict[str, str]]]:
    """这份草稿的权威口径卡，另交回全部能写的卡。

    一张能写的卡时就是它；多张时认下游契约声明的那一张——不是「第一张」。都认不出来时交回 None：写的时候必须显式
    给 target.card_id，读的时候页面也照实列候选。
    """
    cards = editable_cards(graph)
    if len(cards) == 1:
        return cards[0]["id"], cards
    referenced = contract_card(graph)
    if referenced and any(card["id"] == referenced for card in cards):
        return referenced, cards
    return None, cards


def _target_card(payload: "DraftPatchIn", workflow: Workflow) -> str | None:
    """这次 PATCH 写哪张卡：给了 target.card_id 就按它（必须在图上、是可写的那种），没给就按这张草稿的权威卡。

    target.workflow_id 只要给了就必须是这份草稿自己的——不符就回 409 target_mismatch，**没给 card_id 也一样**：
    指到别的工作流上，后面按卡定位的那几步就成了「拿着另一个工作流的名字在本图的卡里找」，先点破比按 id 撞库好。
    多张可写的卡又没写明显式目标时拒（409 card_required），不做「取第一张」；指到的卡不在图上（或者指到了转抄卡）
    拒（409 card_not_found）。
    """
    target = payload.target
    if target is not None and target.workflow_id and target.workflow_id != workflow.id:
        raise CodedHTTPException(409, f"要改的口径卡不在这份草稿的工作流上（现在这份是「{workflow.name}」）",
                                 TARGET_MISMATCH, field="target")
    cards = editable_cards(workflow.graph or {})
    wanted = target.card_id if target is not None else None
    if wanted:
        if not any(card["id"] == wanted for card in cards):
            raise CodedHTTPException(
                409, f"图上没有这张可写的口径卡：{wanted}。可写的卡是源头口径卡（转抄别处的口径卡不能在这里改）",
                CARD_NOT_FOUND, field="target")
        return wanted
    if len(cards) == 1:
        return cards[0]["id"]
    if not cards:
        raise CodedHTTPException(409, "当前画布没有可编辑的口径卡，请先在画布上添加", CARD_NOT_FOUND)
    raise CodedHTTPException(409, "这张草稿的图上有好几张可写的口径卡：请指明要改哪一张（target.card_id）",
                             CARD_REQUIRED, field="target", extra={"cards": cards})


async def _sync_draft_graph(session: AsyncSession, draft: CaliberDraft, payload_data: dict[str, Any],
                            *, card_id: str, expected_revision: int | None) -> None:
    """在当前图上修改这一张口径卡，再新增版本；取数节点、SQL、入口和用户添加的编排保持其身份。"""
    from app.apps.versions import STALE_REVISION, WorkflowChanged, save_graph

    workflow = await session.get(Workflow, draft.workflow_id) if draft.workflow_id else None
    metrics = payload_data.get("metrics")
    if not _doc_draft_workflow(workflow) or not isinstance(metrics, list):
        return
    graph = copy.deepcopy(workflow.graph)
    card = next((n for n in graph.get("nodes") or [] if isinstance(n, dict) and n.get("id") == card_id), None)
    if card is None or card.get("type") != "metrics" or (card.get("data", {}).get("config") or {}).get("caliber_from"):
        raise CodedHTTPException(409, DRAFT_CARD_GONE, CARD_NOT_FOUND)
    config = card.setdefault("data", {}).setdefault("config", {})
    old_ids = {m.get("id") for m in config.get("metrics") or [] if isinstance(m, dict)}
    new_ids = [m.get("id") for m in metrics]
    if any(not isinstance(mid, str) or not mid.strip() for mid in new_ids) or len(set(new_ids)) != len(new_ids):
        raise ValueError("每条指标需要非空且唯一的 ID")
    config["metrics"] = copy.deepcopy(metrics)
    removed = old_ids - set(new_ids)
    for node in graph.get("nodes") or []:
        contract = node.get("data", {}).get("config", {}).get("contract")
        if not isinstance(contract, dict):
            continue
        sources = contract.get("metrics_from") or []
        if card_id not in ([sources] if isinstance(sources, str) else sources):
            continue
        for key in ("required", "expected"):
            if isinstance(contract.get(key), list):
                before = contract[key]
                contract[key] = list(new_ids) if key == "required" and set(before) == old_ids \
                    else [mid for mid in before if mid not in removed]
    try:
        await save_graph(session, workflow, graph, note="编辑口径草稿", expected_revision=expected_revision)
    except WorkflowChanged as e:
        raise CodedHTTPException(409, str(e), STALE_REVISION,
                                 extra={"current_version": getattr(e, "current", workflow.version)}) from None
    await session.flush()
    live = await _draft_graph_payload(session, workflow, workflow.version, card_id=card_id)
    if live is not None:
        payload_data.update(live)


async def _sync_draft_changes(session: AsyncSession, draft: CaliberDraft, payload_data: dict[str, Any], *,
                              card_id: str, expected_revision: int | None,
                              operations: list["DraftOperationIn"]) -> "changes.Change":
    """按字段操作改这一张口径卡，再新增版本（K03）：改在**原图的深拷贝**上，没提到的内容原样带着走。

    和整份换指标那条路同一条并发规矩（K02 的 CAS：expected_revision 对不上或者别人先提交了，回 409 stale_revision）。
    整批操作先在拷贝上跑完：任何一条不合规、或者指不到指标，都在写之前停住——图一个字节都不动，也不新增版本。
    """
    from app.apps.versions import STALE_REVISION, WorkflowChanged, save_graph
    from app.ledger import changes
    from app.ledger.repository import CaliberGone

    workflow = await session.get(Workflow, draft.workflow_id) if draft.workflow_id else None
    if not _doc_draft_workflow(workflow):
        raise CodedHTTPException(409, "这份草稿还没接管到工作流上（没有可改的图）：字段操作落不到图上，"
                                      "请先起草或接上一次运行", CHANGE_UNAVAILABLE)
    try:
        change = changes.apply_operations(workflow.graph or {}, card_id=str(card_id),
                                          operations=[op.model_dump(exclude_unset=True) for op in operations])
    except changes.ChangeRefused as e:
        extra = {key: value for key, value in (("operation", e.operation), ("path", e.path)) if value is not None}
        raise CodedHTTPException(422, e.message, e.code, field="operations", extra=extra) from None
    except CaliberGone:  # 目标卡在这中间没了或者成了转抄卡：和整份换指标那条路同一句话、同一个码
        raise CodedHTTPException(409, DRAFT_CARD_GONE, CARD_NOT_FOUND,
                                 field="target") from None
    try:
        await save_graph(session, workflow, change.graph, note="改口径字段", expected_revision=expected_revision)
    except WorkflowChanged as e:
        raise CodedHTTPException(409, str(e), STALE_REVISION,
                                 extra={"current_version": getattr(e, "current", workflow.version)}) from None
    await session.flush()
    live = await _draft_graph_payload(session, workflow, workflow.version, card_id=card_id)
    if live is not None:
        payload_data.update(live)
    return change


def _doc_title(docs: list[Document]) -> str:
    """草稿标题取第一份文档的名字；多份文档写明还有几份。"""
    first = docs[0]
    title = (first.title or first.source or first.id).strip() or first.id
    return (f"{title}等 {len(docs)} 份文档" if len(docs) > 1 else title)[:200]


#: 一次起草最多续跑几轮：模型一次尽量交多组，一组交不下时按 uncovered 再续，最多 4 轮。
#: 以前一轮只收一组、放到 10 轮；现在多组一次交，轮数只留给「这一轮写不下」的余量。
MAX_ROUNDS = 4
#: 一张草稿图最多收几条指标：再多，一张口径卡和一次试跑都读不动
MAX_METRICS = 60


#: 问题去重时取第一句多长：同一件事每轮换个说法，前面那句「问的是什么」通常一样，后面才是补充
_QUESTION_KEY = 30
_CLAUSE = "，,。；;：:！!？?"
_PUNCT = set(" \t\r\n，。；：、！？,.!?;:'\"（）()[]【】《》<>-_/\\|`~")


async def _extract_in_background(draft_id: str) -> None:
    from app.ledger.authoring import extract

    await extract(draft_id, run_trial=_run_trial, save_trial=_save_trial, trial_binding=_trial_binding)


from app.ledger import jobs as draft_jobs

draft_jobs.executor = _extract_in_background


class DraftExtractIn(BaseModel):
    """从哪几份文档起草、起草时给模型看的附加要求。数据源必须给：它的表结构和目录是 SQL 的唯一依据。

    fixed_filters 是「每次跑都不变的条件」（工厂、产线、属性组）：写死在 SQL 里，不做成入口字段。
    入口只留「每次跑都可能不同」的东西（时间范围，最多再一个业务维度），否则每问一次数都要填一遍编码。
    """

    documents: list[str] = Field(min_length=1, max_length=20)
    collection: str | None = Field(default=None, max_length=100)
    datasource_id: str | None = Field(default=None, max_length=32)
    guidance: str = Field(default="", max_length=2000)
    fixed_filters: str = Field(default="", max_length=1000)


@router.post("/drafts/definitions", status_code=201)
async def import_definitions(payload: DraftExtractIn, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """Import source definitions first. No model calls, SQL execution, or publication."""
    draft = await _definition_draft(payload, session)
    session.add(draft)
    await session.commit()
    return {"draft_id": draft.id, "status": draft.status}


async def _definition_draft(payload: DraftExtractIn, session: AsyncSession) -> CaliberDraft:
    from app.api.deps import current_principal, ensure_role
    from app.ledger.semantic_import import inventory
    from app.core.artifact_store import put_json

    ensure_role("builder")
    docs = []
    for doc_id in dict.fromkeys(payload.documents):
        doc = await session.get(Document, doc_id)
        if doc is None:
            raise HTTPException(422, "文档不存在，可能已被删除")
        docs.append(doc)
    documents = [{"document_id": d.id, "title": d.title or d.source, "content": d.content or ""} for d in docs]
    catalog = inventory(documents)
    if not catalog["items"]:
        raise HTTPException(422, "没有识别到指标定义表。请使用以「指标／量／符号」为首列的 Markdown 表格，或使用原有 AI 起草入口")
    if payload.datasource_id and await session.get(DataSource, payload.datasource_id) is None:
        raise HTTPException(422, "数据源不存在")
    artifact = await put_json({"documents": documents, "inventory": catalog}, kind="semantic_definitions")
    draft = CaliberDraft(title=_doc_title(docs), collection=payload.collection or docs[0].collection or "default",
                         datasource_id=payload.datasource_id, status="path_pending",
                         payload={"semantic_inventory": catalog, "definition_artifact": artifact,
                                  "documents": [{"document_id": d.id, "title": d.title, "chunk_ids": []} for d in docs],
                                  "metrics": [], "open_questions": [], "selected_definition_ids": []},
                         origin={"documents": list(dict.fromkeys(payload.documents)), "guidance": payload.guidance,
                                 "fixed_filters": payload.fixed_filters}, created_by=current_principal().username)
    return draft


@router.get("/drafts/{draft_id}/migration")
async def migration_audit(draft_id: str, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    from app.api.deps import ensure_role
    from app.ledger.migration import audit

    ensure_role("builder")
    draft = await _draft_or_404(session, draft_id)
    workflow = await session.get(Workflow, draft.workflow_id) if draft.workflow_id else None
    if workflow is None:
        raise HTTPException(409, "这份草稿尚未绑定取数图，请先从定义清单实现指标")
    available = {s.name for s in (await session.execute(select(DataSource))).scalars() if s.enabled and _source_tables(s)}
    result = audit(workflow.graph or {}, workflow_id=workflow.id, version=workflow.version, sources=available)
    result["can_rebuild"] = bool((draft.origin or {}).get("documents") or (draft.payload or {}).get("documents"))
    return result


class MigrationIn(BaseModel):
    expected_revision: int = Field(ge=1)


@router.post("/drafts/{draft_id}/migration", status_code=201)
async def prepare_migration(draft_id: str, payload: MigrationIn,
                            session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """Create an independent source-definition draft; no old graph/SQL/publication is changed."""
    from app.api.deps import ensure_role

    ensure_role("builder")
    old = await _draft_or_404(session, draft_id)
    workflow = await session.get(Workflow, old.workflow_id) if old.workflow_id else None
    if workflow is None or workflow.version != payload.expected_revision:
        raise HTTPException(409, "旧口径已变化，请重新检查后准备迁移")
    for existing in (await session.execute(select(CaliberDraft).where(CaliberDraft.status != "rejected"))).scalars():
        migration = (existing.origin or {}).get("migration_from", {})
        if migration.get("draft_id") == old.id and migration.get("revision") == workflow.version:
            return {"draft_id": existing.id, "status": existing.status}
    origin = old.origin or {}
    docs = origin.get("documents") or [d["document_id"] for d in (old.payload or {}).get("documents", [])]
    if not docs:
        raise HTTPException(409, "旧口径没有来源文档，请先导入定义文档，或在原草稿中逐项补齐问数设置")
    source = await session.get(DataSource, old.datasource_id) if old.datasource_id else None
    draft = await _definition_draft(DraftExtractIn(
        documents=docs, datasource_id=source.id if source else None,
        guidance=str(origin.get("guidance") or "")[:2000], fixed_filters=str(origin.get("fixed_filters") or "")[:1000]), session)
    draft.title = f"{old.title} · 迁移定义"
    draft.origin = {**draft.origin, "migration_from": {"draft_id": old.id, "workflow_id": workflow.id,
                                                     "revision": workflow.version}}
    # Preserve the exact old graph as review material, never as executable fallback or formula authority.
    from app.core.artifact_store import put_json
    draft.payload = {**draft.payload, "migration_baseline": await put_json(
        {"graph": workflow.graph, "workflow_id": workflow.id, "revision": workflow.version}, kind="migration_baseline")}
    session.add(draft)
    await session.commit()
    return {"draft_id": draft.id, "status": draft.status}


class ImplementDefinitionsIn(BaseModel):
    selected: list[str] | None = Field(default=None, min_length=1, max_length=2000)
    datasource_id: str = Field(min_length=1, max_length=32)


@router.post("/drafts/{draft_id}/implement", status_code=202)
async def implement_definitions(draft_id: str, payload: ImplementDefinitionsIn, background: BackgroundTasks,
                                session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    from app.api.deps import ensure_role
    from app.db.models import CaliberDraftJob
    from sqlalchemy import update

    ensure_role("builder")
    draft = await _draft_or_404(session, draft_id)
    stored = dict(draft.payload or {})
    catalog = stored.get("semantic_inventory") or {}
    from app.ledger.semantic_import import source_matches

    docs = [await session.get(Document, doc_id) for doc_id in catalog.get("document_hashes", {})]
    if any(doc is None or doc.status != "ready" for doc in docs) or not source_matches(
        catalog, [{"document_id": doc.id, "content": doc.content} for doc in docs if doc is not None]
    ):
        raise HTTPException(409, "原文已更新或尚未处理完成，请重新导入定义后再实现")
    from app.ledger.semantic_import import implementation_scope

    try:
        selected, issues = implementation_scope(catalog, payload.selected)
    except ValueError as e:
        raise HTTPException(422, str(e)) from None
    if not selected:
        raise HTTPException(422, "没有可自动实现的指标，请先补充数据来源或确认同名口径")
    source = await session.get(DataSource, payload.datasource_id)
    try:
        require_authoring_source(source)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    stored["selected_definition_ids"] = selected
    stored["implementation_mode"] = "all" if payload.selected is None else "selected"
    stored["definition_issues"] = issues
    stored["implementation_progress"] = {"total": len(selected), "completed": 0, "pending": len(selected),
                                         "blocked": 0, "active_names": []}
    result = await session.execute(update(CaliberDraft).where(
        CaliberDraft.id == draft_id, CaliberDraft.status == "path_pending", CaliberDraft.workflow_id.is_(None)
    ).values(status="drafting", datasource_id=source.id, payload=stored))
    if result.rowcount != 1:
        raise HTTPException(409, "这份定义已开始生成，请刷新查看进度")
    session.add(CaliberDraftJob(id=draft_id))
    await session.commit()
    background.add_task(draft_jobs.start, draft_id)
    return {"draft_id": draft_id, "status": "drafting"}


@router.post("/drafts/extract", status_code=201)
async def draft_extract(payload: DraftExtractIn, background: BackgroundTasks,
                        session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """从业务文档起草一份口径（app/ledger/draftplan.py）：先建一行 drafting 立刻返回，模型在后台跑。

    文档必须都存在（缺的回 422，一个草稿都不建）；数据源必须给、必须存在，而且已经探查到表结构——SQL 只能对照真实目录
    写，没有目录时让模型猜表，图跑不了。模型产出定义 + SQL + 入口字段后由服务端校验收成草稿图和 v1，后台自动只读试跑。
    起草完的 payload / status / trial 用 GET 看。"""
    from app.api.deps import current_principal, ensure_role

    ensure_role("builder")
    doc_ids = list(dict.fromkeys(str(d) for d in payload.documents if str(d).strip()))
    if not doc_ids:
        raise HTTPException(422, "至少要给一份文档")
    if not payload.datasource_id:
        raise HTTPException(422, "生成取数 SQL 需要数据源：请选一个已经探查过表结构的数据源")
    found = {doc.id: doc for doc in (await session.execute(
        select(Document).where(Document.id.in_(doc_ids)))).scalars()}
    missing = [doc_id for doc_id in doc_ids if doc_id not in found]
    if missing:
        raise HTTPException(422, f"文档不存在，可能已被删除：{'、'.join(missing)}")
    source = await session.get(DataSource, payload.datasource_id)
    try:
        require_authoring_source(source)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    docs = [found[doc_id] for doc_id in doc_ids]
    draft = CaliberDraft(title=_doc_title(docs),
                         collection=payload.collection or (docs[0].collection or "default"),
                         datasource_id=payload.datasource_id or None, status="drafting", payload={},
                         origin={"documents": doc_ids, "guidance": payload.guidance.strip(),
                                 "fixed_filters": payload.fixed_filters.strip()},
                         created_by=current_principal().username)
    session.add(draft)
    await session.flush()
    from app.db.models import CaliberDraftJob

    session.add(CaliberDraftJob(id=draft.id))
    await session.commit()
    background.add_task(draft_jobs.start, draft.id)
    return {"draft_id": draft.id, "status": draft.status}


@router.get("/drafts")
async def list_drafts(status: str | None = Query(default=None, description="按状态筛：drafting / ready / "
                                                   "questions_open / path_pending / attached / rejected / failed"),
                      stage: str | None = Query(default=None, description="按界面阶段筛：drafting / review / "
                                              "discarded / failed"),
                      session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """口径草稿列表，最近改过的在前。只给计数、细状态和界面阶段，正文用 GET /api/ledger/drafts/{id} 看。

    细状态（status）是接口兼容用的；页面按四种阶段（stage）筛：起草中、待审、已丢弃、失败。ready / questions_open /
    path_pending / attached 都归「待审」。
    """
    from app.api.deps import ensure_role
    from app.ledger import docdraft

    ensure_role("builder")
    query = select(CaliberDraft, Workflow).outerjoin(Workflow, Workflow.id == CaliberDraft.workflow_id) \
        .order_by(CaliberDraft.updated_at.desc(), CaliberDraft.id.desc())
    if status:
        if status not in docdraft.STATUSES:
            raise HTTPException(422, f"不认识的状态：{status}")
        query = query.where(CaliberDraft.status == status)
    if stage:
        if stage not in docdraft.STAGES:
            raise HTTPException(422, f"不认识的阶段：{stage}")
        if stage == "review":
            query = query.where(CaliberDraft.status.in_(("ready", "questions_open", "path_pending", "attached")))
        elif stage == "discarded":
            query = query.where(CaliberDraft.status == "rejected")
        else:
            query = query.where(CaliberDraft.status == stage)
    rows = (await session.execute(query)).all()
    return {"drafts": [_draft_summary(draft, graph=workflow.graph if _doc_draft_workflow(workflow) else None)
                       for draft, workflow in rows]}


@router.get("/drafts/{draft_id}")
async def get_draft(draft_id: str, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """一份草稿的完整内容：指标（含引用和 span）、取数 SQL、入口字段、试跑状态、丢弃原因、未决问题、和已发布口径的对齐
    结果、关联工作流名；另给 K05 的两段：字段基线 B 的状态（field_baseline）和验证总情况（validation：覆盖判断、
    最新一条记录、审阅结论——记录本身走 GET /drafts/{id}/validations）。"""
    from app.api.deps import ensure_role

    ensure_role("builder")
    return await _draft_view(session, await _draft_or_404(session, draft_id), ensure_records=True)


@router.get("/drafts/{draft_id}/trial")
async def get_draft_trial(draft_id: str, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """这份草稿的试跑：{run_id, status, metrics: [{id, name, rendered, value}], queries, error}。

    状态只有 succeeded / failed / running 三种；指标值读试跑运行封存的口径卡（和口径试跑同一个读法），queries 是取数节点
    交回的查询快照（前几行、遮罩列换成「已遮罩」，不含 SQL）。还没试跑（老草稿、起草失败）回 404。只读，不发起运行。

    另在 validation 里并进**这一次**试跑对应的验证记录（K05）：状态、期望/实际、覆盖判断和审阅结论。每个分支的
    validation 形状一致（{schema_version, execution, status, checks, record, covers_current, note}），没有记录时
    record 是 null、covers_current 是 null（三态：true / false / null 判不了），**不拿别的最新记录顶上**。响应原有的
    键一个不改；试运行超时、晚些才完成且有稳定身份的那些在这里补记一条记录（见 _ensure_trial_records）。
    """
    from app.api.deps import ensure_role

    ensure_role("builder")
    draft = await _draft_or_404(session, draft_id)
    payload = draft.payload if isinstance(draft.payload, dict) else {}
    trial = payload.get("trial") if isinstance(payload.get("trial"), dict) else {}
    workflow = await session.get(Workflow, draft.workflow_id) if draft.workflow_id else None
    if _doc_draft_workflow(workflow):
        trial = await _trial_view(session, trial, version=workflow.version, graph=workflow.graph)
    await _ensure_trial_records(draft, trial)
    brief = await _trial_record_brief(session, await _trial_record(session, draft.id, trial),
                                      current=_current_materials(workflow))
    if trial.get("status") == "stale":
        return {**trial, "metrics": [], "queries": [],
                "validation": {**await _trial_validation("", "stale", []), **brief}}
    run_id = trial.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        if trial.get("status") in ("needs_input", "running"):
            return {**trial, "metrics": [], "queries": [],
                    "validation": {**await _trial_validation("", str(trial.get("status") or ""), []), **brief}}
        raise HTTPException(404, "这份草稿还没有试运行")
    run = await session.get(Run, run_id)
    if run is None:
        return {"run_id": run_id, "status": _run_status(trial.get("status")), "metrics": [], "queries": [],
                "validation": {**await _trial_validation("", _run_status(trial.get("status")), []), **brief},
                "error": str(trial.get("error") or "")}
    status = _run_status(run.status)
    metrics = await _trial_metrics(run.id) if status == "succeeded" else []
    queries = await _trial_queries(run.id) if status == "succeeded" else []
    return {**trial, "run_id": run.id, "status": status, "metrics": metrics, "queries": queries,
            "validation": {**await _trial_validation(run.id, status, metrics), **brief},
            "error": str(run.error or "")}


class DraftExpectIn(BaseModel):
    """这次试跑要对着比的**固定期望**（K05）：独立于这次执行的一方给的标准答案。

    metrics 是 metric_id → 期望的原值，要**逐条给全卡指标**（全指标比较，漏一条会拒）；source 说明这份期望是谁给的
    （human_confirmed 人工确认 / fixture 固定夹具 / independent_computation 独立计算），**不能拿当前 SQL 的结果
    抄一份当标准答案**。expected 原值比较、不做隐式舍入（R06）。
    """

    model_config = ConfigDict(extra="forbid")

    metrics: dict[str, Any]
    source: str = Field(default="human_confirmed", max_length=40)
    sample_id: str | None = Field(default=None, max_length=120)
    sample_version: str | None = Field(default=None, max_length=40)
    note: str | None = Field(default=None, max_length=500)


class DraftTrialIn(BaseModel):
    inputs: dict[str, Any] = Field(default_factory=dict)
    #: 不带期望也能跑：那样这一条只记 observed，不能算验证通过（见 app/ledger/validation.py）
    expect: DraftExpectIn | None = None


def _checked_expectation(expect: Mapping[str, Any], graph: dict[str, Any]) -> dict[str, Any]:
    """校验这次试跑带的固定期望：指标必须是这张卡上的、要逐条给全，来源只能是三种之一。不合规回 422，不发起运行。"""
    from app.ledger import validation

    card_id, cards = draft_card(graph)
    card_id = str(card_id or (cards[0]["id"] if cards else "") or "")
    metrics = validation.card_metrics(graph, card_id)
    known = [str(metric["id"]) for metric in metrics]
    given = expect.get("metrics") if isinstance(expect.get("metrics"), Mapping) else {}
    unknown = sorted(str(mid) for mid in given if str(mid) not in known)
    if unknown:
        raise HTTPException(422, f"期望里有这张卡上没有的指标：{'、'.join(unknown)}")
    missing = [mid for mid in known if mid not in {str(mid) for mid in given}]
    if missing:
        raise HTTPException(422, f"期望要逐条给全卡指标，还缺：{'、'.join(missing)}"
                                 "（全指标比较：只给一部分不能算通过）")
    source = expect.get("source")
    if source is not None and str(source) not in validation.EXPECTATION_SOURCES:
        raise HTTPException(422, f"期望来源只能是：{'、'.join(validation.EXPECTATION_SOURCES)}")
    return expect


@router.post("/drafts/{draft_id}/trial")
async def run_draft_trial(draft_id: str, payload: DraftTrialIn,
                          session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """按填好的入口字段试跑一次（builder 及以上）。

    自动试跑缺必填输入时停在 needs_input（不拿系统编的日期跑，那样跑出来的数没有意义），人在这里补上再跑。
    参数按入口字段校验，和正式运行同一套（apps/forms）；跑的仍是探针图：只取数与口径卡，不写报告、不进统计。成功时另给
    queries（取数节点交回的查询快照，前几行、遮罩列换成「已遮罩」，不含 SQL）；没有运行、失败或还在跑时为空数组。

    参数完整性（缺参数、未声明参数、值承载不了）按 ledger/params 同一套判：不合规时试跑**不发起**，状态记 needs_input
    并把原因写在 error 里（页面那一块就显示这句），一条 SQL 都不会发出去。

    带 expect（固定期望，K05）时：期望里的指标必须是这张卡上的、要逐条给全（不合规回 422，不发起运行）；跑完按
    ledger/validation 的方法版本 1 比一遍（原值、全指标、全约定形状），结果连同这次的定义/绑定依据追加成一条验证
    记录。不带 expect 也能跑，但那样这条只记 observed——没有独立期望就不能算验证通过。
    """
    from app.api.deps import current_principal, ensure_role
    from app.apps.forms import input_schema, validate_form
    from app.ledger import params

    ensure_role("builder")
    draft = await _draft_or_404(session, draft_id)
    body = draft.payload if isinstance(draft.payload, dict) else {}
    fields = [field for field in body.get("entry_fields") or [] if isinstance(field, dict)]
    workflow_id = draft.workflow_id or body.get("workflow_id")
    if not workflow_id:
        raise HTTPException(409, "这份草稿还没有取数图，不能试运行")
    workflow = await session.get(Workflow, str(workflow_id))
    version = workflow.version if _doc_draft_workflow(workflow) else int(body.get("version") or 1)
    row = (await session.execute(select(WorkflowVersion).where(
        WorkflowVersion.workflow_id == workflow_id, WorkflowVersion.version == version))).scalar_one_or_none()
    if row is None or not isinstance(row.graph, dict):
        raise HTTPException(409, "找不到这份草稿的取数图，可能已被删除")
    # 文档起草的草稿以图为准：画布上改过入口字段之后，试跑按图里的字段校验，不能拿 payload 的旧快照
    if _doc_draft_workflow(workflow):
        fields = _graph_entry_fields(row.graph)
    unknown = sorted(set(payload.inputs) - {str(field.get("name")) for field in fields})
    if unknown:
        raise HTTPException(422, f"不认识的入口字段：{'、'.join(unknown)}")
    expect = payload.expect.model_dump(exclude_none=True) if payload.expect is not None else None
    if expect is not None:
        expect = _checked_expectation(expect, row.graph)
    values: dict[str, Any] = {str(field.get("name")): field.get("default") for field in fields
                              if field.get("default") not in (None, "")}
    # 和 trial_caliber 同一份收值（params.provided）：显式空串算没给，字段没声明的缺省值就是缺参数
    values.update(params.provided(payload.inputs))
    if errors := validate_form(input_schema(fields), values):
        raise HTTPException(422, "；".join(str(e.get("message")) for e in errors[:5]))
    trial = await _run_trial(draft_id=draft.id, workflow_id=str(workflow_id), graph=row.graph,
                             values=values, started_by=current_principal().username, entry_fields=fields, version=version)
    await _save_trial(draft.id, trial, expect=expect)
    brief = await _trial_record_brief(session, await _trial_record(session, draft.id, trial),
                                      current=_current_materials(workflow))
    run_id = str(trial.get("run_id") or "")
    queries = await _trial_queries(run_id) if run_id and trial.get("status") == "succeeded" else []
    metrics = await _trial_metrics(run_id) if run_id and trial.get("status") == "succeeded" else []
    return {**trial, "metrics": metrics, "queries": queries,
            "validation": {**await _trial_validation(run_id, str(trial.get("status") or ""), metrics), **brief}}


#: 审阅决定的审计（subject 是那条验证记录）：谁在什么依据上确认或驳回了哪一次验证
REVIEW_ACTION = "ledger.validation.review"
REVIEW_SUBJECT_KIND = "caliber_validation_record"
#: 没有能记下来的主体时不做审阅：审批人必须来自服务端身份，不能由模型或请求体代写
REVIEW_ACTOR_REQUIRED = "review_actor_required"


@router.get("/drafts/{draft_id}/validations")
async def list_draft_validations(draft_id: str, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """这份草稿的验证记录（K05，新的在前）+ 总情况 + 字段基线状态：K06 读的那份契约。

    每条记录给 covers_current（还覆盖不覆盖**当前**定义/绑定）、expected / actual / status，以及这条上最新的审阅
    （confirmed / rejected，附认证主体与确认范围）。`summary.verdict` 才是结论：没有记录是 none、记录都不覆盖当前
    版本是 stale、只有独立期望对得上**并且**有认证审阅确认了这份记录**全部**指标才算 verified——**样例匹配本身不是
    批准，只确认一部分也不是**（`summary.confirmed` / `summary.unconfirmed` 分别列出确认到的和还没确认的指标）。

    读之前会先把已经到终态、有稳定身份、还没记过的试运行补记一条（超时、晚些才完成的那些）；没有身份的旧记录不补、
    只读不改；记录只追加，不改写旧的。
    """
    from app.api.deps import ensure_role
    from app.ledger import fieldbaseline

    ensure_role("builder")
    draft = await _draft_or_404(session, draft_id)
    workflow = await session.get(Workflow, draft.workflow_id) if draft.workflow_id else None
    payload = draft.payload if isinstance(draft.payload, dict) else {}
    trial = payload.get("trial") if isinstance(payload.get("trial"), dict) else {}
    if _doc_draft_workflow(workflow):
        trial = await _trial_view(session, trial, version=workflow.version, graph=workflow.graph)
    await _ensure_trial_records(draft, trial)
    return {"draft_id": draft.id, "field_baseline": fieldbaseline.status_of(payload),
            **await _draft_records(session, draft, workflow=workflow)}


class DraftReviewIn(BaseModel):
    """对一条验证记录的审阅决定（K05）：只收决定、范围和说明——**不收 actor**，审批人取服务端身份。"""

    model_config = ConfigDict(extra="forbid")

    decision: str = Field(min_length=1, max_length=20)
    note: str | None = Field(default=None, max_length=500)
    metrics: list[str] | None = Field(default=None, max_length=60)


@router.post("/drafts/{draft_id}/validations/{record_id}/review", status_code=201)
async def review_validation(draft_id: str, record_id: str, payload: DraftReviewIn,
                            session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """对一条验证记录做一次审阅（builder 及以上）：confirmed（确认）或 rejected（驳回），**只追加**。

    审批人只来自服务端身份（关登录时是中间件按 X-Actor 解出的自报署名），请求体里没有 actor 这个键（多传回 422）——
    模型和客户端都代写不了审批人，也不能靠「样例对得上」自动变成批准。决定绑定**所审记录**的定义/绑定/取数语义依据：
    定义或绑定之后改过，这条决定保留但不再覆盖新版本（covers_current=false，返回里的 record.review 会说清）。

    确认范围（metrics）可以不写（= 确认这份记录的全部指标）或写成它的一个子集；写空列表拒（422 review_scope）。
    只确认一部分时这条决定本身有效，但草稿摘要不会给卡级 verified——`summary.unconfirmed` 列出还没被确认的指标。

    同一份记录可以再审（追加一条，显示用最新的一条）；没有能记下来的主体时拒（409 review_actor_required）。
    不建多级审批，也不因这条决定自动改草稿状态或发布任何东西。
    """
    from app.api.deps import current_principal, ensure_role
    from app.auth.audit import record as audit
    from app.ledger import review, validation

    ensure_role("builder")
    draft = await _draft_or_404(session, draft_id)
    record = await validation.get_record(session, draft.id, record_id)
    if record is None:
        raise HTTPException(404, "这份草稿上没有这条验证记录")
    principal = current_principal()
    if not principal.username:
        raise CodedHTTPException(409, "审阅要有一个能记下来的主体：请带上身份（登录，或自报 X-Actor）再确认或驳回",
                                 REVIEW_ACTOR_REQUIRED)
    try:
        made = await review.decide(session, draft_id=draft.id, record=record, decision=payload.decision,
                                   actor=principal.username, verified=principal.verified,
                                   scope=payload.metrics, note=payload.note)
    except review.ReviewRefused as e:
        raise CodedHTTPException(422, e.message, e.code, field="decision") from None
    await audit(session, REVIEW_ACTION, REVIEW_SUBJECT_KIND, record.id,
                data={"draft_id": draft.id, "decision": made.decision, "scope": made.scope,
                      "basis": made.basis})
    workflow = await session.get(Workflow, draft.workflow_id) if draft.workflow_id else None
    current = _current_materials(workflow)
    await session.commit()
    return {"ok": True, "draft_id": draft.id,
            "record": validation.as_dict(record, current=current,
                                         review=review.as_dict(made, current=current))}


def _check_metrics(metrics: Any) -> list[dict[str, Any]]:
    """PATCH 换指标时只做结构校验（不重跑引用核对：引用是模型交的，人改过的段落没法再逐字对一遍）。"""
    if not isinstance(metrics, list):
        raise HTTPException(422, "metrics 要是数组")
    for position, metric in enumerate(metrics, start=1):
        if not isinstance(metric, dict):
            raise HTTPException(422, f"第 {position} 条指标不是一个对象")
        name = metric.get("name")
        if not isinstance(name, str) or not name.strip():
            raise HTTPException(422, f"第 {position} 条指标缺少名称")
        for key in ("synonyms", "open_questions"):
            value = metric.get(key)
            if value is not None and (not isinstance(value, list) or not all(isinstance(v, str) for v in value)):
                raise HTTPException(422, f"第 {position} 条指标的 {key} 要是字符串数组")
        if metric.get("definition") is not None and not isinstance(metric.get("definition"), dict):
            raise HTTPException(422, f"第 {position} 条指标的 definition 要是一个对象")
    return [dict(metric) for metric in metrics]



class DraftTargetIn(BaseModel):
    """这次 PATCH 写哪张口径卡。图上有多张可写的卡时必须写明——不做「取第一张」。"""

    workflow_id: str | None = Field(default=None, max_length=32)
    card_id: str | None = Field(default=None, max_length=64)


class DraftOperationIn(BaseModel):
    """一条字段操作（K03）：按稳定 metric_id + 字段路径改一处，不整份换指标。

    value 只有 set 才有：省略 value 和写 null 是两件事（前者拒「set 要带 value」，后者拒「不能设成 null」；
    要「声明成空」写 ''，数组写 []），remove 不接受 value。数组字段整份设置。能改哪些路径、每种的类型见
    app/ledger/changes.py——不认识的键直接拒（extra=forbid），不当没看见。
    """

    model_config = ConfigDict(extra="forbid")

    op: str = Field(min_length=1, max_length=20)
    metric_id: str = Field(min_length=1, max_length=64)
    path: str = Field(min_length=1, max_length=120)
    value: Any = None


class DraftPatchIn(BaseModel):
    """改标题、整体换掉指标、把已经答清的问题消掉，或者按字段改一处（operations）。

    已接管的草稿（文档起草已经建出工作流的那些）还要带两样：expected_revision 是客户端读到的 Workflow.version
    （两个页面先后保存时，后一个基于旧版本的要被拒，而不是盖掉前一个），request_id 是幂等键（重发不新增版本）；
    target 指明改哪张卡。没接管到工作流上的老草稿不看这三项，照旧只改 payload。

    metrics（整份换）和 operations（按字段改）互斥：一次请求只落一版图，不把两条写路径串在一次里。resolved（消问题）
    也归整份那条路，不和 operations 混在一起。
    """

    title: str | None = Field(default=None, max_length=200)
    metrics: list[dict[str, Any]] | None = None
    resolved: list[str] | None = None
    #: 按字段改（K03）：set / remove，每条指明 metric_id 和字段路径
    operations: list[DraftOperationIn] | None = Field(default=None, min_length=1, max_length=50)
    #: 客户端拿着的基准版本（Workflow.version，读草稿时给的 revision）
    expected_revision: int | None = Field(default=None, ge=1)
    #: 幂等键：同一主体、同一工作流、同一张卡上重发同一个键不新增版本
    request_id: str | None = Field(default=None, min_length=1, max_length=64)
    #: 这次写哪张卡（多张可写的卡时必须给）
    target: DraftTargetIn | None = None


def _patch_fingerprint(payload: DraftPatchIn, card_id: str | None) -> str:
    """这次请求的内容指纹：标题、指标、消掉的问题、字段操作、目标卡。不算 expected_revision——同一个 request_id
    带着过期的基准版本重放，认得出是同一件事，不报内容冲突。

    resolved 归一成「去空白、去重、排序」的一份：消问题是按集合做的，勾选顺序变一下不该被当成另一次请求
    （客户端重发同一批问题时顺序可能不同）。operations 保持原顺序、按写下来的样子算（同一条指标上的几条是有先后的），
    「省略 value」和「写 null」也算不同的内容。

    operations 这一项只在这次请求带了它的时候进指纹：不带的老请求算出来的还是原来那一份哈希，升级之前记下的
    request_id 照旧认得出是重放，不会突然变成「同键不同内容」。
    """
    from app.apps.versions import write_fingerprint

    resolved = None if payload.resolved is None else sorted(
        {q.strip() for q in payload.resolved if isinstance(q, str) and q.strip()})
    content: dict[str, Any] = {"title": payload.title, "metrics": payload.metrics, "resolved": resolved,
                               "card": card_id or ""}
    if payload.operations is not None:
        content["operations"] = [op.model_dump(exclude_unset=True) for op in payload.operations]
    return write_fingerprint(content)


@router.patch("/drafts/{draft_id}")
async def patch_draft(draft_id: str, payload: DraftPatchIn, session: AsyncSession = Depends(get_session)
                      ) -> dict[str, Any]:
    """改一份草稿：metrics 整体替换（只做结构校验，不重跑引用核对）；resolved 里的问题从跨指标和各指标的问题里删掉。
    据此重算状态（还有问题 → questions_open，否则有图 → ready、没有图的老草稿 → path_pending）。已经 attach 或驳回的
    草稿不改状态——那时它说的已经不是一份草稿了。

    已接管的草稿按 K02 的写入保护走：必须带 expected_revision（缺了回 409 revision_required）、目标卡要明确（多张
    可写的卡没指明回 409 card_required，指到没有的卡回 409 card_not_found），基准版本过期回 409 stale_revision 并
    带上 current_version（客户端保留自己的编辑，可显式重放）；带了 request_id 的按主体 + 工作流 + 目标卡幂等，同键
    同内容重放不新增版本、同键不同内容回 409 idempotency_conflict。幂等在版本检查之前判：上一次已经写进去的重放，
    即使基准版本已经过期也要成功。

    operations 是 K03 的字段变更：按稳定 metric_id + 字段路径改一处（白名单和四种写法见 app/ledger/changes.py），
    改动打在原图的深拷贝上，没提到的字段、别的指标、扩展字段和其他节点原样保留；一批全成或全不成，失败不新增版本。
    它和整份换指标（metrics）、消问题（resolved）互斥（422 change_conflict：一次请求只落一版图），回执里另带 change
    一段（变更分类、影响指标、定义指纹、失效说明和绑定状态）。带 request_id 的重放交回的是当前视图（带 replayed），
    不再重新算一份变更回执。
    """
    from app.api.deps import current_principal, ensure_role
    from app.apps.versions import (REVISION_REQUIRED, STALE_REVISION, WRITE_CONFLICT, WRITE_REPLAY, claim_write,
                                   remember_write, stale_revision_message)
    from sqlalchemy import update

    ensure_role("builder")
    actor = current_principal().username
    if payload.operations is not None and (payload.metrics is not None or payload.resolved is not None):
        raise CodedHTTPException(422, "字段操作（operations）和整份换指标 / 消问题不能放在同一次请求里："
                                      "两条写路径各落一版图，请分开提交", CHANGE_CONFLICT, field="operations")
    # 和重试对同一草稿串行，SQLite 也通过写锁保护；后台还在生成时不接受会被它覆盖的编辑。
    await session.execute(update(CaliberDraft).where(CaliberDraft.id == draft_id)
                          .values(updated_at=CaliberDraft.updated_at).execution_options(synchronize_session=False))
    draft = await _draft_or_404(session, draft_id)
    if draft.status == "drafting":
        raise HTTPException(409, "草稿仍在起草，请等待完成或停止起草后再编辑")
    body = draft.payload if isinstance(draft.payload, dict) else {}
    payload_data = copy.deepcopy(body)
    workflow = await session.get(Workflow, draft.workflow_id) if draft.workflow_id else None
    taken_over = _doc_draft_workflow(workflow)
    workflow_id = workflow.id if workflow is not None else None
    card_id: str | None = None
    fingerprint = ""
    change: Any = None
    if taken_over:
        # 已接管：改哪张卡是明确的；写要带基准版本，重放要认得出
        card_id = _target_card(payload, workflow) if (payload.metrics is not None or payload.resolved is not None
                                                      or payload.operations is not None) else None
        if payload.expected_revision is None:
            raise CodedHTTPException(
                409, f"这份口径已经接管到工作流上（现在是 v{workflow.version}）：修改要带上读到的版本（expected_revision），"
                     "请刷新页面后再改", REVISION_REQUIRED, extra={"current_version": workflow.version})
        fingerprint = _patch_fingerprint(payload, card_id)
        claim = await claim_write(session, actor=actor, workflow_id=workflow_id, target=card_id,
                                  request_id=payload.request_id, payload_hash=fingerprint)
        if claim.state == WRITE_CONFLICT:
            raise CodedHTTPException(409, "同一个 request_id 已经用不同的内容提交过：请换一个 request_id 再提交",
                                     IDEMPOTENCY_CONFLICT, extra={"current_version": workflow.version})
        if claim.state == WRITE_REPLAY:
            # 上一次已经写进去了：照上次的结果交回，不再新增版本（基准版本过期也不算冲突）
            return {**await _draft_view(session, draft), "replayed": True}
        if payload.expected_revision != workflow.version:
            raise CodedHTTPException(409, stale_revision_message(workflow.version, payload.expected_revision),
                                     STALE_REVISION, extra={"current_version": workflow.version})
        live = await _draft_graph_payload(session, workflow, workflow.version, card_id=card_id)
        if live is not None:
            payload_data.update(copy.deepcopy(live))
    if payload.title is not None:
        if not payload.title.strip():
            raise HTTPException(422, "标题不能为空")
        draft.title = payload.title.strip()
    if payload.metrics is not None:
        payload_data["metrics"] = _check_metrics(payload.metrics)
    if payload.resolved is not None:
        drop = {q.strip() for q in payload.resolved if isinstance(q, str) and q.strip()}
        questions = payload_data.get("open_questions")
        payload_data["open_questions"] = [q for q in questions if isinstance(q, str) and q.strip() not in drop] \
            if isinstance(questions, list) else []
        metrics = payload_data.get("metrics")
        for metric in metrics if isinstance(metrics, list) else []:
            if not isinstance(metric, dict) or not isinstance(metric.get("open_questions"), list):
                continue
            metric["open_questions"] = [q for q in metric["open_questions"]
                                        if not (isinstance(q, str) and q.strip() in drop)]
    if payload.metrics is not None or payload.resolved is not None:
        # 换指标、清问题都要落到图上：草稿页从图读，只改 payload 会被下一次读取覆盖回去
        try:
            await _sync_draft_graph(session, draft, payload_data, card_id=str(card_id),
                                    expected_revision=payload.expected_revision)
        except ValueError as e:
            raise HTTPException(422, f"改完的指标建不成图：{e}") from e
        draft.payload = payload_data
        metrics = payload_data.get("metrics")
        # 起草失败的草稿补上真指标后要能往前走；没有指标时不重算，免得空草稿被当成「待接数据路径」
        if isinstance(metrics, list) and metrics and draft.status in ("drafting", "questions_open",
                                                                     "path_pending", "ready", "failed"):
            draft.status = _status_for(payload_data)
    if payload.operations is not None:
        # 按字段改（K03）：和上面那条路互斥，一次请求只落一版图。状态不重算——改字段不动未决问题，也不动取数路径。
        change = await _sync_draft_changes(session, draft, payload_data, card_id=str(card_id),
                                           expected_revision=payload.expected_revision,
                                           operations=payload.operations)
        draft.payload = payload_data
    if taken_over:
        await remember_write(session, actor=actor, workflow_id=workflow_id, target=card_id,
                             request_id=payload.request_id, payload_hash=fingerprint,
                             revision=workflow.version)
    try:
        await session.commit()
    except IntegrityError:
        # 两路真并发带同一个 request_id：后到的那一路在记请求时撞唯一键，回滚掉自己写的图，按赢的那一路交回。
        # 只有重查确认是同一件事（同键同内容）才当重放；记录还不存在（别处的完整性错误）照旧抛出去，
        # 不能把没写成的请求报成成功。
        await session.rollback()
        if not taken_over or not payload.request_id:
            raise
        reread = await _draft_or_404(session, draft_id)
        claim = await claim_write(session, actor=actor, workflow_id=workflow_id, target=card_id,
                                  request_id=payload.request_id, payload_hash=fingerprint)
        if claim.state == WRITE_CONFLICT:
            raise CodedHTTPException(409, "同一个 request_id 已经用不同的内容提交过：请换一个 request_id 再提交",
                                     IDEMPOTENCY_CONFLICT) from None
        if claim.state != WRITE_REPLAY:
            raise
        return {**await _draft_view(session, reread), "replayed": True}
    return _with_change(await _draft_view(session, draft), change, revision=getattr(workflow, "version", None))


def _with_change(view: dict[str, Any], change: Any, *, revision: int | None) -> dict[str, Any]:
    """把 K03 的变更回执并进草稿视图（只有走 operations 的那条路才带）。"""
    if change is None:
        return view
    from app.ledger import changes

    return {**view, "change": changes.summary(change, applied=bool(change.applied), revision=revision)}


class ContinueDefinitionsIn(BaseModel):
    expected_revision: int = Field(ge=0)
    guidance: str = Field(default="", max_length=2000)
    selected: list[str] | None = Field(default=None, min_length=1, max_length=2000)
    datasource_id: str | None = Field(default=None, min_length=1, max_length=32)


@router.post("/drafts/{draft_id}/continue", status_code=202)
async def continue_definitions(draft_id: str, payload: ContinueDefinitionsIn, background: BackgroundTasks,
                               session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """Generate missing definitions as an independent candidate; never overwrite reviewed SQL."""
    from app.api.deps import ensure_role, current_principal
    from app.core.artifact_store import canonical_json, content_hash
    from app.db.models import CaliberDraftJob
    from sqlalchemy import update

    ensure_role("builder")
    draft = await _draft_or_404(session, draft_id)
    if draft.status not in ("questions_open", "ready", "path_pending", "failed"):
        raise HTTPException(409, "请等待当前任务完成后再继续生成")
    workflow = await session.get(Workflow, draft.workflow_id) if draft.workflow_id else None
    revision = workflow.version if workflow else 0
    if revision != payload.expected_revision:
        raise HTTPException(409, "口径已发生变化，请刷新后继续")
    stored = dict(draft.payload or {})
    inventory = stored.get("semantic_inventory")
    if not inventory:
        raise HTTPException(409, "这份草稿没有导入定义，请先导入定义文档")
    requested_source_id = payload.datasource_id or draft.datasource_id
    if payload.datasource_id and payload.datasource_id != draft.datasource_id and workflow is not None:
        raise HTTPException(409, "已有取数图的草稿不能在续作时切换数据源，请另建迁移定义草稿")
    source = await session.get(DataSource, requested_source_id) if requested_source_id else None
    try:
        require_authoring_source(source)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    from app.ledger.semantic_import import source_matches
    docs = [await session.get(Document, key) for key in inventory.get("document_hashes", {})]
    if any(d is None or d.status != "ready" for d in docs) or not source_matches(
        inventory, [{"document_id": d.id, "content": d.content} for d in docs if d is not None]
    ):
        raise HTTPException(409, "来源文档已经变化，请重新导入定义")
    view = await _draft_view(session, draft, ensure_records=False)
    done = {m.get("source_definition_id") for m in view.get("metrics", [])}
    missing = [key for key in stored.get("selected_definition_ids", []) if key not in done]
    if not missing:
        raise HTTPException(409, "所选指标已经全部生成，无需继续")
    if payload.selected is not None:
        wanted = list(dict.fromkeys(str(k) for k in payload.selected if str(k).strip()))
        if not wanted or not set(wanted) <= set(missing):
            raise HTTPException(422, "仅可选择当前尚未完成的指标继续生成")
        target_missing = wanted
    else:
        target_missing = missing
    key = content_hash(canonical_json({"revision": revision, "missing": target_missing,
                                      "guidance": payload.guidance.strip(), "source": source.id,
                                      "origin": draft.origin, "inventory": inventory}))
    candidates = dict(stored.get("continuation_candidates") or {})
    if key in candidates:
        candidate = await session.get(CaliberDraft, candidates[key])
        if candidate is not None:
            return {"draft_id": candidate.id, "status": candidate.status,
                    "original_draft_id": draft_id, "merge_required": True}
    candidate = CaliberDraft(title=f"{draft.title[:185]} · 补充指标", collection=draft.collection,
        datasource_id=source.id, status="drafting", created_by=current_principal().username,
        origin={**(draft.origin or {}), "guidance": "\n".join(filter(None, [
            str((draft.origin or {}).get("guidance") or ""), payload.guidance.strip()])),
            "continuation_from": {"draft_id": draft.id, "workflow_id": draft.workflow_id,
                                  "revision": revision, "merge_required": True}},
        payload={"semantic_inventory": inventory, "selected_definition_ids": target_missing,
                 "implementation_mode": "selected", "definition_issues": [],
                 "definition_artifact": stored.get("definition_artifact")})
    session.add(candidate)
    await session.flush()
    candidates[key] = candidate.id
    update_values: dict[str, Any] = {"payload": {**stored, "continuation_candidates": candidates}}
    if workflow is None and source.id != draft.datasource_id:
        update_values["datasource_id"] = source.id
    claimed = await session.execute(update(CaliberDraft).where(
        CaliberDraft.id == draft.id, CaliberDraft.updated_at == draft.updated_at,
        CaliberDraft.status == draft.status,
        *([select(Workflow.id).where(Workflow.id == workflow.id, Workflow.version == revision).exists()]
          if workflow else [CaliberDraft.workflow_id.is_(None)])
    ).values(**update_values).execution_options(synchronize_session=False))
    if claimed.rowcount != 1:
        await session.rollback()
        raise HTTPException(409, "草稿已变化或已有续作请求，请刷新后继续")
    session.add(CaliberDraftJob(id=candidate.id))
    await session.commit()
    background.add_task(draft_jobs.start, candidate.id)
    return {"draft_id": candidate.id, "status": "drafting", "original_draft_id": draft_id,
            "merge_required": True}


class MergeContinuationIn(BaseModel):
    expected_revision: int = Field(ge=0)
    candidate_revision: int = Field(ge=1)


@router.post("/drafts/{draft_id}/merge")
async def merge_continuation(draft_id: str, payload: MergeContinuationIn,
                             session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    from app.api.deps import current_principal, ensure_role
    from app.apps.versions import append_version
    from app.ledger import fieldbaseline
    from app.ledger.continuation import append_candidate
    from sqlalchemy import update

    ensure_role("builder")
    # Serialize metadata updates with draft edits and competing merge requests.
    await session.execute(update(CaliberDraft).where(CaliberDraft.id == draft_id)
                          .values(updated_at=CaliberDraft.updated_at).execution_options(synchronize_session=False))
    candidate = await _draft_or_404(session, draft_id)
    origin = dict(candidate.origin or {})
    continuation = dict(origin.get("continuation_from") or {})
    if not continuation.get("draft_id"):
        raise HTTPException(409, "这份草稿不是补充指标候选")
    await session.execute(update(CaliberDraft).where(CaliberDraft.id == continuation["draft_id"])
                          .values(updated_at=CaliberDraft.updated_at).execution_options(synchronize_session=False))
    original = await _draft_or_404(session, continuation["draft_id"])
    if continuation.get("merged_revision") is not None:
        return {**await _draft_view(session, original, ensure_records=False), "merged_candidate_id": candidate.id,
                "replayed": True}
    if candidate.status not in ("ready", "questions_open") or original.status in ("drafting", "rejected", "attached"):
        raise HTTPException(409, "请等待生成完成，并确认原稿仍可编辑")
    incoming = await session.get(Workflow, candidate.workflow_id) if candidate.workflow_id else None
    workflow = await session.get(Workflow, original.workflow_id) if original.workflow_id else None
    if incoming is None or incoming.version != payload.candidate_revision:
        raise HTTPException(409, "候选已变化，请刷新后重新核对")
    if (workflow.version if workflow else 0) != payload.expected_revision:
        raise HTTPException(409, "原稿已变化，请刷新后重新核对，不会覆盖现有编辑")
    source = await session.get(DataSource, original.datasource_id) if original.datasource_id else None
    try:
        require_authoring_source(source)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    if candidate.datasource_id != original.datasource_id:
        raise HTTPException(409, "候选与原稿数据源不一致，请重新核对")
    # Lock the accepted candidate revision too; it must not change during the merge.
    claimed = await session.execute(update(Workflow).where(
        Workflow.id == incoming.id, Workflow.version == payload.candidate_revision
    ).values(version=Workflow.version).execution_options(synchronize_session=False))
    if claimed.rowcount != 1:
        raise HTTPException(409, "候选已变化，请刷新后重新核对")
    original_payload = dict(original.payload or {})
    _, incoming_metrics = fieldbaseline.accepted_card(incoming.graph)
    definition_ids = [m.get("source_definition_id") for m in incoming_metrics]
    incoming_names = [m.get("name") for m in incoming_metrics]
    if (not definition_ids or None in definition_ids
            or not all(incoming_names) or len(set(incoming_names)) != len(incoming_names)
            or not set(definition_ids) <= set(original_payload.get("selected_definition_ids") or [])):
        raise HTTPException(409, "候选包含未请求的或重复的指标定义，请核对；候选已保留")
    if ((candidate.payload or {}).get("semantic_inventory", {}).get("document_hashes")
            != original_payload.get("semantic_inventory", {}).get("document_hashes")):
        raise HTTPException(409, "候选与原稿的来源定义版本不同，请重新核对")
    had_workflow = workflow is not None
    prefix = f"supp_{candidate.id[:12]}"
    try:
        if had_workflow:
            graph = append_candidate(workflow.graph, incoming.graph, prefix=prefix,
                                     allowed_definitions=set(original_payload.get("selected_definition_ids") or []))
            await append_version(session, workflow, graph, expected_revision=payload.expected_revision,
                                 note=f"合并补充指标候选 {candidate.id}")
        else:
            # Keep the independent candidate independent even when the original had no graph.
            from app.db.models import WorkflowVersion
            from app.core.artifact_store import graph_hash
            workflow = Workflow(name=original.title, graph=copy.deepcopy(incoming.graph), status="draft",
                                tags=["doc_draft"], origin={"kind": "doc_draft", "draft_id": original.id,
                                                         "datasource_id": original.datasource_id})
            session.add(workflow)
            await session.flush()
            session.add(WorkflowVersion(workflow_id=workflow.id, version=1, graph=workflow.graph,
                                        graph_hash=graph_hash(workflow.graph), note="接受补充指标候选"))
            original.workflow_id = workflow.id
        live = await _draft_graph_payload(session, workflow, workflow.version, draft=original, payload=original_payload)
    except ValueError as exc:
        await session.rollback()
        raise HTTPException(409, str(exc)) from None
    if live:
        original_payload.update(live)
    original_payload["workflow_id"] = workflow.id
    if not had_workflow:
        from app.core.artifact_store import graph_hash
        if not original_payload.get("source_snapshot") and isinstance((candidate.payload or {}).get("source_snapshot"), dict):
            original_payload["source_snapshot"] = copy.deepcopy(candidate.payload["source_snapshot"])
        original_payload["accepted_graph_hash"] = graph_hash(workflow.graph)
    workflow_id = workflow.id
    workflow_version = workflow.version
    workflow_graph = copy.deepcopy(workflow.graph)
    original_id = original.id
    original_created_by = original.created_by
    candidate_id = candidate.id
    candidate_snapshot = (
        copy.deepcopy(candidate.payload["source_snapshot"])
        if isinstance((candidate.payload or {}).get("source_snapshot"), dict) else None
    )
    completed = {m.get("source_definition_id") for m in original_payload.get("metrics", [])}
    selected = set(original_payload.get("selected_definition_ids") or [])
    completed_def_names = {
        i["name"] for i in original_payload.get("semantic_inventory", {}).get("items", [])
        if i["id"] in completed and i.get("name")
    }
    names = {m.get("name") for m in original_payload.get("metrics", []) if m.get("name")} | completed_def_names
    remaining_count = len(selected - completed)
    cleaned_questions: list[str] = []
    for q in original_payload.get("open_questions", []):
        if any(q.startswith(f"{name}：") for name in names):
            continue
        if q.startswith("另有 ") and "项指标尚未生成可用实现" in q:
            if remaining_count > 0:
                cleaned_questions.append(f"另有 {remaining_count} 项指标尚未生成可用实现，请按上方定义清单补充字段映射或确认口径")
            continue
        cleaned_questions.append(q)
    original_payload["open_questions"] = cleaned_questions
    if isinstance(original_payload.get("definition_diagnostics"), dict):
        original_payload["definition_diagnostics"] = {
            k: v for k, v in original_payload["definition_diagnostics"].items()
            if k in selected - completed
        }
    original_payload["uncovered"] = [i["name"] for i in original_payload.get("semantic_inventory", {}).get("items", [])
                                      if i["id"] in selected - completed]
    original_payload["implementation_progress"] = {"total": len(selected), "completed": len(selected & completed),
        "pending": 0, "blocked": len(selected - completed), "active_names": []}
    original_payload["trial"] = {"status": "stale", "run_id": "", "error": "已合并补充指标，请重新试运行验证完整口径"}
    original.error = None
    original.payload, original.status = original_payload, _status_for(original_payload)
    candidate.origin = {**origin, "continuation_from": {**continuation, "merge_required": False,
                                                        "merged_revision": workflow_version}}
    await session.commit()

    # Commit the write transaction before put_json opens its own session (avoids SQLite write-lock deadlock).
    ref: dict[str, Any] | None = None
    card_id, card_metrics = fieldbaseline.accepted_card(workflow_graph)
    if had_workflow:
        new_metric_ids = {f"{prefix}_{m['id']}" for m in incoming_metrics if m.get("id")}
        updates: dict[str, dict[str, Any]] = {}
        for metric in card_metrics:
            mid = str(metric.get("id") or "")
            if mid in new_metric_ids:
                fields = fieldbaseline.fields_of(metric)
                updates[mid] = {
                    "name": str(metric.get("name") or ""),
                    "fields": {path: cell["value"] for path, cell in fields.items()},
                    "expected_fields": sorted(fields),
                }
        if updates:
            ref = await fieldbaseline.advance(
                ref=fieldbaseline.ref_of(original_payload),
                updates=updates,
                card_id=card_id,
                workflow_id=workflow_id,
                accepted_revision=workflow_version,
                draft_id=original_id,
                source_snapshot=original_payload.get("source_snapshot") or candidate_snapshot,
                accepted_by=current_principal().username,
                card_metric_ids=[str(m.get("id") or "") for m in card_metrics if m.get("id")],
            )
    elif card_metrics:
        ref = await fieldbaseline.put_metrics(
            metrics=card_metrics,
            card_id=card_id,
            workflow_id=workflow_id,
            accepted_revision=workflow_version,
            source_snapshot=original_payload.get("source_snapshot") or candidate_snapshot,
            draft_id=original_id,
            accepted_by=original_created_by,
        )
    original = await _draft_or_404(session, original_id)
    if ref is not None:
        updated_payload = dict(original.payload or {})
        updated_payload[fieldbaseline.REF_KEY] = ref
        updated_payload[fieldbaseline.HISTORY_KEY] = [*fieldbaseline.history(updated_payload), ref]
        original.payload = updated_payload
        await session.commit()
        original = await _draft_or_404(session, original_id)
    return {**await _draft_view(session, original, ensure_records=False), "merged_candidate_id": candidate_id}


@router.post("/drafts/{draft_id}/retry")
async def retry_draft(draft_id: str, background: BackgroundTasks,
                      session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    from app.api.deps import ensure_role

    ensure_role("builder")
    draft = await _draft_or_404(session, draft_id)
    try:
        await draft_jobs.retry(session, draft)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None
    background.add_task(draft_jobs.start, draft.id)
    return {"draft_id": draft.id, "status": "drafting"}


@router.post("/drafts/{draft_id}/stop")
async def stop_draft(draft_id: str, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    from app.api.deps import ensure_role

    ensure_role("builder")
    draft = await _draft_or_404(session, draft_id)
    await session.commit()
    if not await draft_jobs.manager.stop(draft.id, "user"):
        await draft_jobs.finish(session, draft.id, "cancelled", "起草已停止，已保留进度")
    await session.refresh(draft)
    return await _draft_view(session, draft)


class DraftDismissIn(BaseModel):
    note: str | None = Field(default=None, max_length=500)


@router.post("/drafts/{draft_id}/dismiss")
async def dismiss_draft(draft_id: str, payload: DraftDismissIn,
                        session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """这份草稿不是口径：置 rejected，写一条审计（ledger.docdraft.dismiss），以后不再出现在待办里。"""
    from app.api.deps import ensure_role
    from app.auth.audit import record

    ensure_role("builder")
    draft = await _draft_or_404(session, draft_id)
    await session.commit()
    await draft_jobs.manager.stop(draft.id, "user")
    await session.refresh(draft)
    draft.status = "rejected"
    await record(session, DRAFT_DISMISS_ACTION, DRAFT_SUBJECT_KIND, draft.id,
                 data={k: v for k, v in {"title": draft.title, "note": payload.note}.items() if v})
    await session.commit()
    from app.ledger import docdraft

    return {"ok": True, "draft_id": draft.id, "status": draft.status, "stage": docdraft.stage_of(draft.status)}


class DraftAttachIn(BaseModel):
    """按哪次运行补取数路径：运行里执行过的查询就是这份口径的取数，不另写 SQL。"""

    run_id: str = Field(min_length=1, max_length=64)


@router.post("/drafts/{draft_id}/attach")
async def attach_draft(draft_id: str, payload: DraftAttachIn,
                       session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """把定义接到一次真实运行上（app/ledger/docattach.py）：由它建工作流和版本，交回 {workflow_id, version, metrics,
    warnings}。成功后草稿记下工作流 id、置 attached。接不上时照 docattach 自己的状态码和说法回。"""
    from app.api.deps import ensure_role
    from app.ledger.docattach import attach

    ensure_role("builder")
    draft = await _draft_or_404(session, draft_id)
    try:
        out = await attach(session, payload=draft.payload, name=draft.title, run_id=payload.run_id,
                           draft_id=draft.id)
    except Exception as e:  # noqa: BLE001 - docattach 的拒绝带着 status_code / message，照原样交回
        status = getattr(e, "status_code", None)
        if isinstance(status, int) and getattr(e, "message", None):
            raise HTTPException(status, e.message) from e
        raise
    draft.workflow_id = out.get("workflow_id") or draft.workflow_id
    draft.status = "attached"
    draft.error = None
    await session.commit()
    return out
