"""最小发布依据（K07）：每次发布留一份不可变清单，正式用途按它判。

**清单是什么。** 发布（api/workflows.py 的 commit_publish——发布只有一个入口，发布单也走它）时按这一版**不可变**的图
算出一份确定的内容，存进内容寻址工件库（kind=caliber_publication，格式 agentlab.publication/1），再在
workflow_publications 上**追加**一行发布事件。清单固定：图指纹、每张源头口径卡的定义指纹/绑定指纹/取数语义指纹/指标
清单、参数契约、当前的取数依赖（数据源、表、固定版本的上游）、编译器约定（图/绑定/参数契约/验证方法，以及依赖闭包的
解析器版本；K09 的固定执行按 `compiler_problems` 逐项核对），以及发布这一刻引用的 ValidationRecord 与 ReviewDecision。

**清单尽量确定。** 内容只由原始输入决定：这一版不可变的图 + 它引用的那两行记录（定义/绑定/语义指纹、指标、状态、范围
这些**值**都写进内容，同时记下它们的 id）。发布人、发布时间、目录/结构/能力版本、绑定工具现在在不在，这些是**观察到的
环境**，不进内容，单独记在事件行的 `observed` 上。于是：

- 同一版重发一次、引用的记录也没变时，清单内容一字不差（工件哈希相同，只多一行事件）；
- 原始输入还在时任何一刻都能按它重建同一份内容（rebuild）；重建不上（版本行没了、引用的记录被删了、工件读不出来）
  一律**明确失败**并给机器码，不猜、不拿别的版本顶。

**重发旧版。** 每次发布都追加新事件，旧事件和旧清单一个字不改。发布指针 Workflow.published_version 是**推荐指针**
（默认调用读哪一版），最新一次发布事件和它一致；最高已发布版另算（apps/versions.latest_published，只用于升级发现，
见 upgrade_of）。**显式版本请求始终固定到指定的那一版**。老资产不 backfill：加这张表之前发布过的版本没有事件，
读的时候按「老资产」处理（身份 formal_legacy），不补造历史。

**用途判定（purpose）。** purpose 由**服务端入口常量**决定，不由客户端传：trial（试算/试运行）、explore（探索：问数据、
画布、目录）、compose（组合式问数选用口径）、formal（正式运行）、issue（按口径出具）。只有 formal / issue 会因为发布
依据不足被拒；试算、探索、组合一条都不受这套门禁影响（A03、A07：口径不是所有问数的前置手续）。

**requires_proof。** 只对「这一版有发布事件、而且**发布时要求过证明**（事件行上的 required）或**现在还接管着**
（apps/versions.taken_over）」的资产启用——两者取或：发布那一刻的严格性记在事件行上，之后把接管标记去掉（清 tags 之类）
不会让已经按严格发的那些版本降级；发布之后才接管上的照旧要证明。正式运行/出具时，这一版的发布依据必须**过这几道**：
先核工件本身（`read()` 复验内容哈希、就是这一版这一份，`compiler_problems()` 核编译器约定——和 K09 的固定执行同一份
核对，工件被删/被改/按别的约定物化的都不能继续正式执行）；再核清单是不是**就是这一版这一张图**的（图指纹对得上、
图上的源头口径卡一张不缺）；再逐张卡比清单里引用的那一对记录/审阅（**最新的那条**覆盖这一版的验证记录是 matched，
并且有认证主体确认**覆盖这份记录全部指标**的审阅决定——只确认一部分不算，同 K05：空范围、部分范围都不算整张卡被确认，
不在这时候另挑更新的记录补、清单里没有的卡也不现场挑一条顶上）；最后按同一用途**递归判转抄卡（caliber_from）固定引用的
上游那一张卡**——上游要证明就得同样满足，失败按上游那条机器码加下游引用拒，只判被引用的那张卡、循环引用截断、上游是老
资产的不升级。不满足各有机器码：清单工件不在/被改过/不是这一份、编译器约定不认、清单不是这一张图的/缺卡、缺记录、旧
记录不覆盖这一版、不是 matched、审阅被驳回、只确认了一部分、引用的记录/审阅不在。老资产（这一版没有清单）保持原行为，
只是记身份 formal_legacy；不需要证明的记 formal_unproven——**两者都不是 verified**。
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from sqlalchemy import select

from app.apps import versions as app_versions

#: 清单工件的种类与内容格式
KIND = "caliber_publication"
FORMAT = "agentlab.publication/1"

# --------------------------------------------------------------------------
# 用途（服务端入口常量）与身份
# --------------------------------------------------------------------------

#: 试算/试运行（口径试跑、蓝图试运行）：探索运行，不继承正式身份
PURPOSE_TRIAL = "trial"
#: 探索：问数据当场搭的、画布试跑、目录与 look 的默认查看、账本比对挑候选
PURPOSE_EXPLORE = "explore"
#: 组合式问数选用一份口径（ledger/compose）
PURPOSE_COMPOSE = "compose"
#: 正式运行（engine/launch.launch_run 的 run_class=formal）
PURPOSE_FORMAL = "formal"
#: 按口径出具（ledger/issue）
PURPOSE_ISSUE = "issue"

PURPOSES = (PURPOSE_TRIAL, PURPOSE_EXPLORE, PURPOSE_COMPOSE, PURPOSE_FORMAL, PURPOSE_ISSUE)
#: 会按发布依据拦的用途：只有正式运行和出具
PROOF_PURPOSES = (PURPOSE_FORMAL, PURPOSE_ISSUE)

#: 正式运行的身份。**没有 verified 这一档**：发布依据齐全才叫 proven，需要证明而没证明的叫 unproven，
#: 老资产（这一版没有清单）叫 legacy——后两者都保持原来的行为，但都不能被说成「验证过了」
PROVEN = "formal_proven"
UNPROVEN = "formal_unproven"
LEGACY = "formal_legacy"
IDENTITIES = (PROVEN, UNPROVEN, LEGACY)

# --------------------------------------------------------------------------
# 机器码（拒绝时给程序认）
# --------------------------------------------------------------------------

#: 这一版没有发布依据（没有发布事件）：老资产或还没发布过
BASIS_MISSING = "publication_basis_missing"
#: 清单与这一版对不上（定义/绑定/取数语义指纹不同）：不拿它证明这一版
BASIS_MISMATCH = "publication_basis_mismatch"
#: 这张卡没有留下任何验证记录
RECORD_MISSING = "publication_record_missing"
#: 有记录，但没有一条覆盖这一版的定义/绑定/取数语义（旧记录、改过定义）
RECORD_STALE = "publication_record_stale"
#: 有覆盖这一版的记录，但不是 matched（observed / unverified / mismatched / failed）
RECORD_NOT_MATCHED = "publication_record_not_matched"
#: 没有审阅决定
REVIEW_MISSING = "publication_review_missing"
#: 审阅决定绑的依据不是这一版（定义/绑定改过之后）
REVIEW_STALE = "publication_review_stale"
#: 审阅驳回了
REVIEW_REJECTED = "publication_review_rejected"
#: 只确认了一部分指标：部分审阅不算覆盖全部指标
REVIEW_PARTIAL = "publication_review_partial"
#: 审阅不是认证主体做的（这套部署开着登录，而那条决定记的是自报署名）
REVIEW_UNVERIFIED = "publication_review_unverified"
#: 清单引用的验证记录/审阅决定已经不在库里（原始输入缺失，重建不上）
INPUTS_MISSING = "publication_inputs_missing"
#: 清单工件不在
MANIFEST_MISSING = "publication_manifest_missing"
#: 清单工件与哈希不符（被改过）
MANIFEST_TAMPERED = "publication_manifest_tampered"
#: 工件不是这一版这一份清单
MANIFEST_FOREIGN = "publication_manifest_foreign"
#: 清单里没有编译器约定（认不出它是按哪一套物化的）
COMPILER_MISSING = "publication_compiler_missing"
#: 清单里的编译器约定和现在这一套对不上（A09：该拒的是执行，不是按新规则重解释旧发布）
COMPILER_MISMATCH = "publication_compiler_mismatch"

#: 拒绝时给的那句话（机器码 → 白话），reason 更具体时用 reason
CODE_TEXT = {
    BASIS_MISSING: "这一版没有发布依据（发布清单）",
    BASIS_MISMATCH: "这一版的发布清单和它现在的定义/绑定对不上",
    RECORD_MISSING: "这一版没有留下任何验证记录",
    RECORD_STALE: "这一版现有的验证记录都不是对着它现在的定义和绑定做的",
    RECORD_NOT_MATCHED: "这一版最新的验证记录不是「有独立期望、逐条对得上」的（matched）",
    REVIEW_MISSING: "这一版的验证记录还没有经过认证审阅",
    REVIEW_STALE: "这一版的审阅决定绑的是改之前的定义/绑定",
    REVIEW_REJECTED: "这一版的验证记录被审阅驳回了",
    REVIEW_PARTIAL: "这一版的审阅只确认了一部分指标",
    REVIEW_UNVERIFIED: "这一版的审阅不是认证主体做的",
    INPUTS_MISSING: "这一版发布依据引用的验证记录或审阅决定已经不在库里，重建不上",
}


# --------------------------------------------------------------------------
# 编译器与验证方法的版本（A09：执行时禁止按新版编译器悄悄重解释旧发布物）
# --------------------------------------------------------------------------


#: 清单自己的约定版本（格式串里那一版的编号）
MANIFEST_VERSION = 1
#: 执行定义的规范形式版本：和 core/artifact_store.execution_content 的 schema_version 对应（图指纹按它算）
GRAPH_SCHEMA = 1
#: 参数契约里每条字段的键（app/ledger/params.contract 的形状）
CONTRACT_FIELDS = ("name", "type", "required", "default")
#: 编译器约定里必须逐项核对的键（K09 的固定执行按它判「这一版认不认这份清单」）：少一项、多一项都算不兼容
COMPILER_KEYS = ("manifest", "graph_schema", "binding", "parameters", "validation_method", "resolver")


def compiler_versions() -> dict[str, Any]:
    """这份清单是按哪一套确定性约定物化出来的：图/绑定的编译器版本、参数契约的承载方式、验证方法的版本、
    依赖闭包的解析器版本。

    A09：执行时不许按新版编译器悄悄重解释旧发布物。清单里把它写死，读的人（和以后换实现的人）能看出这一版是按
    哪一版约定物化的；版本不兼容时该拒绝的是执行，不是拿新规则去解释旧依据（`compiler_problems` 是那道核对）。
    """
    from app.ledger import bindings, closure, params, validation

    return {
        "manifest": MANIFEST_VERSION,
        "graph_schema": GRAPH_SCHEMA,
        "binding": {"binder": bindings.BINDER, "version": bindings.BINDER_VERSION},
        "parameters": {"adapter": params.ADAPTER, "contract_fields": list(CONTRACT_FIELDS)},
        "validation_method": validation.METHOD_VERSION,
        "resolver": closure.RESOLVER_VERSION,
    }


def compiler_problems(content: Mapping[str, Any]) -> list[dict[str, Any]]:
    """这份清单物化时用的那一套约定，现在这一套认不认（逐项比对 `compiler_versions()`）。

    对不上、缺项、多出认不出的项都给一条 {code, key, message}：调用方据此**拒绝对这一版的固定执行**，绝不按新规则
    现场重解释旧发布。老清单（加 resolver 这一项之前发的）缺 `resolver` 也在这里拒——认不出它按哪一版闭包解析的，
    就不该假装认得。
    """
    from app.core.artifact_store import canonical_json

    recorded = content.get("compiler") if isinstance(content, Mapping) else None
    if not isinstance(recorded, Mapping):
        return [{"code": COMPILER_MISSING, "key": "compiler", "message": "发布清单里没有编译器约定："
                "认不出它是按哪一套确定性约定物化的，不能按现在这一套执行"}]
    wanted = compiler_versions()
    problems: list[dict[str, Any]] = []
    for key in COMPILER_KEYS:
        if canonical_json(recorded.get(key)) != canonical_json(wanted.get(key)):
            problems.append({"code": COMPILER_MISMATCH, "key": key,
                             "message": f"发布清单里的「{key}」约定（{canonical_json(recorded.get(key))}）"
                                        f"和现在这一套（{canonical_json(wanted.get(key))}）不一致"})
    for key in sorted(set(recorded) - set(COMPILER_KEYS)):
        problems.append({"code": COMPILER_MISMATCH, "key": str(key),
                         "message": f"发布清单里有一项现在认不出的编译约定「{key}」"})
    return problems


# --------------------------------------------------------------------------
# 内容：图上的确定部分
# --------------------------------------------------------------------------


def source_cards(graph: Any) -> list[dict[str, Any]]:
    """图里的源头口径卡：[{id, title, caliber, caliber_version}]，按节点在图里的先后。

    和 K02/K03 认权威写入源是同一套（api/ledger.editable_cards）：只有源头口径卡（metrics 节点、没有 caliber_from），
    转抄别处的口径卡不算一份自己的定义。
    """
    from app.apps.tree import node_map

    out: list[dict[str, Any]] = []
    for node_id, node in node_map(graph).items():
        if node.get("type") != "metrics" or (node.get("config") or {}).get("caliber_from") not in (None, "", {}):
            continue
        config = node.get("config") or {}
        out.append({"id": node_id, "title": str(node.get("title") or node_id),
                    "caliber": str(config.get("caliber") or ""),
                    "caliber_version": str(config.get("caliber_version") or "")})
    return out


def fetch_dependencies(graph: Any) -> tuple[list[str], list[str]]:
    """图里取数用的数据源名和表名（去库名前缀），按名字排序。

    只认调用数据库查询工具、工具名不带模板、并且写了 SQL 的节点——模板拼出来的工具名认不出「哪一版库」。
    **不读 SQL 正文**：SQL 里可能写着遮罩值，清单只记依赖的名字。
    """
    from app.apps.tree import node_map
    from app.engine.evidence import sql_tables
    from app.tools.datasource import QUERY_PREFIX

    sources: set[str] = set()
    tables: set[str] = set()
    for node in node_map(graph).values():
        if node.get("type") != "tool":
            continue
        config = node.get("config") or {}
        tool = str(config.get("tool") or "")
        args = config.get("args")
        sql = args.get("sql") if isinstance(args, dict) else None
        if not tool.startswith(QUERY_PREFIX) or "{{" in tool or not isinstance(sql, str) or not sql.strip():
            continue
        sources.add(tool[len(QUERY_PREFIX):])
        tables.update(table.rsplit(".", 1)[-1] for table in sql_tables(sql))
    return sorted(sources), sorted(tables)


def upstream_pins(graph: Any) -> list[dict[str, Any]]:
    """图里固定了上游版本的节点（子工作流、转抄别处的口径卡）：[{node_id, kind, workflow_id, version}]，按节点 id 排。

    固定的一版就是这一版的依赖之一：发布依据要把它写下来，之后那一边退役/改版时才知道牵动了谁。
    """
    from app.apps.versions import pinned_upstream
    from app.engine.schema import GraphSpec

    try:
        spec = GraphSpec.model_validate(graph or {})
    except Exception:  # noqa: BLE001 - 图不成形：依赖这一段留空，图本身另有校验拦
        return []
    out: list[dict[str, Any]] = []
    for node in spec.nodes:
        pin = pinned_upstream(node)
        if pin is None:
            continue
        kind, workflow_id, version = pin
        out.append({"node_id": node.id, "kind": kind, "workflow_id": workflow_id, "version": version})
    return sorted(out, key=lambda item: item["node_id"])


def parameters_of(graph: Any) -> list[dict[str, Any]]:
    """这一版的参数契约：入口字段按声明顺序（app/ledger/params.contract，各入口同一份）。"""
    from app.ledger import params
    from app.ledger.derive import entry_fields

    return params.contract(entry_fields(graph or {}))


# --------------------------------------------------------------------------
# 每张卡的依据：验证记录 + 审阅决定
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class CardBasis:
    """一张源头口径卡在这一版上的依据：定义/绑定/取数语义指纹 + 引用的验证记录与审阅决定 + 齐不齐。"""

    card_id: str
    title: str
    caliber: str
    caliber_version: str
    metric_ids: tuple[str, ...] = ()
    definition_hash: str = ""
    semantic_hash: str = ""
    binding_fingerprints: dict[str, str] = field(default_factory=dict)
    binding_status: str = ""
    #: 引用的验证记录与审阅决定（就是清单里的那两段；没有覆盖这一版的记录时是 None）
    record: dict[str, Any] | None = None
    review: dict[str, Any] | None = None
    ok: bool = False
    code: str | None = None
    reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        """清单内容里这一张卡的那一段（工件、事件行的 cards 都是这一份）。"""
        return {"card_id": self.card_id, "title": self.title, "caliber": self.caliber,
                "caliber_version": self.caliber_version, "metric_ids": list(self.metric_ids),
                "definition_hash": self.definition_hash, "semantic_hash": self.semantic_hash,
                "binding_fingerprints": dict(self.binding_fingerprints), "binding_status": self.binding_status,
                "validation": dict(self.record) if self.record else None,
                "review": dict(self.review) if self.review else None,
                "proof": {"ok": bool(self.ok), "code": self.code, "reason": self.reason,
                          **({} if self.ok else {"message": CODE_TEXT.get(str(self.code), "")})}}


def _card_base(graph: Any, card_id: str, *, workflow_id: str) -> CardBasis:
    """一张卡的指纹部分（不含记录/审阅）：定义、绑定、取数语义、指标清单、绑定状态。卡的名字也按图取。"""
    from app.ledger import bindings, validation

    info = next((card for card in source_cards(graph) if card["id"] == card_id), {})
    materials = validation.materials(graph, card_id, workflow_id=workflow_id)
    metrics = validation.card_metrics(graph, card_id)
    try:
        binding_status = str(bindings.analyze(graph, card_id).status)
    except Exception:  # noqa: BLE001 - 绑定状态算不出来不该让发布没依据，照实留空
        binding_status = ""
    return CardBasis(card_id=card_id, title=str(info.get("title") or card_id),
                     caliber=str(info.get("caliber") or ""), caliber_version=str(info.get("caliber_version") or ""),
                     metric_ids=tuple(str(m["id"]) for m in metrics),
                     definition_hash=str(materials["definition_hash"]),
                     semantic_hash=str(materials["semantic_hash"]),
                     binding_fingerprints={str(k): str(v) for k, v in materials["binding_fingerprints"].items()},
                     binding_status=binding_status)


def record_reference(record: Any) -> dict[str, Any]:
    """清单里对 ValidationRecord 的那一段引用：绑定的依据、结论、范围、方法版本、运行引用。"""
    return {"record_id": str(getattr(record, "id", "")),
            "revision": int(getattr(record, "version", 0) or 0),
            "status": str(getattr(record, "status", "") or ""),
            "metric_ids": [str(m) for m in getattr(record, "metric_ids", None) or []],
            "definition_hash": str(getattr(record, "definition_hash", "") or ""),
            "binding_fingerprints": {str(k): str(v) for k, v in (getattr(record, "binding_fingerprints", None) or {}).items()},
            "semantic_hash": str(getattr(record, "semantic_hash", "") or ""),
            "execution_hash": str(getattr(record, "execution_hash", "") or ""),
            "method_version": int(getattr(record, "method_version", 0) or 0),
            "expectation_source": str(getattr(record, "expectation_source", "") or ""),
            "expectation_hash": str(getattr(record, "expectation_hash", "") or ""),
            "inputs_hash": str(getattr(record, "inputs_hash", "") or ""),
            "run_id": str(getattr(record, "run_id", "") or "")}


def review_reference(decision: Any) -> dict[str, Any]:
    """清单里对 ReviewDecision 的那一段引用：决定、确认范围、所审依据、是不是认证主体做的。

    **不写审批人和时间**：actor 是审阅那一行的属性，按 decision_id 去它那一行查；清单内容要能被重建，不能掺进会变的
    东西（时间），也不能掺进身份（同一份依据换个人来确认，内容仍然只是「这份依据被确认了」）。
    """
    return {"decision_id": str(getattr(decision, "id", "")),
            "decision": str(getattr(decision, "decision", "") or ""),
            "scope": [str(m) for m in getattr(decision, "scope", None) or []],
            "verified": bool(getattr(decision, "verified", False)),
            "basis": {str(k): v for k, v in (getattr(decision, "basis", None) or {}).items()}}


def authenticated(decision: Any) -> bool:
    """这条审阅决定算不算「认证主体做出的」：主体认证过，或者这套部署压根没开登录（那时身份由部署模式决定，
    K05 照实记 verified=False，但服务端主体就是权威）。"""
    if bool(getattr(decision, "verified", False)):
        return True
    from app.auth import current_mode

    return current_mode() == "off"


async def _records_for(session: Any, *, workflow_id: str, card_id: str) -> list[Any]:
    """这张卡在这个工作流上的验证记录，新的在前。"""
    from app.db.models import CaliberValidationRecord

    rows = await session.execute(select(CaliberValidationRecord).where(
        CaliberValidationRecord.workflow_id == workflow_id,
        CaliberValidationRecord.card_id == card_id).order_by(
        CaliberValidationRecord.created_at.desc(), CaliberValidationRecord.id.desc()))
    return list(rows.scalars())


async def _latest_decision(session: Any, record_id: str) -> Any:
    """这条验证记录上最新的一次审阅决定（读整行：要按它自己的依据和范围判）。写的时候只追加，取最后一条。"""
    from app.db.models import CaliberReviewDecision

    return (await session.execute(
        select(CaliberReviewDecision).where(CaliberReviewDecision.record_id == str(record_id))
        .order_by(CaliberReviewDecision.created_at.desc(), CaliberReviewDecision.id.desc()).limit(1))
    ).scalar_one_or_none()


def _judge_pair(base: CardBasis, *, record: Any, decision: Any, current: Mapping[str, Any]) -> CardBasis:
    """一份记录 + 它的审阅决定够不够当这一版的依据（不足就在 code/reason 里说清是哪一种）。"""
    from app.ledger import review as review_mod, validation

    if record is None:
        return replace(base, code=RECORD_MISSING, reason="这一版没有留下验证记录：先试运行一次并给出独立期望")
    if not validation.covers_current(record, current=current):
        return replace(base, code=RECORD_STALE, reason="这一版现有的验证记录都不是对着它现在的定义和绑定做的："
                                                       "定义、取数或绑定改过，请重新试运行并把期望给全")
    if str(getattr(record, "status", "")) != validation.MATCHED:
        return replace(base, code=RECORD_NOT_MATCHED,
                       reason=f"这一版最新的记录是「{getattr(record, 'status', '')}」："
                              "要「有独立期望、逐条对得上」的 matched 记录才能算依据")
    if decision is None:
        return replace(base, code=REVIEW_MISSING, reason="这一版的验证记录还没有经过审阅确认：匹配不等于批准")
    if not review_mod.covers_current(decision, current=current):
        return replace(base, code=REVIEW_STALE, reason="这条审阅决定绑的是改之前的定义或绑定，不算覆盖这一版")
    if str(getattr(decision, "decision", "")) != review_mod.CONFIRMED:
        return replace(base, code=REVIEW_REJECTED, reason="这一版的验证记录被审阅驳回了")
    if not authenticated(decision):
        return replace(base, code=REVIEW_UNVERIFIED, reason="这条审阅决定不是认证主体做的（关登录时的自报署名不算）")
    covered = [str(m) for m in getattr(record, "metric_ids", None) or []]
    scope = [str(m) for m in getattr(decision, "scope", None) or []]
    missing = [mid for mid in covered if mid not in scope]
    if missing:
        return replace(base, code=REVIEW_PARTIAL,
                       reason=f"审阅只确认了 {len(scope)} 条，还有 {len(missing)} 条没被确认"
                              "（确认范围要盖住这份记录的全部指标才算整张卡被确认）")
    return replace(base, record=record_reference(record), review=review_reference(decision), ok=True)


async def card_basis(session: Any, *, workflow_id: str, graph: Any, card_id: str, saved: Any = None) -> CardBasis:
    """一张源头口径卡在这一版上的依据。

    `saved` 不给（发布时）：认这张卡的记录里**最新的那条覆盖当前定义/绑定/取数语义的**（K05 的卡级结论按的就是它：
    之后又试运行过一次、那一次不是 matched，说明最新一次核对的结论就不是「对得上」，不能拿更早的那条顶上），再看它
    有没有认证确认盖住全部指标的审阅决定。`saved` 给了（正式用途核对发布事件时）：只认清单里引用的那一对记录/审阅
    ——**不在那时候另挑更新的记录**，不然「发布依据」就成了随时可以补的后门；引用的行不在了就明确说原始输入缺失。
    """
    from app.db.models import CaliberReviewDecision, CaliberValidationRecord
    from app.ledger import review as review_mod, validation

    base = _card_base(graph, card_id, workflow_id=workflow_id)
    current = {"workflow_id": workflow_id, "card_id": card_id, "definition_hash": base.definition_hash,
               "binding_fingerprints": dict(base.binding_fingerprints), "semantic_hash": base.semantic_hash}
    if saved is not None:
        if (str(saved.get("definition_hash") or "") != base.definition_hash
                or str(saved.get("semantic_hash") or "") != base.semantic_hash
                or dict(saved.get("binding_fingerprints") or {}) != dict(base.binding_fingerprints)):
            return replace(base, code=BASIS_MISMATCH,
                           reason="这份发布清单和这一版现在的定义/绑定对不上：不拿它证明这一版")
        proof = saved.get("proof") if isinstance(saved.get("proof"), Mapping) else {}
        ref = saved.get("validation") if isinstance(saved.get("validation"), Mapping) else None
        if not proof.get("ok") or ref is None:
            return replace(base, code=str(proof.get("code") or RECORD_MISSING),
                           reason=str(proof.get("reason") or CODE_TEXT.get(str(proof.get("code")), "")))
        record = await session.get(CaliberValidationRecord, str(ref.get("record_id") or ""))
        decision = await session.get(CaliberReviewDecision, str((saved.get("review") or {}).get("decision_id") or ""))
        if record is None or decision is None:
            return replace(base, code=INPUTS_MISSING, reason=CODE_TEXT[INPUTS_MISSING])
        return _judge_pair(base, record=record, decision=decision, current=current)
    records = await _records_for(session, workflow_id=workflow_id, card_id=card_id)
    if not records:
        return replace(base, code=RECORD_MISSING, reason=CODE_TEXT[RECORD_MISSING])
    covering = [row for row in records if validation.covers_current(row, current=current)]
    if not covering:
        return replace(base, code=RECORD_STALE, reason=CODE_TEXT[RECORD_STALE])
    record = covering[0]
    return _judge_pair(base, record=record, decision=await _latest_decision(session, record.id), current=current)


async def basis_for(session: Any, *, workflow_id: str, graph: Any) -> list[CardBasis]:
    """这一版每张源头口径卡的依据（发布时算，也用于「需不需要证明」）。"""
    return [await card_basis(session, workflow_id=workflow_id, graph=graph, card_id=card["id"])
            for card in source_cards(graph)]


async def saved_basis(session: Any, *, workflow_id: str, graph: Any, content: Mapping[str, Any],
                      card_ids: Sequence[str] | None = None) -> list[CardBasis]:
    """正式用途核对发布事件时这一版每张源头口径卡的依据：**只认清单内容**——逐张卡读清单里引用的那一对记录与审阅。

    比的一律是**工件内容**（read() 读出来那一份），不是可变的事件行列。清单里没有这张卡时按 BASIS_MISMATCH 判，
    **不在这时候去库里另挑一条记录顶上**（发布依据只能在发布那一刻定下来，另外挑就是给它留一个随时可补的后门）。
    `card_ids` 给了就只判这几张（转抄卡递归判上游那一张卡时用），不给就是图里全部源头口径卡。老资产（这一版没有
    发布事件）不走这里：调用方先判 publication is None。
    """
    stored = {str(card.get("card_id")): card for card in (content.get("cards") or []) if isinstance(card, Mapping)}
    wanted = None if card_ids is None else {str(cid) for cid in card_ids}
    out: list[CardBasis] = []
    for card in source_cards(graph):
        card_id = str(card["id"])
        if wanted is not None and card_id not in wanted:
            continue
        saved = stored.get(card_id)
        if saved is None:
            base = _card_base(graph, card_id, workflow_id=workflow_id)
            out.append(replace(base, code=BASIS_MISMATCH,
                               reason="发布清单里没有这张源头口径卡：这一版没有它的发布依据"))
            continue
        out.append(await card_basis(session, workflow_id=workflow_id, graph=graph, card_id=card_id, saved=saved))
    return out


def manifest_problem(manifest: Any, *, graph: Any) -> dict[str, Any] | None:
    """这份清单是不是**这一版这一张图**的清单：图指纹对不上（错图）、清单里没有图上的某张源头口径卡（缺卡）
    都交回一条版本级的 problem（code=BASIS_MISMATCH），对上了交回 None。

    `manifest` 给**清单内容**（工件读出来的那一份）——比的一律是工件，不认可变的事件行列；给事件行时这里自己读一遍
    工件（读不出来就按那条机器码交回；调用方已经读过就把内容传进来，别读两遍）。发布依据只能在发布那一刻定下来，
    所以这里**不补**：缺的那张卡不去库里另挑一条记录顶上，错图也不拿别的版本顶。老资产（这一版没有发布事件）不走
    这里——调用方先判 publication is None，保持原行为。
    """
    from app.core.artifact_store import graph_hash

    content = manifest if isinstance(manifest, Mapping) else None
    if content is None:
        content, problems = verified_content(manifest)
        if content is None:
            return problems[0] if problems else None
    fixed = graph if isinstance(graph, Mapping) else {}
    if str(content.get("graph_hash") or "") != graph_hash(fixed):
        return {"card_id": "", "code": BASIS_MISMATCH,
                "message": "这一版的发布清单不是这一张图的清单（图指纹对不上）：不拿它证明这一版",
                "reason": "这一版的发布清单不是这一张图的清单"}
    stored = {str(card.get("card_id")) for card in (content.get("cards") or []) if isinstance(card, Mapping)}
    for card in source_cards(fixed):
        if str(card["id"]) in stored:
            continue
        return {"card_id": str(card["id"]), "code": BASIS_MISMATCH,
                "message": f"发布清单里没有这一版图上的源头口径卡「{card['title']}」：这张卡没有发布依据",
                "reason": "发布清单没有覆盖这一版图上的每一张源头口径卡：缺的那张不在这时候另挑记录补"}
    return None


# --------------------------------------------------------------------------
# 转抄卡（caliber_from）：上游那一张卡按同一个用途递归判
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Chained:
    """转抄卡递归判上游的结果：上游这一版要不要证明、以及翻成下游看得见的形状的问题。"""

    required: bool = False
    problems: tuple[dict[str, Any], ...] = ()


def chained_nodes(graph: Any) -> list[dict[str, Any]]:
    """图里转抄别处口径的卡（metrics 节点带 caliber_from）：[{node_id, title, config, workflow_id, version, card_id}]，
    按节点在图里的先后。

    引用不完整（缺工作流、缺卡、版本号不是正整数）的跳过：那是发布门禁（schema 的 caliber_from 检查报 error）和
    运行时各自的活，不是发布依据这一层的事。
    """
    from app.apps.tree import node_map

    out: list[dict[str, Any]] = []
    for node_id, node in node_map(graph).items():
        if node.get("type") != "metrics":
            continue
        ref = (node.get("config") or {}).get("caliber_from")
        if not isinstance(ref, Mapping):
            continue
        raw = ref.get("workflow_version")
        if not str(ref.get("workflow_id") or "") or not str(ref.get("node_id") or "") \
                or isinstance(raw, bool) or not str(raw or "").isdigit() or int(raw) < 1:
            continue
        out.append({"node_id": node_id, "title": str(node.get("title") or node_id),
                    "config": dict(node.get("config") or {}),
                    "workflow_id": str(ref["workflow_id"]), "version": int(raw),
                    "card_id": str(ref["node_id"])})
    return out


def _version_required(workflow: Any, publication: Any) -> bool:
    """这一版要不要证明：发布时要求过（事件行 required）或现在还接管着——和 requires_proof_for 同一条规则，只是不要求
    「图里有源头口径卡」：转抄卡自己不是源头卡，它要证明是因为它引用的上游要。"""
    return bool(getattr(publication, "required", False)) or bool(app_versions.taken_over(workflow))


async def chained_problems(session: Any, *, graph: Any, purpose: str,
                           seen: set[tuple[str, int, str]] | None = None) -> Chained:
    """图里每张转抄卡（caliber_from）固定引用的上游**那一张卡**：按同一个用途递归判它够不够。

    只在会按发布依据拦的用途（formal / issue）上判——试算、探索、组合一条都不受影响。只判被引用的那一张卡：
    上游工作流里别的卡与下游无关，不牵连。上游那一版没有发布事件（老资产）的**明确不升级**，保持原行为；循环引用
    （A 引 B、B 又引回 A）按「这一对已经判过」截断，不转圈。同一个上游被图上多处引用时只在第一处说（同一份问题说两遍
    没有意义）。链子**每一层**的问题都保留（工件、编译器、覆盖，以及那一层自己要求证明时的依据），只有「链子上没有任何
    一层要求证明」时一条都不报——那几层自己按 formal 发起也就是 formal_unproven（老资产、不在证明范围内），下游不另立
    一套更严的说法。问题都翻成下游看得见的形状：机器码照旧（上游是什么码就是什么码）+ 上游引用与整条引用链，下游入口
    照旧只认 problems[0]。
    """
    if purpose not in PROOF_PURPOSES:
        return Chained()
    seen = set() if seen is None else seen
    required = False
    problems: list[dict[str, Any]] = []
    for node in chained_nodes(graph):
        made = await _pinned_card(session, node=node, purpose=purpose, seen=seen)
        required = required or made.required
        problems.extend(made.problems)
    if not required:
        return Chained()
    return Chained(required=True, problems=tuple(problems))


async def _pinned_card(session: Any, *, node: Mapping[str, Any], purpose: str,
                       seen: set[tuple[str, int, str]]) -> Chained:
    """一个转抄卡节点固定引用的上游那一张卡：递归判（那张卡自己也是转抄卡时继续往下走一层）。

    这一层的工件/编译/覆盖问题、以及被引用那张卡自己的依据问题都在这里收着往上报，递归到哪一层都不丢：链子上谁坏了都
    算这条链子过不了（上层正常也不能替下层顶）。node 可以是 `chained_nodes` 给的那一项，也可以是图里节点本身
    （`node_map` 那种形状）——两处都带 config。"""
    from app.apps.tree import node_map
    from app.db.models import Workflow

    node_id = str(node.get("node_id") or node.get("id") or "")
    title = str(node.get("title") or node_id)
    ref = (node.get("config") or {}).get("caliber_from")
    if not isinstance(ref, Mapping):
        return Chained()
    workflow_id, card_id = str(ref.get("workflow_id") or ""), str(ref.get("node_id") or "")
    raw = ref.get("workflow_version")
    if not workflow_id or not card_id or isinstance(raw, bool) or not str(raw or "").isdigit() or int(raw) < 1:
        return Chained()
    version = int(raw)
    key = (workflow_id, version, card_id)
    if key in seen:
        return Chained()
    seen.add(key)
    workflow = await session.get(Workflow, workflow_id)
    row = await app_versions.version_row(session, workflow_id, version)
    if workflow is None or row is None:
        return Chained()          # 上游不在：「固定那一版必须已发布」在发起时另有门禁，运行时也响亮失败
    publication = await latest(session, workflow_id, version)
    if publication is None:
        return Chained()          # 老资产（那一版没有发布事件）：明确不升级，保持原行为
    upstream_graph = row.graph if isinstance(row.graph, Mapping) else {}
    content, problems = verified_content(publication)
    if content is not None and (coverage := manifest_problem(content, graph=upstream_graph)) is not None:
        problems = (*problems, coverage)
    if card_id in {str(card["id"]) for card in source_cards(upstream_graph)}:
        required = _version_required(workflow, publication)
        if required and content is not None:
            basis = (await saved_basis(session, workflow_id=workflow_id, graph=upstream_graph, content=content,
                                       card_ids=[card_id]))[0]
            problems = (*problems, *basis_problems([basis]))
    else:
        # 被引用的那张卡自己也是转抄卡：继续往下走一层——这一层的工件/编译/覆盖问题照旧带着往上报（上层正常也不能替
        # 下层顶：M 的清单删了、S 这边再齐也不作数），要证明与否由链子整体定（chained_problems 那一层判）。引用指不到
        # 图上的节点（nested_node 为空）时同样保留这一层的问题，只是往下没有可判的了
        nested_node = node_map(upstream_graph).get(card_id)
        nested = (await _pinned_card(session, node=nested_node, purpose=purpose, seen=seen)
                  if isinstance(nested_node, Mapping) else Chained())
        required = _version_required(workflow, publication) or nested.required
        problems = (*problems, *nested.problems)
    return Chained(required=required, problems=tuple(_traced(problems, node_id=node_id, title=title,
                                                             upstream_workflow=workflow, upstream_version=version,
                                                             upstream_card=card_id)))


def _traced(problems: Sequence[Mapping[str, Any]], *, node_id: str, title: str, upstream_workflow: Any,
            upstream_version: int, upstream_card: str) -> list[dict[str, Any]]:
    """把上游的问题翻成下游这一环看得见的形状：机器码照旧，点名下游的节点（可追踪），附上上游引用与整条引用链。

    `trace` 从下游这一环往下排（这一环的引用在最前、出问题的那一层在最后）：链子上每层的问题都各自保留，去重按
    「下游的哪张卡 + 机器码 + 整条引用链」来，不同层不会被合成一条。
    """
    hop = {"node_id": node_id, "workflow_id": str(getattr(upstream_workflow, "id", "")),
           "version": int(upstream_version), "card_id": upstream_card}
    name = str(getattr(upstream_workflow, "name", "") or getattr(upstream_workflow, "id", ""))
    out: list[dict[str, Any]] = []
    for problem in problems:
        said = str(problem.get("message") or problem.get("reason") or CODE_TEXT.get(str(problem.get("code")), ""))
        inner = [dict(item) for item in (problem.get("trace") or []) if isinstance(item, Mapping)]
        out.append({**dict(problem), "card_id": node_id,
                    "message": f"「{title}」转抄的「{name}」v{upstream_version} 的口径卡「{upstream_card}」过不了发布依据："
                               f"{said}",
                    "upstream": dict(hop), "trace": [dict(hop), *inner]})
    return out


def problem_key(problem: Mapping[str, Any]) -> tuple[str, str, tuple[tuple[str, int, str], ...]] | None:
    """一条问题的去重键：下游的哪张卡 + 机器码 + 整条引用链（到出问题的那一层）。

    版本级那些（工件、编译器、错图，`card_id` 为空）没有键——逐条留着（编译器的问题是逐项给的）。没有键或者键没见过
    就都算一条，同一件事说两遍才合成一条（覆盖那条点名到某张卡、逐卡判出来同一条）。"""
    card_id = str(problem.get("card_id") or "")
    if not card_id:
        return None
    trace = tuple((str(hop.get("workflow_id") or ""), int(hop.get("version") or 0), str(hop.get("card_id") or ""))
                  for hop in problem.get("trace") or [] if isinstance(hop, Mapping))
    return (card_id, str(problem.get("code") or ""), trace)


# --------------------------------------------------------------------------
# 内容与工件
# --------------------------------------------------------------------------


def build_content(*, workflow_id: str, version: int, level: str, graph: Any, cards: Sequence[CardBasis]) -> dict[str, Any]:
    """这一版的清单内容：**只由原始输入决定**（这一版不可变的图 + 每张卡引用的那两行记录的值）。"""
    from app.core.artifact_store import execution_hash, graph_hash
    from app.ledger.ontology import ontology_manifest_section

    sources, tables = fetch_dependencies(graph)
    out: dict[str, Any] = {
        "format": FORMAT,
        "workflow_id": str(workflow_id),
        "version": int(version),
        "level": str(level or ""),
        "graph_hash": graph_hash(graph or {}),
        "execution_hash": execution_hash(graph or {}),
        "compiler": compiler_versions(),
        "cards": [card.as_dict() for card in cards],
        "parameters": parameters_of(graph),
        "dependencies": {"sources": sources, "tables": tables, "upstream": upstream_pins(graph)},
    }
    ontology = ontology_manifest_section(workflow_id=str(workflow_id), version=int(version), graph=graph or {})
    if ontology is not None:
        out["ontology"] = ontology
    return out


def content_hash(content: Mapping[str, Any]) -> str:
    """清单内容的内容哈希（= 工件 id，也是事件行上的 manifest）。"""
    from app.core.artifact_store import canonical_json, content_hash as hash_of

    return hash_of(canonical_json(content))


def cards_summary(cards: Sequence[CardBasis]) -> list[dict[str, Any]]:
    return [card.as_dict() for card in cards]


def basis_problems(cards: Sequence[CardBasis]) -> list[dict[str, Any]]:
    """依据缺哪一块：[{card_id, code, message, reason}]，齐了就是空。"""
    return [{"card_id": card.card_id, "code": card.code, "message": card.reason or CODE_TEXT.get(str(card.code), ""),
             "reason": card.reason} for card in cards if not card.ok]


def basis_rows(stored: Any) -> list[dict[str, Any]]:
    """事件行上记下的那几段依据摘要 → 「缺哪一块」：[{card_id, code, message}]，齐了就是空。

    读的是事件行（`cards` 列，和清单内容里的那一份逐字相同），不读工件：版本详情、列表这类接口要的是「齐不齐」，
    不是整份清单。
    """
    out: list[dict[str, Any]] = []
    for card in stored or []:
        if not isinstance(card, Mapping):
            continue
        proof = card.get("proof") if isinstance(card.get("proof"), Mapping) else {}
        if proof.get("ok"):
            continue
        code = str(proof.get("code") or RECORD_MISSING)
        out.append({"card_id": str(card.get("card_id") or ""), "code": code,
                    "message": str(proof.get("reason") or proof.get("message") or CODE_TEXT.get(code, ""))})
    return out


async def observe(session: Any, *, workflow: Any, snapshot: Any, catalog: Any = None, schema: Any = None,
                  capabilities: Any = None) -> dict[str, Any]:
    """发布这一刻**观察到**的环境：目录/结构/能力版本、绑定的工具现在在不在。

    这些不进清单内容的哈希（它们随环境变，清单要能被重建），单独记在事件行上，读的人知道当时是什么样子。
    目录/结构/能力版本由调用方（commit_publish 已经算好的那几份）传进来：这里不再查一遍，也不碰版本行上还没提交的写。
    """
    from app.db.base import utcnow

    missing: list[dict[str, str]] = []
    try:
        from app.engine.launch import missing_tools
        from app.engine.schema import GraphSpec

        spec = GraphSpec.model_validate(snapshot.graph or {})
        missing = [{"who": who, "tool": tool, "where": where} for who, tool, where in
                   await missing_tools(session, spec)]
    except Exception:  # noqa: BLE001 - 图不成形/工具表读不出来：观察这一项留空，发布本身另有校验
        missing = []
    return {"at": utcnow().isoformat(),
            "catalog_versions": dict(catalog if catalog is not None else snapshot.catalog_versions or {}),
            "schema_versions": dict(schema if schema is not None else snapshot.schema_versions or {}),
            "capability_versions": dict(capabilities if capabilities is not None
                                        else snapshot.capability_versions or {}),
            "missing_tools": missing}


async def publish(session: Any, *, workflow: Any, snapshot: Any, level: str, actor: str | None,
                  observed: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """发布这一刻的依据：算出内容、存成不可变清单工件、追加一行发布事件。**不提交**（和发布那几列同一个事务）。

    调用方（api/workflows.commit_publish）先做完读（能力版本、目录版本），再调这里：工件是另一个会话按内容寻址写的，
    这边手里还没有未提交的写，不会两个会话抢同一把写锁。
    """
    from app.core.artifact_store import put_json
    from app.db.models import WorkflowPublication

    graph = snapshot.graph or {}
    cards = await basis_for(session, workflow_id=workflow.id, graph=graph)
    content = build_content(workflow_id=workflow.id, version=int(snapshot.version), level=level, graph=graph,
                            cards=cards)
    manifest = await put_json(content, kind=KIND, run_id=None, meta={
        "workflow_id": workflow.id, "version": int(snapshot.version), "level": level,
        "cards": [card.card_id for card in cards], "manifest_version": MANIFEST_VERSION})
    required = requires_proof_for(workflow, cards)
    row = WorkflowPublication(
        workflow_id=workflow.id, version=int(snapshot.version), level=level, graph_hash=content["graph_hash"],
        manifest=manifest, format=FORMAT, cards=cards_summary(cards),
        proven=bool(cards) and not basis_problems(cards), required=required,
        observed=dict(observed) if observed is not None else await observe(session, workflow=workflow, snapshot=snapshot),
        actor=actor)
    session.add(row)
    await session.flush()
    return {"id": row.id, "workflow_id": workflow.id, "version": int(snapshot.version), "level": level,
            "manifest": manifest, "format": FORMAT, "cards": cards_summary(cards),
            "proven": bool(row.proven), "required": required, "problems": basis_problems(cards),
            "observed": dict(row.observed or {})}


# --------------------------------------------------------------------------
# 读：清单、事件、重建
# --------------------------------------------------------------------------


class PublicationUnreadable(ValueError):
    """这份发布依据读不出来 / 重建不上：code 是机器码，message 给人看。"""

    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.message = message
        self.code = code


async def latest(session: Any, workflow_id: str, version: int) -> Any | None:
    """某一版最近一次发布留下的那行事件（重发旧版就是最新那一行）；没有就是 None（老资产或没发布过）。"""
    from app.db.models import WorkflowPublication

    return (await session.execute(
        select(WorkflowPublication).where(WorkflowPublication.workflow_id == str(workflow_id),
                                          WorkflowPublication.version == int(version))
        .order_by(WorkflowPublication.created_at.desc(), WorkflowPublication.id.desc()).limit(1))).scalar_one_or_none()


async def events(session: Any, workflow_id: str, version: int | None = None) -> list[Any]:
    """这个工作流（这一版）全部发布事件，新的在前。旧事件和旧清单不改写，这里能看到重发了几次。"""
    from app.db.models import WorkflowPublication

    stmt = select(WorkflowPublication).where(WorkflowPublication.workflow_id == str(workflow_id))
    if version is not None:
        stmt = stmt.where(WorkflowPublication.version == int(version))
    rows = await session.execute(stmt.order_by(WorkflowPublication.created_at.desc(), WorkflowPublication.id.desc()))
    return list(rows.scalars())


def read(publication: Any) -> dict[str, Any]:
    """读一份发布清单（按内容寻址的工件，读的时候复验哈希）。读不出来、被改过、不是这一份都抛 PublicationUnreadable。"""
    from app.core.artifact_store import load

    if publication is None:
        raise PublicationUnreadable("这一版没有发布清单（发布事件之前发布的老资产）", BASIS_MISSING)
    artifact = str(getattr(publication, "manifest", "") or "")
    try:
        content = load(artifact)
    except ValueError as e:  # 内容与哈希不符：工件被改过
        raise PublicationUnreadable(f"发布清单与它的哈希不符，疑似被修改：{e}", MANIFEST_TAMPERED) from e
    if content is None:
        raise PublicationUnreadable(f"发布清单工件不在了（{artifact[:12]}…）：文件缺失或被回收", MANIFEST_MISSING)
    if not isinstance(content, dict) or content.get("format") != FORMAT \
            or str(content.get("workflow_id")) != str(getattr(publication, "workflow_id", "")) \
            or int(content.get("version") or 0) != int(getattr(publication, "version", 0) or 0):
        raise PublicationUnreadable("这份工件不是这一版这一份发布清单", MANIFEST_FOREIGN)
    if content_hash(content) != artifact:
        raise PublicationUnreadable("发布清单的内容哈希与工件 id 不符", MANIFEST_TAMPERED)
    return content


def verified_content(publication: Any) -> tuple[dict[str, Any] | None, tuple[dict[str, Any], ...]]:
    """读一份发布清单并核**工件本身**：内容哈希复验、就是这一版这一份、编译器约定现在这一套认不认。

    和 K09 的固定执行（ledger/querycore 的 `_verified_manifest`）是同一份核对：正式用途每次执行都要复验，工件被删、
    被改过、不是这一版这一份、按别的约定物化的，一律不能继续正式执行。交回 (内容, 问题)：读不出来时内容是 None，
    问题里带 read() 那条机器码；编译器约定不认时内容照旧交回（读的人还能看它是按哪一套物化的），问题逐项给
    COMPILER_MISMATCH / COMPILER_MISSING。老资产（这一版没有发布事件）不走这里——调用方先判 publication is None。
    """
    try:
        content = read(publication)
    except PublicationUnreadable as e:
        return None, ({"card_id": "", "code": str(e.code), "message": e.message, "reason": e.message},)
    problems = tuple({"card_id": "", "code": str(problem.get("code") or COMPILER_MISMATCH),
                      "key": str(problem.get("key") or ""),
                      "message": str(problem.get("message") or ""),
                      "reason": str(problem.get("message") or "")}
                     for problem in compiler_problems(content))
    return content, problems


async def rebuild(session: Any, publication: Any) -> dict[str, Any]:
    """按原始输入把这份清单重建一遍：**内容一字不差才算「可重建」**。

    原始输入是这一版不可变的图和清单里引用的那两行记录。缺任何一样（版本行被删、引用的记录/审阅不在、工件不在）都抛
    PublicationUnreadable，明确失败，不猜、不拿别的版本顶。回来的是 {content, manifest, matches, problems}。
    """
    from app.db.models import CaliberReviewDecision, CaliberValidationRecord

    content = read(publication)
    graph = await _graph_of(session, publication, expect=content)
    problems: list[dict[str, Any]] = []
    cards: list[CardBasis] = []
    stored = {str(card.get("card_id")): card for card in (getattr(publication, "cards", None) or [])
              if isinstance(card, Mapping)}
    for card in content.get("cards") or []:
        if not isinstance(card, Mapping):
            continue
        card_id = str(card.get("card_id") or "")
        saved = stored.get(card_id) or card
        ref = card.get("validation") if isinstance(card.get("validation"), Mapping) else None
        decision_ref = card.get("review") if isinstance(card.get("review"), Mapping) else None
        if ref is not None:
            record = await session.get(CaliberValidationRecord, str(ref.get("record_id") or ""))
            if record is None:
                raise PublicationUnreadable(
                    f"清单引用的验证记录不在了（口径卡 {card_id}）：原始输入缺失，重建不上", INPUTS_MISSING)
            if record_reference(record) != dict(ref):
                problems.append({"card_id": card_id, "code": BASIS_MISMATCH, "message": "验证记录变了"})
        if decision_ref is not None:
            decision = await session.get(CaliberReviewDecision, str(decision_ref.get("decision_id") or ""))
            if decision is None:
                raise PublicationUnreadable(
                    f"清单引用的审阅决定不在了（口径卡 {card_id}）：原始输入缺失，重建不上", INPUTS_MISSING)
            if review_reference(decision) != dict(decision_ref):
                problems.append({"card_id": card_id, "code": BASIS_MISMATCH, "message": "审阅决定变了"})
        made = await card_basis(session, workflow_id=str(publication.workflow_id), graph=graph, card_id=card_id,
                                saved=saved)
        if (made.definition_hash != str(card.get("definition_hash") or "")
                or made.semantic_hash != str(card.get("semantic_hash") or "")
                or dict(made.binding_fingerprints) != dict(card.get("binding_fingerprints") or {})
                or list(made.metric_ids) != [str(m) for m in card.get("metric_ids") or []]):
            problems.append({"card_id": card_id, "code": BASIS_MISMATCH, "message": "这张卡的定义/绑定与清单不符"})
        cards.append(made)
    made_content = build_content(workflow_id=str(publication.workflow_id), version=int(publication.version),
                                 level=str(publication.level or ""), graph=graph, cards=cards)
    digest = content_hash(made_content)
    manifest = str(getattr(publication, "manifest", "") or "")
    return {"content": made_content, "original": content, "manifest": manifest, "computed": digest,
            "matches": digest == manifest and not problems, "problems": problems}


async def _graph_of(session: Any, publication: Any, *, expect: Mapping[str, Any]) -> dict[str, Any]:
    """这份清单对着的那一版图（原始输入之一）。不在时明确失败。"""
    from app.apps.versions import version_row

    row = await version_row(session, str(publication.workflow_id), int(publication.version))
    if row is None or not isinstance(row.graph, dict) or not row.graph:
        raise PublicationUnreadable(
            f"这一版（{publication.workflow_id} v{publication.version}）的图不在了：原始输入缺失，重建不上", INPUTS_MISSING)
    from app.core.artifact_store import graph_hash

    if str(expect.get("graph_hash") or "") and graph_hash(row.graph) != str(expect["graph_hash"]):
        raise PublicationUnreadable("这一版现在的图和清单里固定的图指纹对不上", BASIS_MISMATCH)
    return row.graph


# --------------------------------------------------------------------------
# 版本解析：推荐版 / 最高版 / 显式版
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Resolved:
    """一次默认调用解析到的版本。explicit=True 表示调用方点名了版本（始终固定到它）。"""

    workflow_id: str
    version: int
    row: Any
    explicit: bool
    recommended: int | None = None
    latest: int | None = None
    publication: Any | None = None
    #: 清单工件读出来的内容（read() 复验过的那一份）：正式用途的逐卡核对也按它，不按可变的事件行列
    publication_content: dict[str, Any] | None = None
    #: 清单工件本身的问题（不在/被改过/不是这一版这一份、编译器约定不认）：和哪张图无关，空就是没问题
    publication_problems: tuple[dict[str, Any], ...] = ()
    #: 清单和这一版这一张图对不上的那一条（错图/缺卡）：按版本行的图核的，正式用途按自己手里那张图再核一遍
    publication_coverage: dict[str, Any] | None = None
    upgrade: dict[str, Any] | None = None

    @property
    def publication_problem(self) -> dict[str, Any] | None:
        """第一条问题：工件/编译器在前、覆盖在后——和正式用途拒绝时先说的那一条同一个顺序。没有问题是 None。"""
        return self.publication_problems[0] if self.publication_problems else self.publication_coverage


async def recommended(session: Any, workflow: Any) -> Any | None:
    """这份资产当前推荐的发布版（Workflow.published_version 指的那一版）：**默认调用读它**。

    目录、look、账本挑候选都按这里解析。「最新的那一版」是另一回事（apps/versions.latest_published，只用于升级发现）。
    推荐指针为空、或者指的那一版查不出来时返回 None；指针指着一条已经查不到的版本行时退回最高已发布版，
    不猜别的版本。
    """
    if workflow is None or not getattr(workflow, "published_version", None):
        return None
    row = await app_versions.version_row(session, workflow.id, int(workflow.published_version))
    if row is not None and app_versions.is_published(row, current=workflow.published_version):
        return row
    return await app_versions.latest_published(session, workflow.id)


async def highest(session: Any, workflow_id: str) -> Any | None:
    """最高已发布版（升级发现用）：apps/versions.latest_published。"""
    return await app_versions.latest_published(session, workflow_id)


async def resolve(session: Any, workflow: Any, *, version: int | None = None, purpose: str,
                  explicit: bool | None = None) -> Resolved | None:
    """按用途解析要用的版本：给了 version 就固定到它（显式请求始终固定）；没给就读推荐版。

    `explicit` 是**调用方有没有点名版本**：不给就按「给了 version 就是点名」推。调用方自己先解析一次、再把那一版交给
    这里时时（engine/launch.launch_run 就是这么做的：默认发起只读一次推荐指针、之后一律按那一版走，并发下指针漂移也
    只影响下一次发起）要显式传 explicit=False，运行来源里才不会把它记成「调用方点名的版本」。

    purpose 由服务端入口常量决定（PURPOSES），这里只认它、不认客户端传的用途。读不出来（工作流没有推荐版、
    指名的版本不在）返回 None，怎么拒绝由各入口按自己原来的说法说。

    这一版有发布事件时顺手核一遍清单（`verified_content`：工件复验 + 编译器约定；`manifest_problem`：是不是这一版
    这一张图、缺不缺卡），结论放在 `publication_content` / `publication_problems` / `publication_coverage` 上——
    读的人也看得见，正式用途按它拒。老资产（没有事件）都是空，保持原行为。
    """
    if purpose not in PURPOSES:
        raise ValueError(f"不认识的用途：{purpose}")
    if workflow is None:
        return None
    newest = await highest(session, workflow.id)
    if version is None:
        row = await recommended(session, workflow)
        if row is None:
            return None
    else:
        row = await app_versions.version_row(session, workflow.id, int(version))
        if row is None:
            return None
    publication = await latest(session, workflow.id, int(row.version))
    content: dict[str, Any] | None = None
    problems: tuple[dict[str, Any], ...] = ()
    coverage: dict[str, Any] | None = None
    if publication is not None:
        content, problems = verified_content(publication)
        if content is not None:
            coverage = manifest_problem(content, graph=row.graph)
    return Resolved(workflow_id=workflow.id, version=int(row.version), row=row,
                    explicit=(version is not None) if explicit is None else bool(explicit),
                    recommended=workflow.published_version, latest=newest.version if newest is not None else None,
                    publication=publication, publication_content=content, publication_problems=problems,
                    publication_coverage=coverage, upgrade=None)


async def upgrade_of(session: Any, workflow: Any) -> dict[str, Any] | None:
    """有没有比推荐版更高的已发布版（升级发现：告诉人「有一版更新的」）。

    这里只**提示**，不替调用方换版本：默认调用读推荐版（可能是一个被重新推上去的旧版），更高的一版要不要升由
    人决定。返回 {version, recommended, latest, reason}，没有更高的就是 None。
    """
    if workflow is None or not getattr(workflow, "published_version", None):
        return None
    latest = await highest(session, workflow.id)
    if latest is None or int(latest.version) <= int(workflow.published_version):
        return None
    return {"version": int(latest.version), "recommended": int(workflow.published_version),
            "latest": int(latest.version),
            "reason": f"「{getattr(workflow, 'name', '') or workflow.id}」还有一版更新的发布版 v{latest.version}"
                      f"（现在推荐的是 v{workflow.published_version}）"}


# --------------------------------------------------------------------------
# 用途判定：需不需要证明、证明齐不齐、这一版是什么身份
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Judged:
    """一次正式用途判定的结果：解析到的版本、发布依据、需不需要证明、身份、以及不够时的机器码。"""

    purpose: str
    workflow_id: str
    version: int
    explicit: bool
    published: bool
    publication: Any | None = None
    requires_proof: bool = False
    identity: str = LEGACY
    cards: tuple[CardBasis, ...] = ()
    problems: tuple[dict[str, Any], ...] = ()
    upgrade: dict[str, Any] | None = None
    recommended: int | None = None
    latest: int | None = None

    @property
    def proven(self) -> bool:
        return self.requires_proof and not self.problems

    @property
    def code(self) -> str | None:
        """不够时的机器码（够了为空）。"""
        return str(self.problems[0]["code"]) if self.problems else None

    @property
    def refusal(self) -> tuple[str, str] | None:
        """(机器码, 白话)：正式用途拦下来时给入口用它原来的说法抛出去。"""
        if not self.problems:
            return None
        first = self.problems[0]
        return str(first["code"]), str(first.get("message") or CODE_TEXT.get(str(first["code"]), ""))

    def as_dict(self) -> dict[str, Any]:
        """运行记录/接口里那一小段：实际用哪一版、哪份清单、什么身份。"""
        return {"purpose": self.purpose, "version": self.version, "explicit": self.explicit,
                "manifest": str(getattr(self.publication, "manifest", "") or "") or None,
                "publication": str(getattr(self.publication, "id", "") or "") or None,
                "identity": self.identity, "requires_proof": self.requires_proof,
                "problems": [dict(p) for p in self.problems],
                "upgrade": dict(self.upgrade) if self.upgrade else None}


def requires_proof_for(workflow: Any, cards: Sequence[CardBasis], publication: Any = None) -> bool:
    """这份资产在这一版上要不要证明：图里有源头口径卡，而且**发布时要求过证明**（事件行上的 required）**或者现在还
    接管着**（apps/versions.taken_over）——两者取或。

    发布那一刻的严格性记在事件行上（publish 写），之后把接管标记去掉（清 tags 之类）不会让已经按严格发的那些版本降级；
    反过来，发布之后才接管上的资产照旧要证明。老资产（这一版没有发布事件）不启用——保持原行为，只是记身份
    formal_legacy；不需要证明的记 formal_unproven。
    """
    return bool(cards) and (bool(getattr(publication, "required", False)) or bool(app_versions.taken_over(workflow)))


async def judge(session: Any, workflow: Any, *, version: int | None = None, purpose: str,
                snapshot: Any = None, explicit: bool | None = None) -> Judged | None:
    """正式用途的共用判定：解析版本、取发布依据、按用途说这一版够不够、身份是什么。

    只有 PURPOSES 里的用途能进来（purpose 由服务端入口常量决定）。formal / issue 之外的用途一样能读，但**不会因为
    缺验证审阅被拦**——试算、探索、组合本来就不该拿正式口径的门禁去卡（A03）。

    这一版**有发布事件**时（老资产没有事件，保持原行为、身份 formal_legacy）：
    - 先核工件本身：`read()` 复验（工件不在、被改过、不是这一版这一份）与 `compiler_problems()`（按别的约定物化的
      不按新规则重解释）——和 K09 的固定执行同一份核对，正式用途每次执行都核，过关才继续；
    - 再核清单覆盖：图指纹对得上、图上每张源头口径卡都在清单里（错图/缺卡当场 BASIS_MISMATCH）；
    - 再逐张卡认清单里引用的那一对记录/审阅（不在核对的时候另挑更新的记录补，清单里没有的卡也不现场挑一条顶上）；
    - 再按同一用途递归判转抄卡（caliber_from）固定引用的上游那一张卡：上游要证明就得同样满足，失败按上游那条机器码
      + 上游引用拒（`chained_problems`）。
    requires_proof = 这一版自己要证明（事件行 required 或现在还接管着）或引用的上游要证明；这些都齐了才叫
    formal_proven，否则 formal_unproven。`explicit` 是调用方有没有点名版本（见 resolve），运行来源里照实记。

    **权限和治理不在这里重判**：发起、出具各自的入口照旧按现有那一套判（角色、应用授权、治理 lint、每日上限），
    这里只判发布依据；用到的说法也都是现成的——is_published / retired、taken_over（接管）、K05 的覆盖判断。
    版本行读不出来、没发布过、已退役这些**原来的拒绝**也不在这里说：各入口照旧按自己的说法拒（HTTP 语义不变），
    这里只在「发布依据不够/读不出来」时说。
    """
    resolved = await resolve(session, workflow, version=version, purpose=purpose, explicit=explicit)
    if resolved is None:
        return None
    row = resolved.row
    published = app_versions.is_published(row, current=getattr(workflow, "published_version", None)) \
        and getattr(row, "retired_at", None) is None
    graph = (snapshot.graph if snapshot is not None else None) or getattr(row, "graph", None) or {}
    cards: tuple[CardBasis, ...] = ()
    problems: list[dict[str, Any]] = []
    requires = False
    identity = LEGACY
    if published:
        if resolved.publication is None:
            identity = LEGACY
        else:
            # 工件/编译器（与哪张图无关）+ 覆盖（按这里要判的那张图再核一遍；resolve 上那份是给读的人看的）
            content = resolved.publication_content
            coverage = manifest_problem(content, graph=graph) if content is not None else None
            version_problems = [*resolved.publication_problems, *([coverage] if coverage else [])]
            own_required = False
            if content is not None:
                cards = tuple(await saved_basis(session, workflow_id=resolved.workflow_id, graph=graph,
                                                content=content))
                own_required = requires_proof_for(workflow, cards, publication=resolved.publication)
            chained = await chained_problems(session, graph=graph, purpose=purpose)
            if purpose in PROOF_PURPOSES:
                requires = own_required or chained.required
                # 同一件事只说一条：覆盖那条点名到某张卡时，逐卡判出来的同一条不再重复；链子上不同层的问题各有各的
                # 引用链（problem_key），不会被合成一条
                problems = []
                spoken: set[tuple[str, str, tuple[tuple[str, int, str], ...]]] = set()
                for found in [*version_problems, *(basis_problems(cards) if own_required else []), *chained.problems]:
                    key = problem_key(found)
                    if key is not None:
                        if key in spoken:
                            continue
                        spoken.add(key)
                    problems.append(found)
                identity = PROVEN if (requires and not problems) else UNPROVEN
            else:
                requires = False
                identity = UNPROVEN
    return Judged(purpose=purpose, workflow_id=resolved.workflow_id, version=resolved.version,
                  explicit=resolved.explicit, published=published,
                  publication=resolved.publication if published else None, requires_proof=requires,
                  identity=identity, cards=cards, problems=tuple(problems),
                  upgrade=await upgrade_of(session, workflow), recommended=resolved.recommended,
                  latest=resolved.latest)


__all__ = ["BASIS_MISMATCH", "BASIS_MISSING", "CODE_TEXT", "COMPILER_KEYS", "COMPILER_MISMATCH", "COMPILER_MISSING",
           "FORMAT", "IDENTITIES", "INPUTS_MISSING", "KIND",
           "LEGACY", "MANIFEST_FOREIGN", "MANIFEST_MISSING", "MANIFEST_TAMPERED", "PROOF_PURPOSES", "PROVEN",
           "PURPOSES", "PURPOSE_COMPOSE", "PURPOSE_EXPLORE", "PURPOSE_FORMAL", "PURPOSE_ISSUE", "PURPOSE_TRIAL",
           "RECORD_MISSING", "RECORD_NOT_MATCHED", "RECORD_STALE", "REVIEW_MISSING", "REVIEW_PARTIAL",
           "REVIEW_REJECTED", "REVIEW_STALE", "REVIEW_UNVERIFIED", "UNPROVEN", "CardBasis", "Chained", "Judged",
           "PublicationUnreadable", "Resolved", "authenticated", "basis_for", "basis_problems", "basis_rows",
           "build_content",
           "card_basis", "cards_summary", "chained_nodes", "chained_problems", "compiler_problems",
           "compiler_versions", "content_hash", "events", "fetch_dependencies",
           "highest", "judge", "latest", "manifest_problem", "observe", "parameters_of", "problem_key",
           "publish", "read",
           "rebuild", "recommended", "requires_proof_for", "resolve", "saved_basis", "source_cards", "upgrade_of",
           "upstream_pins", "verified_content"]
