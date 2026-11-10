"""版本化的受控 SQL 模板绑定（K04）：业务定义里的「统计时间口径」「退款规则」怎么落到取数 SQL 上。

**首版只有一种绑定**（binder=controlled_template, version=1）：取数节点 data.config.binding 里显式声明
一份受控模板——模板里用 `[[time_role]]`、`[[refund_rule]]` 这样的业务 token 占位，`snippets` 说明每个 token 的
每个业务值展开成哪一段 SQL，`values` 说明这一版现在取的是哪个值。展开只做一次、在**作者侧**：K03 的字段变更服务
（app/ledger/changes.py）把定义和这段 SQL 在同一个事务里改掉，执行期绝不再按业务语义重推（A09）。SQL 里的
`{{ input.字段 }}` 仍归 K01，执行期照旧按入口渲染，和业务 token 是两回事。

**不做通用 AST 改写，也不建中央映射表。** 这里没有 SQL 解析器：判定「能不能确定性同步」只看两件事——节点上有一份
形状合法、版本认得的绑定，而且**当前 args.sql 正好就是这份模板按当前业务值展开出来的**（哈希一致 + 逐字相等）。
对不上的（SQL 被手工改过、模板里根本没有这个 token 的落点、映射里没有这个值、同一条 SQL 上的两个指标说法打架）
一律**不同步**，照实说 unsupported / needs_update，并把原因、影响对象和该做什么写在报告里。业务词汇表（哪些字段、
每个字段允许哪些值）是这一层的代码常量，不是一张表、也不按资产复制。

**aligned 只说「定义和实现同步」**（4.5）：不等于业务验证通过，也不改变 semantic_unverified、SQL 检查和质量结论。
旧资产（指标定义里压根没有这两个字段）不在这套判定里——状态是 unbound/not_declared，行为一个字不变。

**这一层给别人的接口**：`read` / `declared`（读数）、`shape_problem`（绑定本身的形状、版本与取值域）、`render`
（按业务值把模板确定性展开）、`analyze` / `plan` / `blocking`（定义 vs 实现的判定、可同步的新配置、拦运行的那句话）、
`capability`（读接口的能力描述）、`fingerprint` / `sql_hash` / `snapshot_material`（绑定指纹与查询快照材料）。
写侧只有 K03 的字段变更服务会改这些值（changes.py 的 _sync_bindings），别的入口一律只读。
"""
from __future__ import annotations

import copy
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any

#: 绑定写在取数节点 data.config 的这个键上
KEY = "binding"
#: 首版认的 binder 与版本
BINDER = "controlled_template"
BINDER_VERSION = 1
#: 业务 token 的写法：[[time_role]]
TOKEN = "[[{name}]]"

#: 绑定的几个状态（4.5）。aligned 只表示定义和实现同步
ALIGNED = "aligned"
NEEDS_UPDATE = "needs_update"
UNSUPPORTED = "unsupported"
#: 这张卡（这些取数节点）压根没声明业务字段：不判同步，旧资产就是这个状态
UNBOUND = "unbound"
#: 单条取数节点上的指标没声明业务字段：这个节点不参与判定
NOT_DECLARED = "not_declared"

#: 该怎么处置（给界面和报告读：拿它决定要不要显示「去画布改」这类按钮）
FIX_NONE = "none"          # 已经对齐，什么也不用做
FIX_SYNC = "sync"          # 定义变了，可以确定性同步 SQL
FIX_AUTHOR = "author"      # 要作者侧补/改受控绑定（没有绑定、没有落点、映射没有这个值）
FIX_MANUAL = "manual"      # 人先要拿主意（指标之间说法打架、SQL 被手工改过）

#: 查询快照里记绑定材料的键（engine/nodes/tools 放，tools/datasource 读）
SNAPSHOT_KEY = "caliber_binding"


@dataclass(frozen=True)
class Choice:
    """一个业务值：机器值 + 给人看的说法。"""

    value: str
    label: str


@dataclass(frozen=True)
class BusinessField:
    """一个业务字段：给人看的名字、允许的值、一句话说明。"""

    field: str
    label: str
    choices: tuple[Choice, ...]
    help: str = ""

    def values(self) -> list[str]:
        return [choice.value for choice in self.choices]

    def label_of(self, value: Any) -> str:
        return next((choice.label for choice in self.choices if choice.value == value), str(value or ""))


#: 首版认识的业务字段与允许值（词汇表在这一层，不建表、不按资产复制）
FIELDS: dict[str, BusinessField] = {
    "time_role": BusinessField(
        "time_role", "统计时间口径",
        (Choice("order_time", "按下单时间"), Choice("payment_time", "按支付时间")),
        "这条指标的统计口径按哪个时间点算：下单时间还是支付时间"),
    "refund_rule": BusinessField(
        "refund_rule", "退款处理",
        (Choice("include_all", "全部计入"), Choice("exclude_full_refund", "排除全额退款")),
        "退款订单算不算进来：全额退款排除、部分退款保留"),
}


def editable_fields() -> list[dict[str, Any]]:
    """服务端能力描述里的 editable_fields：能按字段改的业务定义、允许的值和说明。"""
    return [{"field": spec.field, "label": spec.label, "values": spec.values(),
             "value_labels": {choice.value: choice.label for choice in spec.choices}, "help": spec.help}
            for spec in FIELDS.values()]


def declared(metric: Any) -> dict[str, str]:
    """一条指标声明了哪些业务值：只认词汇表里的字段，值不是字符串或不在允许值里的照实带回（交给判定说原因）。"""
    if not isinstance(metric, Mapping):
        return {}
    out: dict[str, str] = {}
    for name in FIELDS:
        value = metric.get(name)
        if isinstance(value, str) and value.strip():
            out[name] = value.strip()
        elif value not in (None, ""):
            out[name] = str(value)
    return out


def _config_of(node: Any) -> dict[str, Any]:
    data = node.get("data") if isinstance(node, Mapping) else None
    config = data.get("config") if isinstance(data, Mapping) else None
    return config if isinstance(config, dict) else {}


def read(node: Any) -> dict[str, Any] | None:
    """取数节点上的绑定原文（形状不对时也照原样交回，由 shape_problem 说哪里不对）。"""
    binding = _config_of(node).get(KEY)
    return binding if isinstance(binding, dict) else None


def _sql_of(config: Mapping[str, Any]) -> str | None:
    args = config.get("args")
    sql = args.get("sql") if isinstance(args, Mapping) else None
    return sql if isinstance(sql, str) else None


#: 模板里认得出的业务 token（[[time_role]]）
_TOKEN_RE = re.compile(r"\[\[([A-Za-z_][A-Za-z0-9_]*)\]\]")
#: 模板里所有 [[…]] 的写法（认不出的也要照原文报出来）
_ANY_TOKEN_RE = re.compile(r"\[\[[^\]]*\]\]")


def _tokens_of(template: Any) -> list[str]:
    """受控模板里用到的业务字段，按出现顺序去重。裸写的和写在引号里的都算——看的是 token 本身。"""
    return list(dict.fromkeys(_TOKEN_RE.findall(str(template or ""))))


def _stray_tokens(template: Any) -> list[str]:
    """模板里 `[[…]]` 的写法但不属于任何已知字段的那些（照原文）。"""
    known = {TOKEN.format(name=name) for name in FIELDS}
    return sorted({token for token in _ANY_TOKEN_RE.findall(str(template or "")) if token not in known})


class UnresolvedToken(ValueError):
    """受控模板里的 token 这次展开不了（没有值、或没有展开写法）：宁可拒绝，也不把 [[…]] 留在 SQL 里。"""


def sql_hash(sql: str) -> str:
    from app.core.artifact_store import content_hash

    return content_hash(str(sql or ""))


def render(template: str, snippets: Mapping[str, Mapping[str, str]], values: Mapping[str, str]) -> str:
    """把受控模板里的 [[字段]] 按业务值展开成 SQL（纯字符串替换，确定性）。

    **展开不了的 token 直接抛 UnresolvedToken**（没有值、或者没有展开写法）：宁可拒绝一次变更，也不把
    `[[time_role]]` 这样的占位原样留在 SQL 里发出去或者写回 args.sql——那会让人以为业务规则已经落进 SQL 了。
    调用方（analyze / plan / snapshot_material）在展开之前都先过 shape_problem，走到这里还缺就是形状那边漏了。
    """
    out = str(template or "")
    for name in _tokens_of(out):
        if name not in FIELDS:
            raise UnresolvedToken(f"{TOKEN.format(name=name)} 不是认识的业务字段（只认 {'、'.join(FIELDS)}）")
        value = values.get(name)
        snippet = (snippets.get(name) or {}).get(value) if value is not None else None
        if not isinstance(snippet, str) or not snippet.strip():
            raise UnresolvedToken(
                f"{TOKEN.format(name=name)} 展开不了"
                f"（{'当前业务值里没有' if value is None else '允许映射里没有'}「{FIELDS[name].label}」的取值）")
        out = out.replace(TOKEN.format(name=name), snippet)
    return out


def shape_problem(binding: Any) -> str | None:
    """绑定本身的形状/版本/取值域哪里不对；没问题就是 None。

    这是「这条 SQL 是不是受控模板写出来的」的一部分：binder 不认得、版本比这一版新、缺模板，以及**模板、允许映射
    和当前业务值三边的 token 对不上**——模板里用了却没有值（展开不了，不能算 aligned，更不能把 [[…]] 写进 SQL）、
    允许映射里有却没用进模板（这条 SQL 表示不了那个业务值）、模板里的 token 没有展开写法。每一句都点名是哪个字段。
    """
    if not isinstance(binding, Mapping):
        return "这个取数节点没有受控绑定（data.config.binding）：定义改了，SQL 不会跟着改"
    binder = str(binding.get("binder") or "")
    if binder != BINDER:
        return f"认不出这个绑定器「{binder or '（空）'}」：首版只支持 {BINDER}"
    version = binding.get("version")
    if version != BINDER_VERSION:
        return f"绑定的版本是 {version!r}：这一版只认 {BINDER_VERSION}，不按更新的写法重解释旧绑定（A09）"
    for key, what in (("domain", "业务域"), ("mapping_version", "映射版本")):
        if not str(binding.get(key) or "").strip():
            return f"绑定缺{what}（{key}）：映射要带版本才认得出它是什么时候的写法"
    template = binding.get("template")
    if not isinstance(template, str) or not template.strip():
        return "绑定里没有受控模板（template）"
    snippets = binding.get("snippets")
    if not isinstance(snippets, Mapping) or not snippets:
        return "绑定里没有允许映射（snippets）"
    values = binding.get("values")
    if not isinstance(values, Mapping):
        return "绑定里没有当前业务值（values）"
    for name, by_value in snippets.items():
        spec = FIELDS.get(str(name))
        if spec is None:
            return f"允许映射里有不认识的字段「{name}」：首版只认 {'、'.join(FIELDS)}"
        if not isinstance(by_value, Mapping) or not by_value:
            return f"「{spec.label}」没有允许映射"
        for value, snippet in by_value.items():
            if str(value) not in spec.values():
                return f"「{spec.label}」的允许映射里有不认识的取值「{value}」"
            if not isinstance(snippet, str) or not snippet.strip():
                return f"「{spec.label}」的「{spec.label_of(value)}」没有展开写法"
    for name, value in values.items():
        spec = FIELDS.get(str(name))
        if spec is None:
            return f"当前业务值里有不认识的字段「{name}」"
        if str(value) not in (snippets.get(name) or {}):
            return f"「{spec.label}」现在是「{value}」，但允许映射里没有它"
    if stray := _stray_tokens(template):
        return f"受控模板里有认不出的 token：{'、'.join(stray)}（业务 token 只认 {'、'.join(FIELDS)}）"
    used = _tokens_of(template)
    mapped = [str(name) for name in snippets]
    valued = [str(name) for name in values]
    if dead := [name for name in mapped if name not in used]:
        return (f"允许映射里有「{FIELDS[dead[0]].label}」，但模板里没有 {TOKEN.format(name=dead[0])} 的落点："
                "这条 SQL 表示不了这个业务值（要么把它写进模板，要么别声明它）")
    if missing := [name for name in used if name not in snippets]:
        return (f"模板里的 {TOKEN.format(name=missing[0])} 没有展开写法："
                f"允许映射里缺「{FIELDS[missing[0]].label}」")
    if absent := [name for name in used if name not in valued]:
        return (f"受控模板用了 {TOKEN.format(name=absent[0])}，但当前业务值里没有它："
                f"先补上「{FIELDS[absent[0]].label}」的取值——不能把一个没展开的模板当 SQL 发出去")
    return None


def fingerprint(binding: Mapping[str, Any]) -> str:
    """绑定的内容指纹（确定性）：绑定器、版本、业务域与映射版本、当前值、模板与允许映射、当前 SQL 的哈希。

    改了业务值（同一份模板换一种展开）指纹就变；同一份绑定在哪张图上算都是同一个值。"""
    from app.core.artifact_store import canonical_json, content_hash

    return content_hash(canonical_json({
        "binder": str(binding.get("binder") or ""), "version": binding.get("version"),
        "domain": str(binding.get("domain") or ""), "mapping_version": binding.get("mapping_version"),
        "values": {str(k): str(v) for k, v in (binding.get("values") or {}).items()},
        "template": str(binding.get("template") or ""),
        "snippets": {str(k): {str(v): str(s) for v, s in (by_value or {}).items()}
                     for k, by_value in (binding.get("snippets") or {}).items()},
        "rendered_hash": str(binding.get("rendered_hash") or ""),
    }))


# ==========================================================================
# 判定：这张卡的定义和它读的取数节点上的绑定对不对得上
# ==========================================================================


@dataclass(frozen=True)
class NodeState:
    """一个取数节点上的判定结果。"""

    node_id: str
    status: str
    reason: str
    fix: str
    #: 这个节点上指标声明的业务值（每个字段唯一一个值；打架时的值不进这里，只在 reason 里说）
    declared: dict[str, str]
    #: 绑定里现在的值
    values: dict[str, str]
    metrics: tuple[str, ...]
    binding_fingerprint: str = ""

    def as_dict(self) -> dict[str, Any]:
        out = {"node_id": self.node_id, "status": self.status, "reason": self.reason, "fix": self.fix,
               "declared": dict(self.declared), "values": dict(self.values), "metrics": list(self.metrics)}
        if self.binding_fingerprint:
            out["binding_fingerprint"] = self.binding_fingerprint
        return out


@dataclass(frozen=True)
class Report:
    """一张卡的业务定义 vs 它读的取数节点上的绑定：状态、原因、影响对象、该做什么，以及能同步时要写什么。"""

    status: str
    reason: str
    fix: str
    metrics: tuple[str, ...]
    nodes: tuple[NodeState, ...]
    #: 可以确定性同步时：node_id → 新的 data.config（已经只改了 args.sql 与 binding 两处）
    configs: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: 每条指标声明的业务值：metric_id → {字段: 值}（界面按指标显示「这一条按支付时间算」用它）
    metric_values: dict[str, dict[str, str]] = field(default_factory=dict)

    @property
    def aligned(self) -> bool:
        return self.status == ALIGNED

    @property
    def blocked(self) -> bool:
        """定义和实现对不上（或对不上但同步不了）：这一类不许发起运行、不许把旧 SQL 的结果当新定义。"""
        return self.status in (NEEDS_UPDATE, UNSUPPORTED)

    @property
    def fingerprints(self) -> dict[str, str]:
        return {node.node_id: node.binding_fingerprint for node in self.nodes if node.binding_fingerprint}

    def as_dict(self, *, with_nodes: bool = True) -> dict[str, Any]:
        out: dict[str, Any] = {"status": self.status, "reason": self.reason, "fix": self.fix,
                               "metrics": list(self.metrics), "checked": True,
                               "metric_values": {k: dict(v) for k, v in self.metric_values.items()}}
        if self.status == UNSUPPORTED:
            out["unsupported_reason"] = self.reason
        if self.fingerprints:
            out["binding_fingerprints"] = self.fingerprints
        if with_nodes:
            out["nodes"] = [node.as_dict() for node in self.nodes]
        return out


def _locate(graph: Mapping[str, Any], metrics: Iterable[Mapping[str, Any]]) -> tuple[dict[str, list[str]], dict[str, str]]:
    """这些指标各自读到哪几个取数节点，以及**定位不到**的指标各自为什么。

    只认调用工具节点（type=tool）上的 `cell(nodes.X, 0, '列')`：绑定写在取数节点上。式子读的是 code / merge 这类
    别的节点、读的是 vars / rows 这种拿不到节点身份的写法、或者式子根本读不出来时，这条指标定位不到——调用方据此
    报 unsupported，**不能当成「没声明业务字段」放行**（那等于让上游那一版的定义落到旧的实现上）。
    """
    from app.apps.tree import node_map
    from app.engine.expressions import ExpressionError, cell_parts, leaf_refs, parse_expression
    from app.engine.labels import type_label
    from app.engine.metric_spec import metric_expressions

    nodes = node_map(graph)
    located: dict[str, list[str]] = {}
    problems: dict[str, str] = {}
    for metric in metrics:
        metric_id = str(metric.get("id") or "")
        if not metric_id:
            continue
        paths = metric_expressions(metric)
        if paths is None:
            problems[metric_id] = "它的算法读不出输入（指标类型认不出，或者式子不是字符串）"
            continue
        found: list[str] = []
        reason = ""
        for path in paths:
            try:
                tree = parse_expression(path)[0]
            except ExpressionError:
                reason = reason or "式子读不出来（表达式语法不对）"
                continue
            for leaf in leaf_refs(tree):
                parts = cell_parts(leaf)
                if parts is None or not parts[0].startswith("nodes."):
                    reason = reason or ("式子里有不是取数节点某一格的读取：本版只认「从取数节点取某一格」这一种写法"
                                        "（引用的要是 vars 或 rows 这类，也定位不到取数节点）")
                    continue
                node_id = parts[0][len("nodes."):]
                node = nodes.get(node_id)
                if node is None:
                    reason = reason or f"式子读到的节点「{node_id}」不在这张图上"
                elif node.get("type") != "tool":
                    reason = reason or (f"式子读的是节点「{node_id}」（{type_label(node.get('type'))}），不是取数节点")
                elif node_id not in found:
                    found.append(node_id)
        if found:
            located[metric_id] = found
        else:
            problems[metric_id] = reason or "它的算法里没有取数节点（外部常量）"
    return located, problems


def _readers(graph: Mapping[str, Any], metrics: Iterable[Mapping[str, Any]]) -> dict[str, list[str]]:
    """取数节点 id → 读它的指标 id（只看能定位的那种读取：cell(nodes.节点, 0, '列')）。

    别的模块（ledger/closure 的依赖闭包、K08）按这个形状读「哪些指标读哪个取数节点」，它就是 _locate 反过来的一份。
    定位不到的那些指标不在结果里：它们的业务声明由 _locate 单独交回原因，analyze 据此报 unsupported，不当成
    「没声明业务字段」放行。
    """
    located, _problems = _locate(graph, metrics)
    out: dict[str, list[str]] = {}
    for metric_id, node_ids in located.items():
        for node_id in node_ids:
            out.setdefault(node_id, []).append(metric_id)
    return out


def _node_of(graph: Mapping[str, Any], node_id: str) -> dict[str, Any] | None:
    return next((n for n in graph.get("nodes") or []
                 if isinstance(n, Mapping) and str(n.get("id")) == node_id), None)


def _conflicts(declared_by_metric: Mapping[str, Mapping[str, str]]) -> dict[str, list[str]]:
    """同一个取数节点上，几个指标对同一个业务字段的说法打架：字段 → 出现过的值（按出现顺序）。"""
    seen: dict[str, list[str]] = {}
    for values in declared_by_metric.values():
        for name, value in values.items():
            if value not in seen.setdefault(name, []):
                seen[name].append(value)
    return {name: values for name, values in seen.items() if len(values) > 1}


def _declared_here(declared_by_metric: Mapping[str, Mapping[str, str]]) -> dict[str, str]:
    """这个节点上声明的业务值（没有打架的字段），冲突的字段不进这里（由 _conflicts 说）。"""
    out: dict[str, str] = {}
    for values in declared_by_metric.values():
        for name, value in values.items():
            out.setdefault(name, value)
    return out


def _node_state(node_id: str, graph: Mapping[str, Any], declared_by_metric: Mapping[str, Mapping[str, str]]) -> NodeState:
    metrics = tuple(declared_by_metric)
    node = _node_of(graph, node_id) or {}
    binding = read(node)
    if not declared_by_metric:
        return NodeState(node_id, NOT_DECLARED,
                         "这个取数节点上的指标没有声明统计时间口径和退款规则：按旧资产处理，不判同步",
                         FIX_NONE, {}, {}, metrics, fingerprint(binding) if binding else "")
    if conflicts := _conflicts(declared_by_metric):
        detail = "；".join(f"「{FIELDS[name].label}」有的指标说{_value_text(name, values[0])}、"
                          f"有的说{_value_text(name, values[1])}" for name, values in conflicts.items())
        return NodeState(node_id, NEEDS_UPDATE,
                         f"同一条取数 SQL 上的指标说法不一致（{detail}）：一个节点只有一份 SQL，先统一再改",
                         FIX_MANUAL, {}, {}, metrics, fingerprint(binding) if binding else "")
    declared = _declared_here(declared_by_metric)
    if binding is None:
        return NodeState(node_id, NEEDS_UPDATE,
                         "这个取数节点没有受控绑定（data.config.binding）：定义改了，SQL 不会跟着改",
                         FIX_AUTHOR, declared, {}, metrics)
    if problem := shape_problem(binding):
        return NodeState(node_id, UNSUPPORTED, problem, FIX_AUTHOR, declared,
                         {str(k): str(v) for k, v in (binding.get("values") or {}).items()}, metrics,
                         fingerprint(binding))
    snippets = {str(k): dict(v) for k, v in (binding.get("snippets") or {}).items()}
    values = {str(k): str(v) for k, v in (binding.get("values") or {}).items()}
    config = _config_of(node)
    sql = _sql_of(config)
    if sql is None:
        return NodeState(node_id, UNSUPPORTED, "这个取数节点没有一条能读的取数 SQL（args.sql）", FIX_AUTHOR,
                         declared, values, metrics, fingerprint(binding))
    for name, value in declared.items():
        spec = FIELDS[name]
        if name not in snippets:
            return NodeState(node_id, UNSUPPORTED,
                             f"这条 SQL 里没有「{spec.label}」的落点（受控模板里没有 {TOKEN.format(name=name)}）："
                             "这条取数不是受控模板写出来的，改不了", FIX_AUTHOR, declared, values, metrics,
                             fingerprint(binding))
        if value not in snippets[name]:
            return NodeState(node_id, UNSUPPORTED,
                             f"「{spec.label}」的映射里没有{_value_text(name, value)}：这条 SQL 表示不了它",
                             FIX_AUTHOR, declared, values, metrics, fingerprint(binding))
    if _literal_copy(config):
        # 组合式问数（ledger/compose）拷进来的节点：args 已经是服务端按固定那一版的受控模板渲染好的字面语句，
        # 参数值渲染进去了（K01 的契约与它绑定，改过契约就作废、按普通节点判）。这时不能再拿 args.sql 和「模板
        # 展开」逐字比——那比的是值渲染进去之前的形态。业务语义在派生那一刻已经按源版本核过（derive.load_pinned），
        # 这里只比这一版的指标定义和副本上记的业务值对不对得上：对不上就拦住（那份数不是这个定义算出来的）
        if all(values.get(name) == value for name, value in declared.items()):
            return NodeState(node_id, ALIGNED,
                             "这份取数是服务端按固定那一版口径的受控模板渲染好的副本（参数只渲染一次）：定义和它一致"
                             "（aligned 只说这两边对得上，不等于业务验证通过）", FIX_NONE, declared, values,
                             metrics, fingerprint(binding))
        detail = "；".join(
            f"「{FIELDS[name].label}」定义说{_value_text(name, value)}、副本里是{_value_text(name, values.get(name))}"
            for name, value in declared.items() if values.get(name) != value)
        return NodeState(node_id, NEEDS_UPDATE,
                         f"这份取数是按固定那一版口径渲染好的副本，它的业务值和这一版的定义对不上（{detail}）："
                         "回到源口径卡把它同步好，再重新组合", FIX_AUTHOR, declared, values, metrics,
                         fingerprint(binding))
    try:
        rendered = render(str(binding.get("template") or ""), snippets, values)
    except UnresolvedToken as e:
        # 形状校验已经拦过展开不了的绑定；真走到这里（比如别的入口写进来的数据）也不能放行
        return NodeState(node_id, UNSUPPORTED, f"受控模板展开不了：{e}", FIX_AUTHOR, declared, values, metrics,
                         fingerprint(binding))
    if sql_hash(sql) != str(binding.get("rendered_hash") or "") or rendered != sql:
        return NodeState(node_id, NEEDS_UPDATE,
                         "当前 SQL 不是这份绑定展开出来的（被手工改过，或绑定记录已经过时）："
                         "要么把 SQL 改回模板展开的样子，要么重新声明这份绑定", FIX_MANUAL, declared, values,
                         metrics, fingerprint(binding))
    if all(values.get(name) == value for name, value in declared.items()):
        return NodeState(node_id, ALIGNED,
                         "定义和取数 SQL 同步（aligned 只说这两边对得上，不等于业务验证通过）", FIX_NONE,
                         declared, values, metrics, fingerprint(binding))
    return NodeState(node_id, NEEDS_UPDATE,
                     "定义改了，绑定里还是旧的：可以按受控模板确定性地把 SQL 改过来", FIX_SYNC,
                     declared, values, metrics, fingerprint(binding))


def _value_text(name: str, value: Any) -> str:
    spec = FIELDS.get(name)
    text = spec.label_of(value) if spec else str(value)
    return f"「{text}」"


def _literal_copy(config: Mapping[str, Any]) -> bool:
    """这个节点是不是「参数已经渲染成字面语句」的副本（ledger/params 的契约，K01）：组合式问数拷出来的那些。

    契约要核对过哈希才算（helper 自己判）：args 被改过之后契约作废，这里就是 False，按普通节点照旧拿 args.sql
    和模板展开比。
    """
    from app.ledger import params as sql_params

    return bool(sql_params.is_literal(config, "tool"))


def analyze(graph: Mapping[str, Any], card_id: str, metrics: Iterable[Mapping[str, Any]] | None = None) -> Report:
    """算这张卡的业务定义和取数节点上绑定的对得上的程度（读接口、写服务、派生拦阻、运行前检查都用这一份）。

    metrics 不给就读图上这张卡的指标（源头卡）；**转抄卡（caliber_from）必须把实际生效的指标传进来**——钉住那一版
    的定义，而且式子已经按 caliber_from.nodes 的源→本地映射改写到本图的取数节点 id 上（engine/nodes/metrics 的
    _pinned_caliber 就是这么改的）。不传的话转抄卡在本图里没有自己的 metrics，会被当成「没声明业务字段」而放行，
    上游定义和本地那句 SQL 的差别就没人管了。

    **unbound 只留给「确实一条业务声明都没有」的旧资产。** 有指标声明了时间口径/退款规则、但它的算法定位不到任何
    取数节点（读 code / merge 这类别的节点、式子写的是 vars / rows、式子读不出来……）时，报 unsupported：那种情况下
    没人能证明这条 SQL 实现了声明的业务值，放行就等于让定义落到旧的实现上。
    """
    from app.apps.tree import node_map

    definitions = list(metrics) if metrics is not None else [
        m for m in ((node_map(graph).get(str(card_id)) or {}).get("config") or {}).get("metrics") or []
        if isinstance(m, Mapping)]
    metric_values: dict[str, dict[str, str]] = {}
    for metric in definitions:
        if not isinstance(metric, Mapping) or not metric.get("id"):
            continue
        values = declared(metric)
        if values:
            metric_values[str(metric["id"])] = values
    if not metric_values:
        return Report(UNBOUND, "这张卡没有指标声明统计时间口径和退款规则：按旧资产处理，不判绑定同步",
                      FIX_NONE, (), ())
    declared_metrics = [m for m in definitions if isinstance(m, Mapping) and m.get("id") and declared(m)]
    located, unlocated = _locate(graph, declared_metrics)
    by_node: dict[str, dict[str, dict[str, str]]] = {}
    for metric_id, node_ids in located.items():
        for node_id in node_ids:
            by_node.setdefault(node_id, {})[metric_id] = metric_values[metric_id]

    states = tuple(_node_state(node_id, graph, declared_by_metric) for node_id, declared_by_metric in by_node.items())
    declared_ids = tuple(metric_values)
    if unlocated:
        # 有声明、定位不到：不能算「没声明」而放行。原因按指标逐条给出，影响对象是这些指标（metric_values 留着，
        # 界面才知道它声明了什么、在等什么）
        detail = "；".join(f"「{metric_id}」{reason}" for metric_id, reason in list(unlocated.items())[:3])
        more = f"（另有 {len(unlocated) - 3} 条同样的问题）" if len(unlocated) > 3 else ""
        reason = (f"有指标声明了业务口径，但定位不到它读的取数节点：{detail}{more}。"
                  "绑定按取数节点算，定位不到就没法证明这条 SQL 实现了声明的业务值："
                  "请把这条指标改成读取数节点某一格的写法，或者在取数节点上声明受控绑定")
        return Report(UNSUPPORTED, reason, FIX_AUTHOR, declared_ids, states, metric_values=metric_values)

    worst = UNSUPPORTED if any(s.status == UNSUPPORTED for s in states) else (
        NEEDS_UPDATE if any(s.status == NEEDS_UPDATE for s in states) else ALIGNED)
    first = next((s for s in states if s.status == worst), states[0])
    reason = first.reason if worst != ALIGNED else (
        "这一版的统计时间口径和退款规则和取数 SQL 是同步的（aligned 只说两边对得上，不等于业务验证通过）")
    fix = first.fix if worst != ALIGNED else FIX_NONE
    return Report(worst, reason, fix, declared_ids, states, metric_values=metric_values)


def plan(graph: Mapping[str, Any], card_id: str, metrics: Iterable[Mapping[str, Any]] | None = None) -> Report:
    """在 analyze 之上算出可以确定性同步的取数节点配置；有一条同步不了就一条也不改（同事务里要么全落要么不落）。

    新配置是在**原节点配置的深拷贝**上只改两处：args.sql（模板按新业务值展开）和 binding（values / rendered_hash /
    fingerprint）。没提到的键、别的节点、图上的扩展字段一个字不动。
    """
    report = analyze(graph, card_id, metrics)
    syncable = [s for s in report.nodes if s.fix == FIX_SYNC]
    if not syncable or any(s.fix not in (FIX_NONE, FIX_SYNC) for s in report.nodes if s.status != NOT_DECLARED):
        return report
    configs: dict[str, dict[str, Any]] = {}
    for state in syncable:
        node = _node_of(graph, state.node_id) or {}
        binding = read(node) or {}
        config = copy.deepcopy(_config_of(node))
        snippets = {str(k): dict(v) for k, v in (binding.get("snippets") or {}).items()}
        values = {**{str(k): str(v) for k, v in (binding.get("values") or {}).items()}, **state.declared}
        try:
            sql = render(str(binding.get("template") or ""), snippets, values)
        except UnresolvedToken as e:
            # 展开不了就一条也不写：宁可停在 needs_update，也不把 [[time_role]] 原样写回 args.sql
            return replace(report, status=UNSUPPORTED, fix=FIX_AUTHOR, configs={},
                           reason=f"受控模板展开不了，没有改动 SQL：{e}")
        updated = {**binding, "values": values, "rendered_hash": sql_hash(sql)}
        updated["fingerprint"] = fingerprint(updated)
        args = config.get("args")
        config["args"] = {**(dict(args) if isinstance(args, Mapping) else {}), "sql": sql}
        config[KEY] = updated
        configs[state.node_id] = config
    return Report(report.status, report.reason, report.fix, report.metrics, report.nodes, configs,
                  report.metric_values)


def blocking(report: Report) -> str | None:
    """定义和实现不一致时给调用方的那句话（发起运行之前拦）；一致或旧资产时是 None。"""
    return report.reason if report.blocked else None


def capability(graph: Mapping[str, Any], card_id: str,
               metrics: Iterable[Mapping[str, Any]] | None = None) -> dict[str, Any]:
    """读接口的服务端能力描述：能按字段改的业务定义、允许值、现在的值、同步状态、不支持的原因、影响对象和动作。

    **不给模板、不给 snippets、不给 SQL**：这几样是作者侧的实现细节，界面只需要知道业务值和状态。
    """
    report = analyze(graph, card_id, metrics)
    current: dict[str, str] = {}
    for node in report.nodes:
        for name, value in node.values.items():
            current.setdefault(name, value)
    return {"editable_fields": editable_fields(), "current": current, **report.as_dict()}


def snapshot_material(config: Mapping[str, Any], args: Any) -> dict[str, Any] | None:
    """查询快照里记的绑定材料：这条 SQL 是哪份绑定的哪一次展开。

    只在**节点配置里那条 SQL 正好是这份绑定展开出来的**时候给（哈希一致 + 逐字相等）：对不上就不写，不拿一份过时的
    绑定去解释这条实际执行过的语句。核的是节点配置里那条（{{ input.* }} 还在里面），不是这次渲染完的字面语句——所以
    组合式问数拷进来、参数已经渲染成字面值的节点不记这一项（宁可没有，也不记一份对不上的）。不给模板正文和 snippets
    （查询快照已经记了实际语句本身）。
    """
    binding = config.get(KEY) if isinstance(config, Mapping) else None
    if not isinstance(binding, Mapping) or shape_problem(binding):
        return None
    sql = args.get("sql") if isinstance(args, Mapping) else None
    if not isinstance(sql, str):
        return None
    snippets = {str(k): dict(v) for k, v in (binding.get("snippets") or {}).items()}
    values = {str(k): str(v) for k, v in (binding.get("values") or {}).items()}
    if sql_hash(sql) != str(binding.get("rendered_hash") or ""):
        return None
    if render(str(binding.get("template") or ""), snippets, values) != sql:
        return None
    return {"binder": str(binding.get("binder") or ""), "version": binding.get("version"),
            "domain": str(binding.get("domain") or ""), "mapping_version": binding.get("mapping_version"),
            "values": values, "rendered_hash": str(binding.get("rendered_hash") or ""),
            "fingerprint": fingerprint(binding)}


__all__ = ["ALIGNED", "BINDER", "BINDER_VERSION", "FIELDS", "FIX_AUTHOR", "FIX_MANUAL", "FIX_NONE", "FIX_SYNC",
           "KEY", "NEEDS_UPDATE", "NOT_DECLARED", "SNAPSHOT_KEY", "TOKEN", "UNBOUND", "UNSUPPORTED", "BusinessField",
           "Choice", "NodeState", "Report", "analyze", "blocking", "capability", "declared", "editable_fields",
           "fingerprint", "plan", "read", "render", "shape_problem", "snapshot_material", "sql_hash"]
