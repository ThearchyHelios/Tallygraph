"""查看的对象之一：口径（normal-user-apps N2，补 G10：没有按名字检索的口径库）。蓝图助手看「看哪些数」时，先查数据目录，
再查已发布工作流里的口径卡：对到已有的口径卡时优先复用（实例化成 caliber_from），不重写指标式。

只列每个工作流的**推荐发布版**（K07：Workflow.published_version，默认调用读它；最高已发布版只在升级发现里用）里的口径卡，
本身转抄别处口径卡的（caliber_from）不列——源头才能复用（运行时也只认源头，engine/nodes/metrics._pinned_caliber）。

复用的是整张卡（caliber_from 固定到那一版的那张卡），卡上每个指标都要算得出来：卡读的每个输入都要是 cell(nodes.X, 0,
'列名')、X 是查库的调用工具节点。新图里那个库的取数节点就叫 X、交出同样的列（blueprints/templates/_common.data_groups）。
读别的（整形、沙箱、模型写的、按别的行号取）的卡照实说不能复用，请按表重新对。

点名写法：source 写工作流名（或 id），name 写指标名（或指标 id）；只写 source 列那个工作流的全部指标。

**问数据里**（compose=True，next-round 02 做法 4）：同一套口径，另给每张卡的入口字段、**能选用的指标**（每个指标这次要哪几个
入口字段）和「能不能组合」，能组合的写出 use_caliber 的写法；仍然不给 SQL（SQL 里可能写着遮罩值，组合时取数节点由服务端照抄，
见 ledger/compose.py）。能不能组合 K08 S4 起按**每个指标**判（calibers.metric_options：依赖闭包加同一份「输入能不能复用」的
判定）：B 独有那条查询坏了、绑定对不上，只让 B 用不了，A 照旧给组合写法；一个可选指标也没有才标不能组合。这一层只是预筛、
不改任何判断——真正选用时 ledger/compose.instantiate 按这次要算的指标逐项硬校验。读好的版本快照（derive.read_pinned，只读
这一版、不判依赖）交给 GraphApplier，流里不碰库。蓝图助手不走这一支，行为不变。

**普通使用者只看得到有授权的口径（K14）。** 看这份清单的普通使用者（开登录后的 user 角色，`permitted_versions`）只列
`apps/access.usable_calibers` 里那些「未归档应用的当前发布版精确固定着的」版本，别的连解析都不做——模型因此只会从它有授权的
口径里挑，不会挑一个一调就被 QueryCore 拒的。builder / admin / 关登录不按这一层收窄（它们本来就能按口径执行），行为不变；
能不能执行最终还是 QueryCore 按同一份授权判（querycore 的 `_require_caliber`），这一层只是不让模型看见用不了的。

**问数据里的普通使用者**（`query=True`，K14）看的是同一份清单，但**没有 use_caliber**：那条路要把口径的取数节点（含 SQL）
拷进使用者的图里，只有搭建者及以上能走。这一档照样给**入口字段、能算哪些指标**（同一份 `metric_options`：安全，没有 SQL、
没有参数值），再加上「在一个 agent 节点里照这一行调口径查询工具」的写法——使用者问的数因此来自一次固定口径执行，而不是自己
拼一份差不多的 SQL。
"""
from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from app.look.base import PICK_LIMIT, LookTarget
from app.look.pick import Picker, PickFailed

# ==========================================================================
# 写给模型的文字
# ==========================================================================

OVERVIEW_HEAD_PROMPT = "已发布的口径（要复用已有的指标时用 look，kind 写 caliber）："
OVERVIEW_LINE_PROMPT = "- {workflow} v{version}：{metrics}"
OVERVIEW_MORE_PROMPT = "- …另有 {n} 个工作流"
DETAIL_HEAD_PROMPT = "· {name}（指标 id {metric}{unit}）：出自「{workflow}」v{version} 的口径卡「{card}」（{caliber}）"
DETAIL_EXPR_PROMPT = "    指标式：{expression}"
#: 定义说明只在详情里给：它是这张口径的业务边界（适用范围、不包含什么），选口径时要看的就是这一句。
#: 概览一行放不下，也不该让它挤掉指标名
DETAIL_DEFINITION_PROMPT = "    定义：{text}"
#: 定义说明写进查看结果的上限：口述给人看的边界通常一两句，超出的部分在口径账本里看全文
DEFINITION_MAX = 240
#: 一跳已确认本体邻居摘要（无关联时不输出，不改变既有口径详情）
DETAIL_ONTOLOGY_PROMPT = (
    "    本体关联指标（已确认关系，异常穿透可调 ontology_neighbors / source_health / caliber_query）：{neighbors}"
)
ONTOLOGY_NEIGHBOR_LIMIT = 5
_ONTOLOGY_KIND_LABELS = {
    "factor": "构成因子",
    "reconciles_with": "跨源对账",
    "upstream_driver": "上游驱动",
}
DETAIL_REUSE_PROMPT = ("    复用写法（「看哪些数」这一项的 reuse 照抄，不要改）：{reuse}\n"
                       "    这张卡读：{inputs}。实例化时那个库的取数节点就叫这个名字、交出这几列，复用才算得出来")
DETAIL_INPUT_PROMPT = "{node}（{tool}）的列 {columns}"
DETAIL_NOT_REUSABLE_PROMPT = "    不能复用：{reason}。请按表重新对"
#: 问数据里（compose）每张卡写一次：入口字段、能选用的指标、能不能组合
DETAIL_FIELDS_PROMPT = "    口径「{workflow}」v{version} 的入口字段：{fields}"
DETAIL_FIELD_PROMPT = "{name}（{bits}）{desc}"
DETAIL_NO_FIELDS_PROMPT = "    口径「{workflow}」v{version} 的入口没有字段"
#: 这次能选用的指标：每个写它要哪几个入口字段（K08 S4：按这次的选择闭包写，B 那条查询坏了只影响 B）
DETAIL_SELECTABLE_PROMPT = "    能选用的指标：{metrics}"
#: 这次用不了的指标：每个写一句原因。照实写出来，不让模型挑一个一选就被拒的
DETAIL_UNAVAILABLE_PROMPT = "    用不了的指标：{metrics}"
DETAIL_COMPOSE_PROMPT = ("    能组合：选用写法 {op}（card 原样照抄，是那一版图里口径卡的节点 id；id 自己起，生成的口径卡用它当节点 id；"
                         "metrics 从上面能选用的指标里挑，写指标 id；inputs 只填选中的那几个指标要的字段——上面每个指标后面写了要哪些，"
                         "没写的取缺省值）")
DETAIL_NOT_COMPOSABLE_PROMPT = "    不能组合：{reason}。这份口径只能作为参考，查库交给 agent"
#: 普通使用者那一档（K14）：这一档没有 use_caliber（那条路要把口径的取数 SQL 拷进图里），给的是「在一个 agent 节点里照
#: 这一行调口径查询工具」的写法。口径身份、指标 id、inputs 照抄；工具自己读推荐版并当场固定，所以这里没有版本号
DETAIL_QUERY_PROMPT = ("    取数写法（在 agent 节点的 prompt 里让它照这一行调用 {tool} 工具，不要自己写取数 SQL、"
                       "也不要照口径的 SQL 拼一份）：{call}\n"
                       "    这里 workflow 写的是工作流 id，card 是口径卡的节点 id，照抄不要改；inputs 只填这次用得上的入口字段，"
                       "相对时间（上个月、上周这类）先取当前日期再折算")
DETAIL_NO_QUERY_PROMPT = "    这一版没有能按 {tool} 算的指标：{reason}。要这个数就按表自己查"
BRIEF_PROMPT = "· {name}（{workflow} v{version}）：字数超过上限，只列名字，要看指标式时点名再 look"
MISSING_PROMPT = "没找到口径：{items}（只能查看已发布且未退役的版本里的口径卡；别拿名字相近的顶替）"
INDEX_LINE_PROMPT = "{workflow}｜{card}｜{name}（{metric}）｜{unit}"
PICK_WHAT_PROMPT = "口径指标"
PICK_LINE_PROMPT = "每行一个指标：工作流名｜口径卡｜指标名（指标 id）｜单位；source 写工作流名，name 写指标名"

#: 概览最多列几个工作流、每个工作流最多列几个指标名
OVERVIEW_WORKFLOWS = 30
OVERVIEW_METRICS = 8

#: 不能复用的原因（写进查看结果，也是蓝图存的时候拒收的说法）
NOT_TOOL_REASON = "这张口径卡的输入不全是查库结果里的格（有整形、代码、模型写的或别的口径卡的数）"
ROW_REASON = "这张口径卡按别的行号取格，模板的取数只出一行"
CHAINED_REASON = "这张口径卡本身也是转抄别处的口径，请复用源头那一张"


@dataclass(frozen=True)
class CaliberView:
    """一个已发布口径卡上的一个指标。inputs 是整张卡读的取数节点和列（能复用时），reason 是不能复用的原因。"""

    workflow_id: str
    workflow_name: str
    version: int
    level: str
    node_id: str
    card: str
    caliber: str
    metric_id: str
    name: str
    unit: str
    expression: str
    inputs: tuple[tuple[str, str, tuple[str, ...]], ...]
    reason: str | None
    #: 指标卡上的定义说明（说明、适用范围、不包含什么），压成一句；没有定义时为空串
    definition: str = ""
    aliases: tuple[str, ...] = ()

    @property
    def label(self) -> str:
        return f"{self.workflow_name} v{self.version} · {self.name}"

    def reuse(self) -> dict[str, Any] | None:
        """「看哪些数」这一项的 reuse（blueprints/ops._reuse 的形状）；不能复用为 None。"""
        if self.reason is not None:
            return None
        return {"workflow_id": self.workflow_id, "version": self.version, "node_id": self.node_id,
                "metric_id": self.metric_id,
                "inputs": [{"node": n, "tool": t, "columns": list(cols)} for n, t, cols in self.inputs]}


def measured_inputs(nodes: Mapping[str, Any], metrics: Iterable[Any]) -> tuple[dict[str, dict[str, Any]], str | None]:
    """这几个指标读的取数节点与列：{节点 id: {node, tool, columns}}（按第一次被读到的先后，列按出现先后去重）。

    **每个输入都要**是 cell(nodes.X, 0, '列名')、X 是查库的调用工具节点：有一处不是就整份不收、给原因（不是查库结果里的
    格 / 按别的行号取格，按卡上指标的先后取第一条不合规的）。整卡（card_inputs）与按指标（metric_inputs）都走这一份判定，
    不各写一遍。"""
    from app.engine.expressions import ExpressionError, cell_parts, leaf_refs, parse_expression
    from app.tools.datasource import QUERY_PREFIX

    found: dict[str, dict[str, Any]] = {}
    for metric in metrics:
        paths = _metric_paths(metric)
        if paths is None:
            return {}, NOT_TOOL_REASON
        for path in paths:
            try:
                tree = parse_expression(path)[0]
            except ExpressionError:
                return {}, NOT_TOOL_REASON
            for leaf in leaf_refs(tree):
                parts = cell_parts(leaf)
                if parts is None:
                    return {}, NOT_TOOL_REASON
                chain, row, column = parts
                node_id = chain[len("nodes."):] if chain.startswith("nodes.") else ""
                tool = str((nodes.get(node_id) or {}).get("config", {}).get("tool") or "")
                if (nodes.get(node_id) or {}).get("type") != "tool" or not tool.startswith(QUERY_PREFIX):
                    return {}, NOT_TOOL_REASON
                if row.strip() != "0":
                    return {}, ROW_REASON
                try:
                    col = json.loads(column.replace("'", '"')) if column[:1] in ("'", '"') else None
                except ValueError:
                    col = None
                if not isinstance(col, str):
                    return {}, NOT_TOOL_REASON
                entry = found.setdefault(node_id, {"node": node_id, "tool": tool, "columns": []})
                if col not in entry["columns"]:
                    entry["columns"].append(col)
    return found, None


def _card_node(graph: Mapping[str, Any], card_id: str) -> dict[str, Any] | None:
    """图里那张口径卡；不是口径卡（或本身是转抄卡）时给不出可复用的输入。"""
    from app.apps.tree import node_map

    card = node_map(graph).get(card_id)
    if card is None or card["type"] != "metrics" or card["config"].get("caliber_from") not in (None, "", {}):
        return None
    return card


def card_inputs(graph: dict[str, Any], card_id: str) -> tuple[list[dict[str, Any]], str | None]:
    """整张口径卡读的取数节点和列：([{node, tool, columns}], 不能复用的原因)。每个指标式的每个输入都要是
    cell(nodes.X, 0, '列名')、X 是查库的调用工具节点（engine/lineage 认的原始数据，且只取第一行）。"""
    from app.apps.tree import node_map

    nodes = node_map(graph)
    card = nodes.get(card_id)
    if card is None or card["type"] != "metrics":
        return [], "指的不是口径卡"
    if card["config"].get("caliber_from") not in (None, "", {}):
        return [], CHAINED_REASON
    found, reason = measured_inputs(nodes, card["config"].get("metrics") or [])
    return (list(found.values()), None) if reason is None else ([], reason)


def metric_inputs(graph: dict[str, Any], card_id: str, metric: Any) -> tuple[list[dict[str, Any]], str | None]:
    """一个指标读的取数节点和列（判定与 card_inputs 同一份 measured_inputs）：([{node, tool, columns}], 原因)。

    look 的按指标可组合性（metric_options）用它：B 那条查询读不出格，不影响 A 那条。"""
    from app.apps.tree import node_map

    card = _card_node(graph, card_id)
    if card is None:
        return [], "指的不是口径卡"
    found, reason = measured_inputs(node_map(graph), [metric])
    return (list(found.values()), None) if reason is None else ([], reason)


@dataclass(frozen=True)
class MetricOptions:
    """一张源头口径卡这一版**按指标**的可组合性（K08 S4）：整卡那一份闭包的摘要 + 每个指标能不能选。

    ``default`` / ``default_reason`` 是「不点名、算整卡」那一份（能算就给摘要，算不出来就给原因）；``selectable`` 与
    ``unavailable`` 按**源卡顺序**。摘要里只有 query_id、节点、工具、入口字段、指纹与共享声明（computes_unselected），
    **没有 SQL 正文、没有参数值**。"""

    default: dict[str, Any] | None
    default_reason: str | None
    selectable: tuple[dict[str, Any], ...]
    unavailable: tuple[dict[str, Any], ...]

    @property
    def composable(self) -> bool:
        return bool(self.selectable)


def metric_options(graph: Mapping[str, Any], card_id: str, *, workflow_id: str, version: int) -> MetricOptions:
    """这张源头口径卡上每个指标**单独点名**能不能选（K08 S4）。判据只有两条，都是既有那一份判定：

    ① 它读的输入能不能复用：每个输入都是 cell(nodes.X, 0, '列名')、X 是查库的调用工具节点（measured_inputs，与
    card_inputs 同一份）；② 它的依赖闭包有没有未决（ledger/closure：式子读得出来、取数节点在、SQL 有、参数声明对得上、
    绑定对得上）。不成立的进 unavailable 并写明原因——**B 独有那条查询坏了、绑定对不上，不影响 A**；A 和 B 共用一条
    SQL 时那条 SQL 按各自的选择照常保留，还在算谁记在 summary 的 computes_unselected 上。

    这里只是**预筛**（给模型看能不能组合、能选哪些指标）：不改任何判断，真正选用时 ledger/compose.instantiate 按同一份
    闭包逐项硬校验。同一张卡这一版算几次都一样。"""
    from app.ledger import closure as caliber_closure
    from app.ledger.derive import problems_text

    card = _card_node(graph, card_id)
    if card is None:
        return MetricOptions(None, "指的不是口径卡", (), ())
    metrics = [m for m in card["config"].get("metrics") or [] if isinstance(m, dict) and m.get("id")]
    whole = caliber_closure.build(graph, workflow_id, version, card_id)          # 不点名：整卡那一份
    selectable: list[dict[str, Any]] = []
    unavailable: list[dict[str, Any]] = []
    for metric in metrics:
        metric_id, name = str(metric["id"]), str(metric.get("name") or metric["id"])
        _, reason = metric_inputs(graph, card_id, metric)
        if reason is None:
            one = caliber_closure.build(graph, workflow_id, version, card_id, metric_ids=[metric_id])
            if not one.unresolved:
                semantic = _query_semantic_brief(metric)
                selectable.append({"metric_id": metric_id, "name": name, **one.summary(),
                                   **({"query_semantics": semantic} if semantic else {})})
                continue
            reason = problems_text(one.unresolved)
        unavailable.append({"metric_id": metric_id, "name": name, "reason": reason})
    return MetricOptions(whole.summary() if not whole.unresolved else None,
                         problems_text(whole.unresolved) if whole.unresolved else None,
                         tuple(selectable), tuple(unavailable))


def _metric_paths(metric: Any) -> list[str] | None:
    from app.engine.metric_spec import metric_expressions

    return metric_expressions(metric)


def definition_text(definition: Any) -> str:
    """指标卡上的定义说明 → 一句给模型看的文字：说明、适用范围、不包含什么。

    只取这三项：负责人和生效日期是治理信息，模型选口径时用不上，写进去只会挤占额度。
    """
    if not isinstance(definition, dict):
        return ""
    parts: list[str] = []
    text = definition.get("text")
    if isinstance(text, str) and text.strip():
        parts.append(text.strip())
    scope = definition.get("scope")
    if isinstance(scope, str) and scope.strip():
        parts.append(f"适用范围：{scope.strip()}")
    exclusions = definition.get("exclusions")
    if isinstance(exclusions, list):
        kept = [str(x).strip() for x in exclusions if str(x).strip()]
        if kept:
            parts.append("不包含：" + "、".join(kept))
    merged = "；".join(parts)
    return merged[:DEFINITION_MAX] + ("…" if len(merged) > DEFINITION_MAX else "")


def views_of(workflow_id: str, workflow_name: str, version: Any) -> list[CaliberView]:
    """一个已发布版本里每张口径卡（源头的）的每个指标。"""
    from app.apps.tree import node_map

    graph = getattr(version, "graph", None) or {}
    out: list[CaliberView] = []
    for nid, node in node_map(graph).items():
        if node["type"] != "metrics" or node["config"].get("caliber_from") not in (None, "", {}):
            continue
        inputs, reason = card_inputs(graph, nid)
        packed = tuple((i["node"], i["tool"], tuple(i["columns"])) for i in inputs)
        caliber = f"{node['config'].get('caliber') or node['title']} @ {node['config'].get('caliber_version') or 'v1'}"
        for metric in node["config"].get("metrics") or []:
            if not isinstance(metric, dict) or not metric.get("id"):
                continue
            out.append(CaliberView(
                workflow_id=workflow_id, workflow_name=workflow_name, version=int(version.version),
                level=str(getattr(version, "level", None) or ""), node_id=nid, card=node["title"], caliber=caliber,
                metric_id=str(metric["id"]), name=str(metric.get("name") or metric["id"]),
                unit=str(metric.get("unit") or ""), expression=str(metric.get("expression") or ""), inputs=packed,
                reason=reason, definition=definition_text(metric.get("definition")),
                aliases=tuple(s for s in [*(metric.get("synonyms") or []),
                                         *((metric.get("semantic") or {}).get("aliases") or [])] if isinstance(s, str))))
    return out


def field_text(f: dict[str, Any]) -> str:
    """入口字段写给模型的一段：名字（类型，必填，可选值，格式，缺省值）：说明。同 apps/describe 的写法，多写缺省值。"""
    bits = [str(f.get("type") or "string")]
    if f.get("required") and f.get("default") in (None, ""):
        bits.append("必填")
    if isinstance(f.get("enum"), list):
        bits.append("可选值 " + " / ".join(str(v) for v in f["enum"][:10]))
    if f.get("format"):
        bits.append(f"格式 {f['format']}")
    if f.get("default") not in (None, ""):
        bits.append(f"缺省 {f['default']}")
    desc = f"：{f['description']}" if f.get("description") else ""
    return DETAIL_FIELD_PROMPT.format(name=f.get("name"), bits="，".join(bits), desc=desc)


async def permitted_versions(session: Any, actor: Any = None) -> set[tuple[str, int]] | None:
    """这个 actor 能用的口径版本（`(workflow_id, version)` 的集合）；None 表示**不按资产授权收窄**。

    None 是 builder / admin / 关登录（auth/chat_policy.restricted_actor 交回 False）：它们本来就能按口径执行、也能自己
    在画布上绑口径查询，这里不替它们收窄。普通使用者交回它有授权的那些版本（apps/access.usable_calibers：未归档应用的
    当前发布版精确固定着这一版 + AppGrant 至少 use）；一份也没有时是空集合，清单就是空的。`actor` 不给就取当前请求的身份。"""
    from app.apps.access import usable_calibers
    from app.auth.chat_policy import effective_actor, restricted_actor

    who = effective_actor(actor)
    if not await restricted_actor(session, who):
        return None
    return {(str(workflow_id), int(version))
            for workflow_id, version in (await usable_calibers(session, who))}


class CaliberKind:
    kind = "caliber"
    label = "口径"

    def __init__(self, items: list[CaliberView], *, picker: Picker | None = None,
                 options: list[Any] | None = None, query: bool = False,
                 neighbors: Mapping[tuple[str, int, str, str], list[dict[str, Any]]] | None = None) -> None:
        self.items = items
        self.picker = picker
        #: 问数据里（compose / query 两档）：每张卡读好的那一份（入口字段、能算哪些指标、能不能组合），没读时为 None
        self.options = options
        #: 问数据里**普通使用者**那一档（K14）：不给 use_caliber（把口径的 SQL 拷进图），只给按口径查询工具取数的写法。
        #: use_caliber 能不能用看这个标记（api/copilot_context.composing）：options 两档都读，差别只在怎么用
        self.query = query
        #: 已确认的一跳本体关联指标：{(workflow_id, version, node_id, metric_id): [neighbor_dict, ...]}
        self.neighbors: dict[tuple[str, int, str, str], list[dict[str, Any]]] = dict(neighbors or {})

    @classmethod
    async def load(cls, session: Any, *, picker: Picker | None = None, compose: bool = False,
                   actor: Any = None, query: bool = False, ontology: bool = True) -> CaliberKind:
        """读每个工作流**推荐发布版**（Workflow.published_version，K07）里的口径卡。用调用方的会话，开流之前读好。
        compose：问数据里（搭建者及以上），另把每张卡的那一版读好、判能不能组合（见模块说明）。
        query：问数据里（普通使用者），读同一份数据、但只给按口径查询工具取数的写法（use_caliber 不开）。

        默认调用读推荐版（重新推上一版旧版之后默认就跟着它），最高已发布版只在升级发现里用（publications.upgrade_of）；
        purpose 是服务端入口常量：compose 表示这次是「组合式问数选用口径」，否则按探索（查看/给出）。`actor` 是这次是谁
        （Principal 或用户名，不给就取当前请求的身份）：**普通使用者只列有授权的那些版本**（permitted_versions），
        builder / admin / 关登录照旧列全部。"""
        from sqlalchemy import select

        from app.apps import publications
        from app.db.models import Workflow

        purpose = publications.PURPOSE_COMPOSE if (compose or query) else publications.PURPOSE_EXPLORE
        permitted = await permitted_versions(session, actor)
        items: list[CaliberView] = []
        for wf in (await session.execute(select(Workflow).order_by(Workflow.name, Workflow.id))).scalars().all():
            if permitted is not None and not any(str(wf.id) == key[0] for key in permitted):
                continue    # 这个工作流一版也没授权给它：连推荐版都不解析
            resolved = await publications.resolve(session, wf, purpose=purpose)
            if resolved is not None and (permitted is None or (str(wf.id), int(resolved.version)) in permitted):
                items += views_of(wf.id, wf.name, resolved.row)
        read = await _options(session, items) if (compose or query) else None
        neighbors = await _load_ontology_neighbors(
            session,
            items,
            permitted=permitted,
            restricted=bool(query or permitted is not None),
        ) if ontology else {}
        return cls(items, picker=picker, options=read, query=query, neighbors=neighbors)

    def option_of(self, item: CaliberView) -> Any | None:
        if self.options is None:
            return None
        return next((o for o in self.options if (o.workflow_id, o.version, o.node_id) ==
                     (item.workflow_id, item.version, item.node_id)), None)

    def _workflows(self) -> dict[str, list[CaliberView]]:
        out: dict[str, list[CaliberView]] = {}
        for item in self.items:
            out.setdefault(item.workflow_id, []).append(item)
        return out

    def _pool(self, sources: list[str] | None) -> list[CaliberView]:
        return [i for i in self.items if not sources or i.workflow_name in sources or i.workflow_id in sources]

    # ---------------------------------------------------------------- 概览、目录

    async def overview(self) -> list[str]:
        flows = self._workflows()
        if not flows:
            return []
        lines = [OVERVIEW_HEAD_PROMPT]
        for items in list(flows.values())[:OVERVIEW_WORKFLOWS]:
            names = "、".join(i.name for i in items[:OVERVIEW_METRICS]) + ("…" if len(items) > OVERVIEW_METRICS else "")
            lines.append(OVERVIEW_LINE_PROMPT.format(workflow=items[0].workflow_name, version=items[0].version,
                                                     metrics=names))
        if len(flows) > OVERVIEW_WORKFLOWS:
            lines.append(OVERVIEW_MORE_PROMPT.format(n=len(flows) - OVERVIEW_WORKFLOWS))
        return lines

    async def index(self, sources: list[str] | None) -> str:
        return "\n".join(INDEX_LINE_PROMPT.format(workflow=i.workflow_name, card=i.card, name=i.name,
                                                  metric=i.metric_id, unit=i.unit or "-")
                         for i in self._pool(sources))

    # ---------------------------------------------------------------- 点名、挑选

    async def resolve(self, targets: list[LookTarget]) -> tuple[list[tuple[CaliberView, str]], list[LookTarget]]:
        found: list[tuple[CaliberView, str]] = []
        missing: list[LookTarget] = []
        for t in targets:
            source, name = str(t.get("source") or ""), str(t.get("name") or "")
            hits = [i for i in self._pool([source] if source else None) if name in (i.name, i.metric_id)]
            if not hits:
                missing.append(t)
            for hit in hits:
                if all(i is not hit for i, _ in found):
                    found.append((hit, "named"))
        return found, missing

    async def pick(self, need: str, sources: list[str] | None) -> tuple[list[tuple[CaliberView, str]], str | None]:
        pool = self._pool(sources)
        from app.ledger.metric_matching import score

        ranked = [(score(need, item.name, item.aliases), item) for item in pool]
        hits = [(weight, item) for weight, item in ranked if weight]
        if hits:
            best = max(weight for weight, _ in hits)
            matched = [item for weight, item in hits if weight == best]
            if len(matched) <= PICK_LIMIT:
                return [(item, "matched") for item in matched], None
            return [], "同名口径过多，请指定完整指标名或所属口径"
        if len(pool) <= PICK_LIMIT:
            return [(i, "picked") for i in pool], None
        if self.picker is None:
            return [], "没有可用的挑选模型"
        try:
            picks = await self.picker.pick(need=need, what=PICK_WHAT_PROMPT, line=PICK_LINE_PROMPT,
                                           index=await self.index(sources))
        except PickFailed as e:
            return [], str(e)
        out: list[tuple[CaliberView, str]] = []
        for source, name in picks:
            for i in pool:
                if name in (i.name, i.metric_id) and (not source or source in (i.workflow_name, i.workflow_id)) \
                        and all(o is not i for o, _ in out):
                    out.append((i, "picked"))
        if picks and not out:
            return [], "挑出的指标不在已发布的口径卡里"
        return out[:PICK_LIMIT], None

    def key(self, obj: tuple[CaliberView, str]) -> str:
        i = obj[0]
        return f"caliber:{i.workflow_id}:{i.version}:{i.node_id}:{i.metric_id}"

    # ---------------------------------------------------------------- 详情

    async def detail(self, objects: list[tuple[CaliberView, str]], budget: int, *,
                     missing: list[LookTarget] = ()) -> tuple[str, list[dict[str, Any]]]:
        lines: list[str] = []
        used = 0
        #: 问数据里每张卡的入口字段、组合写法只写一次（同一张卡的几个指标挨着列）
        seen: set[tuple[str, int, str]] = set()
        for item, _how in objects:
            block = [DETAIL_HEAD_PROMPT.format(name=item.name, metric=item.metric_id,
                                               unit=f"，单位 {item.unit}" if item.unit else "",
                                               workflow=item.workflow_name, version=item.version, card=item.card,
                                               caliber=item.caliber),
                     DETAIL_EXPR_PROMPT.format(expression=item.expression)]
            if item.definition:
                block.append(DETAIL_DEFINITION_PROMPT.format(text=item.definition))
            item_neighbors = self.neighbors.get((item.workflow_id, item.version, item.node_id, item.metric_id))
            if item_neighbors:
                block.append(DETAIL_ONTOLOGY_PROMPT.format(
                    neighbors=_format_ontology_neighbors(item_neighbors, restricted=self.query)
                ))
            option = self.option_of(item)
            if option is not None:
                block += _query_lines(item, option, seen) if self.query else _compose_lines(item, option, seen)
            elif (reuse := item.reuse()) is not None:
                block.append(DETAIL_REUSE_PROMPT.format(
                    reuse=json.dumps({k: v for k, v in reuse.items() if k != "inputs"}, ensure_ascii=False),
                    inputs="；".join(DETAIL_INPUT_PROMPT.format(node=n, tool=t, columns="、".join(cols))
                                    for n, t, cols in item.inputs)))
            else:
                block.append(DETAIL_NOT_REUSABLE_PROMPT.format(reason=item.reason))
            text = "\n".join(block)
            if used + len(text) > budget:
                text = BRIEF_PROMPT.format(name=item.name, workflow=item.workflow_name, version=item.version)
            used += len(text) + 1
            lines.append(text)
        if missing:
            lines.append(MISSING_PROMPT.format(items="、".join(
                f"{t.get('source') or ''} {t.get('name') or ''}".strip() for t in missing)))
        record = {"kind": "caliber", "named": [i.label for i, how in objects if how == "named"],
                  "picked": [i.label for i, how in objects if how == "picked"],
                  "missing": [f"{t.get('source') or ''} {t.get('name') or ''}".strip() for t in missing]}
        return "\n".join(lines), [record]

    # ---------------------------------------------------------------- 复用

    def reuse_for(self, ref: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
        """蓝图助手写的 reuse → 按这一轮读好的口径核对并补上 inputs：(完整的 reuse, 不能复用的原因)。"""
        for item in self.items:
            if (item.workflow_id, item.version, item.node_id, item.metric_id) == (
                    ref.get("workflow_id"), ref.get("version"), ref.get("node_id"), ref.get("metric_id")):
                return (item.reuse(), None) if item.reason is None else (None, item.reason)
        return None, "只能复用已发布且未退役的版本里的口径卡（look 给出的那一版）"


async def _options(session: Any, items: list[CaliberView]) -> list[Any]:
    """每张卡（工作流、版本、卡）读一次那一版，按**每个指标**判能不能组合（K08 S4）。

    整卡那一道门禁不再先跑：B 独有那条查询坏了、绑定对不上、参数没声明，都只让 B 不可选，A 照旧给组合写法。可选哪些
    指标、各自要哪些入口字段、不能选的为什么，写在 selectable_metrics / unavailable_metrics 上；一个指标也选不了才标
    不能组合（原因照实给）。**这一层只是预筛**：真正选用时 ledger/compose.instantiate 按这次要算的指标逐项硬校验
    （未知指标、参数、绑定），不靠预筛的结论放宽。
    """
    from app.ledger import derive
    from app.ledger.compose import Option

    out: list[Any] = []
    for item in items:
        if any((o.workflow_id, o.version, o.node_id) == (item.workflow_id, item.version, item.node_id) for o in out):
            continue
        base = {"workflow_id": item.workflow_id, "workflow_name": item.workflow_name, "version": item.version,
                "level": item.level, "node_id": item.node_id, "card": item.card}
        try:
            pinned = await derive.read_pinned(session, item.workflow_id, item.version, item.node_id)
        except derive.NotDerivable as e:
            out.append(Option(**base, reason=e.message))
            continue
        made = metric_options(pinned.graph, pinned.node_id, workflow_id=pinned.workflow_id, version=pinned.version)
        reason = option_reason(made)
        if not made.composable:
            out.append(Option(**base, reason=reason, unavailable_metrics=made.unavailable))
            continue
        out.append(Option(**base, pinned=pinned, reason=reason, selectable_metrics=made.selectable,
                          unavailable_metrics=made.unavailable, default_closure=made.default))
    return out


def no_choice_reason(made: MetricOptions) -> str:
    """一个指标也选不了时给模型的那句话：先说第一个指标为什么不行，实在没有就照整卡那份闭包的原因说。"""
    if made.unavailable:
        first = made.unavailable[0]
        return f"「{first.get('name') or first.get('metric_id')}」{first.get('reason')}"
    return made.default_reason or "这张卡这一版没有能选用的指标"


def option_reason(made: MetricOptions) -> str | None:
    """这一版该给的那句「能不能组合」：能组合时是不点名（整卡）那一份算不出来的原因（能算出来就是 None），
    一个指标也选不了时是第一个指标的原因。列表接口、单条接口与问数据的选用选项用的是同一份。"""
    return made.default_reason if made.composable else no_choice_reason(made)


def _card_fields(option: Any) -> list[dict[str, Any]]:
    """这次能算的指标真正要用的入口字段（按声明顺序）：入口字段只列用得上的那些，写法同 apps/describe。"""
    needed = {str(name) for m in option.selectable_metrics for name in m["required_entry_fields"]}
    return [f for f in option.pinned.entry_fields if str(f.get("name")) in needed]


def _query_lines(item: CaliberView, option: Any, seen: set[tuple[str, int, str]]) -> list[str]:
    """普通使用者那一档（K14）：一张卡的入口字段、能算的指标和**按口径查询工具取数**的写法（同一张卡只写一次）。

    和 `_compose_lines` 的差别只有最后一行：那条路是 use_caliber（服务端把口径的取数节点拷进图里），这一条是模型在一个
    agent 节点里调口径查询工具（工具自己读推荐版并当场固定，所以写法里没有版本号）。两档都不给 SQL、不给参数值。"""
    from app.ledger.querycore import CALIBER_QUERY

    key = (item.workflow_id, item.version, item.node_id)
    if key in seen:
        return []
    seen.add(key)
    if not option.composable:
        return [DETAIL_NO_QUERY_PROMPT.format(tool=CALIBER_QUERY,
                                              reason=option.reason or "这张卡这一版没有能算的指标")]
    fields = _card_fields(option)
    lines = [DETAIL_FIELDS_PROMPT.format(workflow=item.workflow_name, version=item.version,
                                         fields="；".join(field_text(f) for f in fields))
             if fields else DETAIL_NO_FIELDS_PROMPT.format(workflow=item.workflow_name, version=item.version)]
    lines.append(DETAIL_SELECTABLE_PROMPT.format(metrics=metric_briefs(option.selectable_metrics)))
    if option.unavailable_metrics:
        lines.append(DETAIL_UNAVAILABLE_PROMPT.format(metrics=metric_briefs(option.unavailable_metrics)))
    call = {"workflow": item.workflow_id, "card": item.node_id,
            "metric_ids": [m["metric_id"] for m in option.selectable_metrics][:6],
            "inputs": {str(f["name"]): "…" for f in fields}}
    lines.append(DETAIL_QUERY_PROMPT.format(tool=CALIBER_QUERY, call=json.dumps(call, ensure_ascii=False)))
    return lines


def _compose_lines(item: CaliberView, option: Any, seen: set[tuple[str, int, str]]) -> list[str]:
    """问数据里一张卡的入口字段、能选用的指标和组合写法（同一张卡只写一次）。

    K08 S4：能选哪些指标、各自要哪些入口字段按**这次的选择闭包**写（B 那条查询坏了只影响 B，共享 SQL 照常保留）；
    用不了的指标连同原因照实写出来，不让模型挑一个一选就被拒的。摘要里没有 SQL、没有参数值。"""
    key = (item.workflow_id, item.version, item.node_id)
    if key in seen:
        return []
    seen.add(key)
    if not option.composable:
        return [DETAIL_NOT_COMPOSABLE_PROMPT.format(reason=option.reason or "这张卡这一版没有能选用的指标")]
    # 入口字段只列这次可选指标真正要用的那些（声明、类型、缺省值照 apps/describe 那个写法）
    fields = _card_fields(option)
    lines = [DETAIL_FIELDS_PROMPT.format(workflow=item.workflow_name, version=item.version,
                                         fields="；".join(field_text(f) for f in fields))
             if fields else DETAIL_NO_FIELDS_PROMPT.format(workflow=item.workflow_name, version=item.version)]
    lines.append(DETAIL_SELECTABLE_PROMPT.format(metrics=metric_briefs(option.selectable_metrics)))
    if option.unavailable_metrics:
        lines.append(DETAIL_UNAVAILABLE_PROMPT.format(metrics=metric_briefs(option.unavailable_metrics)))
    example = {"op": "use_caliber", "id": "c1", "workflow_id": item.workflow_id, "version": item.version,
               "card": item.node_id, "metrics": [m["metric_id"] for m in option.selectable_metrics][:6],
               "inputs": {str(f["name"]): "…" for f in fields}}
    lines.append(DETAIL_COMPOSE_PROMPT.format(op=json.dumps(example, ensure_ascii=False)))
    return lines


def metric_briefs(entries: Iterable[Mapping[str, Any]]) -> str:
    """一行里一个指标一段：能选的写它要哪些入口字段，用不了的写原因（都不放 SQL 与参数值）。"""
    out: list[str] = []
    for m in entries:
        name, metric_id = str(m.get("name") or m.get("metric_id")), str(m.get("metric_id"))
        if "required_entry_fields" in m:
            need = [str(x) for x in m["required_entry_fields"]]
            out.append(f"{name}（{metric_id}，要 {'、'.join(need)}）" if need else f"{name}（{metric_id}，不用入口字段）")
        else:
            out.append(f"{name}（{metric_id}）：{m.get('reason')}")
        if m.get("query_semantics"):
            out[-1] += "；问数定义：" + str(m["query_semantics"])
    return "；".join(out) or "无"


def _query_semantic_brief(metric: Mapping[str, Any]) -> str:
    """Give the authorized Agent the same published meanings used by the direct router.

    Bound the dictionary size in prompts; an omitted member is not permission to guess it.
    The full model remains in the pinned metric definition, not in model-generated SQL.
    """
    from pydantic import ValidationError
    from app.ledger.semantic import QuerySemantics

    if not isinstance(metric.get("semantic"), Mapping):
        return ""
    try:
        model = QuerySemantics.model_validate(metric["semantic"])
    except ValidationError:
        return "定义不完整，先向维护者确认"
    if not model.enabled:
        return "对象、时间和汇总规则待确认，不要自行推断后查询"
    data = model.model_dump(mode="json")
    for entity in data["entities"]:
        entity["members_truncated"] = len(entity["members"]) > 20
        entity["members"] = entity["members"][:20]
    return json.dumps(data, ensure_ascii=False) + "；只按已声明的对象与周期取数；未列出的映射不可猜测"


def _format_ontology_neighbors(entries: list[dict[str, Any]], *, restricted: bool = False) -> str:
    parts: list[str] = []
    for nb in entries[:ONTOLOGY_NEIGHBOR_LIMIT]:
        kind_label = _ONTOLOGY_KIND_LABELS.get(str(nb.get("kind") or ""), str(nb.get("kind") or ""))
        extra: list[str] = [kind_label]
        if nb.get("role"):
            extra.append(f"角色 {nb['role']}")
        if nb.get("entity_key"):
            extra.append(f"对齐实体 {nb['entity_key']}")
        if not restricted and nb.get("sources"):
            extra.append("数据源 " + "/".join(str(s) for s in nb["sources"]))
        parts.append(
            f"{nb['name']}（workflow={nb['workflow_id']}，card={nb['card_id']}，metric={nb['metric_id']}，"
            f"{'，'.join(extra)}）"
        )
    if len(entries) > ONTOLOGY_NEIGHBOR_LIMIT:
        parts.append(f"…另 {len(entries) - ONTOLOGY_NEIGHBOR_LIMIT} 项")
    return "；".join(parts)


async def _load_ontology_neighbors(
    session: Any,
    items: list[CaliberView],
    *,
    permitted: set[tuple[str, int]] | None,
    restricted: bool,
) -> dict[tuple[str, int, str, str], list[dict[str, Any]]]:
    """加载可见口径指标的一跳已确认本体邻居（过滤 proposed / rejected，并按 permitted 收窄受限用户可见范围）。"""
    if not items:
        return {}
    try:
        from app.ledger.ontology import ACTIVE_LINK_STATUSES, build_published_ontology_graph, metric_node_key

        graph = await build_published_ontology_graph(session, include_proposed=False, restricted=restricted)
    except Exception:  # noqa: BLE001
        return {}

    nodes_by_key = {str(n["key"]): n for n in graph.get("nodes") or [] if isinstance(n, Mapping) and n.get("key")}
    item_versions: dict[str, set[int]] = {}
    for item in items:
        item_versions.setdefault(metric_node_key(item.workflow_id, item.node_id, item.metric_id), set()).add(item.version)

    out: dict[tuple[str, int, str, str], list[dict[str, Any]]] = {}

    def _attach(focal_ref: Mapping[str, Any], peer_ref: Mapping[str, Any], edge: Mapping[str, Any]) -> None:
        focal_key = metric_node_key(
            str(focal_ref.get("workflow_id") or ""),
            str(focal_ref.get("card_id") or ""),
            str(focal_ref.get("metric_id") or ""),
        )
        versions = item_versions.get(focal_key)
        if not versions:
            return
        peer_key = metric_node_key(
            str(peer_ref.get("workflow_id") or ""),
            str(peer_ref.get("card_id") or ""),
            str(peer_ref.get("metric_id") or ""),
        )
        peer = nodes_by_key.get(peer_key)
        if peer is None:
            return
        peer_wf = str(peer["workflow_id"])
        peer_ver = int(peer["version"])
        if permitted is not None and (peer_wf, peer_ver) not in permitted:
            return
        entry: dict[str, Any] = {
            "kind": str(edge.get("kind") or ""),
            "role": edge.get("role"),
            "entity_key": edge.get("entity_key"),
            "workflow_id": peer_wf,
            "workflow_name": str(peer.get("workflow_name") or peer_wf),
            "version": peer_ver,
            "card_id": str(peer["card_id"]),
            "metric_id": str(peer["metric_id"]),
            "name": str(peer.get("name") or peer["metric_id"]),
        }
        if not restricted and peer.get("sources"):
            entry["sources"] = list(peer["sources"])
        for ver in versions:
            slot_key = (str(focal_ref["workflow_id"]), ver, str(focal_ref["card_id"]), str(focal_ref["metric_id"]))
            bucket = out.setdefault(slot_key, [])
            if not any(
                b["workflow_id"] == entry["workflow_id"]
                and b["card_id"] == entry["card_id"]
                and b["metric_id"] == entry["metric_id"]
                and b["kind"] == entry["kind"]
                for b in bucket
            ):
                bucket.append(entry)

    for edge in graph.get("edges") or []:
        if not isinstance(edge, Mapping) or edge.get("status") not in ACTIVE_LINK_STATUSES:
            continue
        src = edge.get("from")
        dst = edge.get("to")
        if not isinstance(src, Mapping) or not isinstance(dst, Mapping):
            continue
        _attach(src, dst, edge)
        if edge.get("kind") == "reconciles_with":
            _attach(dst, src, edge)
    return out


async def verify_reuse(session: Any, ref: dict[str, Any]) -> dict[str, Any]:
    """蓝图存的时候按库里那一版核对复用的口径卡：已发布且未退役、是源头的口径卡、有这个指标、能复用。交回补上 inputs
    的 reuse；不行抛 BpRefused（bp.data_unmapped）。用户在卡片上选的、助手提的都过这一道。"""
    from app.apps import versions as app_versions
    from app.blueprints.ops import BpRefused
    from app.db.models import Workflow

    workflow = await session.get(Workflow, str(ref.get("workflow_id") or ""))
    published = await app_versions.published_versions(session, workflow.id) if workflow is not None else []
    version = next((v for v in published if v.version == ref.get("version")), None)
    if workflow is None or version is None:
        raise BpRefused("复用的口径卡要出自已发布且未退役的工作流版本，这一版不是（可能已退役或删除）", code="bp.data_unmapped")
    hit = next((i for i in views_of(workflow.id, workflow.name, version)
                if (i.node_id, i.metric_id) == (ref.get("node_id"), ref.get("metric_id"))), None)
    if hit is None:
        inputs, reason = card_inputs(version.graph or {}, str(ref.get("node_id") or ""))
        raise BpRefused(f"「{workflow.name}」v{version.version} 里没有这个口径指标"
                        + (f"：{reason}" if reason and not inputs else ""), code="bp.data_unmapped")
    reuse = hit.reuse()
    if reuse is None:
        raise BpRefused(f"「{workflow.name}」v{version.version} 的「{hit.name}」不能复用：{hit.reason}。请按表重新对",
                        code="bp.data_unmapped")
    return reuse
