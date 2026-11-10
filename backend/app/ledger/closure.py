"""K08 的依赖闭包：一张**已经读好的**口径卡「这次要算哪些指标、要执行哪几条查询、要哪些入口参数」（S1 解析，S2 接线）。

**这一层是纯函数。** 进的是图（dict）+ 定位（workflow_id / version / card_id）+ 选择（可选 metric_ids，或显式
whole_card=True），出的是一个不可变的 DependencyClosure：不读库、不写库、不调模型、不 import 运行路径。S2 已经把它接进
``derive.load_pinned`` / ``derive.select_pinned`` / ``derive.caliber_block`` 与 ``compose.check_inputs`` /
``compose.instantiate``：给了选择就按这份闭包决定拷哪些取数节点、要哪些入口字段、口径卡上记哪几个指标；不给选择还是整卡
（闭包附在 Pinned 上供调用方读身份与依赖）。``engine/nodes/metrics`` 那边「只算选中的那几个指标」的接缝是
``effective_metrics``（按口径卡上记的选择筛定义、保持源卡相对顺序），见文末「S2 接线到了哪」。

**为什么要单独一份。** 现在「这张卡要读哪些取数节点」只有整卡一个粒度（look/calibers.card_inputs、
derive.load_pinned、bindings 的指标→节点映射都按整卡算）。K08 要的是同一份依赖模型同时供影响分析、验证范围和执行
计划用，而且要支持**选一部分指标**：选 A 时 B 独有且无依赖的查询和参数不进这次范围，共享的那条 SQL 保留、并显式声明
它还在算未选中的指标（不做投影——那要发布阶段拆模板或有验证过的投影能力）。

**身份**（§4.1、§4.6）。query_id 是**逻辑**查询身份：节点上声明了 ``data.config.query_id`` 就用它（别名，改名、
换图都不变），否则由 ``workflow_id + node_id`` 定。它**不含版本**——同一版图换个版本号，查询身份不变，版本另记在
``source`` 上。每个查询记录里同时留着真实出处 ``source = {workflow_id, version, node_id}``：别名不是源身份，换名的
副本（caliber_from.nodes 那种映射）认的是本图里真正执行的那个节点。**两条内容一样的 SQL 不合并**——不拿 SQL 内容哈希
当身份，两个节点就是两条查询（哪怕它们字节相同）。

**选择**。不给 metric_ids 也不给 whole_card 时，缺省保持整卡（现在各入口的行为）；显式给 metric_ids 就是按源卡顺序
归一的显式选择（调用方传的顺序、重复项都不影响结果）。指标不认识、取数式读不出来 / 引用不存在的节点 / 一个指标谁也
不依赖、查询没有可读 SQL、SQL 引用未声明的参数或认不出的写法、绑定对不上——一律进 ``unresolved``，``require()``
抛错。**绝不因为解析不了就静默退回整卡**；只有显式（或缺省）的整卡模式才产出整卡范围的闭包。

**绑定判定只看这次要执行的节点。** ``bindings.analyze`` 收的是**选中的指标**：未选中的指标独有节点上的问题根本不进
这次范围（反例「B 独有错误导致 A 失败」）；未选中的指标和 A 共享同一条 SQL 时，这条 SQL 仍按 A 的说法判、并如实由
``computes_unselected`` 声明它还在算谁（B 的说法不牵连 A 的结论；B 的数也不交回）。

**复用而不是另起一套**。指标的取数式读的是 ``engine.metric_spec.metric_expressions``；参数（槽位、未声明、认不出
的写法）读的是 ``ledger.params``；卡内容指纹读的是 ``data.caliber_identity.card_hash_of``；「哪个取数节点被哪个指标
读」和「定义 vs 绑定对不对得上」读的是 ``ledger.bindings``（``_readers`` / ``analyze``）。这里只把结果收成闭包，
不另写第三份指标读数算法。``derive.entry_fields`` 也直接复用。

**两个哈希分工**（区别是有意的，测试锁住）：

- ``selection_hash``：本次**选择**的身份——resolver_version、source（workflow/version/card）、模式、选中指标、查询
  身份、必需入口字段。**不含绑定指纹**：换参数值、定义与 SQL 一起同步到新版本之后，只要选的是同一批指标、同一批
  查询，这次选择的身份不变。
- ``closure_hash``：整份**闭包**的身份——在 selection 之上再算上整卡指纹、排除的查询、入口字段、每条查询的明细
  （含 SQL 哈希与**绑定指纹**）、跨指标契约、未决项、图 defaults 与卡的 on_missing。绑定一变，这个哈希就变。

resolver_version 两个都进（解析算法变了，两个身份都得变）。

**S2 接线到了哪、还剩什么**（照实说）：

- **已接**：`derive.load_pinned` / `derive.select_pinned` 按这份闭包裁取数节点、入口字段与绑定判定的范围；
  `derive.caliber_block` 把选择记进口径卡的 `caliber_from.metrics`（按源卡顺序）、改名映射只列保留的查询；
  `compose.check_inputs` / `instantiate` 按选择判参数与拷节点。未决依赖一律照实拒（`derive.SELECTION`），不静默
  退回整卡。闭包里的 `required_entry_fields`、`entry_fields`、`on_missing`、`defaults` 就是它们各自要的那几样。
- **查询的认法**：这一层认「被指标读到的调用工具节点」（`bindings._readers`）；`look/calibers.card_inputs` 那一支
  （给模型看的口径目录、蓝图复用）另要求工具名以 `QUERY_PREFIX` 开头、行号是 0，两侧各按自己的门槛收，没有合并成一
  条（改它的行为不在这一片）。
- **转抄卡（`caliber_from`）**：这里报 `card_chained`——本图里没有自己的定义。要按固定那一版的指标解析，得由调用方把
  钉住那一版、已按 `caliber_from.nodes` 改名映射改写过的指标传进来：派生路径本来只认源头卡，没有抽这层适配。
- **共享 SQL 不做投影（S3）**：`Query.computes_unselected` 只是声明「这条 SQL 还在算谁」。要真不执行未选中指标，得在
  发布阶段把模板拆开，或有验证过的投影能力；这一层明确不做投影，共享 SQL 原样保留。
- **运行时只算选中的几个指标**：`effective_metrics` / `selection_index` 是那一份统一的人读接口（按卡上记的选择筛定义、
  保持源卡相对顺序）；引擎那一侧（`engine/nodes/metrics`）按口径卡上的 `caliber_from.metrics` 读它，`nodes.<卡>.metrics[i]`
  的下标也按它算——K08 的另一半接的就是这一份。
- **S4（影响分析 / 验证范围）已接**：`Closure.summary()` 是这一份闭包的无 SQL 摘要（身份、这次算哪些指标、要执行哪几条
  查询与共享声明、必需入口字段、绑定指纹），look 的选用选项（`Option.selectable_metrics` / `unavailable_metrics` /
  `default_closure`）与口径描述（`ledger/descriptor` 的 `closure` / `selectable_metrics` / `computes_unselected`）都读它；
  按指标判能不能选的是 `look/calibers.metric_options`（闭包 + 同一份「输入能不能复用」的判定，不另写依赖解析）。
- **S5 / S6**：`queries`（`query_id`/`node_id`/`source`/`sql_hash`）是执行与证据要记的身份，`sql_hash` 只记事实、不当身份、
  不拿它合并节点；`contract_dependencies` 只列了能确定性读出来的两处（`ratio`、`report_contract`），执行侧怎么保住跨指标
  约定（比例契约的分子分母同源、出具契约点名的指标）还没定。
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from app.apps.tree import node_map
from app.ledger import bindings, params
from app.ledger.derive import entry_fields as graph_entry_fields

#: 解析算法版本：进 selection_hash 与 closure_hash（换解析规则就是换身份）
RESOLVER_VERSION = "closure/1"

#: 选择模式（§4.6）
WHOLE_CARD = "whole_card"
EXPLICIT = "explicit"

#: 未决项（unresolved）的机器码
CARD_CHAINED = "card_chained"                      # 这张卡本身是转抄别处的（caliber_from）：本图里没有定义可解析
CARD_EMPTY = "card_has_no_metrics"                 # 整卡模式，可这张卡一个指标也没有
CONFLICTING_SELECTION = "conflicting_selection"    # whole_card 与 metric_ids 同时给了
EMPTY_SELECTION = "empty_selection"                # 显式给了空选择（[]）：不静默当整卡
METRIC_UNKNOWN = "metric_unknown"                  # 点名的指标不在这张卡上
EXPRESSION_UNREADABLE = "expression_unreadable"    # 指标的取数式读不出来（kind 不认识、式子缺失）
NO_QUERY = "no_query_dependency"                   # 指标一个查库节点也不依赖
UNKNOWN_NODE = "unknown_node"                      # 式子引用了图里不存在的节点
QUERY_NO_SQL = "query_sql_missing"                 # 取数节点没有可读的 SQL
UNDECLARED_PARAMETER = "undeclared_parameter"      # SQL 引用了入口没声明的参数（params.UNDECLARED）
UNSUPPORTED_PARAMETER = "unsupported_parameter"    # SQL 里的参数认不出写法（params.UNSUPPORTED）
DUPLICATE_QUERY_ID = "duplicate_query_id"          # 两条查询用了同一个逻辑身份：谁是它说不清
BINDING_UNRESOLVED = "binding_unresolved"          # 定义和取数节点上的受控绑定对不上（K04）
DUPLICATE_METRIC_ID = "duplicate_metric_id"        # 同一张卡上两个指标用了同一个 id：谁是它说不清

CODES = (CARD_CHAINED, CARD_EMPTY, CONFLICTING_SELECTION, EMPTY_SELECTION, METRIC_UNKNOWN, EXPRESSION_UNREADABLE,
         NO_QUERY, UNKNOWN_NODE, QUERY_NO_SQL, UNDECLARED_PARAMETER, UNSUPPORTED_PARAMETER, DUPLICATE_QUERY_ID,
         DUPLICATE_METRIC_ID, BINDING_UNRESOLVED)


class ClosureRefused(ValueError):
    """闭合解析的**输入本身**不可用（图不是图、卡不在、卡不是口径卡、版本不是整数）：调用方先修正输入。

    数据层面的问题（指标不认识、式子读不出来、参数没声明、绑定对不上）不走这里，进 Closure.unresolved。"""


class UnresolvedClosure(Exception):
    """require() 时闭包里还有没解决的依赖：problems 是 Closure.unresolved 的原样一份。"""

    def __init__(self, problems: Sequence[Mapping[str, Any]]) -> None:
        self.problems = tuple(dict(p) for p in problems)
        super().__init__("；".join(str(p.get("message") or p.get("code")) for p in self.problems))


@dataclass(frozen=True)
class Query:
    """一条逻辑查询：这次要执行的一条取数。query_id 是逻辑身份，node_id / source 是它在本图里的真实出处。"""

    query_id: str
    node_id: str
    tool: str
    #: 真实出处：{workflow_id, version, node_id}（别名不当源身份，版本另记）
    source: dict[str, Any]
    #: 读这条查询的**选中**指标（按源卡顺序）
    metric_ids: tuple[str, ...]
    #: 没选中、但这条 SQL 照样在算的卡上指标（共享 SQL 保留时如实声明，不做投影）
    computes_unselected: tuple[str, ...]
    #: 这条 SQL 用到的入口字段名（按入口声明顺序）
    required_entry_fields: tuple[str, ...]
    #: 节点配置里那条 SQL 的内容哈希（记事实，不当身份、不拿它合并节点）
    sql_hash: str
    #: 这条查询上受控绑定的指纹（没有声明业务字段、或没有绑定时空串）
    binding_fingerprint: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"query_id": self.query_id, "node_id": self.node_id, "tool": self.tool, "source": dict(self.source),
                "metric_ids": list(self.metric_ids), "computes_unselected": list(self.computes_unselected),
                "required_entry_fields": list(self.required_entry_fields), "sql_hash": self.sql_hash,
                "binding_fingerprint": self.binding_fingerprint}


@dataclass(frozen=True)
class Closure:
    """一张口径卡（可选一部分指标）的依赖闭包。字段见模块说明与 §4.6 的最小集合。"""

    source: dict[str, Any]
    mode: str
    whole_card: bool
    #: 卡上所有指标 id（按源卡顺序）；原始整卡身份
    card_metric_ids: tuple[str, ...]
    #: 这次要算的指标 id（按源卡顺序归一）
    metric_ids: tuple[str, ...]
    #: 这次要执行的查询身份（按源卡顺序）
    query_ids: tuple[str, ...]
    #: 卡上有、这次不执行的查询身份（B 独有）
    excluded_query_ids: tuple[str, ...]
    queries: tuple[Query, ...]
    #: 这次真正要用到的入口字段（按入口声明顺序）
    required_entry_fields: tuple[dict[str, Any], ...]
    #: 图里入口声明的全部字段（照抄，保真交给下游）
    entry_fields: tuple[dict[str, Any], ...]
    #: query_id → 绑定指纹
    binding_fingerprints: dict[str, str]
    #: 这次选择必须一起保留的跨指标约定（见 _contract_dependencies）
    contract_dependencies: tuple[dict[str, Any], ...]
    #: 没解决的依赖：[{code, message, metric_id?, node_id?, query_id?}]；非空时 require() 抛错
    unresolved: tuple[dict[str, Any], ...]
    resolver_version: str
    source_card_hash: str
    selection_hash: str
    closure_hash: str
    #: 卡的「缺输入时」与图级 defaults：这次选择照样要带上（§4.6 保留全局必要约束）
    on_missing: Any = None
    defaults: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.unresolved

    def require(self) -> "Closure":
        """闭包可用时交回自己，有未决项时抛 UnresolvedClosure。调用方拿它当「能不能按这份闭包执行」的闸。"""
        if self.unresolved:
            raise UnresolvedClosure(self.unresolved)
        return self

    def metric_query_ids(self, metric_id: str) -> list[str]:
        """某个指标这次要执行的查询身份（按源卡顺序）。"""
        return [q.query_id for q in self.queries if str(metric_id) in q.metric_ids]

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": dict(self.source), "mode": self.mode, "whole_card": self.whole_card,
            "card_metric_ids": list(self.card_metric_ids), "metric_ids": list(self.metric_ids),
            "query_ids": list(self.query_ids), "excluded_query_ids": list(self.excluded_query_ids),
            "queries": [q.as_dict() for q in self.queries],
            "required_entry_fields": [dict(f) for f in self.required_entry_fields],
            "entry_fields": [dict(f) for f in self.entry_fields],
            "binding_fingerprints": dict(self.binding_fingerprints),
            "contract_dependencies": [dict(d) for d in self.contract_dependencies],
            "unresolved": [dict(p) for p in self.unresolved], "resolver_version": self.resolver_version,
            "source_card_hash": self.source_card_hash, "selection_hash": self.selection_hash,
            "closure_hash": self.closure_hash, "on_missing": self.on_missing, "defaults": dict(self.defaults),
        }


    def summary(self) -> dict[str, Any]:
        """这份闭包的摘要（K08 S4）：这次算哪些指标、要执行哪几条查询（query_id / 节点 / 工具 / 各自算谁 / 共享声明）、
        必需入口字段、绑定指纹、跨指标约定与两份身份哈希。**没有 SQL 正文、没有参数值**——目录页、look 的选用选项
        （ledger/compose.Option）与口径描述（ledger/descriptor）读这一份；SQL 只在服务端实例化那一刻照抄。"""
        return {
            "mode": self.mode, "whole_card": self.whole_card,
            "metric_ids": list(self.metric_ids), "card_metric_ids": list(self.card_metric_ids),
            "query_ids": list(self.query_ids), "excluded_query_ids": list(self.excluded_query_ids),
            "queries": [{"query_id": q.query_id, "node_id": q.node_id, "tool": q.tool,
                         "metric_ids": list(q.metric_ids), "computes_unselected": list(q.computes_unselected),
                         "required_entry_fields": list(q.required_entry_fields),
                         "sql_hash": q.sql_hash, "binding_fingerprint": q.binding_fingerprint}
                        for q in self.queries],
            "required_entry_fields": [str(f.get("name")) for f in self.required_entry_fields],
            "binding_fingerprints": dict(self.binding_fingerprints),
            "contract_dependencies": [dict(d) for d in self.contract_dependencies],
            "unresolved": [dict(p) for p in self.unresolved],
            "selection_hash": self.selection_hash, "closure_hash": self.closure_hash,
            "source_card_hash": self.source_card_hash, "resolver_version": self.resolver_version,
        }


# ==========================================================================
# 这次算哪些指标：在源卡里的位置（运行时统一的一份人读接口）
# ==========================================================================


@dataclass(frozen=True)
class SelectionIndex:
    """一次选择在源卡里的位置：要算哪些指标、各自在源卡第几个、选择里认不出的 id。

    ``definitions`` 与 ``positions`` 一一对应，都按**源卡相对顺序**（不是调用方给的顺序）。整卡模式（selection 是 None）
    里就是卡上的全部定义原样（顺序、条目一个不动），``whole_card`` 为真、``unknown`` 为空。
    """

    definitions: tuple[Any, ...]
    positions: tuple[int, ...]
    whole_card: bool
    unknown: tuple[str, ...]

    @property
    def metric_ids(self) -> tuple[str, ...]:
        return tuple(str(d.get("id")) for d in self.definitions if isinstance(d, Mapping) and d.get("id"))

    def as_dict(self) -> dict[str, Any]:
        return {"metric_ids": list(self.metric_ids), "positions": list(self.positions),
                "whole_card": self.whole_card, "unknown": list(self.unknown)}


def selection_index(definitions: Iterable[Any], selection: Sequence[str] | None = None) -> SelectionIndex:
    """一次选择在源卡里的位置：算哪些指标、各自的下标、选择里认不出的 id（取舍与顺序只由这一处算）。

    - ``selection`` 是 None（或没给）：整卡——原样交回全部定义（顺序与条目一个不动）；
    - 给了序列：只留 id 在里面的定义，**按源卡顺序**（调用方传的顺序、重复项都不影响结果）；选择里这张卡上没有的 id 进
      ``unknown``（照实交回，既不静默当整卡，也不静默丢掉）；
    - 同一个 id 在卡上出现多次只取第一次（口径卡的 id 该唯一；这一层不替它去重之后算两遍）。

    运行时按口径卡上记的选择（``caliber_from.metrics``）读这一份：卡片顺序、认不出的 id 都按同一处判定。"""
    defs = list(definitions or [])
    if selection is None:
        return SelectionIndex(tuple(defs), tuple(range(len(defs))), True, ())
    wanted = list(dict.fromkeys(str(m).strip() for m in selection if m is not None and str(m).strip()))
    chosen = set(wanted)
    kept: list[tuple[int, Any]] = []
    seen: set[str] = set()
    for position, definition in enumerate(defs):
        metric_id = str(definition.get("id") or "").strip() if isinstance(definition, Mapping) else ""
        if metric_id and metric_id in chosen and metric_id not in seen:
            seen.add(metric_id)
            kept.append((position, definition))
    return SelectionIndex(tuple(d for _, d in kept), tuple(p for p, _ in kept), False,
                          tuple(m for m in wanted if m not in seen))


def effective_metrics(definitions: Iterable[Any], selection: Sequence[str] | None = None) -> list[Any]:
    """这次要算的指标定义（运行时统一的一份）：selection 给了就按它筛，**保持源卡相对顺序**；没给就是整卡（原样）。

    实现在 ``selection_index`` 之上：位置与取舍只由那一处算。引擎按口径卡上记的选择读它，把「这张卡这次算哪几个指标」
    统一到一处，别处不再各写一遍筛选与排序。"""
    return list(selection_index(definitions, selection).definitions)


# ==========================================================================
# 身份与哈希
# ==========================================================================


def query_id_of(workflow_id: str, node_id: str, config: Mapping[str, Any] | None = None) -> str:
    """一条查询的逻辑身份：节点上声明了 ``config.query_id`` 就用它（别名），否则由 ``workflow_id:node_id`` 定。

    **不含版本**：同一版图换个版本号，查询身份不变（版本记在 source 上，§4.6）。别名不是源身份——调用方要记出处时
    仍看节点 id（Query.source）。"""
    explicit = (config or {}).get("query_id")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    return f"{workflow_id}:{node_id}"


def _hash(payload: Any) -> str:
    from app.core.artifact_store import canonical_json, content_hash

    return content_hash(canonical_json(payload))


def _problem(code: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"code": code, "message": message, **{k: v for k, v in extra.items() if v not in (None, "")}}


def _config(node: Any) -> dict[str, Any]:
    config = (node.get("data") or {}).get("config") if isinstance(node, Mapping) else None
    return config if isinstance(config, dict) else {}


def _sql_of(config: Mapping[str, Any]) -> str | None:
    args = config.get("args")
    sql = args.get("sql") if isinstance(args, Mapping) else None
    return sql if isinstance(sql, str) else None


def _names(fields: Sequence[Mapping[str, Any]]) -> list[str]:
    return [str(f["name"]) for f in fields if f.get("name")]


# ==========================================================================
# 解析
# ==========================================================================


def _metric_problems(nodes: Mapping[str, Mapping[str, Any]], metric: Mapping[str, Any]) -> tuple[list[str] | None, list[dict[str, Any]]]:
    """一个指标的取数式读不读得出来、引用了图里不存在的节点没有。交回 (取数式, 未决项)。

    只做**诊断**（式子读不出来、引用了谁）：真正的「指标→取数节点」映射一律走 bindings._readers（同一份读数算法），
    不在这里另写一遍。"""
    from app.engine.expressions import ExpressionError, cell_parts, leaf_refs, parse_expression
    from app.engine.metric_spec import metric_expressions

    metric_id = str(metric.get("id"))
    paths = metric_expressions(metric)
    if paths is None:
        return None, [_problem(EXPRESSION_UNREADABLE,
                               f"指标「{metric_id}」的取数式读不出来（kind 不认识，或式子在当前类型下是空的）："
                               "解析不了依赖，不能按它执行", metric_id=metric_id)]
    problems: list[dict[str, Any]] = []
    ghosts: list[str] = []
    for path in paths:
        try:
            tree, _, _ = parse_expression(path)
        except ExpressionError as e:
            problems.append(_problem(EXPRESSION_UNREADABLE, f"指标「{metric_id}」的取数式解析不了：{e}",
                                     metric_id=metric_id))
            continue
        for leaf in leaf_refs(tree):
            parts = cell_parts(leaf)
            if parts is None or not parts[0].startswith("nodes."):
                continue
            node_id = parts[0][len("nodes."):]
            if node_id not in nodes and node_id not in ghosts:
                ghosts.append(node_id)
    for node_id in ghosts:
        problems.append(_problem(UNKNOWN_NODE, f"指标「{metric_id}」的取数式引用了图里不存在的节点「{node_id}」："
                                              "先把它接上，别拿别的节点的数顶替", metric_id=metric_id, node_id=node_id))
    return list(paths), problems


def _contract_dependencies(graph: Mapping[str, Any], card_metric_ids: Sequence[str], selected: Sequence[str],
                           selected_metrics: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """这次选择必须一起保留的**跨指标约定**（照实列，不在 S1 里改任何东西）：

    - ``ratio``：选中指标声明的比例契约（分子、分母要来自同一条查询的那一行）——按列名记下来，交给 S2 决定怎么保；
    - ``report_contract``：图里某个节点的 ``config.contract`` 点名了这张卡的指标（``metrics_from`` 那套约定的
      ``required``）：选 A 时它可能还点着未选中的 B，如实记下「选了哪些、还剩哪些没选」。

    只认能确定性读出来的两处；别的跨指标关系（模型写的自然语言、没声明的约定）不在这里猜。"""
    selected_set = set(selected)
    card_set = set(card_metric_ids)
    out: list[dict[str, Any]] = []
    for metric in selected_metrics:
        contract = metric.get("contract")
        ratio = contract.get("ratio") if isinstance(contract, Mapping) else None
        if isinstance(ratio, Mapping) and str(ratio.get("numerator") or "").strip() \
                and str(ratio.get("denominator") or "").strip():
            out.append({"kind": "ratio", "metric_id": str(metric.get("id")),
                        "numerator": str(ratio["numerator"]).strip(),
                        "denominator": str(ratio["denominator"]).strip()})
    for node_id, node in node_map(graph).items():
        contract = (node.get("config") or {}).get("contract")
        required = contract.get("required") if isinstance(contract, Mapping) else None
        if not isinstance(required, (list, tuple)):
            continue
        named = [str(m) for m in required if str(m) in card_set]
        if not named:
            continue
        out.append({"kind": "report_contract", "node_id": node_id, "metric_ids": named,
                    "unselected": [m for m in named if m not in selected_set]})
    return out


def duplicate_metric_ids(metrics: Sequence[Mapping[str, Any]]) -> list[str]:
    """这张卡上重复出现的非空指标 id（按第一次出现的先后；重复几次只报一次）。

    同一张卡上每个指标 id 只能出现一次：重复就是谁是谁说不清——闭包按定义解析、运行时按指标集筛、下游按位置取
    ``nodes.<卡>.metrics[i]`` 会各算一份。判定只有这一处（closure.build 与 derive.load_pinned 都读它）。"""
    seen: set[str] = set()
    out: list[str] = []
    for metric in metrics:
        metric_id = str(metric.get("id") or "") if isinstance(metric, Mapping) else ""
        if not metric_id:
            continue
        if metric_id in seen and metric_id not in out:
            out.append(metric_id)
        seen.add(metric_id)
    return out


def build(graph: Mapping[str, Any], workflow_id: str, version: int, card_id: str, *,
          metric_ids: Sequence[str] | None = None, whole_card: bool = False) -> Closure:
    """解析一张已读好的口径卡的依赖闭包（纯函数，无 IO / LLM，不改任何模块的行为）。

    - ``metric_ids`` 不给（None）且 ``whole_card=False``：缺省保持整卡（和现在各入口一样）；
    - 给了 ``metric_ids``：显式选择，按**源卡顺序**归一（调用方传的顺序与重复项不影响结果）；
    - ``metric_ids=[]``：显式空选择 → unresolved（不静默当整卡）；
    - ``whole_card=True`` 与 ``metric_ids`` 同时给：unresolved（不按任何一边静默放宽）；
    - 同一张卡上两个指标用同一个非空 id：unresolved（``duplicate_metric_id``），哪一种模式都报——重复的 id 说不清算的是
      哪一份，闭包按定义解析、运行时按指标集筛、下游按位置取 ``nodes.<卡>.metrics[i]`` 会各算一份（也不静默挑第一或最后）。

    输入本身不可用（图不是图、卡不在、卡不是口径卡、版本不是整数）抛 ClosureRefused；数据层面的问题进 unresolved。
    """
    if not isinstance(graph, Mapping):
        raise ClosureRefused("依赖闭包要的是一张图（dict）")
    if metric_ids is not None and (isinstance(metric_ids, (str, bytes)) or not isinstance(metric_ids, Sequence)):
        raise ClosureRefused("metric_ids 要给一串指标 id（或 None 表示不指定选择）")
    try:
        version_no = int(version)
    except (TypeError, ValueError):
        raise ClosureRefused(f"口径版本要是整数：{version!r}") from None
    workflow_id, card_id = str(workflow_id or ""), str(card_id or "")

    nodes = node_map(graph)
    card_node = nodes.get(card_id)
    if card_node is None:
        raise ClosureRefused(f"图里没有节点「{card_id}」：定位不到要解析的口径卡")
    if card_node.get("type") != "metrics":
        raise ClosureRefused(f"「{card_id}」不是口径卡（metrics 节点）：不能按口径算依赖")

    config = card_node.get("config") or {}
    source = {"workflow_id": workflow_id, "version": version_no, "card_id": card_id}
    unresolved: list[dict[str, Any]] = []

    entry_fields = tuple(graph_entry_fields(graph))
    declared_fields = params.declared(entry_fields)
    field_names = _names(entry_fields)

    chained = config.get("caliber_from") not in (None, "", {})
    if chained:
        unresolved.append(_problem(CARD_CHAINED,
                                   f"「{card_id}」是转抄别处口径的卡：本图里没有自己的定义，"
                                   "要按固定那一版的指标（式子已按改名映射改写）再解析",
                                   node_id=card_id))
    metrics = [] if chained else [m for m in config.get("metrics") or []
                                  if isinstance(m, Mapping) and m.get("id")]
    card_metric_ids = list(dict.fromkeys(str(m["id"]) for m in metrics))
    if duplicates := duplicate_metric_ids(metrics):
        # 同一张卡上 id 重复：卡的指标身份本身坏了（闭包按定义解析、运行时按指标集筛、下游按位置取 metrics[i] 各算一份）。
        # 不是「某个指标的问题」，所以哪一种模式都照实报，也不静默取第一份或最后一份
        unresolved.append(_problem(DUPLICATE_METRIC_ID,
                                   f"「{card_id}」上有指标 id 重复：{'、'.join(duplicates)}——同一张卡上每个指标 id "
                                   "只能有一个，谁是它说不清，先改成不重的 id", metric_id=duplicates[0]))

    # ---- 选择：归一（按源卡顺序）------------------------------------------------
    if whole_card and metric_ids is not None:
        # 两边都给了：照实报冲突，并按更窄的一边（点名的指标）走——绝不悄悄放大到整卡
        unresolved.append(_problem(CONFLICTING_SELECTION,
                                   "whole_card 和 metric_ids 同时给了：要么点指标、要么整卡，不按哪一边静默放宽"))
        mode = EXPLICIT
    elif whole_card or metric_ids is None:
        mode = WHOLE_CARD
    else:
        mode = EXPLICIT
    if mode == WHOLE_CARD:
        selected = list(card_metric_ids)
    else:
        wanted = list(dict.fromkeys(str(m) for m in metric_ids or []))
        if not wanted:
            unresolved.append(_problem(EMPTY_SELECTION, "metric_ids 给的是空的：这次一个指标也不选（不静默当整卡）"))
            selected = []
        else:
            for metric_id in [m for m in wanted if m not in card_metric_ids]:
                unresolved.append(_problem(METRIC_UNKNOWN,
                                           f"「{card_id}」上没有指标「{metric_id}」：有 "
                                           f"{'、'.join(card_metric_ids) or '（一个也没有）'}", metric_id=metric_id))
            chosen = set(wanted) & set(card_metric_ids)
            selected = [mid for mid in card_metric_ids if mid in chosen]
    selected_set = set(selected)
    if mode == WHOLE_CARD and not card_metric_ids and not chained:
        unresolved.append(_problem(CARD_EMPTY, f"「{card_id}」一个指标也没有：整卡模式下没有可执行的范围",
                                   node_id=card_id))
    by_id: dict[str, dict[str, Any]] = {}
    for metric in metrics:
        by_id.setdefault(str(metric["id"]), metric)     # id 重复时取第一份：和运行时按指标集筛的认法一致（重复本身已未决）
    selected_metrics = [by_id[mid] for mid in selected if mid in by_id]

    # ---- 指标 → 取数节点：走 bindings 的读数算法（不另写一份）--------------------
    all_readers = bindings._readers(graph, metrics)
    sel_readers = bindings._readers(graph, selected_metrics)
    nodes_by_metric: dict[str, list[str]] = {}
    for node_id, mids in all_readers.items():
        for metric_id in mids:
            nodes_by_metric.setdefault(metric_id, []).append(node_id)
    card_nodes_order = [node_id for node_id in nodes if node_id in all_readers]
    retained_order = [node_id for node_id in nodes if node_id in sel_readers]
    excluded_query_nodes = [node_id for node_id in card_nodes_order if node_id not in sel_readers]

    # ---- 选中指标自己的诊断（式子读不出来、引用不存在的节点、谁也不依赖）--------
    for metric in selected_metrics:
        metric_id = str(metric["id"])
        paths, problems = _metric_problems(nodes, metric)
        unresolved += problems
        if paths and not problems and not nodes_by_metric.get(metric_id):
            unresolved.append(_problem(NO_QUERY, f"指标「{metric_id}」的取数式没有引用任何查库的取数节点："
                                                "依赖解析不了，不能按它执行", metric_id=metric_id))

    # ---- 定义 vs 绑定：只看这次真正要执行的那些节点（未选中指标的绑定问题不牵连 A）--
    report = bindings.analyze(graph, card_id, selected_metrics)
    for state in report.nodes:
        if state.status in (bindings.NEEDS_UPDATE, bindings.UNSUPPORTED):
            unresolved.append(_problem(BINDING_UNRESOLVED, f"取数节点「{state.node_id}」：{state.reason}",
                                       node_id=state.node_id))
    fingerprints = report.fingerprints

    # ---- 每条查询：身份、参数、绑定指纹、共享 SQL 还在算谁 ----------------------
    queries: list[Query] = []
    seen_query_ids: dict[str, str] = {}
    used_anywhere: set[str] = set()
    for node_id in retained_order:
        cfg = nodes[node_id].get("config") or {}
        query_id = query_id_of(workflow_id, node_id, cfg)
        if query_id in seen_query_ids:
            unresolved.append(_problem(DUPLICATE_QUERY_ID,
                                       f"取数节点「{node_id}」和「{seen_query_ids[query_id]}」用了同一个逻辑身份"
                                       f"「{query_id}」：谁是这条查询说不清，先改 name/query_id",
                                       query_id=query_id, node_id=node_id))
        else:
            seen_query_ids[query_id] = node_id
        sql = _sql_of(cfg)
        used: list[str] = []
        if sql is None or not sql.strip():
            unresolved.append(_problem(QUERY_NO_SQL, f"取数节点「{node_id}」没有可读的 SQL（args.sql）："
                                                     "这条查询执行不了", node_id=node_id, query_id=query_id))
        else:
            for problem in params.check_template(sql, fields=declared_fields, values=None, node_id=node_id,
                                                 where=f"口径「{card_id}」的入口"):
                unresolved.append(_problem(problem.code, problem.message, node_id=node_id, query_id=query_id))
            try:
                used = list(dict.fromkeys(slot.field for slot in params.slots_of(sql)))
            except params.ParameterRefused:
                used = []
        required = [name for name in field_names if name in set(used) and name in declared_fields]
        used_anywhere.update(required)
        queries.append(Query(
            query_id=query_id, node_id=node_id, tool=str(cfg.get("tool") or ""),
            source={"workflow_id": workflow_id, "version": version_no, "node_id": node_id},
            metric_ids=tuple(mid for mid in card_metric_ids
                             if mid in selected_set and node_id in nodes_by_metric.get(mid, [])),
            computes_unselected=tuple(mid for mid in card_metric_ids
                                      if mid not in selected_set and node_id in nodes_by_metric.get(mid, [])),
            required_entry_fields=tuple(required),
            sql_hash=bindings.sql_hash(sql) if sql else "",
            binding_fingerprint=str(fingerprints.get(node_id) or "")))

    required_entry_fields = tuple(f for f in entry_fields if str(f.get("name")) in used_anywhere)
    binding_fingerprints = {q.query_id: q.binding_fingerprint for q in queries if q.binding_fingerprint}
    query_ids = [q.query_id for q in queries]
    excluded_query_ids = [query_id_of(workflow_id, node_id, nodes[node_id].get("config") or {})
                          for node_id in excluded_query_nodes]

    contract_dependencies = _contract_dependencies(graph, card_metric_ids, selected, selected_metrics)

    source_card_hash = _card_hash(card_id, config, metrics)
    selection_hash = _hash({
        "resolver_version": RESOLVER_VERSION, "source": source, "mode": mode,
        "metric_ids": selected, "query_ids": query_ids,
        "required_entry_fields": [str(f.get("name")) for f in required_entry_fields],
    })
    closure_hash = _hash({
        "resolver_version": RESOLVER_VERSION, "source": source, "mode": mode,
        "card_metric_ids": card_metric_ids, "metric_ids": selected, "query_ids": query_ids,
        "excluded_query_ids": excluded_query_ids,
        "queries": [q.as_dict() for q in queries],
        "required_entry_fields": [str(f.get("name")) for f in required_entry_fields],
        "entry_fields": [dict(f) for f in entry_fields],
        "binding_fingerprints": binding_fingerprints,
        "contract_dependencies": contract_dependencies,
        "unresolved": unresolved,
        "on_missing": config.get("on_missing"), "defaults": dict(graph.get("defaults") or {}),
        "source_card_hash": source_card_hash,
    })
    return Closure(
        source=source, mode=mode, whole_card=mode == WHOLE_CARD,
        card_metric_ids=tuple(card_metric_ids), metric_ids=tuple(selected), query_ids=tuple(query_ids),
        excluded_query_ids=tuple(excluded_query_ids), queries=tuple(queries),
        required_entry_fields=required_entry_fields, entry_fields=entry_fields,
        binding_fingerprints=binding_fingerprints, contract_dependencies=tuple(contract_dependencies),
        unresolved=tuple(unresolved), resolver_version=RESOLVER_VERSION, source_card_hash=source_card_hash,
        selection_hash=selection_hash, closure_hash=closure_hash,
        on_missing=config.get("on_missing"), defaults=dict(graph.get("defaults") or {}))


def _card_hash(card_id: str, config: Mapping[str, Any], metrics: Sequence[Mapping[str, Any]]) -> str:
    """整卡内容指纹：复用 caliber_identity.card_hash_of（只按指标语义字段 + 口径名/版本算，顺序不影响）。"""
    from app.data.caliber_identity import card_hash_of

    return card_hash_of({"node_id": card_id, "metrics": list(metrics),
                         "caliber": config.get("caliber"), "caliber_version": config.get("caliber_version")})


__all__ = ["BINDING_UNRESOLVED", "CARD_CHAINED", "CARD_EMPTY", "CODES", "CONFLICTING_SELECTION", "Closure",
           "ClosureRefused", "DUPLICATE_METRIC_ID", "DUPLICATE_QUERY_ID", "EMPTY_SELECTION", "EXPLICIT",
           "EXPRESSION_UNREADABLE", "METRIC_UNKNOWN", "NO_QUERY", "QUERY_NO_SQL", "Query", "RESOLVER_VERSION",
           "SelectionIndex", "UNDECLARED_PARAMETER", "UNKNOWN_NODE", "UNSUPPORTED_PARAMETER", "UnresolvedClosure",
           "WHOLE_CARD", "build", "duplicate_metric_ids", "effective_metrics", "query_id_of", "selection_index"]
