"""受限的自然语言修改与固定请求路由（K12）：自然语言只产出一份**提案**，写仍然走 K03 那条路。

这一层解决两件事，都刻意做得很小、很受限：

**一、自然语言改口径只产出「K03 同一份 ChangeSet 提案」。** 一次提议只说三样东西——一批 K03 的字段操作
（op / metric_id / path / value，白名单和写法见 app/ledger/changes.py）、这次会影响什么（影响指标、失效说明、绑定状态，
由 `changes.summary` 在同一条路上算出来）、以及**必须先问清的澄清项**。没有第二条写路径：提案不落库、不新增版本，
真要用它，客户端把 operations 交给现有的 `PATCH /api/ledger/drafts/{id}`（expected_revision / request_id 幂等 /
CAS / 绑定同步 / 校验一条不少），或者调这里的薄适配入口（同样落回 `PATCH`，且要带客户端确认）。**失败不擅自改口径**：
说不清就只给澄清项，一条操作都不产出。

识别只认**已声明的能力**（`bindings.editable_fields()`：统计时间口径、退款处理等业务字段，每个字段允许的取值和说法）。
不写通用聊天 DSL，也不解析任意字段：一句话里提到某个业务字段、又指定了它某个允许的取值，才落成操作；提到字段却说不清
取哪个值（比如「排除退款」——词汇表里只有「排除全额退款」和「全部计入」，全额/部分没分开），就产出澄清项，不替它猜。
没提到任何已声明能力，同样只给澄清项，不静默当成「不用改」。

判「有没有在谈这个字段」按**规则/条件**算，不按名词算：光有名词（`_FIELD_NOUNS`）不算——指标名里带「退款」
（「退款率」）不等于在谈退款规则，`propose` 还会先把卡上指标的名字和同义词挖掉再识别。**否定不反向**：登记过的否定
（`_NEGATED_MEANS`，「不排除全额退款」= 全部计入、也就是「不要排除/不排除/无需排除全额退款」都落成 `include_all`）
翻成它真正的意思；没登记的否定（「不按支付时间」）既不认这个值、也不认它的反面，只报说不清。

**模型只能提建议。** `from_model` 收的是一份模型产出的 JSON：里面的 operations 照 `changes` 的白名单和值域逐条校验，
**actor / review / verified / approved 这类「自报批准」的键一律丢掉**（记进 ignored_claims），影响摘要和失效结论一律
由平台按图现算——模型说「已批准」「已验证」「不会失效」都不算数。

**二、Ask Data 的固定请求路由。** 已经明确 workflow + card + metric_ids + inputs 的确定请求直接进 QueryCore
（app/ledger/querycore.py），路由只把版本固定一次（点名就固定到它、不点名读推荐版并当场固定），交给 `execute` 时带上
那一版，QueryCore 不再另挑一次；执行路径内部 LLM 仍为 0。没有匹配到口径（工作流/卡指不到、这一版没有这张卡）就**明确
进探索**，含糊的业务条件（说不清取哪个值）先澄清；请求里的业务条件和匹配到的那一版声明的算法对不上时，**不静默换计算
规则**，照实说要先受限修改口径或点名匹配的那一版。外层模型可以提建议（`from_model`），但路由本身不调模型。

**三、普通问数的保守默认路由。** `auto_route` 给「没有显式固定请求」的普通问数一次**高置信才命中**的机会：按推荐发布
版的指标 name / 唯一 synonym 规范化精确匹配**唯一**一个指标、这句话没有含糊业务条件、这次入口取值拿得全且类型可证，
才交回一条固定请求（调用方据此执行）；**任何一处不确定都交回 None**，调用方原样走旧的 Agent 路径，不向用户多说什么、也
不替它挑一份口径。不模糊相似、不按整卡、不问模型；跨卡同名或一句指到不止一个指标都不自动。

匹配**仍按全部发布口径**看（`_auto_cards`），所以「没匹配到」和「命中了但这个人没有使用权」区分得开：调用方把
`allowed`（`(workflow_id, version)` 的集合，来自资产授权，app/apps/access.usable_calibers）传进来，唯一命中却不在其中
时交回**终止型**的 `ROUTE_REFUSED` / `ROUTE_NOT_PERMITTED`——这不是「未匹配」，调用方据此照实说没有授权，**不回退旧的
Agent 路径**：那条路会去翻底表，等于拿着一句高置信的问法绕开这份口径的授权。`allowed` 是 None 表示调用方已放行
（搭建者及以上：它们本来就能直接按口径执行），不按资产授权收窄——那几个角色不进资产授权函数。
"""
from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from app.ledger import bindings, changes
from app.ledger.repository import GraphBackedCaliberRepository

# 机器码（接口按它们回结构化拒绝；给人看的说法另在 message / question 里）
#: 一句话里提到的业务条件说不清取哪个值：必须先澄清
PROPOSAL_AMBIGUOUS = "change_proposal_ambiguous"
#: 一句话里没有提到任何已声明的能力
PROPOSAL_NONE = "change_proposal_no_match"

#: 澄清项的类别
KIND_AMBIGUOUS = "ambiguous"     # 提到了字段，但取值说不清
KIND_NO_MATCH = "no_match"       # 没提到任何已声明能力
KIND_MISSING = "missing"         # 请求少了确定请求必需的一项（路由用：要算哪几个指标）

# 固定请求路由的结论与机器码
ROUTE_EXECUTE = "querycore"     # 确定请求：直接进 QueryCore，不重复解析/规划
ROUTE_CLARIFY = "clarify"       # 还缺一项或业务条件说不清：先澄清
ROUTE_EXPLORE = "explore"       # 没有匹配口径 / 业务条件和口径对不上：进探索，不静默换规则
#: 唯一命中、但这个人对那一版没有使用权：**终止型**——不是「未匹配」，不回退旧的 Agent 路径（那条路会去翻底表）
ROUTE_REFUSED = "refused"
ROUTE_AMBIGUOUS = "route_ambiguous"          # 业务条件说不清
ROUTE_VERSION_INVALID = "route_version_invalid"   # 点名的版本号不是正整数：先说清，绝不静默推荐版
ROUTE_METRICS_REQUIRED = "route_metric_ids_required"
ROUTE_NO_CALIBER = "route_no_matching_caliber"
ROUTE_CONDITION_MISMATCH = "route_condition_mismatch"
ROUTE_READY = "route_execute"
#: 普通问数里自动命中的固定请求（保守默认路由）：命中直接执行，未命中交回 None 走旧 Agent 路径
ROUTE_AUTO = "route_auto_match"
#: 命中了、但不在这次调用方给的授权范围里（见 auto_route 的 allowed）
ROUTE_NOT_PERMITTED = "route_not_permitted"

#: 「命中但无权」给人的说法：自动路由的拒绝和固定请求那道闸**共用同一句**，不出现工作流 / 卡 / 版本这类内部引用
NOT_PERMITTED = "这句话指向的那份固定口径没有授权给你使用：请换一种问法，或让搭建者把这份口径授权给你"

#: 光凭它就说明在谈这个字段（够具体的说法，不依赖名词）。加了新的声明白名单字段（bindings.FIELDS）时在这里登记；
#: 没登记的字段不参与自然语言识别（但仍可经模型建议 from_model 按字段改）。
_FIELD_OPERATORS: dict[str, tuple[str, ...]] = {
    "refund_rule": ("排除", "剔除", "计入", "算入", "算进", "纳入", "包含", "过滤", "扣除", "去掉"),
}

#: 字段的「名词」：**要和搭配词一起出现**才算在谈这个字段。光有名词不算——指标名里带「退款」（退款率）不等于
#: 在谈退款规则，不能因此去问退款处理取哪个值。
_FIELD_NOUNS: dict[str, tuple[str, ...]] = {
    "time_role": ("时间", "日期"),
    "refund_rule": ("退款", "退货", "退单"),
}

#: 和名词搭配的词：名词 + 其中任一个出现，才当成在谈这个字段。
#: 退款这一列刻意不收「按」「口径」这类通用词——「退款率按支付时间口径算」谈的是时间口径，不是退款规则。
_FIELD_MATCHERS: dict[str, tuple[str, ...]] = {
    "time_role": ("按", "统计", "口径", "算"),
    "refund_rule": ("规则", "处理", "方式"),
}

#: 每个已声明取值除词汇表里的 label 之外另认的说法（label 一定认；加新声明取值不改这里也认）。
#: 这些是**受控词汇表**的另一层说法，不是通用自然语言解析：只认下面这些整词，不猜。
#: 「不要排除」「不排除」这类**否定说法本身就是一条正向声明**（不排除 = 全部计入），单列在这里；
#: 别的否定（比如「不按支付时间」）不认，绝不当成它的反面。
_ALIASES: dict[tuple[str, str], tuple[str, ...]] = {
    ("time_role", "order_time"): ("按下单时间", "下单时间", "按订单时间", "按创建时间"),
    ("time_role", "payment_time"): ("按支付时间", "支付时间", "按付款时间", "付款时间"),
    ("refund_rule", "include_all"): ("不排除", "不要排除", "无需排除", "不用排除", "不必排除",
                                     "不排除退款", "不排除退货", "不剔除", "退款全算"),
    ("refund_rule", "exclude_full_refund"): ("排除全额退款", "全额退款排除", "剔除全额退款"),
}

#: 否定词（出现在取值说法之前时构成否定）。「取消」这类也算否定：否定后面的取值不认，也不翻成它的反面。
_NEG_PREFIXES: tuple[str, ...] = ("不要", "无需", "不用", "不必", "不能", "没有", "取消", "不", "别", "勿")

#: 否定**某个取值说法**时真正的意思（不排除全额退款 = 全部计入）。只有这里登记了的才把否定翻成对应的值；
#: 别的否定一律不算命中（既不认这个值，也不认它的反面）——「其他否定也不能反向」。
_NEGATED_MEANS: dict[tuple[str, str], str] = {
    ("refund_rule", "exclude_full_refund"): "include_all",
}


def _is_negative_form(phrase: str) -> bool:
    """这个说法本身是不是一条否定形式的**正向**声明（「不排除」这类）：是的话不再按否定处理。"""
    return any(phrase.startswith(prefix) for prefix in _NEG_PREFIXES)


def _negated(text: str, at: int) -> bool:
    """取值说法前面紧挨着一个否定词（最多看 3 个字的窗口）。"""
    before = text[max(0, at - 4):at]
    return any(before.endswith(prefix) for prefix in _NEG_PREFIXES)

#: 模型不能自报的键（顶层和每条操作里都清掉）：批准、主体、审阅、验证结论、失效结论这类，一律由平台现算。
_FORBIDDEN_CLAIM_KEYS: frozenset[str] = frozenset({
    "actor", "role", "review", "reviewer", "reviewed", "reviewed_by", "approve", "approved", "approval",
    "approver", "verified", "verify", "authorized", "status", "signoff", "signed_off", "applied", "change",
    "validation", "summary", "created_by", "performed_by",
})


def declared_capabilities() -> list[dict[str, Any]]:
    """已声明的能力（业务字段 + 允许的取值和说法）：服务端词汇表原样交回，识别只看它。"""
    return bindings.editable_fields()


def _phrases_with(capability: Mapping[str, Any], value: str,
                  aliases: Mapping[tuple[str, str], tuple[str, ...]]) -> tuple[str, ...]:
    """一个允许取值的所有说法：词汇表里的 label、机器值本身，加上登记过的同义说法（去空白、去重）。"""
    labels = (capability.get("value_labels") or {}).get(value)
    raw: list[Any] = [labels, value, *(aliases.get((str(capability.get("field")), str(value)), ()))]
    out: list[str] = []
    for phrase in raw:
        text = str(phrase or "").strip()
        if text and text not in out:
            out.append(text)
    return tuple(out)


def _discussed(capability: Mapping[str, Any], text: str, *,
               aliases: Mapping[tuple[str, str], tuple[str, ...]]) -> bool:
    """这句话在不在谈这个字段的**规则/条件**：写了字段名、出现它某个允许取值的说法、出现够具体的操作词，
    或者名词 + 搭配词一起出现。**光有名词不算**：指标名里带「退款」（退款率）不等于在谈退款规则。"""
    name = str(capability.get("field") or "")
    if name and name in text:
        return True
    if any(phrase and phrase in text for value in capability.get("values") or ()
           for phrase in _phrases_with(capability, value, aliases)):
        return True
    if any(operator in text for operator in _FIELD_OPERATORS.get(name, ())):
        return True
    return (any(noun in text for noun in _FIELD_NOUNS.get(name, ()))
            and any(matcher in text for matcher in _FIELD_MATCHERS.get(name, ())))


def _blanked(text: str, ignore: Sequence[str]) -> str:
    """把不该参与识别的说法（卡上的指标名/同义词）按原长度挖成空格：指标名里带的「退款」不算在谈退款规则。"""
    out = text
    for phrase in ignore:
        piece = str(phrase or "").strip()
        if piece and piece in out:
            out = out.replace(piece, " " * len(piece))
    return out


@dataclass(frozen=True)
class FieldMatch:
    """一句话里认出来的一个业务条件：哪个字段、取了哪个允许的值、原话里是哪个说法。"""

    field: str
    value: str
    label: str
    phrase: str

    def as_dict(self) -> dict[str, Any]:
        return {"field": self.field, "value": self.value, "label": self.label, "phrase": self.phrase}


@dataclass(frozen=True)
class Clarification:
    """一个必须先问清的澄清项：kind 是类别，field 是哪个字段（路由里可能是 metric_ids），options 是候选。

    「含糊业务条件必须先澄清」就是靠它：识别到某字段却说不清取哪个值时，把该字段的允许取值原样列出来，不替人猜。"""

    kind: str
    field: str | None
    question: str
    options: tuple[dict[str, str], ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "field": self.field, "question": self.question,
                "options": [dict(option) for option in self.options]}


@dataclass(frozen=True)
class Proposal:
    """一份受限修改提案：K03 的 operations、影响摘要（preview）、澄清项、模型自报的键。

    ready 为真才谈得上应用（有操作、没有澄清项）。operations 原样可以交给 `PATCH /api/ledger/drafts/{id}`；
    preview 就是在同一批操作上跑一次 `changes` 得到的回执（含失效与绑定结论），但**不落库、不新增版本**。"""

    card: str
    operations: tuple[dict[str, Any], ...] = ()
    clarifications: tuple[Clarification, ...] = ()
    metrics: tuple[str, ...] = ()
    preview: dict[str, Any] | None = None
    ignored_claims: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    @property
    def ready(self) -> bool:
        return bool(self.operations) and not self.clarifications

    def as_dict(self) -> dict[str, Any]:
        return {"card": self.card, "ready": self.ready,
                "operations": [dict(op) for op in self.operations],
                "metrics": list(self.metrics),
                "clarifications": [item.as_dict() for item in self.clarifications],
                "preview": dict(self.preview) if self.preview is not None else None,
                "ignored_claims": list(self.ignored_claims),
                "notes": list(self.notes)}


# --------------------------------------------------------------------------
# 自然语言 → 业务条件
# --------------------------------------------------------------------------


def match_conditions(instruction: str, *,
                     ignore: Sequence[str] = ()) -> tuple[list[FieldMatch], list[Clarification]]:
    """一句话 →（认出来的业务条件, 必须先澄清的项）。只认已声明的能力，不做通用解析。

    提到某个字段、又指定了它某个允许取值 → 一条 FieldMatch；提到字段却说不清取值（或者一句话里说了两个冲突的取值）→
    一条 ambiguous 澄清项。同一个字段只取一条：说清了就是那一条，说不清就只给澄清项、不产出任何操作。

    `ignore` 是**不该参与识别的说法**（卡上的指标名/同义词）：挖掉它们之后再判——指标名里带「退款」不等于在谈退款规则。
    否定按 `_NEGATED_MEANS` 处理：「不排除全额退款」是「全部计入」，别的否定不认（不翻成反面）。"""
    text = _blanked(str(instruction or "").strip(), ignore)
    matches: list[FieldMatch] = []
    clarifications: list[Clarification] = []
    if not text.strip():
        return matches, clarifications
    for capability in declared_capabilities():
        name = str(capability.get("field") or "")
        if not name or not _discussed(capability, text, aliases=_ALIASES):
            continue
        found, unresolved = _scan_values(capability, text, aliases=_ALIASES)
        if len(found) == 1 and not unresolved:
            value = found[0]
            phrase = _first_phrase(_phrases_with(capability, value, _ALIASES), text) or value
            matches.append(FieldMatch(field=name, value=value, label=str(capability.get("label") or name),
                                      phrase=phrase))
            continue
        options = tuple({"value": str(value), "label": str((capability.get("value_labels") or {}).get(value) or value)}
                        for value in capability.get("values") or ())
        label = capability.get("label") or name
        if not found:
            question = (f"这句话提到「{label}」，但没说清取哪个值："
                        f"请指定{'、'.join(option['label'] for option in options) or '具体取值'}")
        elif unresolved:
            question = f"这句话里「{label}」的否定说不清是什么意思：请直接说是{'、'.join(option['label'] for option in options)}"
        else:
            question = (f"这句话里「{label}」说了不止一个值"
                        f"（{'、'.join(option['label'] for option in options if option['value'] in found)}）：只能取一个")
        clarifications.append(Clarification(kind=KIND_AMBIGUOUS, field=name, question=question, options=options))
    return matches, clarifications


def _scan_values(capability: Mapping[str, Any], text: str,
                 *, aliases: Mapping[tuple[str, str], tuple[str, ...]]) -> tuple[list[str], bool]:
    """这句话里出现过的允许取值：按**最长的说法先认**并就地消费掉（长说法里的短子串不会被再认一遍）。

    交回 (认到的取值, 有没有说不清的否定)。否定**紧挨在取值说法之前**时（「不排除全额退款」）：登记过否定含义的翻成对应的
    值（→ 全部计入）；没登记的**不算命中**（既不认这个值，也不认它的反面），只记一句「说不清」。否定形式本身（「不排除」）
    已经是一条正向声明，不再按否定处理。"""
    field = str(capability.get("field") or "")
    candidates: list[tuple[str, str, bool]] = []
    for value in capability.get("values") or ():
        for phrase in _phrases_with(capability, value, aliases):
            candidates.append((phrase, str(value), _is_negative_form(phrase)))
    consumed = [False] * len(text)
    found: list[str] = []
    unresolved = False
    for phrase, value, negative_form in sorted(candidates, key=lambda item: -len(item[0])):
        start = 0
        while phrase:
            at = text.find(phrase, start)
            if at < 0:
                break
            end = at + len(phrase)
            start = at + 1
            if any(consumed[at:end]):
                continue
            for index in range(at, end):
                consumed[index] = True
            if negative_form:
                if value not in found:
                    found.append(value)
            elif _negated(text, at):
                mapped = _NEGATED_MEANS.get((field, value))
                if mapped is None:
                    unresolved = True
                elif mapped not in found:
                    found.append(mapped)
            elif value not in found:
                found.append(value)
    return found, unresolved


def _first_phrase(phrases: Sequence[str], text: str) -> str | None:
    hits = [phrase for phrase in phrases if phrase and phrase in text]
    return max(hits, key=len) if hits else None


# --------------------------------------------------------------------------
# 业务条件 → K03 operations（在原图的深拷贝上试跑）
# --------------------------------------------------------------------------


def _metric_mentions(metrics: Sequence[Mapping[str, Any]]) -> list[str]:
    """卡上指标的名字和同义词：识别前先把它们挖掉，指标名里带的词不算在谈业务规则。

    进不了已声明取值说法的那些才挖（是某个说法的一部分就留着），免得把「支付」这种指标名挖掉之后
    「按支付时间」也认不出来了。"""
    protected = [phrase for capability in declared_capabilities()
                 for value in capability.get("values") or ()
                 for phrase in _phrases_with(capability, value, _ALIASES)]
    out: list[str] = []
    for metric in metrics:
        for name in (metric.get("name"), *(metric.get("synonyms") or [])):
            piece = str(name or "").strip()
            if piece and piece not in out and not any(piece in phrase for phrase in protected):
                out.append(piece)
    return out


def _named_metrics(metrics: Sequence[Mapping[str, Any]], text: str) -> list[str]:
    """一句话里点名了哪几条指标：按 metric_id、名称、同义词认（只在卡上现有的指标里认）。"""
    out: list[str] = []
    for metric in metrics:
        metric_id = str(metric.get("id") or "")
        if not metric_id:
            continue
        names = [metric_id, str(metric.get("name") or "")]
        names += [str(s) for s in metric.get("synonyms") or [] if isinstance(s, str)]
        for name in names:
            name = name.strip()
            if name and name in text and metric_id not in out:
                out.append(metric_id)
                break
    return out


def _targets(capability_field: str, metrics: Sequence[Mapping[str, Any]], *, named: Sequence[str],
             explicit: Sequence[str]) -> list[str]:
    """这条业务条件要改哪几条指标：显式点名 > 声明过这个字段的 > 句子里点名的 > 整卡。

    「声明过这个字段的」优先：改的是这张卡现有的统计口径，不动没表态的指标（不擅自把整卡的口径一起换掉）。"""
    all_ids = [str(metric.get("id")) for metric in metrics if metric.get("id")]
    if explicit:
        return [str(m) for m in explicit]
    declaring = [str(metric.get("id")) for metric in metrics
                 if metric.get("id") and bindings.declared(metric).get(capability_field) is not None]
    if declaring:
        return declaring
    if named:
        return list(named)
    return all_ids


def _preview(graph: Mapping[str, Any], card_id: str, operations: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """在**原图的深拷贝**上把这一批操作跑一遍，交回和真落库同一形状的回执（不落库）。"""
    change = changes.apply_operations(graph, card_id=str(card_id), operations=list(operations))
    made = changes.summary(change, applied=bool(change.applied), revision=None)
    return {**made, "preview": True, "persisted": False}


def preview(graph: Mapping[str, Any], card_id: str, operations: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """一次 K03 字段变更的预览（不落库）：同一批操作交给真落库那条路会得到同一份回执。"""
    return _preview(graph, card_id, operations)


def propose(graph: Mapping[str, Any], card_id: str, instruction: str, *,
            metrics: Sequence[str] | None = None) -> Proposal:
    """自然语言 → 受限修改提案。**只产出提案**：operations + 影响摘要 + 澄清项，不落库、不新增版本。

    识别只认已声明的能力；说不清（含糊）或没提到任何能力时只给澄清项，一条操作都不产出。认出来的条件按
    `_targets` 落到具体的 metric_id 上，然后再在原图的深拷贝上跑一遍 `changes`，所以回执里的失效与绑定结论和真落库
    时是同一份。"""
    text = str(instruction or "").strip()
    repo = GraphBackedCaliberRepository(graph, card_id=str(card_id))
    card_metrics = repo.metrics()
    # 指标名/同义词先挖掉再识别：指标名里带「退款」（退款率）不等于在谈退款规则
    matches, clarifications = match_conditions(text, ignore=_metric_mentions(card_metrics))
    if clarifications:
        return Proposal(card=str(card_id), clarifications=tuple(clarifications),
                        notes=("识别到含糊的业务条件：先澄清，一条操作也不产出（不替人猜取值）",))
    if not matches:
        return Proposal(card=str(card_id), clarifications=(Clarification(
            kind=KIND_NO_MATCH, field=None,
            question="这句话里没有能按字段改的已声明能力：要改统计时间口径、退款处理这类业务字段，请指明字段和取值",
            options=tuple({"value": str(item.get("field")), "label": str(item.get("label") or item.get("field"))}
                          for item in declared_capabilities())),),
            notes=("没有匹配到已声明能力：只给澄清项，不静默当成「不用改」",))

    named = _named_metrics(card_metrics, text)
    explicit = [str(m) for m in (metrics or []) if str(m or "").strip()]
    targets: dict[str, list[str]] = {}
    for match in matches:
        targets[match.field] = _targets(match.field, card_metrics, named=named, explicit=explicit)

    operations: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for match in matches:
        for metric_id in targets.get(match.field, []):
            key = (metric_id, match.field)
            if key in seen:
                continue
            seen.add(key)
            operations.append({"op": "set", "metric_id": metric_id, "path": f"/{match.field}", "value": match.value})
    if not operations:
        return Proposal(card=str(card_id), clarifications=(Clarification(
            kind=KIND_MISSING, field="metric_ids",
            question="这张口径卡上没有可改的指标：先确认要改哪几条指标",
            options=tuple({"value": str(metric.get("id")), "label": str(metric.get("name") or metric.get("id"))}
                          for metric in card_metrics if metric.get("id"))),))

    made = _preview(graph, card_id, operations)
    impacted = tuple(str(m) for m in made.get("metrics") or [])
    return Proposal(card=str(card_id), operations=tuple(operations), metrics=impacted, preview=made,
                    notes=("提案只是建议：默认不自动落版，应用要走现有的 PATCH（expected_revision / request_id / CAS / "
                           "绑定同步 / 校验）",))


# --------------------------------------------------------------------------
# 模型建议 → 提案（只提建议，不能自报批准）
# --------------------------------------------------------------------------


def from_model(graph: Mapping[str, Any], card_id: str, payload: Mapping[str, Any] | None, *,
               metrics: Sequence[str] | None = None) -> Proposal:
    """外层模型的建议 → 提案：operations 逐条过 K03 的白名单和值域，**自报批准/主体/验证的键一律丢掉**。

    只采信 operations：影响摘要、失效结论、澄清项都由平台按图现算。模型写的 actor / review / verified / approved /
    summary / change / validation 这类键（顶层和每条操作里的都算）进 ignored_claims，绝不当成批准或验证通过。"""
    raw = payload if isinstance(payload, Mapping) else {}
    ignored = _strip_claims(raw)
    operations: list[dict[str, Any]] = []
    for item in raw.get("operations") or []:
        if not isinstance(item, Mapping):
            continue
        cleaned = {key: item[key] for key in ("op", "metric_id", "path", "value") if key in item}
        ignored += [key for key in item if key not in cleaned]
        operations.append(dict(cleaned))
    if not operations:
        return Proposal(card=str(card_id), clarifications=(Clarification(
            kind=KIND_NO_MATCH, field=None, question="这次建议里没有可用的字段操作"),),
            ignored_claims=tuple(dict.fromkeys(ignored)),
            notes=("模型没给出操作：不落任何东西",))
    if metrics:
        wanted = {str(m) for m in metrics}
        operations = [op for op in operations if str(op.get("metric_id") or "") in wanted] or operations

    made = _preview(graph, card_id, operations)
    return Proposal(card=str(card_id), operations=tuple(operations),
                    metrics=tuple(str(m) for m in made.get("metrics") or []), preview=made,
                    ignored_claims=tuple(dict.fromkeys(ignored)),
                    notes=("模型只提建议：operations 过白名单与值域，影响摘要和失效结论由平台现算；"
                           "自报的批准 / 主体 / 验证结论不采信",))


def _strip_claims(raw: Mapping[str, Any]) -> list[str]:
    return [str(key) for key in raw if str(key) in _FORBIDDEN_CLAIM_KEYS]


# --------------------------------------------------------------------------
# Ask Data 固定请求路由
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Route:
    """一条请求该走哪条路：querycore（直接进固定执行）/ clarify（先澄清）/ explore（进探索）/ refused（命中但没授权）。

    request 是归一化后的固定请求（mode 为 querycore 时交给执行那条路）；clarifications 是澄清项。refused 是**终止型**：
    调用方照实说没有授权，不回退旧的 Agent 路径。"""

    mode: str
    code: str
    reason: str
    request: dict[str, Any] = field(default_factory=dict)
    clarifications: tuple[Clarification, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {"mode": self.mode, "code": self.code, "reason": self.reason,
                "request": dict(self.request),
                "clarifications": [item.as_dict() for item in self.clarifications]}


def _ids(value: Any) -> list[str]:
    if isinstance(value, str):
        value = [value]
    return [str(item) for item in (value or []) if str(item or "").strip()]


def _explicit_version(value: Any) -> tuple[int | None, str | None]:
    """这条请求点名的版本：(版本号, 为什么不算数)。

    十进制数字字符串（"2"）按**显式版本**处理——和整数 2 一样固定到那一版，不退回推荐版。没写（None / 空串）是
    (None, None)，交给默认解析；写了但不是正整数（"abc"、"2.5"、0、-1、true、对象……）是 (None, 一句为什么)，
    入口据此先澄清/拒绝，**绝不静默按推荐版执行**（那等于悄悄换了一版口径）。"""
    if value is None:
        return None, None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None, None
        if not text.isdigit():
            return None, f"版本号「{text}」不是一个正整数"
        number = int(text)
    elif isinstance(value, bool):
        return None, "版本号要是正整数"
    elif isinstance(value, int):
        number = value
    elif isinstance(value, float) and value.is_integer():
        number = int(value)
    else:
        return None, "版本号要是正整数"
    if number < 1:
        return None, "版本号要是正整数（从 1 起）"
    return number, None


def _source_card_metrics(graph: Mapping[str, Any], card_id: str) -> list[dict[str, Any]] | None:
    """这一版图里的源头口径卡上的指标；不是卡、是转抄卡、或者卡不在图里时 None。"""
    for node in graph.get("nodes") or []:
        if not isinstance(node, Mapping) or str(node.get("id")) != str(card_id):
            continue
        if node.get("type") != "metrics":
            return None
        config = ((node.get("data") or {}).get("config") or {}) if isinstance(node.get("data"), Mapping) else {}
        if config.get("caliber_from") not in (None, "", {}):
            return None
        return [dict(metric) for metric in config.get("metrics") or [] if isinstance(metric, Mapping)]
    return None


def _condition_problem(matches: Sequence[FieldMatch], card_metrics: Sequence[Mapping[str, Any]]) -> str | None:
    """请求里的业务条件 vs 这一版声明的算法：对不上就返回一句为什么，能对上返回 None。"""
    for match in matches:
        spec = bindings.FIELDS.get(match.field)
        label = spec.label if spec else match.field
        declared = [bindings.declared(metric).get(match.field) for metric in card_metrics]
        declared = [value for value in declared if value is not None]
        if not declared:
            return (f"这一版口径没有声明「{label}」：请求里的「{match.phrase}」说的是另一套算法，"
                    "不静默按这一版算——请先受限修改口径把它声明上，或点名声明了它的那一版")
        if any(value != match.value for value in declared):
            return (f"请求里的「{match.phrase}」和这一版声明的「{label}」对不上：不静默换计算规则——"
                    "请先受限修改口径，或点名匹配的那一版")
    return None


async def _locate(session: Any, *, workflow_ref: str, card_id: str, version: int | None,
                  purpose: str) -> tuple[dict[str, Any] | None, str | None]:
    """定位要执行的那一版这一张卡：交回 (info, 为什么进探索)。

    info 带 workflow_id / version / metrics（这一版这张源头卡上的指标）；指不到、版本没发布、卡不是源头卡时
    info 为 None、另给一句为什么——调用方据此进探索，不静默换一份口径。版本在这里**固定一次**（点名就固定到它、
    不点名读推荐版并当场固定），交给 execute 时带上那一版，QueryCore 不再另挑一次（不重复解析/规划）。"""
    from app.apps import publications
    from app.apps.versions import is_published
    from app.db.models import Workflow
    from app.ledger import querycore

    if not workflow_ref:
        return None, "这条请求没有指向一份固定口径：进入探索，不静默挑一份口径执行"
    try:
        workflow_id = await querycore._workflow_id(session, workflow_ref)
    except querycore.QueryCoreRefused:
        return None, f"找不到口径「{workflow_ref}」：进入探索，不静默挑一份口径执行"
    workflow = await session.get(Workflow, workflow_id)
    resolved = await publications.resolve(session, workflow, version=version, purpose=purpose)
    if resolved is None:
        return None, "这份口径现在没有可执行的已发布版本：进入探索"
    if not is_published(resolved.row, current=getattr(workflow, "published_version", None)) \
            or resolved.row.retired_at is not None:
        return None, "这一版没有发布或已退役：进入探索，不换一版静默执行"
    if not card_id:
        return None, "这条请求没有点名口径卡：进入探索，不静默挑一张卡"
    card_metrics = _source_card_metrics(resolved.row.graph or {}, card_id)
    if card_metrics is None:
        return None, f"这一版里没有源头口径卡「{card_id}」：进入探索"
    return {"workflow_id": resolved.workflow_id, "version": int(resolved.version), "metrics": card_metrics}, None


async def route(session: Any, *, request: Mapping[str, Any] | None,
                purpose: str | None = None) -> Route:
    """按一条请求决定走哪条路：能确定的直接进 QueryCore，说不清/没匹配的口径进澄清或探索。

    判定顺序：版本号先看（不是正整数的先澄清，绝不静默推荐版）→ 定位口径（工作流 + 版本 + 源头口径卡）→ 含糊业务
    条件先澄清（定位到卡就先把卡上的指标名挖掉再判，指标名里带「退款」不算在谈退款规则）→ 请求里的业务条件和这一版
    声明的算法对不上就进探索（不静默换计算规则）→ 指标没点名就澄清 → 都明确才交回一条可以交给
    `querycore.execute` 的固定请求。定位不到口径时仍按原话判一次含糊条件（含糊照样先澄清），然后进探索。

    `version` 是十进制数字字符串（"2"）时按**显式版本**处理；非法版本先澄清/拒绝，不静默按推荐版执行。
    **不调模型**：外层模型可以用 `from_model` 提建议，路由本身零模型。"""
    from app.ledger import querycore

    # 用途由宿主常量定（这里默认交互入口那一档）；客户端传不了 purpose
    purpose = str(purpose or querycore.DEFAULT_PURPOSE)
    req = dict(request or {})
    workflow_ref = str(req.get("workflow_id") or req.get("workflow") or "").strip()
    card_id = str(req.get("card_id") or req.get("card") or "").strip()
    metric_ids = _ids(req.get("metric_ids"))
    inputs = req.get("inputs") if isinstance(req.get("inputs"), Mapping) else {}
    instruction = str(req.get("instruction") or "").strip()

    # 0) 版本：字符串数字按显式版本处理；非法版本先澄清，绝不静默推荐版
    wanted_version, version_problem = _explicit_version(req.get("version"))
    if version_problem is not None:
        return Route(ROUTE_CLARIFY, ROUTE_VERSION_INVALID, version_problem,
                     clarifications=(Clarification(
                         kind=KIND_MISSING, field="version",
                         question="要固定到哪一版？版本号写正整数；不确定就先不写，按推荐版执行"),))

    # 1) 定位口径（版本固定一次）
    located, reason = await _locate(session, workflow_ref=workflow_ref, card_id=card_id,
                                    version=wanted_version, purpose=purpose)

    # 2) 含糊业务条件：定位到卡就按卡上的指标名屏蔽后再判；定位不到就按原话判。含糊一律先澄清、不产出操作
    ignore = _metric_mentions(located["metrics"]) if located else []
    matches, clarifications = match_conditions(instruction, ignore=ignore) if instruction else ([], [])
    if clarifications:
        return Route(ROUTE_CLARIFY, ROUTE_AMBIGUOUS,
                     "请求里的业务条件说不清取哪个值：先澄清，不按旧口径静默执行",
                     clarifications=tuple(clarifications))
    if located is None:
        return Route(ROUTE_EXPLORE, ROUTE_NO_CALIBER, reason or "没有匹配到口径：进入探索")

    # 3) 业务条件 vs 这一版声明的算法：对不上就不能静默按这一版算
    if matches and (problem := _condition_problem(matches, located["metrics"])) is not None:
        return Route(ROUTE_EXPLORE, ROUTE_CONDITION_MISMATCH, problem)

    # 4) 指标必须点名（空的选择不静默当整卡）
    if not metric_ids:
        return Route(ROUTE_CLARIFY, ROUTE_METRICS_REQUIRED,
                     "这条请求没有点名要算哪几个指标：先澄清要算哪几个，不静默当整卡",
                     clarifications=(Clarification(
                         kind=KIND_MISSING, field="metric_ids", question="要算哪几个指标？",
                         options=tuple({"value": str(metric.get("id")), "label": str(metric.get("name") or metric.get("id"))}
                                       for metric in located["metrics"] if metric.get("id"))),))

    return Route(ROUTE_EXECUTE, ROUTE_READY,
                 "请求已经明确（口径 + 卡 + 指标 + 取值）：直接进 QueryCore，不重复解析/规划",
                 request={"workflow_id": located["workflow_id"], "card_id": card_id, "metric_ids": metric_ids,
                          "inputs": dict(inputs), "version": located["version"], "purpose": purpose})


# --------------------------------------------------------------------------
# 普通问数的保守默认路由：高置信确定的请求直接进 QueryCore，其余一律回旧路径
#
# 这一层只回答一个问题：「这句话是不是**高置信**地指向一份固定口径的某一个指标，参数也拿得全？」是就交回一条
# 可以直接交给 querycore.execute 的固定请求；**任何一处不确定都交回 None**，由调用方原样走旧的 Agent 路径（不向用户
# 多说什么、也不澄清）。
#
# 判「这句话在问哪个指标」用**拆完之后一字不差**，不用包含、也不用模糊相似：先从原话里去掉明确完整的日期/单月
# （`_auto_remove_dates`，和参数映射同一批写法）、句子里点到的口径/工作流/卡名（`_auto_remainder` 的提示）、以及两端的
# 通用疑问/查询/统计措辞（`_auto_trim`），剩下的**业务语义**必须和某个指标的 name / 唯一 synonym 完全相等。
# 「销售额增长率」拆完是「销售额增长率」，和「销售额」不等 → 不直达（增长率/占比/同比/环比/拆解/趋势/明细/排行这些
# 分析意图词**刻意不算通用措辞**，见 `_AUTO_ANALYSIS_WORDS`）；「2026年10月销售额是多少」「查一下销售额」拆完都是
# 「销售额」→ 直达。跨卡同名或一句指到不止一个指标也不自动；不按整卡、不问模型。
# --------------------------------------------------------------------------

#: 归一化时去掉的字符（空白与常见标点）：只用来对比说法，不改变原话
_AUTO_PUNCT = re.compile(r"[\s\u3000，,。.、；;：:！!？?（）()【】\[\]「」『』\"'“”‘’《》<>~·\-_/]+")
#: 明确完整的单日：2026-10-01 / 2026年10月1日
_AUTO_DAY_CN = re.compile(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]")
_AUTO_DAY_DASH = re.compile(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})(?![\d-])")
#: 明确完整的单月：2026-10 / 2026年10月（份）（后面不接日）
_AUTO_MONTH_CN = re.compile(r"(?<!\d)(\d{4})\s*年\s*(\d{1,2})\s*月份?(?!\s*\d{1,2}\s*[日号])")
_AUTO_MONTH_DASH = re.compile(r"(?<!\d)(\d{4})-(\d{2})(?![\d-])")

#: 通用疑问、查询与统计措辞：识别前先从问题**两端**去掉这些整词（只去两端，不去中间，免得把指标名里的话拆碎）。
#: 这些词不承载业务语义，去掉之后剩下的才是「这句话在问哪个指标」。
_AUTO_FILLERS: tuple[str, ...] = (
    # 疑问
    "是多少", "有多少", "是几", "多少", "数值", "数字", "数据", "结果", "答案",
    # 查询 / 统计
    "查询一下", "查询", "查一下", "查一查", "查查", "看一下", "看下", "看看", "瞧瞧",
    "统计一下", "统计", "汇总一下", "汇总", "合计", "总计", "算一下", "计算", "给出", "得到",
    # 客套 / 语气 / 指代
    "请问", "麻烦", "帮我", "帮忙", "给我", "告诉我", "我想知道", "想知道", "问一下", "问下",
    "这个", "那个", "一下", "呢", "吗", "吧", "啊", "的", "了",
)
#: 长词先去掉：先认「是多少」再认「多少」，长说法里的短说法不会被再拆一遍
_AUTO_FILLERS_SORTED: tuple[str, ...] = tuple(sorted(_AUTO_FILLERS, key=len, reverse=True))
#: 额外的分析意图词（增长率、占比、同比、环比、拆解、趋势、明细、排行……）：**刻意不算通用措辞**——它们留在剩余业务
#: 语义里，使「和指标名一字不差」不成立，于是回旧路径。列在这里是为了让人一眼看到这条边界（测试也核对它俩不相交）。
_AUTO_ANALYSIS_WORDS: tuple[str, ...] = (
    "增长率", "增速", "增幅", "降幅", "增长", "占比", "比重", "比例", "百分比", "同比", "环比", "趋势", "走势",
    "拆解", "分解", "下钻", "明细", "排行", "排名", "对比", "比较", "差异", "变化", "波动", "分布", "构成",
)

#: 开始/结束字段的取名提示（ASCII 按词边界比，中文按子串比）
_AUTO_START_HINTS: tuple[str, ...] = ("start", "begin", "from", "since", "开始", "起始")
_AUTO_END_HINTS: tuple[str, ...] = ("end", "until", "before", "结束", "截止")
#: 日期区间能落进去的字段类型（值写成 ISO 文本，别的类型承载不了）
_AUTO_DATE_TYPES = ("string", "date", "datetime", "time")


def _normalize(text: Any) -> str:
    """对比用的归一化：去空白与常见标点、转小写。只用来比说法，不改原话、不做同义改写。"""
    return _AUTO_PUNCT.sub("", str(text or "").lower())


def _auto_metric_terms(metric: Mapping[str, Any]) -> list[str]:
    """一个指标参与匹配的说法：name 与 synonyms（只收非空字符串）。"""
    out: list[str] = []
    for value in [metric.get("name"), *(metric.get("synonyms") or [])]:
        text = str(value or "").strip()
        if text and text not in out:
            out.append(text)
    return out


async def _auto_cards(session: Any) -> list[dict[str, Any]]:
    """每个工作流**推荐发布版**（Workflow.published_version，且确实已发布未退役）里的源头口径卡，一张卡一条。

    只读版本行、不查业务库、不调模型。推荐指针指不到一条已发布的版本（空指针、或指着已退役/没发布的）时**跳过这个
    工作流**：宁可少自动一次，也不替它猜一版。"""
    from sqlalchemy import select

    from app.apps.versions import is_published
    from app.db.models import Workflow, WorkflowVersion

    rows = (await session.execute(
        select(Workflow, WorkflowVersion)
        .join(WorkflowVersion, WorkflowVersion.workflow_id == Workflow.id)
        .where(WorkflowVersion.version == Workflow.published_version))).all()
    out: list[dict[str, Any]] = []
    for workflow, row in rows:
        if not is_published(row, current=workflow.published_version) or row.retired_at is not None:
            continue
        for node in (row.graph or {}).get("nodes") or []:
            if not isinstance(node, Mapping) or node.get("type") != "metrics":
                continue
            data = node.get("data") if isinstance(node.get("data"), Mapping) else {}
            config = data.get("config") if isinstance(data.get("config"), Mapping) else {}
            if config.get("caliber_from") not in (None, "", {}):
                continue
            metrics = [dict(m) for m in config.get("metrics") or [] if isinstance(m, Mapping) and m.get("id")]
            if not metrics:
                continue
            out.append({"workflow_id": workflow.id, "workflow_name": str(workflow.name or ""),
                        "version": int(row.version), "card_id": str(node.get("id") or ""),
                        "card_title": str(data.get("label") or node.get("id") or ""),
                        "caliber": str(config.get("caliber") or ""), "metrics": metrics})
    return out


def _auto_hinted(ref: Mapping[str, Any], question_norm: str) -> bool:
    """这句话里点到了这个指标所在的工作流/口径/卡的名字（唯一提示用）。"""
    for phrase in (ref.get("workflow_name"), ref.get("caliber"), ref.get("card_title")):
        normalized = _normalize(phrase)
        if len(normalized) >= 2 and normalized in question_norm:
            return True
    return False


def _auto_date_spans(text: str) -> list[tuple[int, int]]:
    """原话里明确完整的单日/单月的位置（去掉它们之后剩下的才是业务语义）。和参数映射用的是同一批写法。"""
    spans: list[tuple[int, int]] = []
    for pattern in (_AUTO_DAY_CN, _AUTO_DAY_DASH, _AUTO_MONTH_CN, _AUTO_MONTH_DASH):
        spans += [(match.start(), match.end()) for match in pattern.finditer(text)]
    return sorted(spans)


def _auto_remove_dates(text: str) -> str:
    out, cursor = "", 0
    for start, end in _auto_date_spans(text):
        if start < cursor:
            continue                         # 重叠的取长的那一处
        out += text[cursor:start] + " "
        cursor = end
    return out + text[cursor:]


def _auto_trim(text: str) -> str:
    """把通用疑问/查询/统计措辞从**两端**反复去掉（长词先去）。两端之外的一个字都不动。"""
    out = text
    changed = True
    while changed and out:
        changed = False
        for phrase in _AUTO_FILLERS_SORTED:
            if out.startswith(phrase):
                out, changed = out[len(phrase):], True
            if out.endswith(phrase):
                out, changed = out[:-len(phrase)], True
    return out


def _auto_remainder(question: str, hints: Sequence[str]) -> str:
    """这句话剩下的**业务语义**（归一化后）：去掉明确完整的日期/单月、句子里点到的口径/工作流/卡名（`提示的`），
    再去掉两端的通用问法、查询、统计措辞。剩下的必须和某个指标 name / 唯一 synonym **一字不差**才谈得上高置信。"""
    norm = _normalize(_auto_remove_dates(question))
    for hint in sorted({_normalize(item) for item in hints}, key=len, reverse=True):
        if len(hint) >= 2 and hint + "的" in norm:
            norm = norm.replace(hint + "的", "")
    return _auto_trim(norm)


def _auto_match(cards: Sequence[Mapping[str, Any]], remainder: str, question_norm: str) -> list[dict[str, Any]]:
    """剩余业务语义与指标的 name / synonym **一字不差**才算命中；多一个字、少一个字都不算。

    指到不止一个指标（同名跨卡等）时用工作流/口径/卡的**唯一提示**收窄；收窄之后还是不止一个就交回空（回旧路径）。
    不做模糊相似、不做包含匹配、不按整卡。"""
    if len(remainder) < 2:
        return []
    matched: dict[tuple[str, str, str], dict[str, Any]] = {}
    for card in cards:
        for metric in card["metrics"]:
            if not any(_normalize(term) == remainder for term in _auto_metric_terms(metric)):
                continue
            matched[(card["workflow_id"], card["card_id"], str(metric["id"]))] = {
                **card, "metric_id": str(metric["id"]), "metric": metric}
    if len(matched) > 1:
        marked = [ref for ref in matched.values() if _auto_hinted(ref, question_norm)]
        return marked if len(marked) == 1 else []
    return list(matched.values())


def _auto_hits(name: str, hints: Sequence[str]) -> bool:
    """字段名命中了哪一类提示：ASCII 提示按词边界比（total 不算 to），中文提示按子串比。"""
    lowered = str(name or "").lower()
    for hint in hints:
        if hint.isascii():
            if re.search(rf"(?:^|[^a-z]){re.escape(hint)}(?:[^a-z]|$)", lowered):
                return True
        elif hint in lowered:
            return True
    return False


def _auto_interval_fields(fields: Sequence[Mapping[str, Any]]) -> tuple[str | None, str | None]:
    """入口字段里配对的那个开始/结束日期字段：各只有一个、名字不重、类型承载得了 ISO 日期才算，否则不算（不猜）。"""
    from app.ledger import params

    starts: list[str] = []
    ends: list[str] = []
    for field in fields:
        name = str(field.get("name") or "").strip()
        if not name or params.field_type(field) not in _AUTO_DATE_TYPES:
            continue
        is_start = _auto_hits(name, _AUTO_START_HINTS)
        is_end = _auto_hits(name, _AUTO_END_HINTS)
        if is_start == is_end:
            continue                         # 都不像、或又像开始又像结束（起止时间）：不拿来配对
        (starts if is_start else ends).append(name)
    if len(starts) == 1 and len(ends) == 1 and starts[0] != ends[0]:
        return starts[0], ends[0]
    return None, None


def _auto_date_interval(text: str) -> tuple[str, str] | None:
    """原话里明确完整的单日/单月 → 半开区间 (含, 不含)。

    单日 `2026-10-01` / `2026年10月1日` → [当天, 次日)；单月 `2026-10` / `2026年10月` → [当月 1 日, 次月 1 日)。
    别的（「上个月」「上个季度」「最近一周」）都交回 None——相对说法要按当下时刻算，这里不替它算。"""
    day = _AUTO_DAY_CN.search(text) or _AUTO_DAY_DASH.search(text)
    if day is not None:
        try:
            start = date(int(day.group(1)), int(day.group(2)), int(day.group(3)))
        except ValueError:
            return None
        return start.isoformat(), date.fromordinal(start.toordinal() + 1).isoformat()
    month = _AUTO_MONTH_CN.search(text) or _AUTO_MONTH_DASH.search(text)
    if month is not None:
        year, number = int(month.group(1)), int(month.group(2))
        if not 1 <= number <= 12:
            return None
        first = date(year, number, 1)
        after = date(year + 1, 1, 1) if number == 12 else date(year, number + 1, 1)
        return first.isoformat(), after.isoformat()
    return None


def _auto_inputs(selected: Any, text: str) -> dict[str, Any] | None:
    """这次要交给 QueryCore 的入口取值：能拿全（含声明的缺省值）且过得去真实校验才交回，否则 None。

    用派生收窄出来的**这次真正会用到的**入口字段与取数节点判，和 querycore 执行时同一套：`params.resolve` 定值（显式
    非空优先、否则声明缺省值）、`params.check_nodes` 判完整性/承载、`apps.forms.validate_form` 判必填与类型。任何一处
    过不去就交回 None——缺不能确定的日期或非日期必填值都回旧路径，绝不带着不确定的参数去执行。"""
    from app.apps.forms import input_schema, validate_form
    from app.ledger import params

    fields = [dict(f) for f in selected.entry_fields]
    inferred: dict[str, Any] = {}
    interval = _auto_date_interval(text)
    if interval is not None:
        start_field, end_field = _auto_interval_fields(fields)
        if start_field is None or end_field is None:
            return None                      # 有明确日期，却没有能配对的开始/结束字段：不猜
        inferred[start_field], inferred[end_field] = interval
    values = params.resolve(fields, inferred)
    if params.check_nodes(selected.fetches, fields=fields, values=values,
                          where="自动路由的入口声明的字段"):
        return None
    if validate_form(input_schema(fields), values):
        return None
    return values


def _permitted(allowed: Any) -> set[tuple[str, int]] | None:
    """调用方给的授权范围归一化：`(workflow_id, version)` 的集合（或 dict 的键，usable_calibers 就是那种字典）。

    None 表示**调用方已显式放行**（搭建者 / admin / 关登录）：它们本来就能直接按口径执行，不按资产授权收窄。认不出来
    的条目直接跳过——授权信息读不出来的那一条不当成有权。"""
    if allowed is None:
        return None
    keys = allowed.keys() if isinstance(allowed, Mapping) else allowed
    out: set[tuple[str, int]] = set()
    for item in keys or ():
        if isinstance(item, str) or not isinstance(item, Sequence) or len(item) != 2:
            continue
        workflow_id, version = item
        try:
            out.add((str(workflow_id), int(version)))
        except (TypeError, ValueError):
            continue
    return out


async def auto_route(session: Any, *, question: str, purpose: str | None = None,
                     allowed: Any = None) -> Route | None:
    """普通问数的**保守默认路由**：高置信确定的请求交回一条可以直接交给固定执行的请求，否则 None。

    只在下面每一件都说得清时才命中：按推荐发布版的指标 name / 唯一 synonym 规范化精确匹配**唯一**一个指标（跨卡同名或
    一句指到不止一个就不自动）；这句话没有含糊的业务条件、也没有和那次口径声明的算法对不上；派生出来的这次入口取值
    拿得全、类型可证。任何一处不确定（含指不到、派生不出、版本没发布）都交回 None——调用方原样走旧的 Agent 路径，
    不向用户多说什么、也不替它挑一份口径。**零模型**：只看库里的发布版与图，不问模型也不做模糊相似。

    `allowed` 是这个人能用的 `(workflow_id, version)` 集合（资产授权，见 `_permitted`）：匹配照旧按**全部发布口径**看，
    所以「没匹配到」和「命中了但没权」分得开——唯一命中却不在 allowed 里交回**终止型**的 `ROUTE_REFUSED` /
    `ROUTE_NOT_PERMITTED`，调用方照实说没有授权，**不回退旧的 Agent 路径**（那条路会去翻底表）。allowed 为 None 是调用方
    已放行（搭建者及以上），不做这道闸。歧义、参数不全、没匹配到仍然交回 None，由调用方走旧路。"""
    from app.ledger import derive, querycore

    text = str(question or "").strip()
    if not text:
        return None
    purpose = str(purpose or querycore.DEFAULT_PURPOSE)
    permitted = _permitted(allowed)
    cards = await _auto_cards(session)
    hints = [item for card in cards for item in (card["workflow_name"], card["caliber"], card["card_title"])]
    candidates = _auto_match(cards, _auto_remainder(text, hints), _normalize(text))
    if len(candidates) != 1:
        return None
    chosen = candidates[0]
    # 授权闸在最前面：唯一命中但这个人的授权范围里没有这一版时，结果只能是「没授权」，不能落到下面任何一条会把这句话
    # 交给旧 Agent 路径（去翻底表）的分支上
    if permitted is not None and (str(chosen["workflow_id"]), int(chosen["version"])) not in permitted:
        return Route(ROUTE_REFUSED, ROUTE_NOT_PERMITTED, NOT_PERMITTED)
    # 含糊业务条件、或和这次口径声明的算法对不上：都不自动（交给旧路径，不在这里澄清、不改口径）
    ignore = _metric_mentions(chosen["metrics"])
    matches, clarifications = match_conditions(text, ignore=ignore)
    if clarifications:
        return None
    if matches and _condition_problem(matches, chosen["metrics"]) is not None:
        return None
    # 这次入口取值：按派生收窄出来的字段与节点判，拿不全就不自动
    try:
        pinned = await derive.read_pinned(session, chosen["workflow_id"], chosen["version"], chosen["card_id"])
        selected = derive.select_pinned(pinned, metric_ids=[chosen["metric_id"]])
    except derive.NotDerivable:
        return None
    inputs = _auto_inputs(selected, text)
    if inputs is None:
        return None
    return Route(ROUTE_EXECUTE, ROUTE_AUTO,
                 f"自动命中固定口径「{chosen['caliber'] or chosen['card_title']}」的指标，直接按固定口径执行，不重复解析/规划",
                 request={"workflow_id": chosen["workflow_id"], "card_id": chosen["card_id"],
                          "metric_ids": [chosen["metric_id"]], "inputs": inputs,
                          "version": chosen["version"], "purpose": purpose})


__all__ = ["KIND_AMBIGUOUS", "KIND_MISSING", "KIND_NO_MATCH", "NOT_PERMITTED", "PROPOSAL_AMBIGUOUS",
           "PROPOSAL_NONE", "ROUTE_AMBIGUOUS", "ROUTE_AUTO", "ROUTE_CLARIFY", "ROUTE_CONDITION_MISMATCH",
           "ROUTE_EXECUTE", "ROUTE_EXPLORE", "ROUTE_METRICS_REQUIRED", "ROUTE_NO_CALIBER", "ROUTE_NOT_PERMITTED",
           "ROUTE_READY", "ROUTE_REFUSED", "ROUTE_VERSION_INVALID", "Clarification", "FieldMatch", "Proposal",
           "Route", "auto_route", "declared_capabilities", "from_model", "match_conditions", "preview", "propose",
           "route"]
