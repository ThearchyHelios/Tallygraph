"""「已发布且未退役」的工作流版本：发起正式运行、口径升版判定、子图钉版本、应用钉版本都用这一个说法（P5，A1）。

以前三处各有各的判断：发起只认当前发布版（launch.py:92，F1），口径升版取 max(version) 把草稿也算进去（F3）。
工作流每保存一次就生成一个版本快照，没发布过的快照只是草稿：level 为空、也不是当前发布版。

K02 起，改图的几处（画布保存、口径草稿、回滚、发布单的保存版本）都走 append_version / save_graph 这一条路：
条件更新抢版本（CAS）+ 可选的客户端基准版本（expected_revision），旧写抛 StaleRevision；对已经接管的资产
（文档起草建出来的口径卡）缺基准版本就拒。写入幂等（同一主体 + 工作流 + 目标的 request_id 只落一次）也在这里。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import set_committed_value

from app.db.models import CaliberWriteRequest, Workflow, WorkflowVersion
from app.db.models_apps import App, AppVersion
from app.db.models_process import ActionRecord

#: 退役被拒时的机器码
RETIRE_CURRENT = "version_retire_current"
RETIRE_UNPUBLISHED = "version_retire_unpublished"
RETIRE_ALREADY = "version_retire_already"

#: 写入保护的机器码（K02）：客户端拿着的基准版本已经过期；已接管的资产必须给基准版本（老页面照旧写会绕过并发保护）
STALE_REVISION = "stale_revision"
REVISION_REQUIRED = "revision_required"
#: 幂等键的机器码：同一个 request_id 配了不同的内容
IDEMPOTENCY_CONFLICT = "idempotency_conflict"


class WorkflowChanged(ValueError):
    """保存期间另一处已经提交了新版本，需要重新读取后再编辑。"""


class StaleRevision(WorkflowChanged):
    """写入的基准版本对不上：客户端拿的那一版已经过期，或者另一处已经先提交了新版本。

    current 是库里现在的版本号（409 的 current_version）。是 WorkflowChanged 的一种，原来按它兜底的调用方照旧兜得住。
    """

    def __init__(self, message: str, current: int) -> None:
        super().__init__(message)
        self.current = current


def stale_revision_message(current: int, expected: int | None = None) -> str:
    """基准版本过期时给用户的一句话：发生了什么、现在是什么版本、怎么办。

    客户端照旧保留自己的编辑（画布上是冲突横幅给两条路，草稿页上是提示后再点一次），所以这里不要求「刷新」，
    只说清「没替你重发、确认后再提交」。"""
    if expected is None:
        return f"工作流已经有新的修改（现在是 v{current}）：你这次没有写进去，也没有替你重发，请确认后再提交"
    return (f"这份内容基于 v{expected}，现在已经是 v{current}：有人先改过。你刚才的修改还留着，"
            "没有替你重发，确认后可以基于最新的一版再提交一次")


async def append_version(session: AsyncSession, workflow: Workflow, graph: dict[str, Any], *,
                         note: str = "", expected_revision: int | None = None, digest: str | None = None,
                         status: str = "draft") -> int:
    """在当前工作流上换一份新图并新增一版（回滚旧版、发布单按蓝图重建都走这里）：和保存同一条 CAS 规矩。

    expected_revision 是客户端拿着的基准版本（Workflow.version）；给了就先核对，对不上抛 StaleRevision。写的时候按
    读到的版本做条件更新，另一处在这中间提交了新版本（CAS 打不中）也是 StaleRevision，带着库里现在的版本号。
    只新增快照、不动任何旧版本；由调用方提交事务。返回新的版本号。
    """
    from app.core.artifact_store import graph_hash

    previous = workflow.version
    if expected_revision is not None and expected_revision != previous:
        raise StaleRevision(stale_revision_message(previous, expected_revision), previous)
    result = await session.execute(update(Workflow).where(
        Workflow.id == workflow.id, Workflow.version == previous,
    ).values(graph=graph, version=previous + 1, status=status).execution_options(synchronize_session=False))
    if result.rowcount != 1:
        fresh = await session.scalar(select(Workflow.version).where(Workflow.id == workflow.id))
        raise StaleRevision(stale_revision_message(int(fresh or previous)), int(fresh or previous + 1))
    for key, value in (("graph", graph), ("version", previous + 1), ("status", status)):
        set_committed_value(workflow, key, value)
    session.add(WorkflowVersion(workflow_id=workflow.id, version=previous + 1, graph=graph,
                                graph_hash=digest or graph_hash(graph), note=note or ""))
    return previous + 1


async def save_graph(session: AsyncSession, workflow: Workflow, graph: dict[str, Any], *,
                     note: str | None = None, expected_revision: int | None = None) -> bool:
    """画布与口径草稿共用的保存：只新增快照，不改任何旧版本；由调用方提交事务。

    expected_revision 是客户端读到的 Workflow.version（乐观锁）：不给就只靠 CAS 挡住并发写，给了就先把「是不是
    基于最新一版改的」也一起核对——两个页面先后保存旧数组时，后一个被拒，而不是盖掉前一个。
    """
    if graph == workflow.graph:
        if expected_revision is not None and expected_revision != workflow.version:
            raise StaleRevision(stale_revision_message(workflow.version, expected_revision), workflow.version)
        return False
    await append_version(session, workflow, graph, note=note or "", expected_revision=expected_revision)
    return True


# --------------------------------------------------------------------------
# 写入幂等（K02）：同一主体在同一份资产上的同一个 request_id 只落一次
# --------------------------------------------------------------------------

#: claim_write 的三种判定
WRITE_NEW = "new"
WRITE_REPLAY = "replay"
WRITE_CONFLICT = "conflict"


@dataclass(frozen=True)
class WriteClaim:
    """一次写入请求的幂等判定。new：第一次见；replay：同键同内容，按上一次做完的结果交回；conflict：同键不同内容。"""

    state: str
    revision: int | None = None


def write_fingerprint(payload: Any) -> str:
    """这次请求的内容指纹：只算要落下去的那几项（标题、指标、消掉的问题、目标卡），不算基准版本——
    同一个 request_id 带着已经过期的基准版本重放，认得出是同一次请求，不报内容冲突。"""
    from app.core.artifact_store import canonical_json, content_hash

    return content_hash(canonical_json(payload))


def _write_key(*, actor: str | None, workflow_id: str, target: str | None, request_id: str) -> Any:
    return select(CaliberWriteRequest).where(
        CaliberWriteRequest.actor == (actor or ""), CaliberWriteRequest.workflow_id == workflow_id,
        CaliberWriteRequest.card_id == (target or ""), CaliberWriteRequest.request_id == request_id)


async def claim_write(session: AsyncSession, *, actor: str | None, workflow_id: str, target: str | None,
                      request_id: str | None, payload_hash: str) -> WriteClaim:
    """查这一次写入请求是不是已经做过：没给 request_id 就按每次都写（老客户端）。

    顺序重试用得上：上一次已经提交、这次再发的同键同内容请求在这里认出来，直接交回上一次的结果，不再写一遍。
    幂等判定在版本检查之前：重放即使基准版本已经过期也要成功。

    并发下的收敛不靠这里（两次同时查都可能看到「还没有」）：一条路是 CAS 只有一个能写出新版本，另一条路是
    记请求时撞 (actor, workflow_id, card_id, request_id) 唯一键，调用方按 IntegrityError 再查一次，落到重放。
    """
    if not request_id:
        return WriteClaim(WRITE_NEW)
    row = (await session.execute(_write_key(actor=actor, workflow_id=workflow_id, target=target,
                                            request_id=request_id))).scalar_one_or_none()
    if row is None:
        return WriteClaim(WRITE_NEW)
    if row.payload_hash != payload_hash:
        return WriteClaim(WRITE_CONFLICT)
    revision = (row.result or {}).get("revision") if isinstance(row.result, dict) else None
    return WriteClaim(WRITE_REPLAY, revision if isinstance(revision, int) else None)


async def remember_write(session: AsyncSession, *, actor: str | None, workflow_id: str, target: str | None,
                         request_id: str | None, payload_hash: str, revision: int | None) -> None:
    """记下这一次写入请求（和这次改图同一个事务提交）：同键不同内容的下一次来就认得出是冲突。

    顺序重试不用走这里（claim_write 已经按上一次的结果交回了）；两路真并发时两边都可能没查到记录，最后靠
    (actor, workflow_id, card_id, request_id) 唯一键收敛——后到的那一路在这一步撞完整性错误，调用方回滚后
    重查记录：赢的那一路写的图还在，按它的结果当重放交回。
    """
    if not request_id:
        return
    session.add(CaliberWriteRequest(actor=actor or "", workflow_id=workflow_id, card_id=target or "",
                                    request_id=request_id, payload_hash=payload_hash,
                                    result={"revision": revision}))


class RetireRefused(Exception):
    """不退役。status_code / message / code 就是 HTTP 回的状态码、detail、机器码。"""

    def __init__(self, status_code: int, message: str, code: str | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.code = code


def taken_over(workflow: Any) -> bool:
    """这份资产是不是已经接管到工作流上了（K02）：文档起草建出来的口径卡，以及从草稿接上一次运行建出来的
    （docattach）。

    两类都是「口径草稿的产物」，图上只有口径卡和取数，改它要走和草稿页同一条并发规矩。新的起草产物在 origin
    里写 kind=doc_draft；docattach 的产物（含以前建的）没有写 kind，只能按它建时打的标签认——两处都打了
    tags=["doc_draft"]（app/ledger/authoring.py、app/ledger/docattach.py），别的创建路径（手建、蓝图
    blueprint、从运行提取 extracted、固化 certified）都没有这个标签，不会误伤。

    对它的图写入必须带基准版本（expected_revision）：老页面、老客户端照旧无条件写会绕过并发保护，这里拒绝。
    """
    origin = getattr(workflow, "origin", None)
    if isinstance(origin, dict) and origin.get("kind") == "doc_draft":
        return True
    return "doc_draft" in (getattr(workflow, "tags", None) or ())


def is_published(version: Any, *, current: int | None = None) -> bool:
    """这一版能不能被使用：level 不为空且没退役。level 为空的老数据，只认它就是 current（当前发布版）的情形。

    level 是发布时写在这一版上的（api/workflows.py 的 publish_workflow），记级别之前发布的老版本没有；那时只有
    当前发布版能发起正式运行，照旧只认它。"""
    if getattr(version, "retired_at", None) is not None:
        return False
    if getattr(version, "level", None):
        return True
    return current is not None and getattr(version, "version", None) == current


def _published_clause(current: int | None) -> Any:
    """is_published 的 SQL 写法：读不出整行（图很大）也能筛。"""
    level_set = WorkflowVersion.level.is_not(None)
    either = or_(level_set, WorkflowVersion.version == current) if current is not None else level_set
    return and_(either, WorkflowVersion.retired_at.is_(None))


async def _current(session: AsyncSession, workflow_id: str) -> int | None:
    return await session.scalar(select(Workflow.published_version).where(Workflow.id == workflow_id))


async def published_versions(session: AsyncSession, workflow_id: str) -> list[Any]:
    """这个工作流全部「已发布且未退役」的版本（WorkflowVersion），按版本号升序。"""
    current = await _current(session, workflow_id)
    rows = await session.execute(
        select(WorkflowVersion).where(WorkflowVersion.workflow_id == workflow_id, _published_clause(current))
        .order_by(WorkflowVersion.version))
    return list(rows.scalars())


async def latest_published(session: AsyncSession, workflow_id: str) -> Any | None:
    """**最高**的「已发布且未退役」的版本；没有就是 None。

    **只用于升级发现**（K07）：告诉人「有一版更新的」、口径升版的处置判定（engine/governance）都按它比。默认调用
    （目录、look、账本挑候选、正式运行的默认版本）读的是**推荐版**——Workflow.published_version 指的那一版，见
    app/apps/publications.recommended / resolve。重发一版旧版之后，推荐版是那个旧版，这里仍然是最高版。
    """
    current = await _current(session, workflow_id)
    return (await session.execute(
        select(WorkflowVersion).where(WorkflowVersion.workflow_id == workflow_id, _published_clause(current))
        .order_by(WorkflowVersion.version.desc()).limit(1))).scalar_one_or_none()


async def version_row(session: AsyncSession, workflow_id: str, version: int) -> Any | None:
    return (await session.execute(select(WorkflowVersion).where(
        WorkflowVersion.workflow_id == workflow_id, WorkflowVersion.version == version))).scalar_one_or_none()


# --------------------------------------------------------------------------
# 子工作流固定的版本（D-q）
# --------------------------------------------------------------------------

#: 固定的上游版本不能用的机器码：子工作流节点（workflow_version）、口径卡转抄别处的口径（caliber_from）。发起正式运行
#: （engine/launch.py）、发布工作流与发布前检查（engine/lint_process.py 的 lint_subgraph_pins）、应用检查（apps/lint.py）
#: 都按 subgraph_pin_problems 判，机器码相同
SUBGRAPH_UNPUBLISHED = "subgraph_version_unpublished"
SUBGRAPH_RETIRED = "subgraph_version_retired"
CALIBER_UNPUBLISHED = "caliber_version_unpublished"
CALIBER_RETIRED = "caliber_version_retired"


@dataclass(frozen=True)
class SubgraphPinProblem:
    node_id: str
    #: 子工作流节点的标题
    title: str
    code: str
    #: 节点固定的是哪个工作流的哪一版：门禁拿读好的结果去查另一张图（自动修复后的预览）时，按它核对节点没改过
    workflow_id: str
    version: int
    #: 说到「「月报」v3 没有发布过」为止；前后半句（不能发起正式运行 / 正式运行发起时会被拒绝）由调用方接
    reason: str
    #: 怎么改
    advice: str
    #: 句子的主语：「子工作流「…」」或「口径卡「…」」
    who: str = ""
    #: 节点上哪个配置项固定了版本（问题定位到它）
    field: str = "workflow_version"


def pinned_upstream(node: Any) -> tuple[str, str, int] | None:
    """节点固定的上游版本：(种类 subgraph / caliber, 工作流 id, 版本)；没固定就是 None。子工作流写在 workflow_id /
    workflow_version，口径卡转抄别处的口径写在 caliber_from 里。"""
    from app.engine.schema import NodeType

    cfg = node.config
    if node.type == NodeType.SUBGRAPH:
        wid, pinned = cfg.get("workflow_id"), cfg.get("workflow_version")
        kind = "subgraph"
    elif node.type == NodeType.METRICS and isinstance(cfg.get("caliber_from"), dict):
        wid, pinned = cfg["caliber_from"].get("workflow_id"), cfg["caliber_from"].get("workflow_version")
        kind = "caliber"
    else:
        return None
    if not wid or not str(pinned or "").isdigit():
        return None
    return kind, str(wid), int(pinned)


async def subgraph_pin_problems(session: AsyncSession, spec: Any) -> list[SubgraphPinProblem]:
    """图里每个固定了上游版本的节点（子工作流、转抄别处口径的口径卡），固定的那一版必须「已发布且未退役」
    （is_published）；不是的逐个交回。

    固定版本就是方法卡的机制：外层发布时认可的是「用它的 vN」。vN 只是一张保存出来的草稿快照、或者已经退役，正式运行
    照样执行它、照样按它的口径算，就等于绕过了发布门禁、绕过了退役。发起时拦（launch.py），发布时也按同一条拦——不然钉在
    草稿上的工作流照样发得出去，之后每一次正式运行（定时任务、应用入口、按需视图、补算）都在发起时才被拒。
    没固定版本的跟随最新，那是受管级门禁管的事（governed.subgraph_unpinned），这里不管。"""
    out: list[SubgraphPinProblem] = []
    for node in spec.nodes:
        pin = pinned_upstream(node)
        if pin is None:
            continue
        kind, wid, version = pin
        caliber = kind == "caliber"
        who = f"口径卡「{node.title}」" if caliber else f"子工作流「{node.title}」"
        field = "caliber_from" if caliber else "workflow_version"
        unpublished, retired = (CALIBER_UNPUBLISHED, CALIBER_RETIRED) if caliber else (SUBGRAPH_UNPUBLISHED, SUBGRAPH_RETIRED)
        workflow = await session.get(Workflow, wid)
        row = await version_row(session, wid, version) if workflow is not None else None
        if workflow is None or row is None:
            out.append(SubgraphPinProblem(node.id, node.title, unpublished, wid, version,
                                          f"工作流版本 v{version} 不存在", "请在节点中重新选择版本", who, field))
        elif row.retired_at is not None:
            out.append(SubgraphPinProblem(node.id, node.title, retired, wid, version,
                                          f"「{workflow.name}」v{version} 已退役", "请把节点改为固定一个在用的发布版本",
                                          who, field))
        elif not is_published(row, current=workflow.published_version):
            out.append(SubgraphPinProblem(node.id, node.title, unpublished, wid, version,
                                          f"「{workflow.name}」v{version} 没有发布过",
                                          "请先发布那个工作流，或把节点改为固定一个已发布的版本", who, field))
    return out


async def backfill_legacy_levels(session: AsyncSession) -> int:
    """给记级别之前发布过的老版本补上 level（启动时跑一次，幂等）。

    level 是后来才加的列：那之前发布过的版本都是空的，按 is_published 只认当前发布版——而子工作流、口径卡钉住的往往是
    更早的某一版，新规则会把它们误判成「没发布过」。补的依据只有一条，有据可查：这一版发起过正式运行。老代码里正式运行
    只能从当时的发布版发起，所以它一定发布过。级别记 published：当时是不是受管级查不出来，往低处记（应用的级别取最低，
    只会少给信任，不会多给）。没有正式运行凭据的照旧为空，不猜。"""
    from sqlalchemy import exists, update

    from app.db.models import Run

    evidence = exists().where(Run.workflow_id == WorkflowVersion.workflow_id, Run.version == WorkflowVersion.version,
                              Run.run_class == "formal")
    result = await session.execute(update(WorkflowVersion)
                                   .where(WorkflowVersion.level.is_(None), WorkflowVersion.retired_at.is_(None), evidence)
                                   .values(level="published").execution_options(synchronize_session=False))
    return result.rowcount or 0


# --------------------------------------------------------------------------
# 账本（L4）：一个工作流版本被谁固定、生效了几次、正式运行了几次。退役确认、版本详情、删除约束、账本接口共用
# --------------------------------------------------------------------------


async def pinning_apps(session: AsyncSession, workflow_id: str, version: int | None = None) -> list[dict[str, Any]]:
    """当前发布版固定着这个工作流（version 给了就只看这一版）的应用：[{app_id, name, app_version, alias, version}]。

    只看各应用当前的发布版：应用升过级、不再固定它的旧版本不算「正在用」。归档的应用不算。pins 存在 JSON 里，
    应用的数量是人建的、不会很多，读出来逐个看。"""
    rows = (await session.execute(
        select(App.id, App.name, AppVersion.version, AppVersion.pins)
        .join(AppVersion, and_(AppVersion.app_id == App.id, AppVersion.version == App.published_version))
        .where(App.status != "archived"))).all()
    out: list[dict[str, Any]] = []
    for app_id, name, app_version, pins in rows:
        for pin in pins or []:
            if not isinstance(pin, dict) or pin.get("workflow_id") != workflow_id:
                continue
            if version is not None and pin.get("version") != version:
                continue
            out.append({"app_id": app_id, "name": name, "app_version": app_version, "alias": pin.get("alias"),
                        "version": pin.get("version")})
    return out


async def effect_count(session: AsyncSession, workflow_id: str, version: int | None = None) -> int:
    """生效记录的条数（ActionRecord）。演练（simulated）不算生效：它没有发出去。"""
    from sqlalchemy import func

    stmt = select(func.count(ActionRecord.id)).where(ActionRecord.workflow_id == workflow_id,
                                                      ActionRecord.state != "simulated")
    if version is not None:
        stmt = stmt.where(ActionRecord.workflow_version == version)
    return int(await session.scalar(stmt) or 0)


async def formal_run_count(session: AsyncSession, workflow_id: str, version: int) -> int:
    from sqlalchemy import func

    from app.db.models import Run

    return int(await session.scalar(select(func.count(Run.id)).where(
        Run.workflow_id == workflow_id, Run.version == version, Run.run_class == "formal")) or 0)


async def usage(session: AsyncSession, workflow_id: str, version: int) -> dict[str, Any]:
    """{apps, effects, formal_runs}：版本详情、账本接口、退役确认给的同一份。"""
    from app.apps.impact import dependents

    return {"apps": await pinning_apps(session, workflow_id, version),
            "effects": await effect_count(session, workflow_id, version),
            "formal_runs": await formal_run_count(session, workflow_id, version),
            **await dependents(session, workflow_id, version)}


# --------------------------------------------------------------------------
# 退役
# --------------------------------------------------------------------------


async def retire_version(session: AsyncSession, workflow_id: str, version: int, *, actor: str | None,
                         note: str | None) -> dict[str, Any]:
    """退役一个版本。当前发布版不能退（409）。返回 {retired, affected_apps: [...]}；写审计。

    退役之后：从它发起正式运行被拒（run_version_retired）；固定着它的应用标「需升级」，入口拒绝发起新实例，视图保留
    最后一次的数据并写明固定的版本已退役；进行中的实例照常走完——这里不碰任何运行。"""
    from app.apps.audit import record
    from app.db.base import utcnow

    workflow = await session.get(Workflow, workflow_id)
    if workflow is None:
        raise RetireRefused(404, "工作流不存在，可能已被删除")
    row = await version_row(session, workflow_id, version)
    if row is None:
        raise RetireRefused(404, f"「{workflow.name}」不存在 v{version} 版本")
    if row.retired_at is not None:
        raise RetireRefused(409, f"v{version} 已经退役", RETIRE_ALREADY)
    if version == workflow.published_version:
        raise RetireRefused(409, f"v{version} 是当前发布版本，不能退役。请先发布一个新版本，再退役这一版",
                            RETIRE_CURRENT)
    if not is_published(row, current=workflow.published_version):
        raise RetireRefused(409, f"v{version} 没有发布过，不需要退役", RETIRE_UNPUBLISHED)
    from app.apps.impact import dependents

    affected = await pinning_apps(session, workflow_id, version)
    impact = await dependents(session, workflow_id, version)
    row.retired_at = utcnow()
    row.retired_by = actor
    row.retire_note = (note or "").strip() or None
    await record(session, action="workflow_version.retire", subject_kind="workflow_version",
                 subject_id=f"{workflow_id}:{version}", actor=actor,
                 data={"workflow_id": workflow_id, "workflow_name": workflow.name, "version": version,
                       "note": row.retire_note, "affected_apps": affected, "impact": impact})
    await session.commit()
    return {"retired": {"workflow_id": workflow_id, "version": version, "retired_at": row.retired_at,
                        "retired_by": actor, "retire_note": row.retire_note},
            "affected_apps": affected, "impact": impact}
