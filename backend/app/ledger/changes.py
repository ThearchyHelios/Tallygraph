"""受限的字段变更服务（K03）：一批字段操作打在**原图的深拷贝**上，交回新图和这次到底改了什么。

一条操作只说三件事：op（set / remove）、metric_id（口径卡上的稳定身份）、path（白名单里的字段路径）。指标身份、
证据归属、治理归属、任意路径和按数组位置定位都不在能改的范围内（4.2）。改在深拷贝上做：没提到的字段、别的指标、
别人加的扩展字段和其他节点原样带着走——不重建一份定义，也不做通用 JSON Patch。

白名单（FIELDS）覆盖当前指标上真有的业务字段和 definition 里给人看的那几项。**明确不能改**的写在 FORBIDDEN 里，
落进这里会得到一句具体的话，而不是笼统的「不认识这个字段」：id/kind/rule/assessment/external/params/contract（身份、
指标类型和取值契约）、definition.citations（证据归属）、definition.owner / definition.effective_from（治理归属）。

四种写法的语义（4.2 要求各自不同且可测）：

| 写法 | 语义 |
| --- | --- |
| 省略 | 这条操作没提这个字段（整批都没提）就是**不动**它；反过来，set 省略 value 是错的（不知道要设成什么） |
| set + 值 | 把这个字段设成这个值 |
| set + ''（数组是 []） | 字段在、值是明确的空值——「声明成空」，和 remove 的「没声明」不是一回事 |
| set + null | 拒（operation_value）：「清空」有 remove、「声明为空」有 ''，null 只能是这两者之一，不替客户端猜 |
| remove | 把字段从指标里删掉，回到「没声明」。不接受 value |

`definition` 整个对象不在（没这个键，或者就是 null）时：`set /definition/text|scope|exclusions` 会把它建出来，
`remove` 是同一条的「本来就没有」——无变化，不是路径非法。键在、但不是对象（被人写成了字符串或数组）照旧拒：
数据坏了就说清楚，不替人猜，也不把原来的值丢掉。

数组字段（同义词、维度、过滤条件、不包含）按**整份设置**处理，没有按元素的写法，也没有按位置定位的路径。
一批操作要么全部成功（落成一个新版本），要么一条都不落：先在原图的深拷贝上把整批跑完，中途任何一条不合规、
或者指标指不到，整批都不写（4.2）。整批跑完发现内容其实没变（比如先设后删），也不落版本。

变更分类（classification）按字段本身的类别算，见 FIELDS 的 kind：只改了展示类字段时报 display，动了定义类字段
报 definition。**分类不代表可以跳过验证**：图是唯一真相，口径卡的内容一变，图的执行指纹就变，旧试跑与旧验证
一律保守失效（4.8）——分类只是给页面说清「这次动的是哪一类」，回执里另给定义指纹的前后值（definition_hash）
作准。

**业务字段怎么落到 SQL 上（K04）。** `time_role`（统计时间口径）和 `refund_rule`（退款处理）是受控的业务字段：
它们同时在**定义**（指标里声明）和**实现**（取数节点上的受控绑定，ledger/bindings）里。一次变更里两边一起改：
能按受控模板确定性展开的，就在同一个事务里把取数节点的 args.sql 和 binding 一起改掉（synced）；无绑定、映射不
支持、SQL 被手工改过、同一条 SQL 上的指标说法打架的，**定义照常保存、SQL 一个字不动**，回执里照实报
needs_update / unsupported + 原因 + 影响对象 + 该做什么。执行期绝不按业务语义重推（A09）。
"""
from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from app.ledger import bindings
from app.ledger.repository import GraphBackedCaliberRepository, MetricGone

#: 一次请求最多几条操作：再多不是「改一处」该有的样子（要整份重写就走整份换指标那条路）
MAX_OPERATIONS = 50

#: 拒绝的机器码（接口一律回 422）
OPERATION_INVALID = "operation_invalid"      # op 不认识、一批是空的、太多条
OPERATION_PATH = "operation_path"            # 路径不在白名单、按数组位置定位、明确不能改的那几项
OPERATION_VALUE = "operation_value"          # 值不对：类型不符、null、set 没有 value、remove 带了 value
OPERATION_METRIC = "operation_metric"        # 卡上没有这个 metric_id

class ChangeRefused(Exception):
    """这次变更不合规（接口回 422）：code 是机器码，operation 是第几条（从 0 数），path 是那一条的字段路径。"""

    def __init__(self, message: str, *, code: str = OPERATION_INVALID, operation: int | None = None,
                 path: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.operation = operation
        self.path = path


@dataclass(frozen=True)
class Field:
    """白名单里的一个字段：kind 决定它算展示还是定义（见模块说明），value_type 是值类型，nonempty 是「写了就不能空」。"""

    kind: str
    value_type: str
    label: str
    nonempty: bool = False
    #: 值类型是 choice 时的允许值（业务字段的取值域，见 ledger/bindings.FIELDS）
    choices: tuple[str, ...] = ()


#: 能按字段改的路径。定义类的判定和 data/caliber_identity.SEMANTIC_FIELDS 对齐：语义字段算定义，其余（同义词）算展示。
FIELDS: dict[str, Field] = {
    "/semantic": Field("definition", "semantic", "问数语义"),
    "/links": Field("definition", "links", "本体关系"),
    "/name": Field("definition", "text", "指标名称", nonempty=True),
    "/synonyms": Field("display", "texts", "同义词"),
    "/unit": Field("definition", "text", "单位"),
    "/decimals": Field("definition", "int", "小数位"),
    "/format": Field("definition", "text", "展示格式"),
    "/expression": Field("definition", "text", "取值表达式", nonempty=True),
    "/grain": Field("definition", "text", "粒度"),
    "/time_field": Field("definition", "text", "时间字段"),
    "/numerator": Field("definition", "text", "分子"),
    "/denominator": Field("definition", "text", "分母"),
    "/dimensions": Field("definition", "texts", "维度"),
    "/filters": Field("definition", "texts", "过滤条件"),
    "/definition/text": Field("definition", "text", "口径说明"),
    "/definition/scope": Field("definition", "text", "适用范围"),
    "/definition/exclusions": Field("definition", "texts", "不包含"),
}

#: 业务字段（K04）：统计时间口径、退款处理。词汇表和允许值在 ledger/bindings（读接口的能力描述共用同一份），
#: 这里只是把它们接进按字段改的白名单。它们进了 data/caliber_identity.SEMANTIC_FIELDS：改了定义身份就变，
#: 绑定同步状态跟着重判（见 ledger/bindings 与 docs 的 4.5）。
FIELDS.update({f"/{name}": Field("definition", "choice", spec.label, choices=tuple(spec.values()))
               for name, spec in bindings.FIELDS.items()})

#: 明确不能改的路径：身份、指标类型、取值契约、证据归属和治理归属（4.2）。写清楚为什么，不混进「不认识这个字段」。
FORBIDDEN: dict[str, str] = {
    "/id": "指标身份不能改：改名、重排都不换 ID，删掉之后也不把这个 ID 分给别的业务指标（4.1）",
    "/kind": "指标类型（算式 / 规则 / 评估 / 外部常量）决定读哪一份输入，不按字段改",
    "/rule": "规则型指标的规则要整份换，不按字段改",
    "/assessment": "评估型指标的评分协议要整份换，不按字段改",
    "/external": "外部常量自带来源和生效时间，不按字段改",
    "/params": "取值参数要整份换，不按字段改",
    "/contract": "取值契约是结构声明（MetricContract）：改它要过校验，不按字段改",
    "/open_questions": "未决问题走「回答并移除」那条路（PATCH 的 resolved）",
    "/definition/citations": "引用原文是证据归属：只能由核对过证据的流程写，字段操作改不了它",
    "/definition/owner": "负责人是治理信息，不在字段变更的范围内",
    "/definition/effective_from": "生效日期是治理信息，不在字段变更的范围内",
}

#: 路径上「没有这个键」的标记（和值就是 None 分开：设成 null 是被拒的，读到的 None 只可能来自数据本身）
_MISSING = object()


@dataclass(frozen=True)
class Change:
    """一次字段变更的结果：新图（原图的深拷贝 + 这几处）、改了什么、算哪一类、定义指纹前后值、绑定的处置。

    applied 是「内容真的变了」：整批最后和前一份内容一致（先设后删）时是 False，调用方据此不落新版本。
    binding 是 ledger/bindings 的判定：定义和取数节点上的受控绑定对不对得上、能不能同步、影响哪些对象；
    synced 表示这一次在同一事务里把取数 SQL 也按受控模板改过来了。
    """

    graph: dict[str, Any]
    card: str
    applied: bool
    classification: str
    metrics: tuple[str, ...]
    operations: tuple[dict[str, Any], ...]
    definition_hash: tuple[str, str]
    binding: bindings.Report
    synced: bool = False


def field_of(path: str, *, operation: int | None = None) -> Field:
    """路径 → 白名单里的字段；不在白名单、按数组位置定、或者属于明确不能改的那几项时 ChangeRefused。"""
    text = str(path or "")
    if not text.startswith("/"):
        raise ChangeRefused(f"字段路径要以 / 开头（现在是「{text}」）", code=OPERATION_PATH, operation=operation, path=text)
    if text in FORBIDDEN:
        raise ChangeRefused(f"{text} 不能按字段改：{FORBIDDEN[text]}", code=OPERATION_PATH, operation=operation, path=text)
    segments = [segment for segment in text.split("/") if segment]
    if any(segment.isdigit() for segment in segments):
        raise ChangeRefused(f"不能按数组位置定位（{text}）：数组字段整份设置，指标身份用 metric_id，不用位置",
                            code=OPERATION_PATH, operation=operation, path=text)
    field = FIELDS.get(text)
    if field is None:
        allowed = "、".join(sorted(FIELDS))
        raise ChangeRefused(f"不能改这个字段：{text}。能按字段改的是：{allowed}",
                            code=OPERATION_PATH, operation=operation, path=text)
    return field


def _read(index: int, raw: Any) -> tuple[str, str, str, Any]:
    """读一条操作并校验形状：(op, metric_id, path, value)。「有没有写 value」在这一步就判完（省略和 null 是两件事）。"""
    if not isinstance(raw, Mapping):
        raise ChangeRefused(f"第 {index + 1} 条操作不是一个对象", operation=index)
    op = str(raw.get("op") or "").strip()
    if op not in ("set", "remove"):
        raise ChangeRefused(f"不认识的 op：{op or '（空）'}。只支持 set（按字段设值）和 remove（删掉这个字段）",
                            operation=index)
    metric_id = raw.get("metric_id")
    if not isinstance(metric_id, str) or not metric_id.strip():
        raise ChangeRefused(f"第 {index + 1} 条操作缺少 metric_id（指标在口径里的稳定身份）", operation=index)
    path = raw.get("path")
    if not isinstance(path, str) or not path.strip():
        raise ChangeRefused(f"第 {index + 1} 条操作缺少 path（字段路径）", operation=index)
    has_value = "value" in raw
    if op == "remove" and has_value:
        raise ChangeRefused(f"remove 不接受 value（{path.strip()}）：要「声明成空」用 set + ''（数组用 []），"
                            "要「没声明」才用 remove", code=OPERATION_VALUE, operation=index, path=path.strip())
    if op == "set" and not has_value:
        raise ChangeRefused(f"set 要带 value（{path.strip()}）：字符串写 ''，数组写 []，清空字段用 remove",
                            code=OPERATION_VALUE, operation=index, path=path.strip())
    return op, metric_id.strip(), path.strip(), raw.get("value")


def _checked(index: int, path: str, field: Field, value: Any) -> Any:
    """按字段的值类型校验这个值。null 单独拒：它是「清空」还是「声明为空」说不清，不替客户端猜。"""
    def refuse(message: str) -> None:
        raise ChangeRefused(message, code=OPERATION_VALUE, operation=index, path=path)

    if value is None:
        refuse(f"{path} 不能设成 null：「清空」用 remove、「声明成空」用 ''（数组用 []），两种都不是就写具体值")
    if field.value_type == "semantic":
        from pydantic import ValidationError
        from app.ledger.semantic import QuerySemantics

        try:
            return QuerySemantics.model_validate(value).model_dump(mode="json")
        except ValidationError as error:
            refuse(f"问数语义不完整：{error}")
    if field.value_type == "links":
        from app.ledger.ontology import OntologyInvalid, normalize_links

        try:
            return normalize_links(value)
        except OntologyInvalid as error:
            refuse(error.message)
    if field.value_type == "choice":
        if not isinstance(value, str):
            refuse(f"{field.label}（{path}）要是字符串")
        if value not in field.choices:
            spec = bindings.FIELDS.get(path[1:])
            allowed = "、".join(f"{choice}（{spec.label_of(choice) if spec else choice}）" for choice in field.choices)
            refuse(f"{field.label}（{path}）只能是：{allowed}")
        return value
    if field.value_type == "text":
        if not isinstance(value, str):
            refuse(f"{field.label}（{path}）要是字符串")
        if field.nonempty and not value.strip():
            refuse(f"{field.label}（{path}）不能是空串")
        return value
    if field.value_type == "texts":
        if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
            refuse(f"{field.label}（{path}）要是字符串数组，而且是整份设置（不能按位置改其中一项）")
        return copy.deepcopy(value)
    if isinstance(value, bool) or not isinstance(value, int):
        refuse(f"{field.label}（{path}）要是整数")
    if value < 0:
        refuse(f"{field.label}（{path}）不能是负数")
    return value


def _container(metric: dict[str, Any], path: str, *, index: int, op: str) -> tuple[dict[str, Any], str]:
    """值所在的容器和键名：/definition/text 装在 metric["definition"] 里，别的都在指标自己身上。

    装子字段的对象还没建（没这个键，或者就是 null）时：set 顺手建一个——字段本来就该装在里面（好几条指标就只有
    definition 之外的内容）；remove 当「本来就没有这个字段」处理，是无变化，不是路径非法，也不往指标上补一个空对象。
    键在、但不是对象（比如被人写成了字符串或数组）才拒：那是数据坏了，不替人猜该改成什么，更不把原来的值丢掉。
    """
    head, _, tail = path[1:].partition("/")
    if not tail:
        return metric, head
    block = metric.get(head)
    if block is None:
        if op == "remove":
            return {}, tail
        block = metric[head] = {}
        return block, tail
    if not isinstance(block, dict):
        raise ChangeRefused(f"指标上的 {head} 不是对象，不能按字段改它（{path}）",
                            code=OPERATION_PATH, operation=index, path=path)
    return block, tail


def _same(before: Any, after: Any) -> bool:
    if before is _MISSING or after is _MISSING:
        return before is _MISSING and after is _MISSING
    return bool(before == after)


def apply_operations(graph: Mapping[str, Any] | None, *, card_id: str,
                     operations: Sequence[Mapping[str, Any]]) -> Change:
    """把整批字段操作打到原图的深拷贝上，交回新图和这次改了什么。

    整批原子：所有操作都先在拷贝上跑，任何一条不合规、或者指标指不到就 ChangeRefused，图一个字节都不动（4.2）。
    同一条指标上的多条操作按给的顺序依次生效（后面的看得见前面的结果）。
    """
    repo = GraphBackedCaliberRepository(graph, card_id=card_id)
    if len(operations) > MAX_OPERATIONS:
        raise ChangeRefused(f"一次最多改 {MAX_OPERATIONS} 条：再多请走整份换指标那条路")
    if not operations:
        raise ChangeRefused("operations 是空的：要改哪个字段就写哪几条")

    before_hash = repo.definition_hash()
    working: dict[str, dict[str, Any]] = {}
    kinds: set[str] = set()
    report: list[dict[str, Any]] = []
    for index, raw in enumerate(operations):
        op, metric_id, path, value = _read(index, raw)
        field = field_of(path, operation=index)
        if metric_id not in working:
            try:
                working[metric_id] = repo.metric(metric_id)
            except MetricGone:
                raise ChangeRefused(
                    f"这张口径卡上没有指标「{metric_id}」：首版不支持用字段操作新增或删除指标（改名、重排都不换 ID）",
                    code=OPERATION_METRIC, operation=index, path=path) from None
        container, key = _container(working[metric_id], path, index=index, op=op)
        was = container.get(key, _MISSING)
        if op == "remove":
            container.pop(key, None)
        else:
            container[key] = _checked(index, path, field, value)
        now = container.get(key, _MISSING)
        moved = not _same(was, now)
        if moved:
            kinds.add(field.kind)
        report.append({"op": op, "metric_id": metric_id, "path": path, "changed": moved})

    changed = {metric_id: metric for metric_id, metric in working.items() if metric != repo.metric(metric_id)}
    if not changed:
        # 内容没变：交回原图的一份深拷贝（调用方照常走保存，save_graph 认出没变就不新增版本）
        return Change(graph=repo.graph_copy(), card=repo.card_id,
                      applied=False, classification="none", metrics=(), operations=tuple(report),
                      definition_hash=(before_hash, before_hash),
                      binding=bindings.analyze(graph or {}, repo.card_id))
    new_graph = repo.replace_metrics(changed)
    if any(op.get("path") == "/links" for op in report):
        from app.ledger.ontology import OntologyCycleError, check_factor_dag, workflow_ontology_index

        idx = workflow_ontology_index(workflow_id="", version=0, graph=new_graph)
        factor_edges = [
            (
                f"{e['from']['workflow_id'] or 'self'}:{e['from']['card_id']}:{e['from']['metric_id']}",
                f"{e['to']['workflow_id'] or 'self'}:{e['to']['card_id']}:{e['to']['metric_id']}",
            )
            for e in idx["edges"]
            if e["kind"] == "factor" and e["status"] != "rejected"
        ]
        try:
            check_factor_dag(factor_edges)
        except OntologyCycleError as error:
            raise ChangeRefused(error.message, code=OPERATION_VALUE, path="/links") from error
    final, binding_report, synced = _sync_bindings(new_graph, repo.card_id)
    after = GraphBackedCaliberRepository(final, card_id=repo.card_id)
    return Change(graph=final, card=repo.card_id, applied=True,
                  classification="definition" if "definition" in kinds else "display",
                  metrics=tuple(metric_id for metric_id in repo.ids() if metric_id in changed),
                  operations=tuple(report), definition_hash=(before_hash, after.definition_hash()),
                  binding=binding_report, synced=synced)


def _sync_bindings(graph: dict[str, Any], card_id: str) -> tuple[dict[str, Any], bindings.Report, bool]:
    """定义改完之后，取数节点上的受控绑定跟着改（K04，4.5）：能确定性同步的把它一起改掉，改不了的原样留着。

    只改这几处：取数节点 args.sql（受控模板按新业务值展开）和 binding 的 values / rendered_hash / fingerprint。
    有一条同步不了就**一条也不改**（定义照常保存，读的时候照实说 needs_update / unsupported + 原因和影响对象）：
    一半改一半不改比都不改更糟——那才是「旧 SQL 的结果冒充新定义」。执行期不重推任何业务语义（A09）。
    """
    planned = bindings.plan(graph, card_id)
    if not planned.configs:
        return graph, planned, False
    synced = GraphBackedCaliberRepository(graph, card_id=card_id).replace_node_configs(planned.configs)
    return synced, bindings.analyze(synced, card_id), True


def summary(change: Change, *, applied: bool, revision: int | None) -> dict[str, Any]:
    """给接口的变更回执（K03 的「变更分类、影响摘要、绑定状态、任务引用」，K04 接上真实的绑定状态）。

    只说这次真发生了什么：定义指纹前后的值、这次动到的指标、旧验证怎么失效、取数 SQL 同步到哪一步。绑定状态
    来自 ledger/bindings 的实际判定（aligned / needs_update / unsupported / unbound），**不再恒为 unknown，也不
    再把「改过定义」说成对齐**；aligned 只说定义和取数实现同步，不等于业务验证通过，也不改质量结论。验证记录
    K05 已经落库（caliber_validation_records，读法见 app/ledger/validation 与
    GET /api/ledger/drafts/{id}/validations）：哪条记录还算覆盖当前定义/绑定、有没有独立期望、有没有认证审阅，
    都在那边算——所以这里的「下一步」仍是一句动作，不编一个任务号。
    """
    before, after = change.definition_hash
    metrics = list(change.metrics) if applied else []
    if change.synced:
        binding_reason = ("取数 SQL 已按受控模板在同一次变更里改过来：定义和实现是同步的"
                          "（aligned 只说明这两边对得上，不等于业务验证通过）")
    else:
        binding_reason = change.binding.reason
    binding = {**change.binding.as_dict(), "reason": binding_reason, "synced": change.synced}
    return {
        "applied": applied,
        "classification": change.classification if applied else "none",
        "card": change.card,
        "metrics": metrics,
        "operations": [dict(item) for item in change.operations],
        "revision": revision,
        "definition_hash": {"before": before, "after": after, "changed": before != after},
        "validation": {
            "invalidated": applied, "scope": "card", "metrics": metrics,
            "reason": "口径卡的内容变了（图的执行指纹跟着变）：旧的试运行记录和验证不再算覆盖这一版，"
                      "按卡片保守失效（4.8）"
                      if applied else "这次操作没有改动任何内容：图没换版本，原来的验证仍然对着这一版",
            "next": "重新试运行，按新定义取一次独立样例" if applied else "不用重新试运行",
        },
        "binding": binding,
    }


__all__ = ["FIELDS", "FORBIDDEN", "MAX_OPERATIONS", "OPERATION_INVALID", "OPERATION_METRIC", "OPERATION_PATH",
           "OPERATION_VALUE", "Change", "ChangeRefused", "Field", "apply_operations", "field_of", "summary"]
