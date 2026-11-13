"""组合式问数：口径做骨架，Agent 填缺口（docs/design/next-round/02-口径账本.md 做法 4，拍板 L3）。

问数据的助手看到已发布口径（look 的 caliber，问数据里另给入口字段和「能不能组合」，不给 SQL）之后，可以输出一行

  {"op":"use_caliber","id":"c1","workflow_id":"…","version":3,"card":"card","metrics":["gmv"],"inputs":{"start":"…"}}

选用一份口径。**取数节点不让模型写**：由服务端按那一版确定性地内联实例化（ledger/derive.caliber_block，全站唯一的
派生图生成器）——口径卡读的取数节点照抄，节点 id 加前缀「c1_」，模板里的 {{ input.字段 }} 按 inputs 渲染成字面 SQL
（和运行时同一个渲染器）；口径卡 id 就是 c1，caliber_from 固定到那一版的那张卡、带改名映射 {原 id: c1_原 id}
（metrics 运行时按它改写式子）。取数节点首尾相接、最后一个接口径卡；入口连到第一个取数节点（GraphApplier 接上）。
模型只管把口径卡 c1 接到报告、报告里引用 [[m:指标 id]]。

**为什么不用子图节点。** 子图的证据不交回父图（报告引用不到子图里的数），使用者也用不了子工作流（auth/chat_policy）。
内联实例化只用到 tool 和 metrics 两种已放行的节点，证据照常封存，比对也照常复证（ledger/match 认出这些查询是助手
选用的，标 composed「助手选用」，不标「等价已证明」：选哪份口径是助手凭问法判断的）。

**只算选中的指标（K08 S2/S4）。** 模型写的 metrics 不只是「报告里引用哪几个数」：它决定这次真正要执行哪些取数——服务端按它
算一份依赖闭包（ledger/closure），只拷选中的指标要用的取数节点、入口字段收窄到这次会用到的，并把选中的指标记在口径卡的
caliber_from.metrics 上（运行时按 closure.effective_metrics 只算这几个）。一次选用里的指标一律按**源卡顺序**归一：
Composed.metrics、口径卡上记的 metrics、selection_hash 与执行数组都按这一份，调用方传的顺序与重复项都不影响结果。只选 A 时
不再要求 B 的参数；A 和 B 共用一条 SQL 时那条 SQL 保留、并在结果里声明它还在算未选中的 B（不做投影）。点名的指标解析不出
依赖（不在这张卡上、式子读不出来、绑定对不上）一律拒收，不静默退回整卡。

**选项是预筛，不是放行**（K08 S4）。look 读好的每一张卡带 ``Option``：``selectable_metrics``（能一个一个点名的指标，各自
要哪些入口字段）与 ``unavailable_metrics``（用不了的与原因）按每个指标判（B 独有那条查询坏了、绑定对不上，只让 B 不可选）；
至少有一个可选才给组合写法，一个也没有才标不能组合。``default_closure`` 是不点名（整卡）那一份闭包的摘要。这些摘要只含
query_id、字段、工具、指纹与共享声明，**不含 SQL 正文、不含参数值**。真正选用（``instantiate``）时仍按这次要算的指标逐项
硬校验，不拿预筛的结论放宽。

**参数校验**（不合规拒收，原因交回模型改，GraphApplier.refusals；判断都在 ledger/params，一处规则各入口同一套）：
- 只能选这一轮 look 能看到的那一版（每个工作流的**推荐发布版本**，K07：Workflow.published_version，默认调用读它），而且
  那张卡能组合（derive.load_pinned：卡是
  源头、每个输入都是取数节点第一行的格、取数模板只引用入口字段、模板里的参数都在入口字段里声明过）；
- inputs 只认口径入口声明的字段，没写的补缺省值，再过 apps/forms 同一套表单校验（类型、可选值、必填、格式）；
- 取数模板里的每个参数这次都要有值：没给、也没声明缺省值的（含声明成可选的）报 missing_parameter，不按空串发 SQL；
- 参数要渲染进 SQL：写在引号里的不能带引号、反斜杠、换行；裸写的只能是数；写在注释里的不能带换行和注释结束符——
  否则渲染出来的就不是那一版口径的 SQL 了（旧适配器承载不了的值明确拒绝，见 ledger/params）；
- metrics 只能是这张卡上的指标 id；id 不能和图里已有的节点撞。

**值只渲染一次。** 拷出来的取数节点里，`{{ input.字段 }}` 在这一步渲染成字面值（值只被模板替换这一次），同时把参数
契约写在节点的 `data.config["parameters"]` 里（ledger/params 的 marker）：args 里已经是字面语句，运行时的模板渲染一概
不碰它——不然值里带 `{{ vars.question }}` 这种文本会被运行时替换成那个变量的值，SQL 被数据改掉（K01 的二次渲染反例）。
契约按**这次渲染出来的 args** 的哈希与节点绑定：复制走、改过 SQL 之后契约作废，那个节点回退成普通模板（正常渲染、
正常判参数、画布照常扫变量）。契约里另记源模板哈希、规范化输入哈希、参数类型与写在哪儿、那一版取数节点的固定引用
（出处只是个地址：本切片没有实现按它自动还原模板并核对）。

实例化是确定的：同样的版本、同样的操作得到一模一样的节点（测试锁住）。这里只是纯计算：版本快照由 look/calibers 的
CaliberKind 在开流之前用会话读好（GraphApplier.apply 是同步的，流里不碰库）。
"""
from __future__ import annotations

import copy
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from app.ledger import params
from app.ledger.derive import Pinned, caliber_block, declared_entry_fields

#: 操作名
OP = "use_caliber"
#: 拒收时的问题编号（自查交回模型、过程里照实写一行）
REFUSED = "ledger.use_caliber_refused"
#: 操作 id：口径卡的节点 id，取数节点加它做前缀。简短英文小写下划线（同节点 id 的约定）
_OP_ID = re.compile(r"^[a-z][a-z0-9_]{0,23}$")

#: 写给模型的拒收说明（也上界面：过程里那一行）
NOT_LISTED = "只能选用这一轮查看到的已发布口径：「{workflow}」v{version} 不在里面（口径要先 look，版本以查看结果为准）"
WRONG_VERSION = "「{workflow}」这一轮能选用的是 v{latest}（这一轮的推荐发布版本），不是 v{version}"
#: card 写错时交回的原因要能一次改对：说清 card 填什么、可填哪几个（节点 id）；写成了自己起的 id 的另说一句
CARD_UNKNOWN = ("「{workflow}」v{version} 里没有口径卡「{card}」：card 要填那一版工作流图里口径卡的节点 id，可填的有 {cards}"
                "（照抄查看结果里 use_caliber 写法中的 card）")
CARD_IS_OP_ID = "；「{card}」是你给这次选用起的 id，不是口径卡的节点 id"
CARD_AMBIGUOUS = "「{workflow}」v{version} 有几张口径卡（节点 id：{cards}），请在 card 里写选用哪一张的节点 id"
NOT_COMPOSABLE = "「{workflow}」v{version} 的口径卡「{card}」不能组合：{reason}"
BAD_ID = "use_caliber 的 id 要写成简短的英文小写加下划线（例如 c1），它就是口径卡的节点 id，当前为「{id}」"
ID_TAKEN = "use_caliber 的 id「{id}」和图里已有的节点「{node}」撞了，请换一个"
NO_METRICS = "use_caliber 的 metrics 要写这张口径卡上要用的指标 id（有：{metrics}）"
METRIC_UNKNOWN = "口径卡「{card}」上没有指标「{metric}」（有：{metrics}）"
FIELD_UNKNOWN = "口径「{workflow}」的入口没有字段「{name}」（有：{fields}）"
#: 这个字段这一版入口声明过，只是不在这次选中的指标的范围里（K08 复核）：说清是范围问题，不是口径没这个字段
FIELD_OUT_OF_SELECTION = ("「{name}」这一版入口声明过，但不在这次选用的范围里：这次算 {metrics}，只要 {fields}；"
                          "口径「{workflow}」的入口还有 {others}，那是别的指标要的——要用它就把那些指标一起选上")
INPUTS_NOT_OBJECT = "use_caliber 的 inputs 要写成字段和值的对照（JSON 对象）"
TEMPLATE_UNREADABLE = params.UNREADABLE_TEXT


class ComposeRefused(Exception):
    """不实例化这次选用：message 给模型看、也写进过程里那一行。"""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


@dataclass(frozen=True)
class Option:
    """这一轮能选用的一张口径卡：读好的那一版（pinned），以及**按指标**的可组合性（K08 S4）。

    - ``selectable_metrics``：能一个一个点名的指标（按源卡顺序）。每项是这一份依赖的摘要（``closure.summary()`` 加
      ``metric_id`` / ``name``）：这次要用的入口字段、要执行的查询（query_id、节点、工具、各自算谁、共享声明）、绑定指纹
      与两份身份哈希——**没有 SQL 正文、没有参数值**。
    - ``unavailable_metrics``：用不了的指标与原因（按源卡顺序）。
    - ``default_closure``：不点名（整卡）那一份闭包的摘要；整卡算不出来时是 None，原因在 ``reason``。

    ``composable`` 只看「至少有一个指标可选」：B 独有那条查询坏了、绑定对不上，A 照旧能选。这一层只是预筛，不改任何
    判断——真正选用时 ledger/compose.instantiate 按这次要算的指标逐项硬校验（未知指标、参数、绑定），不靠预筛放宽。
    """

    workflow_id: str
    workflow_name: str
    version: int
    level: str
    node_id: str
    card: str
    pinned: Pinned | None = None
    reason: str | None = None
    selectable_metrics: tuple[dict[str, Any], ...] = ()
    unavailable_metrics: tuple[dict[str, Any], ...] = ()
    default_closure: dict[str, Any] | None = None

    @property
    def composable(self) -> bool:
        """这一轮能不能选用：至少有一个指标可选。没做过分指标分析的老调用方（直接给了 pinned）按能不能读出这一版算。"""
        if self.pinned is None:
            return False
        return bool(self.selectable_metrics) or self.reason is None


@dataclass
class Composed:
    """一次选用实例化出来的东西：要加的节点、连线（取数首尾相接、最后接口径卡），交给界面和模型的结果。"""

    op_id: str
    option: Option
    nodes: list[dict[str, Any]]
    edges: list[dict[str, Any]]
    metrics: list[dict[str, Any]]
    inputs: dict[str, Any] = field(default_factory=dict)
    #: 这次选择的依赖闭包（K08 S2）：身份、这次要执行的查询、共享查询还在算谁（结果里照实带出去）
    closure: Any = None

    @property
    def fetch_ids(self) -> list[str]:
        return [str(n["id"]) for n in self.nodes if n.get("type") != "metrics"]

    def result(self) -> dict[str, Any]:
        """交给界面（过程里那一行）、记进这一轮的操作流的结果。不放 SQL、不放参数值（参数值助手自己写的，界面不必再给）。"""
        from app.ledger.labels import LEVEL_TEXT

        out = {"status": "ok", "workflow_id": self.option.workflow_id, "workflow_name": self.option.workflow_name,
               "version": self.option.version, "level": self.option.level,
               "level_text": LEVEL_TEXT.get(self.option.level, ""), "card": self.option.card, "card_id": self.op_id,
               "fetches": self.fetch_ids, "metrics": [{"id": m["id"], "name": m["name"]} for m in self.metrics],
               "nodes": len(self.nodes)}
        if self.closure is not None:
            # 这次选择的身份（selection_hash：同一批指标同一批查询）与整份闭包的身份（closure_hash：绑定变了它就变）
            out["selection"] = {"mode": self.closure.mode, "metric_ids": list(self.closure.metric_ids),
                               "query_ids": list(self.closure.query_ids),
                               "excluded_query_ids": list(self.closure.excluded_query_ids),
                               "selection_hash": self.closure.selection_hash,
                               "closure_hash": self.closure.closure_hash,
                               "resolver_version": self.closure.resolver_version,
                               "source_card_hash": self.closure.source_card_hash}
            # 共享查询照实声明：这条 SQL 这次也还在算这些没选中的指标（不做投影）
            shared = [{"query_id": q.query_id, "node_id": q.node_id, "computes_unselected": list(q.computes_unselected)}
                      for q in self.closure.queries if q.computes_unselected]
            if shared:
                out["shared_queries"] = shared
        return out


def signature(op: Mapping[str, Any]) -> str:
    """一次选用的身份（接着写时重发的同一个操作认得出来）：去掉服务端写上的结果。"""
    return json.dumps({k: v for k, v in op.items() if k != "result"}, ensure_ascii=False, sort_keys=True, default=str)


def _names(items: list[str]) -> str:
    return "、".join(items[:12]) or "无"


def _selected_ids(pinned: Pinned) -> list[str]:
    """这一份（可能裁过的）口径这次算哪些指标：裁过就是选择里的那几个（按源卡顺序），没裁就是卡上的全部。"""
    closure = pinned.closure
    if closure is not None and not closure.whole_card:
        return [str(m) for m in closure.metric_ids]
    return [str(m["id"]) for m in pinned.metrics]


def slot_contexts(nodes: Any, *, strict: bool = True) -> dict[str, set[str]]:
    """几个调用工具节点的 args.sql 里每个入口字段写在哪：{字段: {quoted / bare / comment}}。

    判断在 ledger/params（各入口同一套参数语义）：这里只把 ``params.slots_of`` 的结果收成 {字段: {写在哪儿}}，给要看
    承载方式的地方（``params.literal_problem`` 的那一套）读；按指标的可组合性（look/calibers.metric_options）走闭包，
    不从这里拿。strict=False 时认不出写法的那条跳过、不抛。"""
    out: dict[str, set[str]] = {}
    for node in nodes:
        if not isinstance(node, dict):
            continue
        args = ((node.get("data") or {}).get("config") or {}).get("args")
        sql = args.get("sql") if isinstance(args, dict) else None
        if not isinstance(sql, str):
            continue
        try:
            slots = params.slots_of(sql)
        except params.ParameterRefused as e:
            if not strict:
                continue
            raise ComposeRefused(TEMPLATE_UNREADABLE.format(reason=e.problems[0].message)) from None
        for slot in slots:
            out.setdefault(slot.field, set()).add(slot.context)
    return out


def _value_problem(name: str, value: Any, contexts: set[str]) -> str | None:
    """值写在 SQL 的哪个位置、旧适配器（内联文本替换）承载得了吗（ledger/params.literal_problem）。"""
    return params.literal_problem(name, value, contexts)


#: 点名要算的指标解析不出依赖（derive.select_pinned 的未决项）：照实交回，拒收这次选用
SELECTION_REFUSED = "口径卡「{card}」这次点名的指标算不出依赖：{reason}"


def _selected(pinned: Pinned, metric_ids: "Sequence[str] | None" = None, whole_card: bool = False) -> Pinned:
    """按这次要算的指标裁一份口径（derive.select_pinned：只留这次要执行的取数、入口字段收窄、带上选择的闭包）。

    算不出依赖（点名的指标不在这张卡上、式子读不出来、绑定对不上、点名的取数没有可读 SQL）抛 ComposeRefused 交回模型改
    ——**不静默退回整卡**。"""
    from app.ledger import derive

    try:
        return derive.select_pinned(pinned, metric_ids=metric_ids, whole_card=whole_card)
    except derive.NotDerivable as e:
        raise ComposeRefused(SELECTION_REFUSED.format(card=pinned.card_title, reason=e.message)) from None


def check_inputs(pinned: Pinned, inputs: Any, *, metric_ids: "Sequence[str] | None" = None,
                 whole_card: bool = False) -> dict[str, Any]:
    """按口径入口的字段校验助手给的参数，补缺省值，交回这次要渲染进取数节点的值（拷节点时用它渲染一次）。

    metric_ids / whole_card 给了就先按这次要算的指标裁一份（_selected）：只按**这次真正要执行的取数**判参数与入口字段——
    只选 A 时不再要求 B 那条 SQL 的参数，B 独有的入口字段也不在这次范围里（照实拒）。不给就是整张卡的入口（和以前一样）。

    字段这一栏分两种说法（K08 复核）：这一版入口**声明过**、只是不在这次选中的指标的范围里 → FIELD_OUT_OF_SELECTION
    （说清这次算哪些指标、只要哪些字段、剩下的字段是谁要的）；入口**根本没声明** → FIELD_UNKNOWN。两种都拒，不改行为。

    不合规抛 ComposeRefused（一句一处）：字段不认识、必填没给、类型/可选值不对、模板里的参数这次没有值（missing）、
    模板引用了没声明的字段（undeclared）、值承载不了（literal）。"""
    if metric_ids is not None or whole_card:
        pinned = _selected(pinned, metric_ids=metric_ids, whole_card=whole_card)
    from app.apps.forms import input_schema, validate_form

    if inputs is None:
        inputs = {}
    if not isinstance(inputs, dict):
        raise ComposeRefused(INPUTS_NOT_OBJECT)
    fields = {str(f.get("name")).strip(): f for f in pinned.entry_fields if isinstance(f.get("name"), str)}
    if unknown := [k for k in inputs if k not in fields]:
        # 字段「不在本次选择范围」与「口径没有这个字段」是两回事（K08 复核）：前者说清这次算哪些指标、只要哪些字段
        declared = declared_entry_fields(pinned)
        names = {str(f.get("name") or "").strip() for f in declared}
        if later := [k for k in unknown if k in names]:
            raise ComposeRefused(FIELD_OUT_OF_SELECTION.format(
                name=later[0], workflow=pinned.workflow_name, metrics=_names(_selected_ids(pinned)),
                fields=_names(list(fields)),
                others=_names([str(f.get("name")) for f in declared
                               if str(f.get("name") or "").strip() not in fields])))
        raise ComposeRefused(FIELD_UNKNOWN.format(workflow=pinned.workflow_name, name=unknown[0],
                                                  fields=_names(list(fields))))
    values = {name: copy.deepcopy(f.get("default")) for name, f in fields.items()
              if f.get("default") not in (None, "")}
    # 收值和试跑同一份（params.provided）：显式传的空串算没给——字段没声明缺省值时就是缺参数（上面判），
    # 声明了 default="" 的照缺省值走（作者写的就是「空值不过滤」）
    values.update(params.provided(inputs))
    if errors := validate_form(input_schema(list(pinned.entry_fields)), values):
        raise ComposeRefused("；".join(e["message"] for e in errors))
    problems = params.check_nodes(pinned.fetches, fields=pinned.entry_fields, values=values,
                                 where=f"口径「{pinned.workflow_name}」的入口")
    if problems:
        raise ComposeRefused(params.describe(problems))
    return values


def instantiate(option: Option, op: Mapping[str, Any], *, taken: set[str]) -> Composed:
    """按选用的那一版确定性地实例化（见模块说明）。taken 是图里已有的节点 id。不合规抛 ComposeRefused。

    op.metrics 贯穿参数校验与拷贝：先按它裁一份（_selected 的依赖闭包），check_inputs 按**这次真正要执行的取数**判参数
    （只选 A 时不再要求 B 的参数），caliber_block 也只拷 A 那条查询、把这次的选择记在口径卡上。选中的指标解析不出依赖
    ——拒收，不静默退回整卡。

    一次选用里的指标按**源卡顺序**归一（同一批指标换个写法——调用方传的顺序、重复项——得到同一份结果：Composed.metrics、
    口径卡上记的 metrics、selection_hash 与执行数组都按这一份，见 K08 S4）。"""
    pinned = option.pinned
    if pinned is None or not option.composable:
        raise ComposeRefused(NOT_COMPOSABLE.format(workflow=option.workflow_name, version=option.version,
                                                   card=option.card, reason=option.reason or "读不到这一版"))
    op_id = str(op.get("id") or "").strip()
    if not _OP_ID.match(op_id):
        raise ComposeRefused(BAD_ID.format(id=op_id))
    wanted = [str(m) for m in op.get("metrics") or [] if isinstance(m, (str, int))] \
        if isinstance(op.get("metrics"), list) else []
    on_card = {str(m["id"]): m for m in pinned.metrics}
    if not wanted:
        raise ComposeRefused(NO_METRICS.format(metrics=_names(list(on_card))))
    if missing := [m for m in wanted if m not in on_card]:
        raise ComposeRefused(METRIC_UNKNOWN.format(card=option.card, metric=missing[0], metrics=_names(list(on_card))))
    selected = _selected(pinned, wanted)
    values = check_inputs(selected, op.get("inputs"))
    block = caliber_block(selected, prefix=f"{op_id}_", card_id=op_id, inputs=values)
    if clash := next((str(n["id"]) for n in block.nodes if str(n["id"]) in taken), None):
        raise ComposeRefused(ID_TAKEN.format(id=op_id, node=clash))
    nodes = block.nodes
    edges = [{"source": a["id"], "target": b["id"]} for a, b in zip(nodes, nodes[1:])]
    metrics = [{"id": m, "name": str(on_card[m].get("name") or m)} for m in _ordered(pinned, selected, wanted)]
    return Composed(op_id=op_id, option=option, nodes=nodes, edges=edges, metrics=metrics, inputs=values,
                    closure=selected.closure)


def _ordered(pinned: Pinned, selected: Pinned, wanted: list[str]) -> list[str]:
    """这次算的指标 id，按**源卡顺序**归一（和 caliber_from.metrics、运行时、selection_hash 同一份）。

    裁剪过的那一份带着闭包，直接读它；没有闭包（理论上到不了）就按卡上指标的顺序取。"""
    closure = selected.closure
    if closure is not None:
        return [str(m) for m in closure.metric_ids]
    return [str(m["id"]) for m in pinned.metrics if str(m["id"]) in set(wanted)]


class Composer:
    """GraphApplier 用的：这一轮能选用的口径卡（CaliberKind 在开流之前读好），按 use_caliber 实例化。"""

    def __init__(self, options: list[Option]) -> None:
        self.options = options

    def pick(self, op: Mapping[str, Any]) -> Option:
        workflow_id = str(op.get("workflow_id") or "").strip()
        version = op.get("version")
        cards = [o for o in self.options if o.workflow_id == workflow_id]
        if not cards:
            named = str(op.get("workflow_name") or workflow_id or "（没写工作流）")
            raise ComposeRefused(NOT_LISTED.format(workflow=named, version=version))
        name = cards[0].workflow_name
        latest = cards[0].version
        if str(version) != str(latest):
            raise ComposeRefused(WRONG_VERSION.format(workflow=name, latest=latest, version=version))
        card = str(op.get("card") or "").strip()
        listed = [f"「{o.node_id}」（口径卡「{o.card}」）" for o in cards]
        if card:
            hit = next((o for o in cards if card in (o.node_id, o.card)), None)
            if hit is None:
                why = CARD_UNKNOWN.format(workflow=name, version=latest, card=card, cards=_names(listed))
                if card == str(op.get("id") or "").strip():
                    why += CARD_IS_OP_ID.format(card=card)
                raise ComposeRefused(why)
            return hit
        if len(cards) > 1:
            raise ComposeRefused(CARD_AMBIGUOUS.format(workflow=name, version=latest, cards=_names(listed)))
        return cards[0]

    def compose(self, op: Mapping[str, Any], *, taken: set[str]) -> Composed:
        return instantiate(self.pick(op), op, taken=taken)


__all__ = ["OP", "REFUSED", "ComposeRefused", "Composed", "Composer", "Option", "check_inputs", "instantiate",
           "signature", "slot_contexts", "value_problem"]

value_problem = _value_problem
