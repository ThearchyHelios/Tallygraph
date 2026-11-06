"""账本（BE-3，只读）：工作流版本被哪些应用钉住、生效计数；生效记录查询。设计文档 §2.9 L4。

不另建存储：ActionRecord 本身带着工作流、版本、graph_hash、能力版本、payload_hash、回执；「口径 → 应用」就是固定这一版的
应用版本。用在发布弹窗、退役确认、版本列表三处。

口径账本（docs/design/next-round/02-口径账本.md）在这里加的接口：
- GET /api/runs/{run_id}/ledger（runs_router，和证据同一道门）：一次运行里每个数的口径标签、整题判断、每条查询的比对、能做的
  动作、出具过的正式运行、对过的账（app/ledger/view.py）；
- GET /api/ledger/stats（router，搭建者）：账本自身给出的统计 E1–E4，从比对工件、运行、审计现算（app/ledger/stats.py）；
- POST /api/ledger/answers/{run_id}/issue（answers_router，登录即可，第 3 波）：按口径正式出具（app/ledger/issue.py，拍板
  L1、L5：参数只从比对工件取，使用者只能按他有权使用的应用固定着的版本出具）；
- POST /api/ledger/answers/{run_id}/reconcile（同上）：口径对账，点开才跑一次确定性派生的只读图（app/ledger/reconcile.py）；
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
这几个新接口都不放给访问密钥（auth/keys.KEY_ROUTES 不登记）。
"""
from __future__ import annotations

import asyncio
import copy
import logging
import re
from typing import Any

from datetime import datetime

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import SessionLocal, get_session
from app.db.models import CaliberDraft, Chunk, DataSource, Document, Run, Workflow, WorkflowVersion
from app.db.models_process import ActionRecord

logger = logging.getLogger(__name__)

from app.ledger.authoring import (
    _can_repair, _columns_digest, _draft_payload, _entry_notes, _focus_tables, _fresh_metrics, _merge_questions, _merged_fields, _problems_text, _published_metrics, _question_count, _question_key, _schema_material, _source_tables, _status_for
)

router = APIRouter(prefix="/api/ledger", tags=["ledger"])
#: 口径账本里按运行读的那几个（一个答案里每个数的口径标签等，docs/design/next-round/02-口径账本.md）：挂在
#: /api/runs/{run_id}/ledger… 下，和证据接口同一道门（main.py 挂 evidence_scope：使用者只读、而且要能读这次运行）
runs_router = APIRouter(prefix="/api/runs", tags=["ledger"])
#: 按口径正式出具、口径对账这类要写的（docs/design/next-round/02-口径账本.md 做法 5、6）：使用者也能发起（总方案拍板
#: 账本-L5：对账都可以，正式出具只限有权使用的应用固定着的口径版本），所以 main.py 只挂登录即可，接口里按运行和应用授权再判
answers_router = APIRouter(prefix="/api/ledger/answers", tags=["ledger"])


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
async def reconcile_answer(run_id: str, payload: ReconcileIn) -> dict[str, Any]:
    """口径对账（app/ledger/reconcile.py）：按那一版口径确定性派生只读图，发起一次探索运行（来源 reconcile），差值在对账卡
    里算。参数是推断出来的、不全的要先核对（409，code ledger_reconcile_check_inputs）；认不出列怎么配的要人选（409，
    ledger_reconcile_needs_pairs）；多行时维度是遮罩列、键不唯一照实拒绝（409）。"""
    from app.api.coded import CodedHTTPException
    from app.api.deps import current_principal
    from app.ledger import reconcile as ledger_reconcile

    try:
        run = await ledger_reconcile.reconcile(run_id, ledger_reconcile.Request(
            query=payload.query, workflow_id=payload.workflow_id, version=payload.version, inputs=payload.inputs,
            confirm=payload.confirm, pairs=payload.pairs), principal=current_principal())
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
    情况。都算过了的、这一版已经不是当前发布版的（之后又发布了更新的版本）回 409，detail 写为什么。"""
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
    不代表这次运行一定能出具：全局自动出具开关、每日上限这类运行时条件不在这里判。"""
    from app.ledger.descriptor import list_calibers as list_descriptors

    return {"calibers": await list_descriptors(session, workflow_id=workflow_id)}


@router.get("/calibers/{workflow_id}/{version}/{node_id}")
async def caliber_descriptor(workflow_id: str, version: int, node_id: str,
                             session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """一张源头口径卡的描述（形状同列表里的一条）。工作流不存在、这一版没发布过或已退役、节点不是源头口径卡（转抄卡也算）
    回 404。同样不返回 SQL、参数值；issuance 只描述版本策略，不代表这次运行一定能出具。"""
    from app.ledger.descriptor import caliber_descriptor as descriptor_of

    made = await descriptor_of(session, workflow_id, version, node_id)
    if made is None:
        raise HTTPException(404, "没有这份口径：工作流不存在、这一版没发布过或已退役，或者节点不是源头口径卡")
    return made


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

    试跑不是正式出具：来源记 trial，不进使用统计、不自动出具；参数按入口 Schema 校验，缺的补缺省值。不能派生的口径
    （输入不是查库结果里的格、取数模板引用了运行时变量……）照实回 409。最多等 30 秒；还没跑完就回 running，前端按
    run_id 去看运行。"""
    from app.api.deps import current_principal
    from app.apps.forms import input_schema, validate_form
    from app.engine.runner import run_manager
    from app.ledger.derive import NotDerivable, caliber_probe, load_pinned
    from app.tools.datasource import READ_ONLY_OPTION

    try:
        pinned = await load_pinned(session, workflow_id, int(version), node_id)
    except NotDerivable as e:
        raise HTTPException(409, e.message) from None
    if not pinned.published or pinned.retired:
        raise HTTPException(409, "这一版没有发布或已退役，不能试运行")
    fields = [dict(f) for f in pinned.entry_fields]
    known = {str(f.get("name")) for f in fields}
    unknown = sorted(str(k) for k in payload.inputs if str(k) not in known)
    if unknown:
        raise HTTPException(422, f"不认识的入口字段：{'、'.join(unknown)}")
    values = {str(f["name"]): f.get("default") for f in fields if f.get("default") not in (None, "")}
    values.update({str(k): v for k, v in payload.inputs.items()})
    if errors := validate_form(input_schema(fields), values):
        raise HTTPException(422, "；".join(str(e.get("message")) for e in errors[:5]))

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
    return workflow is not None and isinstance(workflow.origin, dict) and workflow.origin.get("kind") == "doc_draft"


async def _trial_view(session: AsyncSession, trial: dict[str, Any], *, version: int,
                      graph: dict[str, Any]) -> dict[str, Any]:
    """历史试跑只能证明当时的版本和输入；运行超时返回 running 后，读时从 Run 刷新终态。"""
    from app.core.artifact_store import canonical_json, content_hash, execution_hash, graph_hash

    if not trial:
        return {}
    run = await session.get(Run, trial["run_id"]) if trial.get("run_id") else None
    same_graph = (trial.get("execution_hash") == execution_hash(graph) if trial.get("execution_hash")
                  else trial.get("version") == version and trial.get("graph_hash") == graph_hash(graph))
    if (not same_graph
            or (run is not None and trial.get("inputs_hash") != content_hash(canonical_json(run.input or {})))):
        return {**trial, "status": "stale", "error": "口径已修改或试运行缺少版本记录，请重新试运行"}
    if trial.get("run_id") and run is None:
        return {**trial, "status": "stale", "error": "试运行记录已不存在，请重新试运行"}
    if run is not None:
        return {**trial, "status": _run_status(run.status), "error": str(run.error or "")}
    return trial


async def _save_trial(draft_id: str, trial: dict[str, Any], *, replace: bool = False) -> bool:
    """旧版本或较早的试跑晚结束时，不能覆盖新的验证结果。"""
    async with SessionLocal() as fresh:
        row = await fresh.scalar(select(CaliberDraft).where(CaliberDraft.id == draft_id).with_for_update())
        if row is None or not isinstance(row.payload, dict):
            return False
        workflow = await fresh.get(Workflow, row.workflow_id) if row.workflow_id else None
        if _doc_draft_workflow(workflow):
            from app.core.artifact_store import execution_hash, graph_hash

            same_graph = (trial.get("execution_hash") == execution_hash(workflow.graph) if trial.get("execution_hash")
                          else trial.get("version") == workflow.version and trial.get("graph_hash") == graph_hash(workflow.graph))
            if not same_graph:
                return False
        body = dict(row.payload)
        current = body.get("trial") or {}
        if not replace and current.get("trial_id") and current.get("trial_id") != trial.get("trial_id"):
            return False
        if not replace and current.get("run_id") and current.get("run_id") != trial.get("run_id"):
            return False
        body["trial"] = trial
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
    from app.ledger import docdraft

    payload = draft.payload if isinstance(draft.payload, dict) else {}
    if graph is not None:
        from app.apps.tree import node_map

        card = next((node for node in node_map(graph).values() if node.get("type") == "metrics"
                     and not (node.get("config") or {}).get("caliber_from")), None)
        payload = {**payload, "metrics": ((card or {}).get("config") or {}).get("metrics") or []}
    metrics = payload.get("metrics")
    return {"id": draft.id, "title": draft.title, "status": draft.status,
            "stage": docdraft.stage_of(draft.status),
            "metric_count": len(metrics) if isinstance(metrics, list) else 0,
            "question_count": _question_count(payload), "workflow_id": draft.workflow_id,
            "created_at": draft.created_at.isoformat() if draft.created_at else None, "error": draft.error}


async def _draft_view(session: AsyncSession, draft: CaliberDraft) -> dict[str, Any]:
    """一份草稿的完整样子：payload 摊平在上层（界面直接读 metrics / open_questions），另加状态和关联工作流名。

    文档起草建了工作流和 v1 之后，图是唯一真相：画布上改过指标或取数 SQL，这里从图里取最新的那几项覆盖 payload
    里的起草快照。接上一次运行（docattach）的草稿仍看 payload。
    """
    payload = draft.payload if isinstance(draft.payload, dict) else {}
    workflow = await session.get(Workflow, draft.workflow_id) if draft.workflow_id else None
    view = {**payload, **_draft_summary(draft),
            "collection": draft.collection, "datasource_id": draft.datasource_id,
            "origin": draft.origin if isinstance(draft.origin, dict) else {},
            "model_id": draft.model_id or payload.get("model_id"), "created_by": draft.created_by,
            "updated_at": draft.updated_at.isoformat() if draft.updated_at else None,
            "workflow_name": workflow.name if workflow is not None else None}
    if _doc_draft_workflow(workflow):
        live = await _draft_graph_payload(session, workflow, workflow.version, draft=draft, payload=payload)
        if live is not None:
            view.update(live)
            view["metric_count"] = len(live["metrics"])
            view["question_count"] = _question_count(view)
    from app.db.models import CaliberDraftJob

    job = await session.get(CaliberDraftJob, draft.id)
    if job:
        view["job"] = {"status": job.status, "attempt": job.attempt, "error": job.error,
                       "retryable": draft.status == "failed" and not draft.workflow_id
                                    and job.status not in ("queued", "running")}
    return view


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
                               payload: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """从草稿工作流的某一版图里取出草稿页要显示的那几项（指标、入口、SQL、groups）。

    图是唯一真相：画布上改了指标名、定义或取数 SQL，草稿页跟着图变，不显示 payload 里起草那一刻的快照。取数节点
    按 SQL 分组，指标按表达式里引用的节点归组；表达式不引用取数节点的（比如只读 vars）归到第一组。图里没有口径卡
    或没有指标时交回空指标列表，不能退回起草时的旧快照。
    """
    from app.apps.tree import node_map

    row = (await session.execute(select(WorkflowVersion).where(
        WorkflowVersion.workflow_id == workflow.id,
        WorkflowVersion.version == int(version)))).scalar_one_or_none()
    if row is None or not isinstance(row.graph, dict):
        return None
    nodes = node_map(row.graph)
    card = next((node for node in nodes.values()
                 if node.get("type") == "metrics"
                 and not (node.get("config") or {}).get("caliber_from")), None)
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
    if source is None:
        raise HTTPException(422, "数据源不存在，可能已被删除")
    if not _source_tables(source):
        raise HTTPException(422, "这个数据源还没有表结构：先探查表结构，再起草口径")
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
    结果、关联工作流名。"""
    from app.api.deps import ensure_role

    ensure_role("builder")
    return await _draft_view(session, await _draft_or_404(session, draft_id))


@router.get("/drafts/{draft_id}/trial")
async def get_draft_trial(draft_id: str, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """这份草稿的试跑：{run_id, status, metrics: [{id, name, rendered, value}], queries, error}。

    状态只有 succeeded / failed / running 三种；指标值读试跑运行封存的口径卡（和口径试跑同一个读法），queries 是取数节点
    交回的查询快照（前几行、遮罩列换成「已遮罩」，不含 SQL）。还没试跑（老草稿、起草失败）回 404。只读，不发起运行。"""
    from app.api.deps import ensure_role

    ensure_role("builder")
    draft = await _draft_or_404(session, draft_id)
    payload = draft.payload if isinstance(draft.payload, dict) else {}
    trial = payload.get("trial") if isinstance(payload.get("trial"), dict) else {}
    workflow = await session.get(Workflow, draft.workflow_id) if draft.workflow_id else None
    if _doc_draft_workflow(workflow):
        trial = await _trial_view(session, trial, version=workflow.version, graph=workflow.graph)
    if trial.get("status") == "stale":
        return {**trial, "metrics": [], "queries": []}
    run_id = trial.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        if trial.get("status") in ("needs_input", "running"):
            return {**trial, "metrics": [], "queries": []}
        raise HTTPException(404, "这份草稿还没有试运行")
    run = await session.get(Run, run_id)
    if run is None:
        return {"run_id": run_id, "status": _run_status(trial.get("status")), "metrics": [], "queries": [],
                "error": str(trial.get("error") or "")}
    status = _run_status(run.status)
    metrics = await _trial_metrics(run.id) if status == "succeeded" else []
    queries = await _trial_queries(run.id) if status == "succeeded" else []
    return {**trial, "run_id": run.id, "status": status, "metrics": metrics, "queries": queries,
            "validation": await _trial_validation(run.id, status, metrics), "error": str(run.error or "")}


class DraftTrialIn(BaseModel):
    inputs: dict[str, Any] = Field(default_factory=dict)


@router.post("/drafts/{draft_id}/trial")
async def run_draft_trial(draft_id: str, payload: DraftTrialIn,
                          session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """按填好的入口字段试跑一次（builder 及以上）。

    自动试跑缺必填输入时停在 needs_input（不拿系统编的日期跑，那样跑出来的数没有意义），人在这里补上再跑。
    参数按入口字段校验，和正式运行同一套（apps/forms）；跑的仍是探针图：只取数与口径卡，不写报告、不进统计。成功时另给
    queries（取数节点交回的查询快照，前几行、遮罩列换成「已遮罩」，不含 SQL）；没有运行、失败或还在跑时为空数组。
    """
    from app.api.deps import current_principal, ensure_role
    from app.apps.forms import input_schema, validate_form

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
    values: dict[str, Any] = {str(field.get("name")): field.get("default") for field in fields
                              if field.get("default") not in (None, "")}
    values.update({str(key): value for key, value in payload.inputs.items()})
    if errors := validate_form(input_schema(fields), values):
        raise HTTPException(422, "；".join(str(e.get("message")) for e in errors[:5]))
    trial = await _run_trial(draft_id=draft.id, workflow_id=str(workflow_id), graph=row.graph,
                             values=values, started_by=current_principal().username, entry_fields=fields, version=version)
    await _save_trial(draft.id, trial)
    run_id = str(trial.get("run_id") or "")
    queries = await _trial_queries(run_id) if run_id and trial.get("status") == "succeeded" else []
    metrics = await _trial_metrics(run_id) if run_id and trial.get("status") == "succeeded" else []
    return {**trial, "metrics": metrics, "queries": queries,
            "validation": await _trial_validation(run_id, str(trial.get("status") or ""), metrics)}


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


async def _sync_draft_graph(session: AsyncSession, draft: CaliberDraft, payload_data: dict[str, Any]) -> None:
    """在当前图上修改口径卡，再新增版本；取数节点、SQL、入口和用户添加的编排保持其身份。"""
    from app.apps.versions import WorkflowChanged, save_graph

    workflow = await session.get(Workflow, draft.workflow_id) if draft.workflow_id else None
    metrics = payload_data.get("metrics")
    if not _doc_draft_workflow(workflow) or not isinstance(metrics, list):
        return
    graph = copy.deepcopy(workflow.graph)
    card = next((n for n in graph.get("nodes") or [] if n.get("type") == "metrics"
                 and not (n.get("data", {}).get("config") or {}).get("caliber_from")), None)
    if card is None:
        raise HTTPException(409, "当前画布没有可编辑的口径卡，请先在画布上添加")
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
        if card["id"] not in ([sources] if isinstance(sources, str) else sources):
            continue
        for key in ("required", "expected"):
            if isinstance(contract.get(key), list):
                before = contract[key]
                contract[key] = list(new_ids) if key == "required" and set(before) == old_ids \
                    else [mid for mid in before if mid not in removed]
    try:
        await save_graph(session, workflow, graph, note="编辑口径草稿")
    except WorkflowChanged as e:
        raise HTTPException(409, str(e)) from None
    await session.flush()
    live = await _draft_graph_payload(session, workflow, workflow.version)
    if live is not None:
        payload_data.update(live)


class DraftPatchIn(BaseModel):
    """改标题、整体换掉指标、或把已经答清的问题消掉。"""

    title: str | None = Field(default=None, max_length=200)
    metrics: list[dict[str, Any]] | None = None
    resolved: list[str] | None = None


@router.patch("/drafts/{draft_id}")
async def patch_draft(draft_id: str, payload: DraftPatchIn, session: AsyncSession = Depends(get_session)
                      ) -> dict[str, Any]:
    """改一份草稿：metrics 整体替换（只做结构校验，不重跑引用核对）；resolved 里的问题从跨指标和各指标的问题里删掉。
    据此重算状态（还有问题 → questions_open，否则有图 → ready、没有图的老草稿 → path_pending）。已经 attach 或驳回的
    草稿不改状态——那时它说的已经不是一份草稿了。"""
    from app.api.deps import ensure_role
    from sqlalchemy import update

    ensure_role("builder")
    # 和重试对同一草稿串行，SQLite 也通过写锁保护；后台还在生成时不接受会被它覆盖的编辑。
    await session.execute(update(CaliberDraft).where(CaliberDraft.id == draft_id)
                          .values(updated_at=CaliberDraft.updated_at).execution_options(synchronize_session=False))
    draft = await _draft_or_404(session, draft_id)
    if draft.status == "drafting":
        raise HTTPException(409, "草稿仍在起草，请等待完成或停止起草后再编辑")
    body = draft.payload if isinstance(draft.payload, dict) else {}
    payload_data = copy.deepcopy(body)
    workflow = await session.get(Workflow, draft.workflow_id) if draft.workflow_id else None
    if _doc_draft_workflow(workflow):
        live = await _draft_graph_payload(session, workflow, workflow.version)
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
            await _sync_draft_graph(session, draft, payload_data)
        except ValueError as e:
            raise HTTPException(422, f"改完的指标建不成图：{e}") from e
        draft.payload = payload_data
        metrics = payload_data.get("metrics")
        # 起草失败的草稿补上真指标后要能往前走；没有指标时不重算，免得空草稿被当成「待接数据路径」
        if isinstance(metrics, list) and metrics and draft.status in ("drafting", "questions_open",
                                                                     "path_pending", "ready", "failed"):
            draft.status = _status_for(payload_data)
    await session.commit()
    return await _draft_view(session, draft)


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
