"""SQL 模板参数完整性：一处认模板里的参数，各入口同一套语义（开工任务书 v3 · K01，§4.4 参数完整性）。

**一条规则，几个入口。** 口径的取数 SQL 模板里参数写成 `{{ input.字段 }}`。试跑（api/ledger 的 trial，含草稿试跑）、
组合式问数（ledger/compose）、派生图（ledger/derive 的 probe / reconcile / recall）和运行时的数据源查询
（engine/nodes/tools.run_tool）都按这里的一套判断：**缺参数、未声明参数、认不出的写法，在渲染之前就拒，绝不发出一条
少了个值的 SQL**（以前 `{{ input.store }}` 取不到值时渲染成空串，`AND region = ''` 照样发出去）。

正式出具（ledger/issue）与召回计划（ledger/recall）都接了这里的参数完整性（check_nodes：issue 在代填之前拒、
recall 在计划这一步判「这一组重算得了吗」），并且各留了一把自己的分寸（issue 跳过认不出写法的取数模板、
recall 两版各自判）。

| 情况 | 行为 |
| --- | --- |
| 必需参数没给值、也没有缺省值 | missing_parameter |
| 可选参数没给值、也没声明的缺省值 | missing_parameter（不能当空串） |
| 显式给了空串、字段也没声明的缺省值 | missing_parameter——空串不是值，是省略（入口收值 provided 与运行时的存在性判断同一套） |
| SQL 引用了入口没声明的字段 | undeclared_parameter |
| 参数写在表名、列名里，或用了倒推不出值的写法（vars.、模板函数、别的过滤器） | unsupported_parameter |
| 引号里的值带引号 / 反斜杠 / 换行，裸写的不是数，注释里的不能带换行或 `*/` | literal_unsupported |
| 可选参数声明了缺省值（含 `""`：作者写的就是「空值不过滤」） | 按缺省值渲染，不算缺参数 |

（还有一层在图那边：engine/variables 会拦「引用了没人产出的 `{{ input.x }}`」的节点，那一条在运行发起前就红——组合式问数
拷进来的节点因此**保留不了模板**，只能渲染成字面语句 + 这里的参数契约；那条校验按同一份契约跳过 args / sql，见下。）

**运行时的兜底认哪些写法。** 取数 SQL 在发出之前按 `input` 引用过一遍参数完整性，认法和
engine/expressions.resolve_path 一致：`input.字段`、`input['字段']`、`input["字段"]`、`input[0]`、
`input.rows[0].name` 都算，取的是第一层的那个字段（下标里的字是字面量，resolve_path 取的就是字面量）。认不出取哪个
字段的写法——光秃秃的 `{{ input }}`、`input['a.b']`、`input[a[b]]`、`input x`——resolve_path 取不到值、会渲染成空串，
SQL 执行路径按 unsupported_parameter 直接拒，不当「没引用参数」放过去。vars. / nodes. 这些别的命名空间不在这趟里
（照旧由画布的变量校验管）；口径模板那边的严格解析（slots_of，只认 `{{ input.字段 }}`）没有变。

**这次的值怎么定（resolve）。** 显式给的（不是 None、不是空串）优先，否则用声明的缺省值——`default` 给了就用，**含 `0`
与空串**（`resolve`）；检查、渲染、快照材料里的输入哈希用的都是这一份。已经自己补过缺省值的入口（试跑、组合式问数）传进来
的就是补好的 values，结果不变；`plan()` 这条共享层接口按这一份定值：字段声明了 `default="east"` 而调用方没给值时，渲染出来
的是 `east`，不是空串（`default=0` 同理，渲染出来是 `0`）。

**值只当数据，只经历一次模板替换。** 本仓库的数据源适配器是「内联文本替换」：值渲染进语句文本，没有驱动级参数绑定
（ADAPTER = "inline_text"，快照里照实写，不假装是驱动绑定）。所以值必须**只被模板替换一次**——渲染进模板的那一次；渲染
出来的结果不会再被当模板解释（`{{ vars.x }}` 这样的文本落在值里就是字面量）。为此：

- 组合式问数（ledger/compose）在拷节点时把值渲染进 SQL（只有这一次），同时在节点上写一份参数契约（见 marker）：
  args 里已经是字面语句，运行时（engine/nodes/tools.run_tool）的模板渲染一概不碰它——不然值里的 `{{ vars.question }}`
  会在运行时被当成模板替换成那个变量的值，SQL 被数据改掉；
- 对账图里「这次的取数」放进去的是**执行过的 SQL 原文**；按维度值对齐的合并语句（ledger/reconcile.align_sql）是服务端
  按查询结果拼的字面 SQL：两处都写 marker，运行时的模板渲染一概不碰它们（tools.run_tool / merge.run_merge）；
- 画布校验（engine/variables）按 literal_keys 跳过带契约节点的 args / sql：里面是数据，不按变量引用判——否则值里恰好
  有几个花括号，就等于那张图跑不起来。

**参数契约（marker）与它注解的那条语句绑定。** 写进节点 `data.config[KEY]` 的那一份记：承载方式（内联）、**rendered_hash**
（它注解的那一项——tool 的 `args`、merge 的 `sql`——的确定哈希）、源模板哈希、规范化输入哈希、参数契约（字段、类型、
缺省值）与每个参数写在哪儿（quoted / bare / comment）、出处。绑定值不另存一份：它就是内联进实际执行语句里的那些字，实际
语句（payload["sql"]）本身落进快照，展示用 SQL 不作为执行证明。

**哈希对不上，契约就作废。** 节点会被复制、会被改：改过 args / sql 之后，契约说的已经不是这一条语句了。所以
`literal_keys()` 一律先核对 rendered_hash——对得上才跳过渲染、才跳过画布校验的模板扫描、才把契约材料记进快照；对不上
（复制后改了、改回普通模板、只是改了个别的参数）就当**没写契约**：正常渲染、正常判参数，画布的变量校验也照常扫。
绝不能因为节点上留着一份旧契约，就把改过的、带 `{{ }}` 的语句原样发出去。

**快照材料**里的源模板正文不写：engine/variables 会把节点配置里每一段带 {{ }} 的字都当活引用扫，正文留在节点上会把取数
模板的占位符报成「没有任何节点产出」。出处（source）记的是那一版取数节点的固定引用，**本切片只把它当地址写下来**——没有
实现「按引用自动还原模板并核对哈希」，要复核得照着这个引用去读那一版。

**还没做的**（照实说，不当成已完成）：驱动级参数绑定（占位符交给驱动、绑定值单独传）只对试点方言有意义，本切片没有
实现，所以含引号、反斜杠、换行的值仍然明确拒绝（literal_unsupported），不能因此宣布特殊字符与各类型都已支持；类型契约
只做到规范化声明类型和值类别（R02 那类「同名字段声明成不同数字类型怎么合并」的规则在 ledger/draftplan._merged_type，
不在这里）。
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

#: 参数问题的机器码（§4.4 的三条 + 值承载不了的一条）
MISSING = "missing_parameter"
UNDECLARED = "undeclared_parameter"
UNSUPPORTED = "unsupported_parameter"
LITERAL = "literal_unsupported"

CODES = (MISSING, UNDECLARED, UNSUPPORTED, LITERAL)

#: 承载参数的方式：驱动不提供参数绑定，值一律内联进语句文本（旧适配器）。快照按它照实写
ADAPTER = "inline_text"

#: 节点配置里写参数契约的键（ledger/compose、ledger/derive、ledger/reconcile 写，engine 侧按它认）
KEY = "parameters"
#: args / sql 里已经是渲染好的字面语句（值渲染进去的那一次已经发生过）：运行时的模板渲染不再碰它
LITERAL_ARGS = "literal"
#: 契约里记的、它注解的那一项（tool 的 args / merge 的 sql）的确定哈希：对不上就说明契约过时了
HASH_KEY = "rendered_hash"
#: 带参数契约的节点上，「已经渲染成字面值」的那一项在哪个键上（按节点类型）：调用工具的 args、合并查询的 sql。
#: engine/variables 扫模板引用、engine/nodes/* 渲染参数时都要跳过它们——里面是数据，不是活模板
LITERAL_KEY = {"tool": "args", "merge": "sql"}

#: 运行时查询快照记参数材料时，ToolContext.extra 里用的键（tools/datasource 读）
SNAPSHOT_KEY = "sql_parameters"

#: 值写在哪儿（data/sql_semantics._template_slots 的 context）：引号里的字 / 裸写 / 注释里 / 表名列名
CONTEXTS = ("quoted", "bare", "comment", "name")
#: 参数能带的过滤器（和 data/sql_semantics._SLOT_FILTERS 同一份）：别的过滤器倒推不回值
FILTERS = frozenset({"trim", "compact"})

#: 写给人和模型的几句话（各入口同一句，改就一处改）
MISSING_TEXT = "参数「{name}」这次没有值：{where}没给值、也没有缺省值，不能当空串用（missing_parameter）"
UNDECLARED_TEXT = "SQL 模板引用了没有声明的参数「{name}」：{where}是 {fields}（undeclared_parameter）"
UNSUPPORTED_TEXT = "SQL 模板里的参数认不出来：{reason}（unsupported_parameter）"
#: 引用了入口、但看不出取的是哪个字段的写法：resolve_path 取不到值 → 会渲染成空串，SQL 执行路径照实拒
DYNAMIC_TEXT = ("SQL 模板里的入口引用「{{{{ {expr} }}}}」看不出取的是哪个字段（这样写会渲染成空串）："
                "请写成 {{{{ input.字段 }}}}，点号与 {{{{ input['字段'] }}}} 都行，下标里要是字面量（unsupported_parameter）")
QUOTED_TEXT = "参数「{name}」写在 SQL 的引号里，值里不能有引号、反斜杠或换行（literal_unsupported）"
BARE_TEXT = "参数「{name}」直接写在 SQL 里（没有引号），只能是数，当前为「{value}」（literal_unsupported）"
COMMENT_TEXT = "参数「{name}」写在 SQL 的注释里，值里不能有换行或注释结束符（literal_unsupported）"
#: 取数模板根本读不出来（模板语法、引号没配对上）时套在原因外面的那一句
UNREADABLE_TEXT = "取数节点的 SQL 模板认不出参数写在哪：{reason}"

_NUMBER = re.compile(r"^\s*-?\d+(?:\.\d+)?\s*$")
#: 运行时按它挑出模板里的 input 引用（宽松：只认 {{ input.字段 }}，别的模板写法不归参数完整性管）


class ParameterRefused(Exception):
    """参数不合规：不渲染、不发 SQL。problems 给程序认（机器码），message 给人看。"""

    def __init__(self, problems: Sequence[Problem]) -> None:
        self.problems = tuple(problems)
        super().__init__(describe(self.problems))

    @property
    def code(self) -> str:
        return self.problems[0].code if self.problems else UNSUPPORTED

    @property
    def message(self) -> str:
        return str(self)


@dataclass(frozen=True)
class Slot:
    """模板里的一处 `{{ input.字段 }}`：第几处、字段、过滤器、写在哪儿。"""

    index: int
    field: str
    filters: tuple[str, ...] = ()
    context: str = "bare"

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.field, "filters": list(self.filters), "slot": self.context}


@dataclass(frozen=True)
class Problem:
    """一处参数问题：机器码 + 给人看的一句话（含节点，便于在画布上定位）。"""

    code: str
    message: str
    name: str | None = None
    node_id: str | None = None
    where: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "name": self.name, "node_id": self.node_id, "message": self.message}


@dataclass(frozen=True)
class Statement:
    """一次取数的结果：渲染好的字面语句 + 进查询快照的参数材料（源模板、两种哈希、参数类型与写在哪儿）。"""

    statement: str
    material: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:  # pragma: no cover - 只为人读
        return self.statement


def describe(problems: Iterable[Problem]) -> str:
    """几处问题合成一句话（给人和模型看的那句）。"""
    return "；".join(p.message for p in problems)


# ==========================================================================
# 认得模板里的参数
# ==========================================================================


def slots_of(template: str, *, dialect: str | None = None) -> tuple[Slot, ...]:
    """模板里每一处 `{{ input.字段 }}`（含写在哪儿、带了什么过滤器）。认不出写法、写在表名列名里的抛
    ParameterRefused(unsupported_parameter)——判断和带值比对那一套（data/sql_semantics._template_slots）是同一份。"""
    from app.data.sql_semantics import _NotComparable, _template_slots

    try:
        spans, _ = _template_slots(template, dialect=dialect)
    except _NotComparable as e:
        raise ParameterRefused([Problem(UNSUPPORTED, str(e))]) from None
    return tuple(Slot(index=s.index, field=s.field, filters=tuple(s.filters), context=s.context) for _, _, s in spans)


def input_refs(template: str) -> tuple[str, ...]:
    """模板里按 input 取值、而且**认得出取的是哪个字段**的那些引用，按出现先后去重。

    认法和 engine/expressions.resolve_path 一个走法：`input.字段`、`input['字段']`、`input["字段"]`、`input[0]`、
    `input.rows[0].name` 都算（下标里的字是字面量——resolve_path 取的就是字面量，不做表达式求值），取的是第一层的
    那个字段。非 input 的模板写法（vars.、nodes.、模板函数的具体实现）不归这里管：普通画布上的取数 SQL 照样能引用
    运行时的值。

    认不出的写法（`{{ input }}`、`input['a.b']`、`input[a[b]]`……）**不在这里**——它们取不到值、会渲染成空串，由
    dynamic_input_writings 交出来，SQL 执行路径按 unsupported_parameter 拒。"""
    return tuple(dict.fromkeys(field for field in _input_fields(template) if field))


def dynamic_input_writings(template: str) -> tuple[str, ...]:
    """模板里引用了 input、但**认不出取的是哪个字段**的写法，原样一份（按出现先后去重）。

    resolve_path 对这些写法取不到值（返回 None），模板渲染出来是空串：SQL 执行路径不能把它当「没引用参数」放过去。"""
    return tuple(dict.fromkeys(writing for _, writing in _scanned_inputs(template) if writing))


def mentions_input(template: str) -> bool:
    """模板里有没有按 input 取值的地方（认得出的与认不出的都算）：SQL 执行路径按它决定要不要过参数完整性。"""
    return bool(input_refs(template) or dynamic_input_writings(template))


#: 模板里一处 {{ … }}（和 engine/expressions 的 _TEMPLATE_RE 同一个写法）
_TEMPLATE_EXPR = re.compile(r"\{\{(.*?)\}\}", re.DOTALL)
#: 路径的一段：名字 + 若干 [下标]（和 engine/expressions.resolve_path 同一个写法）
_SEGMENT = re.compile(r"^([^\[\]]*)((?:\[[^\[\]]+\])*)$")
_INDEX = re.compile(r"\[([^\[\]]+)\]")
#: 表达式开头是不是 input 这个命名空间（`input.x` / `input['x']` / `input[0]` / 光秃秃的 `input`）。
#: 用词边界：`inputs`、`input_value` 这类别的名字不算。写成 `input x` 这种（以 input 起头但合不上路径走法）
#: 也归到这里，由 _scanned_inputs 判成「认不出取哪个字段」——resolve_path 对它同样取不到值。
_INPUT_HEAD = re.compile(r"^input\b")


def _lookups(path: str) -> list[str] | None:
    """按 resolve_path 的走法把一条路径拆成一层层的取法（名字 / 下标里的字），写不进那套写法返回 None。

    resolve_path 是「按点切段、每段再拆名字与若干下标」，认不出的一段就整个取不到值——这里逐字照搬那一套，
    所以「这里认得出」= 「resolve_path 取得到值」。"""
    out: list[str] = []
    for raw in path.split("."):
        raw = raw.strip()
        if not raw:
            continue
        m = _SEGMENT.match(raw)
        if m is None:
            return None
        name, idx_part = m.group(1).strip(), m.group(2)
        if name:
            out.append(name)
        out += [idx.strip().strip("'\"") for idx in _INDEX.findall(idx_part)]
    return out


def _template_path(expr: str) -> str:
    """一处 `{{ … }}` 里的路径部分（去掉过滤器）；模板函数按它的第一个参数认（同 engine/variables._root_of）。"""
    from app.engine.expressions import template_call

    path = expr.split("|")[0].strip()
    if "(" in path and (call := template_call(path)) is not None:
        return call[1][0] if call[1] else ""
    return path


def _scanned_inputs(template: str) -> list[tuple[str, str]]:
    """模板里每一处按 input 取值的地方：[(字段名, 认不出的原样写法)]，两者只有一个非空。

    认得出字段的给 (字段名, "")；认不出取哪个字段的（会渲染成空串）给 ("", 那处表达式原文)。**范围是「以 input 起头的
    那几条路径」**：不是 input 的引用（vars.、nodes.、别的模板函数、别的根名字）两边都空，不在这趟里——那些由画布的
    变量校验管，取数 SQL 照旧能引用它们。"""
    out: list[tuple[str, str]] = []
    for m in _TEMPLATE_EXPR.finditer(template or ""):
        expr = m.group(1).strip()
        path = _template_path(expr)
        if not path or not _INPUT_HEAD.match(path):
            continue
        lookups = _lookups(path)
        if lookups is None or lookups[0] != "input" or len(lookups) < 2:
            # 认不出取的是哪个字段（写法不合 resolve_path、或者就是光秃秃的 {{ input }}）：会渲染成空串
            out.append(("", expr or path))
            continue
        out.append((lookups[1], ""))
    return out


def _input_fields(template: str) -> list[str]:
    return [field for field, _ in _scanned_inputs(template)]


def declared(fields: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """入口字段 → {字段名: 字段（原样的一份）}。没名字的不要；同名的取第一个（和 derive.entry_fields 一个认法）。"""
    out: dict[str, dict[str, Any]] = {}
    for f in fields:
        name = f.get("name")
        if isinstance(name, str) and name.strip():
            out.setdefault(name.strip(), dict(f))
    return out


#: 声明类型 → 规范类型。认不出的一律 string（各入口同一套：类型判定不依赖字段先后，R02 的一半）
_TYPES = {"string": "string", "text": "string", "integer": "integer", "int": "integer", "number": "number",
          "float": "number", "decimal": "number", "boolean": "boolean", "bool": "boolean", "date": "date",
          "datetime": "datetime", "time": "time", "file": "file", "array": "array", "object": "object"}


def field_type(field: Mapping[str, Any]) -> str:
    """字段声明的参数类型，规范成一个词。没写、认不出的按 string（不按值反推：同一个值在不同字段上含义不同）。"""
    return _TYPES.get(str(field.get("type") or "").strip().lower(), "string")


def value_kind(value: Any) -> str:
    """这次的实际值是什么类别：text / number / boolean / null / list / object。"""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float, Decimal)):
        return "number"
    if isinstance(value, str):
        return "text"
    if isinstance(value, (list, tuple)):
        return "list"
    if isinstance(value, Mapping):
        return "object"
    return type(value).__name__


def _names(names: Iterable[str]) -> str:
    items = list(names)
    return "、".join(items) if items else "（没有入口字段）"


def _given(value: Any) -> bool:
    """这个值算不算「给了」：None 与空串都不算——空串不是值，是省略。三处按同一份判：入口收值（provided）、
    缺参数判断（_unresolved / runtime_problems）、渲染前定值（resolve）。"""
    return value is not None and value != ""


def provided(values: Mapping[str, Any]) -> dict[str, Any]:
    """用户这次「给了值」的那些键：None 和空串都算没给——空串不是值，是省略。

    各入口收值用同一份（试跑 api/ledger.trial_caliber、草稿试跑、组合式问数 compose.check_inputs）：显式传了 `""`、
    字段又没声明缺省值时，接着判就是 missing_parameter（不能当空串用）；字段声明了 `default=""`（作者写的就是「空值不过滤」）
    的照缺省值渲染，不算缺（见 _unresolved）。"""
    return {str(k): v for k, v in values.items() if _given(v)}


def resolve(fields: Iterable[Mapping[str, Any]], values: Mapping[str, Any]) -> dict[str, Any]:
    """这次要渲染进去的那一份值：**显式给的（不是 None、不是空串）优先，否则用声明的缺省值**（`default` 给了就用——
    含 `0` 与空串）。

    共享层替入口把这一份定下来：检查、渲染、快照的输入哈希都用它。字段声明了 `default="east"` 而调用方没给值时，渲染出来
    的必须是 `east`——以前 plan() 直接拿原来的 values 渲染，声明了却渲染成空串。已经自己补过缺省值的入口（组合式问数、
    试跑）传进来的就是补好的 values，这里不改变它们的结果；入口没声明的键（开放入口：值由发起时传）原样留着。
    """
    out = provided(values)
    for name, field in declared(fields).items():
        if name in out:
            continue                     # 显式给了非空值：以它为准
        default = field.get("default")
        if default is not None:
            out[name] = default          # 否则用声明的缺省值（0 / 空串也算声明过的）
    return out


def _written(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, Decimal):
        from app.engine.expressions import _format_decimal

        return _format_decimal(value)
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, default=str)
    return str(value)


def _bare_scalar_ok(value: Any) -> bool:
    if isinstance(value, bool):
        return True
    if isinstance(value, int):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, Decimal):
        return value.is_finite()
    if isinstance(value, str):
        return bool(_NUMBER.match(value))
    return False


def _unresolved(name: str, field: Mapping[str, Any] | None, values: Mapping[str, Any]) -> bool:
    """这个参数这次没有可用的值：没给、给了 None、或者给了空串（空串不是值，是省略——和入口收值 params.provided 同一套），
    而且声明的缺省值也是 None。

    声明了缺省值的算「按声明省略」，不算缺——含 `""`：作者写的就是「空值不过滤」（`AND ('{{ input.x }}' = '' OR …)`），
    那是声明的省略规则，不是没给值。"""
    if _given(values.get(name)):
        return False
    return (field or {}).get("default") is None


# ==========================================================================
# 判断：模板、值
# ==========================================================================


def literal_problem(name: str, value: Any, contexts: Iterable[str]) -> str | None:
    """值写在 SQL 的哪个位置、旧适配器（内联文本替换）承载得了吗：承载不了给一句原因，承载得了为 None。

    引号里的字里再出现引号 / 反斜杠 / 换行、裸写的位置上不是有限数值或布尔值、注释里带换行或注释结束符——这些都会让渲染出来的语句
    不再等于「那一版口径的 SQL」。真支持特殊字符要等驱动级参数绑定（见模块说明），那之前明确拒绝，不硬塞。"""
    text = _written(value)
    where = set(contexts)
    if "quoted" in where and any(ch in text for ch in ("'", "\\", "\n", "\r")):
        return QUOTED_TEXT.format(name=name)
    if "bare" in where and not _bare_scalar_ok(value):
        return BARE_TEXT.format(name=name, value=text[:40])
    if "comment" in where and ("\n" in text or "\r" in text or "*/" in text):
        return COMMENT_TEXT.format(name=name)
    return None


def check_template(template: str, *, fields: Mapping[str, Mapping[str, Any]], values: Mapping[str, Any] | None = None,
                   node_id: str | None = None, where: str = "入口字段",
                   dialect: str | None = None) -> list[Problem]:
    """一条取数模板按声明的入口字段过一遍：未声明、认不出的写法；给了 values 的，再判缺值与值承载不了。

    values 为 None 是「此刻还没有这次调用的值」（读一版口径时就是这样）：只判模板和声明对不对得上。
    认不出写法（unsupported）时不接着往下判——值写在哪儿都还没定，别的结论不可靠。"""
    try:
        slots = slots_of(template, dialect=dialect)
    except ParameterRefused as e:
        return [Problem(UNSUPPORTED, UNREADABLE_TEXT.format(reason=p.message), node_id=node_id) for p in e.problems]
    problems: list[Problem] = []
    seen: set[str] = set()
    for slot in slots:
        name = slot.field
        if name in seen:
            continue
        seen.add(name)
        field = fields.get(name)
        if field is None:
            problems.append(Problem(UNDECLARED, UNDECLARED_TEXT.format(name=name, where=where,
                                                                      fields=_names(fields)),
                                    name=name, node_id=node_id, where=where))
        elif values is not None and _unresolved(name, field, values):
            problems.append(Problem(MISSING, MISSING_TEXT.format(name=name, where=where),
                                    name=name, node_id=node_id, where=where))
    if values is None:
        return problems
    seen_literal: set[str] = set()
    for name in (s.field for s in slots):
        field = fields.get(name)
        if field is None or name not in values or name in seen_literal:
            continue
        seen_literal.add(name)
        # 同一个字段的每一处槽位一起判（写在引号里 / 裸写 / 注释里各算一处），一个字段只报一条
        if problem := literal_problem(name, values[name], (s.context for s in slots if s.field == name)):
            problems.append(Problem(LITERAL, problem, name=name, node_id=node_id, where=where))
    return problems


def _sql_of(node: Mapping[str, Any]) -> str | None:
    args = ((node.get("data") or {}).get("config") or {}).get("args") if isinstance(node, Mapping) else None
    sql = args.get("sql") if isinstance(args, Mapping) else None
    return sql if isinstance(sql, str) and sql else None


def check_nodes(nodes: Iterable[Mapping[str, Any]], *, fields: Iterable[Mapping[str, Any]],
                values: Mapping[str, Any] | None = None, where: str = "入口字段",
                dialect: str | None = None) -> list[Problem]:
    """几个调用工具节点（原始 JSON）的 args.sql 各过一遍，问题按节点先后合起来。values 不给就只判模板与声明。"""
    declared_fields = declared(fields)
    out: list[Problem] = []
    for node in nodes:
        if not isinstance(node, Mapping):
            continue
        sql = _sql_of(node)
        if sql is None:
            continue
        out += check_template(sql, fields=declared_fields, values=values, node_id=str(node.get("id") or "") or None,
                              where=where, dialect=dialect)
    return out


def runtime_problems(template: str, *, values: Mapping[str, Any],
                     declared: Mapping[str, Mapping[str, Any]]) -> list[Problem]:
    """运行时的同一条判断（宽松认法）：模板里按 input 取值的地方这次取不到值就拒。

    认法和 engine/expressions.resolve_path 一致（`input.字段`、`input['字段']`、`input[0]`、`input.a.b[0].c` 都算，
    取的是第一层的字段）；认不出取哪个字段的写法（`{{ input }}`、`input['a.b']`、`input[a[b]]`）同样拒——它们渲染成
    空串，不能当「没引用参数」放过去。

    「有值」和入口收值（params.provided）同一套：**None 和空串都算没给**（空串不是值，是省略），取到别的值才放行。
    开放入口（入口节点没声明字段、键由发起时传）也一样——否则 `{{ input['store'] }}` 传了个空串就会渲染成 `''`，
    发出一条 `AND region = ''`。

    - 没给值、入口也没声明过它 → undeclared_parameter；
    - 没给值、声明过、声明的缺省值也是 None（含只声明成可选的）→ missing_parameter；声明了缺省值（含 `""`：作者写的
      就是「空值不过滤」）的按声明保留，照缺省值渲染。
    只看会不会渲染成空串：普通画布上的取数 SQL 引用 vars./nodes. 的不归它管。
    """
    problems: list[Problem] = [
        Problem(UNSUPPORTED, DYNAMIC_TEXT.format(expr=writing)) for writing in dynamic_input_writings(template)]
    for name in input_refs(template):
        if _given(values.get(name)):
            continue
        field = declared.get(name)
        # 这次运行的输入里有这个键（只是值空着）就不算「没声明」——那是这次没给值；连键都没有、入口也没声明才是没声明
        if field is None and name not in values:
            problems.append(Problem(UNDECLARED, UNDECLARED_TEXT.format(
                name=name, where="这次运行的入口", fields=_names(sorted(declared))), name=name))
        elif _unresolved(name, field, values):
            problems.append(Problem(MISSING, MISSING_TEXT.format(name=name, where="这次运行的入口"), name=name))
    return problems


# ==========================================================================
# 渲染与快照材料
# ==========================================================================


def render(template: str, values: Mapping[str, Any]) -> str:
    """模板按值渲染成字面语句。走的是运行时同一个渲染器（engine/expressions.render_template）：值替换进来就不再被
    模板解释一遍。这里只负责「值是我的、模板只过一次」，缺值缺声明的判断在 check_* / plan。"""
    from app.engine.expressions import render_template

    return render_template(template, {"input": dict(values)}) if "{{" in template else template


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def material(template: str, *, values: Mapping[str, Any], declared_fields: Mapping[str, Mapping[str, Any]],
             source: Mapping[str, Any] | None = None, with_template: bool = True,
             dialect: str | None = None) -> dict[str, Any]:
    """查询快照要记的那一份：源模板、源模板哈希、规范化输入哈希、参数契约、每个参数的类型与写在哪儿、承载方式。

    绑定值不在这里再存一份——内联方式下它就写在实际执行语句里，而实际语句本身在快照里（payload["sql"]）。

    with_template=False 时不写源模板正文，只留哈希（写进节点配置的那一份就是这样：engine/variables 会把节点配置里
    每一段带 {{ }} 的字都当成活引用扫一遍，正文留在节点上会把取数模板的占位符报成「没人产出」）。这一版口径的模板由
    source 里的固定引用（工作流 + 版本 + 节点）还原，哈希用来核对还原出来的还是不是当时那一份。"""
    names = input_refs(template)
    bound: list[dict[str, Any]] = []
    try:
        by_field = {s.field: s.context for s in slots_of(template, dialect=dialect)}
    except ParameterRefused:
        by_field = {}
    for name in names:
        field = declared_fields.get(name) or {}
        item: dict[str, Any] = {"name": name, "kind": value_kind(values.get(name)),
                                "declared": field_type(field) if field else None,
                                "slot": by_field.get(name)}
        bound.append(item)
    payload = {k: values.get(k) for k in names}
    out: dict[str, Any] = {
        "adapter": ADAPTER,
        "binding": "values_in_statement",
        "template_hash": _hash(template),
        "inputs_hash": _hash(json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)),
        "declared": contract(declared_fields.values()),
        "parameters": bound,
    }
    if with_template:
        out["template"] = template
    if source:
        out["source"] = dict(source)
    return out


def plan(template: str, *, fields: Iterable[Mapping[str, Any]], values: Mapping[str, Any],
         source: Mapping[str, Any] | None = None, node_id: str | None = None,
         where: str = "入口字段", dialect: str | None = None) -> Statement:
    """「现在就发一条 SQL」的入口用：先按声明的字段把这次的值定下来（resolve：显式非空值优先、否则声明缺省值），
    再判参数完整性，过了才渲染成字面语句，附快照材料——检查、渲染、材料里的输入哈希用的是**同一份 resolved values**。

    不合规抛 ParameterRefused（缺参数、未声明、认不出的写法、值承载不了），调用方据此回 4xx，别发 SQL。

    values 按同一套存在性语义收（None 与空串都算没给）：没过滤就传进来的调用方，等于把「显式空串」当值渲染出去——
    resolve 会先把它按「没给」处理（有声明缺省值就用缺省值，没有就判缺参数）。已经自己补过缺省值的入口传进来的是
    补好的 values，结果不变。

    注意：现在**没有正式入口调它**（试跑、组合式问数、派生图、正式出具各走自己的入口，都在发起/派生之前用
    check_nodes / check_template 判过）——它是给下一个入口（QueryCore 的固定执行）准备的共享层接口，行为由测试锁住。"""
    declared_fields = declared(fields)
    resolved = resolve(declared_fields.values(), values)
    problems = check_template(template, fields=declared_fields, values=resolved, node_id=node_id, where=where,
                              dialect=dialect)
    if problems:
        raise ParameterRefused(problems)
    return Statement(statement=render(template, resolved),
                     material=material(template, values=resolved, declared_fields=declared_fields, source=source,
                                       dialect=dialect))


# ==========================================================================
# 节点的参数契约（组合式问数、对账图写，运行时认）
# ==========================================================================


def marker(*, rendered: Any, template: str | None = None, values: Mapping[str, Any] | None = None,
           declared_fields: Iterable[Mapping[str, Any]] = (), source: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """写进节点 data.config[KEY] 的参数契约：**rendered 那一项（tool 的 args / merge 的 sql）已经是渲染好的字面语句**，
    运行时的模板渲染一概不碰它——不然值里的 `{{ }}` 会被再解释一遍（K01 的二次渲染反例）。

    rendered 必给：契约按它的哈希与节点上那一项绑定，节点被复制、被改过之后契约自动作废（见 literal_keys）。写契约的人
    必须把它要注解的那份内容原样交进来（和写进节点的必须是同一个对象）。

    template/values 给了就再把材料写上（源模板哈希、规范化输入哈希、参数契约、每个参数写在哪儿、出处）；**不写源模板
    正文**：engine/variables 会把节点配置里每一段带 {{ }} 的字都当活引用扫，正文留在节点上会把取数模板的占位符报成
    「没有任何节点产出」，那张图就跑不起来了。"""
    out: dict[str, Any] = {"mode": LITERAL_ARGS, "adapter": ADAPTER, HASH_KEY: rendered_hash(rendered)}
    if template is not None:
        out.update(material(template, values=values or {}, declared_fields=declared(declared_fields),
                            source=source, with_template=False))
    elif source:
        out["source"] = dict(source)
    return out


def contract(fields: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """参数契约：入口字段按声明顺序写成 {name, type, required, default}（快照与审核看得懂的那一份）。"""
    out: list[dict[str, Any]] = []
    for field in declared(fields).values():
        out.append({"name": field["name"], "type": field_type(field), "required": bool(field.get("required")),
                    **({"default": field["default"]} if field.get("default") is not None else {})})
    return out


def mode_of(config: Mapping[str, Any] | None) -> str | None:
    """契约里写的模式（data.config[KEY]）。**只看写了什么，不核对哈希**——判断要不要跳过渲染、要不要跳过画布校验，
    一律用 literal_keys / is_literal；这个只用来断言「服务端写下了契约」。"""
    marker_ = (config or {}).get(KEY)
    mode = marker_.get("mode") if isinstance(marker_, Mapping) else None
    return LITERAL_ARGS if mode == LITERAL_ARGS else None


def rendered_hash(value: Any) -> str:
    """契约注解的那一项（tool 的 args / merge 的 sql）的确定哈希。字典按键排序（canonical_json），字符串按原文。"""
    from app.core.artifact_store import canonical_json

    return _hash(value if isinstance(value, str) else canonical_json(value))


def literal_keys(config: Mapping[str, Any] | None, node_type: str | None) -> tuple[str, ...]:
    """这个节点上「服务端渲染好的字面语句」在哪个键上（tool 的 args / merge 的 sql）——**核对过哈希**才算，其余为空。

    契约是服务端写在节点上的，但节点会被复制、会被改：改过 args / sql 之后，契约说的已经不是这一条语句了。只有契约里的
    rendered_hash 与当前那一项对得上，才按 literal 处理（跳过渲染、跳过模板扫描、快照用契约里的材料）；对不上（复制后
    改了、改回普通模板、改了别的参数）一律当没写契约——正常渲染、正常判参数，画布的变量校验也照常扫。"""
    keys = LITERAL_KEY.get(str(node_type or ""))
    if not keys or mode_of(config) != LITERAL_ARGS:
        return ()
    marker_ = (config or {}).get(KEY)
    if not isinstance(marker_, Mapping):
        return ()
    if marker_.get(HASH_KEY) != rendered_hash((config or {}).get(keys)):
        return ()
    return (keys,)


def is_literal(config: Mapping[str, Any] | None, node_type: str | None) -> bool:
    """这个节点的 args / sql 是不是「服务端渲染好的字面语句，而且没被改过」：是就跳过渲染、跳过模板扫描。"""
    return bool(literal_keys(config, node_type))


def marker_material(config: Mapping[str, Any] | None, node_type: str | None) -> dict[str, Any] | None:
    """契约里要进查询快照的部分（去掉 mode 与 rendered_hash）：源模板哈希、规范化输入哈希、参数契约、参数类型与写在哪儿、
    出处。**只有核对过哈希才给**——过时的契约说的不是这一条语句，拿它当证据就是失真。"""
    if not is_literal(config, node_type):
        return None
    marker_ = (config or {}).get(KEY)
    return {k: v for k, v in marker_.items() if k not in ("mode", HASH_KEY)}


def contract_of(config: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """节点上那份参数契约声明的参数（name / type / required / default），原样的一份。没写契约的空数组。"""
    marker_ = (config or {}).get(KEY)
    items = marker_.get("declared") if isinstance(marker_, Mapping) else None
    return [dict(d) for d in items or [] if isinstance(d, Mapping) and d.get("name")]


__all__ = ["ADAPTER", "CODES", "CONTEXTS", "FILTERS", "HASH_KEY", "KEY", "LITERAL", "LITERAL_ARGS", "LITERAL_KEY",
           "MISSING", "ParameterRefused", "Problem", "SNAPSHOT_KEY", "Slot", "Statement", "UNDECLARED", "UNSUPPORTED",
           "check_nodes", "check_template", "contract", "contract_of", "declared", "describe", "dynamic_input_writings",
           "field_type", "input_refs", "is_literal", "literal_keys", "literal_problem", "marker", "marker_material",
           "material", "mentions_input", "mode_of", "plan", "provided", "render", "rendered_hash", "resolve",
           "runtime_problems", "slots_of", "value_kind"]
