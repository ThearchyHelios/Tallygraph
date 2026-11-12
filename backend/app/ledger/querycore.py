"""进程内 QueryCore 固定执行（K09）：按一份**已发布的固定口径**跑一次，交回类型化标量与证据引用。

这一层是「按固定计划执行」的服务，不是又一个入口平台。一次调用只说五件事：口径身份（workflow + card，版本可点名、
不点名就读**推荐版并当场固定**）、要算哪几个指标（K08 闭包）、这次入口参数的**类型化取值**、预算与 trace。回来的是
一份结果：类型化 metrics、这次实际用到的发布/manifest 引用（K07）、selection/closure 身份、参数解释与哈希（K01）、
数据时点与质量状态、每条查询的执行/attempt/工件/来源证据引用，以及「这次到底算成了没有」。

**这一版只按显式选择执行。** `metric_ids` 必须点名（空的选择直接拒，不静默当整卡）；交回的是标量值（声明成表的指标
在发 SQL 之前就拒）。「整卡」与表形输出留给以后的版本，不在这里按缺省猜。

**固定的是发布物，不是当下这张图。** 执行的是不可变版本行里的那一版图（K07 的发布清单就是按它物化的），清单必须读得
出来、且**就是这一版这一张图**的（图指纹对得上、图上的源头口径卡一张不缺）；清单里记着的编译器/绑定/参数/解析器约定
和现在这一套不兼容就**明确拒绝**，绝不按新编译器现场重解释旧发布（A09）。没有发布清单的老资产（legacy）照样拒：旧 trial
路径一个字不动，但不能拿它冒充 QueryCore 的固定发布执行。

**执行路径零模型、零编译。** 内部 LLM = 0（派生的探针图里只有入口、取数、口径卡、出口，没有报告/Agent 节点），
作者侧 SQL 编译 = 0（只按固定计划做参数绑定与选择展开：K01 的 `params.plan` 把这一版模板按这次的值渲染成字面语句，
K08 的闭包决定执行哪几条查询、要哪些参数）。不建通用 Agent、不建任务平台、不注册驱动、不做结果缓存。

**进程内、可超时。** `run_manager.start` 起一次探索运行（`READ_ONLY_OPTION` 只读边界），随后在**同一个事件循环里**
`await` 它收尾（`run_manager.wait_idle`：等任务，不阻塞循环）。到点还没收尾就照实交回 `status="running"` + run_id，
**绝不把超时说得像成功**；空指标、部分查询没结果、某个指标被遮罩也一样 `ok=False`。

**身份与用途由宿主定，客户端改不了。** 认证主体来自服务端身份（HTTP 入口从 `current_principal()` 取，宿主入口由调用方
显式传），purpose 是**宿主常量**：默认交互入口是 `compose`（探索运行，走 K07 的同一套判定但不继承正式身份，也不拿它绕过
formal）；给 K10 父 Run 用的是 `execute_formal`（purpose=formal，复用同一个 `publications.judge`，发布依据不齐就拒）。
客户端没有传 purpose / role 的路子——两个入口都不收这两个字段。

**普通使用者按资产授权（K14）。** **每一个用途**（compose 的普通问数、交互入口、画布试跑、父 Run 的 compose 调用，以及 formal
的 `execute_formal` 与正式父 Run 派生的那一次执行）在**固定 workflow + version 之后、起 Run 之前**都多判一道：这一版口径有没有
授权给这次的 actor（`apps/access.caliber_role`，靠的是 AppGrant 加上应用当前发布版 `AppVersion.pins` 精确固定着这一版），
到 `use` 及以上才放行，否则当场 403（`querycore_not_authorized`）——HTTP 入口、固定请求路由、自动路由、`caliber_query` 工具
这几条路都靠它兜底，谁也不能靠「换一个入口」绕过去；应用图里引用一份没授权给它的口径同样在这次执行被拒。**正式还要多一道**：
formal 在授权之后照旧走 `publications.judge`（发布依据不齐照样拒），两道都过才执行。builder / admin / 关登录由**调用方**显式
放行（`auth/chat_policy.restricted_actor`），这几档不写进资产授权函数；actor 缺失（认不出是谁）按普通使用者算：资产授权查不到
主体就是没有授权，照拒。

**服务端替普通使用者准备的那条路（`execute_prepared`）。** 普通使用者的模型生成的图里**不要求**它自己画口径查询：服务端
在准备一轮问数据时把 `caliber_query` 自动绑进它的 Agent / 调用工具节点（chat/launch，只给有授权口径的主体绑），模型因此
能自己挑一份有授权的口径调用。要一条**deterministic** 的固定执行（一条确定请求直达、不经模型）时用 `execute_prepared`：
服务端按固定的 workflow / version / card / metric_ids / inputs 搭一张最小的**父图**（入口 → 调用工具 caliber_query → 出口），
经 `user_chat_run` 那套白名单与选项（遮罩 + 只读）起一次探索父 Run，**实际执行走的是 `caliber_query` 工具**（派生的子运行
就是 `execute` 那一次固定执行），当场 await 收尾，交回与 `execute` 同形的 Outcome：run_id 是这次调用起的父 Run
（普通使用者读得到它：显式绑定、白名单、遮罩/只读、每条 query artifact 接回的证据都在它上面），execution_run_id 是它派生
出来真正跑那几条 SQL 的子运行。用户不编辑图，执行层却有明确的绑定、白名单与父 Run 证据。

**K10：父 Run 的工具入口（`caliber_query`）。** 一次运行里的 Agent / 调用工具节点绑上这个工具，就按一份固定口径把数取回来：
工具用的是**父 Run 的上下文**——父 Run 的用途决定这次走 formal 还是 compose（正式父 Run 用 formal judge；交互、画布这类
compose 父 Run 拿不到正式身份），父 Run 固定的上传/记录源版本、运行选项（遮罩、只读、数据目录视图）与来源照原样带下去。
执行仍然是 K09 的进程内固定执行：派生出来的探针运行是**明确的子 Run**（origin 里记着 `parent_run_id`，来源也带下去，
不失去父关系），并且**当场 await 到收尾**。回来的是与交互工作台同一种形状的结果：metrics + queries 数组，每条查询都带
query_id / execution_id / attempt 与工件、来源、参数材料；父 Run 的节点据此把每条 query artifact 以合法引用接回自己的封存
证据（伪造的、别的运行落的、不是 query_snapshot 的工件一律拒绝，见 capabilities.registry.snapshot_owned）。

**值只被模板替换一次、且不无条件转 float。** 每个参数的解释与哈希来自 `params.plan` 的同一份 resolved 值（检查、渲染、
快照材料共一份）；实际执行过的语句与它不一致（运行期被重新绑定）照实报 `querycore_rebound`。交回的值原样保留类型：
JSON 里的整数就是精确整数（大整数不转 float），十进制文本原样保留（记成 `decimal`），布尔/字符串/空值各按各的。

**交回的是证据引用，不是 SQL 正文。** 每条查询给工件 id、来源（workflow/version/node）、attempt（那次执行的执行号）、
调用 id、列与行数、是否截断、以及**这次计划的语句哈希/参数输入哈希与执行时那份的哈希**——两串哈希对得上就说明这次没有
运行期重绑定。口径的 SQL 正文（连同快照里记着的源模板）不外发：里面可能写着遮罩值，要读正文走证据接口（那里按遮罩处理）。

**K13：量这一层自己。** 交回的 `measurement` 记这一次的分段耗时（发布/定位、闭包/计划、参数、排队、数据库、证据、总耗时）、
计数（计划与真发出去的 SQL 条数、内部模型调用数、交回的查询条数与被截断的条数、问题/错误/超时）与身份（trace / run /
query ids）。测量只读时钟与这次运行自己留下的记录与封存事件，不改执行路径：不加缓存、不改计划、不重试。**量不到的一律
是 None**——没跑完（超时）就没有执行证据，数据库那一截与 SQL 条数不填 0 冒充；排队那一项来自运行记录的 `usage.queue_ms`
（引擎在等并发名额前后量的那一段），不是等人审批的 `wait_ms`，老运行没有这一项就是 None。
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.apps import publications
from app.tools.registry import ToolContext, execution_id_of, register

logger = logging.getLogger(__name__)

#: 默认交互入口的用途（探索/组合式问数那一档）：不是正式身份，也不因为缺验证审阅被拦
DEFAULT_PURPOSE = publications.PURPOSE_COMPOSE
#: 宿主给 K10 父 Run 用的用途：和正式运行同一套发布依据判定（复用 publications.judge）
HOST_PURPOSE = publications.PURPOSE_FORMAL

#: K10 注册进工具登记表的**真实工具名**（不是 `db_query__` 前缀那类动态工具）：父 Run 的 Agent / 调用工具节点绑它，
#: 就按一份固定口径取一次数（app/ledger/querycore.execute），并把每条 query artifact 接回父 Run 的证据。
CALIBER_QUERY = "caliber_query"
#: 子 Run 的 origin 里记父运行 id 的键（capabilities.registry.snapshot_owned 按它认「明确派生的子运行」）
PARENT_RUN_KEY = "parent_run_id"
#: 运行选项（Run.options / RunContext.extra）里放这次口径查询预算的键：父 Run 发起时定下就照它，没定就走缺省
BUDGET_OPTION = "caliber_query_budget"


def origin_kind() -> str:
    """这次执行记在运行上的来源（engine/origin.QUERYCORE_ORIGIN）：口径的固定执行，和口径试跑同一档探索运行。

    只从 engine/origin 的常量取，别处不许另写一个字符串。"""
    from app.engine.origin import QUERYCORE_ORIGIN

    return QUERYCORE_ORIGIN


#: 一次执行默认最多等多久（秒）——到点交回 running，不冒充成功
DEFAULT_TIMEOUT_S = 30.0
#: 默认预算：这次最多执行几条查询（闭包里的查询数超过就拒，不静默少算）
DEFAULT_MAX_QUERIES = 32

# --------------------------------------------------------------------------
# 机器码：拒绝时给程序认（值层面/执行层面的问题另见 problems 里的码）
# --------------------------------------------------------------------------

NOT_FOUND = "querycore_not_found"
CARD_MISSING = "querycore_card_missing"
VERSION_MISSING = "querycore_version_missing"
NOT_PUBLISHED = "querycore_version_unpublished"
MANIFEST_REQUIRED = "querycore_manifest_required"
MANIFEST_UNREADABLE = "querycore_manifest_unreadable"
INCOMPATIBLE = "querycore_incompatible"
SELECTION_REFUSED = "querycore_selection_refused"
INPUTS_REFUSED = "querycore_inputs_refused"
UNKNOWN_INPUT = "querycore_unknown_input"
PARAMETERS_REFUSED = "querycore_parameters_refused"
BUDGET_REFUSED = "querycore_budget_refused"
OUTPUT_UNSUPPORTED = "querycore_output_unsupported"
#: 工具入口（K10）：不在任何运行里调用，就没有父运行可以把证据接回去
NO_PARENT = "querycore_no_parent"
#: 普通使用者按这一版口径执行，但资产授权里没有给它（K14）：起 Run 之前就拒
NOT_AUTHORIZED = "querycore_not_authorized"
#: 父 Run 上已经固定的数据版本和这次要执行的口径对不上（另一份口径目标、源已经换了一版）：明确拒，不静默换一版
DATA_VERSION_CONFLICT = "querycore_data_version_conflict"
#: 准备好的那次执行（execute_prepared）里子运行跑的版本和这一层固定/授权的那一版对不上（推荐版在中途换过）：照实说，
#: 不把另一版的数当这一版交出去
VERSION_MOVED = "querycore_version_moved"
#: 上面这一批（起 Run 之前拒绝的那几个码）：写给「普通使用者看到的失败说法只留码、不留口径身份」的地方按它们认
#: （api/runs 的失败说法投影），别处不要另列一份
REFUSAL_CODES = (NOT_FOUND, CARD_MISSING, VERSION_MISSING, NOT_PUBLISHED, MANIFEST_REQUIRED, MANIFEST_UNREADABLE,
                 INCOMPATIBLE, SELECTION_REFUSED, INPUTS_REFUSED, UNKNOWN_INPUT, PARAMETERS_REFUSED, BUDGET_REFUSED,
                 OUTPUT_UNSUPPORTED, NO_PARENT, NOT_AUTHORIZED, DATA_VERSION_CONFLICT, VERSION_MOVED)

#: 结果里的问题码（ok=False 的原因）
NO_RUN = "querycore_run_missing"
EXECUTION = "querycore_execution"
TIMEOUT = "querycore_timeout"
EVIDENCE = "querycore_evidence"
EMPTY = "querycore_empty"
METRIC_MISSING = "querycore_metric_missing"
METRIC_NO_VALUE = "querycore_metric_no_value"
METRIC_MASKED = "querycore_metric_masked"
QUERY_MISSING = "querycore_query_missing"
REBOUND = "querycore_rebound"

#: 运行到这几个状态就是终态了（interrupted 也算：它停在那儿不再自己往下跑）；别的都是还在跑
TERMINAL_RUN_STATUSES = ("succeeded", "failed", "cancelled", "interrupted")

#: 十进制文本：带小数点/指数，或者位数多到 float 存不下（大整数不转 float，原样保留）
_DECIMAL_TEXT = re.compile(r"^[+-]?(?:\d+\.\d*|\.\d+|\d+(?:\.\d+)?[eE][+-]?\d+|\d{16,})$")
#: 取值本身就写成日期/时刻（含时区偏移）：算「这次数据窗口」时按它认，认字段声明的类型之外再认一层值
_DATE_LIKE = re.compile(r"^\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)?$")
#: 入口字段声明成时间类的：算「这次数据窗口」用
_TIME_TYPES = ("date", "datetime", "time")


class QueryCoreRefused(Exception):
    """这次固定执行不发起（服务层面就拒）：message 给人看，code 给程序认，status_code 是 HTTP 入口该回的状态。

    problems 是底层那一层原样的几条（参数完整性问题、闭包的未决项、发布依据的缺口……），带各自的机器码。"""

    def __init__(self, message: str, code: str, *, status_code: int = 409,
                 problems: Sequence[Any] = ()) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.status_code = status_code
        self.problems = tuple(problems)

    def as_dict(self) -> dict[str, Any]:
        return {"detail": self.message, "code": self.code,
                **({"problems": [dict(p) for p in self.problems]} if self.problems else {})}


# --------------------------------------------------------------------------
# 类型化取值：只认类型，不改值
# --------------------------------------------------------------------------


def typed(value: Any) -> str:
    """这个值在 JSON 里的类型：integer / number / decimal / boolean / string / null / 别的类型名。

    **不把 decimal 和大整数转成 float**（R06 的原值口径）：JSON 里的整数已经是精确整数原样交回，十进制文本（驱动
    Decimal 落进工件后按文本存下来的）原样保留并标成 decimal，不四舍五入、不截位。"""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, Decimal):
        return "decimal"
    if isinstance(value, str):
        return "decimal" if _DECIMAL_TEXT.match(value.strip()) else "string"
    if isinstance(value, (list, tuple)):
        return "array"
    if isinstance(value, Mapping):
        return "object"
    return type(value).__name__


def exact(value: Any) -> Any:
    """值原样交回，只把 Decimal 换成它的十进制文本（精确位数一个不少）。

    接口这一层会把 Decimal 编码成 float（丢精度），所以十进制不能以 Decimal 的形状出这道门：换成文本、类型标成
    decimal。别的值（int / float / str / bool / None）原样，不做任何转换。"""
    return str(value) if isinstance(value, Decimal) else value


def _typed_metric(metric: Mapping[str, Any], hidden: str | None) -> dict[str, Any]:
    """一个指标的类型化投影：值原样保留（遮罩时不给值），带类型、单位、小数位与质量结论。"""
    out: dict[str, Any] = {k: metric[k] for k in ("id", "name", "unit", "status", "rendered", "decimals", "format")
                           if metric.get(k) is not None}
    if hidden:
        out.update(masked=True, rendered=hidden, value=None, value_type="null", status="masked")
        out["quality"] = {"status": "unknown", "checks": [
            {"code": "masked", "status": "unknown", "detail": "指标值已遮罩，不能在此作为结果交出"}]}
        return out
    value = metric.get("value")
    out["value"] = exact(value)
    out["value_type"] = typed(value)
    if metric.get("contract_findings"):
        out["contract_findings"] = [
            dict(f) if isinstance(f, Mapping) else str(f)
            for f in metric["contract_findings"]
            if f
        ]
    out["quality"] = _metric_quality(metric)
    return out


def _metric_quality(metric: Mapping[str, Any]) -> dict[str, Any]:
    from app.engine.validation import metric_quality

    return metric_quality(dict(metric))


# --------------------------------------------------------------------------
# 预算
# --------------------------------------------------------------------------


def _budget_of(budget: Mapping[str, Any] | None) -> dict[str, Any]:
    """这次执行的预算：最多几条查询、最多等多久。给了的照用，没给的用缺省（不新建一套额度体系）。"""
    made = {"max_queries": DEFAULT_MAX_QUERIES, "timeout_s": DEFAULT_TIMEOUT_S}
    for key in ("max_queries", "timeout_s"):
        if budget and budget.get(key) is not None:
            made[key] = budget[key]
    return made


# --------------------------------------------------------------------------
# 输入：类型化
# --------------------------------------------------------------------------


def _inputs_of(pinned: Any, inputs: Mapping[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """这次入口的取值：不认识的字段拒（不静默丢掉），按声明类型校验，再按 K01 的语义把缺省值补上。

    收值和试跑、组合式问数同一份（`params.provided`：显式传的空串算没给），校验用同一份表单（apps/forms），
    定值用同一份 `params.resolve`。

    **这次选择用什么参数就是什么参数**：给了选择时，入口契约收窄成这次要用的那几个（K08 的 required_entry_fields）——
    声明过、但这次不用的字段照收不误（不报「不认识」），**但不带进这次执行**，列表交回调用方记在 ignored 里，
    不悄悄用它跑出另一份数（B 独有那条查询的参数，选 A 时一律为 0）。"""
    from app.apps.forms import input_schema, validate_form
    from app.ledger import derive, params

    if not isinstance(inputs, Mapping):
        raise QueryCoreRefused("入口参数要给一组字段和值", INPUTS_REFUSED, status_code=422)
    narrowed = [dict(f) for f in pinned.entry_fields]
    full = [dict(f) for f in derive.entry_fields(pinned.graph)] or narrowed
    unknown = sorted(str(k) for k in inputs if str(k) not in params.declared(full))
    if unknown:
        raise QueryCoreRefused(f"不认识的入口字段：{'、'.join(unknown)}", UNKNOWN_INPUT, status_code=422)
    used = set(params.declared(narrowed))
    values = params.resolve(narrowed, params.provided({k: v for k, v in inputs.items() if str(k) in used}))
    errors = validate_form(input_schema(narrowed), values)
    if errors:
        raise QueryCoreRefused("；".join(str(e.get("message")) for e in errors[:5]), INPUTS_REFUSED,
                               status_code=422, problems=[dict(e) for e in errors])
    return values, sorted(str(k) for k in inputs if str(k) not in used)


# --------------------------------------------------------------------------
# 这次执行什么：K08 闭包 + K01 计划
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class _Plan:
    """一条查询的 K01 计划：渲染好的字面语句 + 快照材料（模板哈希、输入哈希、每个参数写在哪儿）。"""

    node_id: str
    query_id: str
    tool: str
    statement: str
    material: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        from app.ledger import params

        return {"query_id": self.query_id, "node_id": self.node_id, "tool": self.tool,
                "template_hash": self.material.get("template_hash"),
                "statement_hash": params.rendered_hash(self.statement),
                "inputs_hash": self.material.get("inputs_hash"),
                "adapter": self.material.get("adapter"),
                "parameters": [dict(p) for p in self.material.get("parameters") or []],
                "declared": [dict(d) for d in self.material.get("declared") or []]}


def _sql_of(node: Mapping[str, Any]) -> str | None:
    args = ((node.get("data") or {}).get("config") or {}).get("args") if isinstance(node, Mapping) else None
    sql = args.get("sql") if isinstance(args, Mapping) else None
    return sql if isinstance(sql, str) and sql.strip() else None


def _plans(pinned: Any, closure: Any, values: Mapping[str, Any]) -> tuple[list[_Plan], list[dict[str, Any]]]:
    """这次要执行的每条查询的 K01 计划（闭包已经决定执行哪几条、要哪些参数）。

    检查、渲染、快照材料用的是同一份 resolved 值（params.plan）；不合规（缺参数、未声明、值承载不了）在这里就把
    逐条问题交回，**一条 SQL 都不发**。"""
    from app.ledger import params

    by_node = {str(query.node_id): query for query in closure.queries}
    plans: list[_Plan] = []
    problems: list[dict[str, Any]] = []
    for node in pinned.fetches:
        node_id = str(node.get("id"))
        sql = _sql_of(node)
        if sql is None:
            continue                        # 闭包已经拒过「取数节点没有可读 SQL」，这里不重判
        try:
            statement = params.plan(sql, fields=pinned.entry_fields, values=values,
                                    source={**pinned.ref(), "node_id": node_id}, node_id=node_id,
                                    where=f"口径「{pinned.card_title}」的入口")
        except params.ParameterRefused as e:
            problems += [p.as_dict() for p in e.problems]
            continue
        query = by_node.get(node_id)
        plans.append(_Plan(node_id=node_id, query_id=str(getattr(query, "query_id", "") or ""),
                           tool=str(getattr(query, "tool", "") or ""), statement=statement.statement,
                           material=dict(statement.material)))
    return plans, problems


def _scalar_output(pinned: Any, metric_ids: Sequence[str]) -> None:
    """首版只交标量：声明成 `contract.shape = table` 的指标在这里明确拒（不悄悄按第一行给个值）。"""
    by_id = {str(m.get("id")): m for m in pinned.metrics}
    for metric_id in metric_ids:
        contract = (by_id.get(str(metric_id)) or {}).get("contract")
        if isinstance(contract, Mapping) and str(contract.get("shape") or "scalar") != "scalar":
            raise QueryCoreRefused(
                f"指标「{metric_id}」声明成表（contract.shape={contract.get('shape')}）：这一版只交标量结果",
                OUTPUT_UNSUPPORTED, status_code=422)


# --------------------------------------------------------------------------
# 证据：类型化指标 + 每条查询的执行引用
# --------------------------------------------------------------------------


async def _sealed_of(run_id: str, memo: dict[str, Any] | None) -> Any:
    """这一次收尾的封存证据。给了 memo 就复用：`read_sealed` 每次都要把整条事件流重读一遍、重算一遍清单哈希
    （K13 量下来约 3.5ms），而一次收尾里指标、查询引用、执行量测三处读的是**同一份**已经封好的快照，没必要读三遍。
    读不出来时照旧抛，由调用的那一处记一条自己的问题（语义不变）。"""
    from app.engine.evidence_reader import read_sealed

    if memo is None:
        return await read_sealed(run_id)
    if "sealed" not in memo:
        memo["sealed"] = await read_sealed(run_id)
    return memo["sealed"]


async def _metrics_from(sealed_run_id: str, *, card_id: str, metric_ids: Sequence[str],
                        memo: dict[str, Any] | None = None,
                        ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """从封存证据里取这次要算的那几个指标，按闭包的顺序、**只认这张卡节点**（同一节点执行过多次取最后一次）。

    遮罩按试跑同一套（api/evidence 的 _Masks / _metric_masks）：被遮的指标不给值，也照实标出来——数据源设了遮罩列
    的时候，这里绝不能把值交出去。交回 (类型化指标, 原始指标)，原始那一份给质量报告用。

    memo 是这一次收尾里几处共用的封存证据（见 _sealed_of）：给了就用那一份，没给就自己读一次。"""
    from app.api.evidence import _Masks, _metric_masks
    from app.engine.evidence_reader import metric_cards

    sealed = await _sealed_of(sealed_run_id, memo)
    if not sealed.trusted:
        return [], {"trusted": False}
    masks = _Masks(sealed)
    found: dict[str, dict[str, Any]] = {}
    raw: dict[str, dict[str, Any]] = {}
    for node_id, artifact, card in metric_cards(sealed):
        if str(node_id) != str(card_id):
            continue
        for metric in card.get("metrics") or []:
            if not isinstance(metric, dict) or not metric.get("id"):
                continue
            metric_id = str(metric["id"])
            if metric_id not in set(metric_ids):
                continue
            # 同一张卡执行过多次时按**最后一次**的产出算（和「节点最后一次产出」同一个认法），
            # 不拿早先那次的值
            _, hidden = await _metric_masks(metric, node_id, artifact, sealed, masks, card=card)
            found[metric_id] = _typed_metric(metric, hidden)
            raw[metric_id] = dict(metric)
    ordered = [found[mid] for mid in metric_ids if mid in found]
    return ordered, {"trusted": True, "metrics": {mid: raw[mid] for mid in metric_ids if mid in raw}}


def _tables_of(sql: Any) -> list[str]:
    from app.engine.evidence import sql_tables

    try:
        return sql_tables(str(sql or ""))
    except Exception:  # noqa: BLE001 - SQL 读不出表名只影响这一条引用，不影响结果本身
        return []


async def _queries_from(sealed_run_id: str, *, closure: Any, plans: Mapping[str, _Plan],
                        memo: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """闭包里每条要执行的查询的执行引用：用了哪份快照工件、来源、attempt(exec)、列与行数、执行时的参数材料与哈希。

    每条查询按**逐次记下的执行**（sealed.query_records：一次执行一条，带 node_id / exec / execution_id）认它自己那一次，
    不按工件 id 去重的字典认：同一份内容会得到同一个工件 id（内容寻址），两条不同的逻辑查询（两个节点写同一条 SQL、
    参数一样、数据一样）会落到**同一个**工件上，`sealed.queries` 那种字典只剩最后一条，早先那条就查不到自己了。

    同一个节点执行过多次时按**最后一次**算（同「节点最后一次产出」的认法）。读不出来/没有快照的照实留空，由调用方
    记一条问题（不编造）。

    memo 是这一次收尾里几处共用的封存证据（见 _sealed_of）：给了就用那一份，没给就自己读一次。"""
    from app.core.artifact_store import load

    sealed = await _sealed_of(sealed_run_id, memo)
    out: list[dict[str, Any]] = []
    for query in closure.queries:
        plan = plans.get(str(query.node_id))
        item: dict[str, Any] = {"query_id": query.query_id, "node_id": query.node_id, "tool": query.tool,
                                "source": dict(query.source), "metric_ids": list(query.metric_ids),
                                "computes_unselected": list(query.computes_unselected),
                                "required_entry_fields": list(query.required_entry_fields),
                                "sql_hash": query.sql_hash, "binding_fingerprint": query.binding_fingerprint,
                                "artifact": None, "attempt": None, "execution_id": None, "call_id": None, "via": None,
                                "columns": [], "row_count": None, "truncated": None, "column_types": {},
                                "data_source": None, "data_version": None, "tables": [], "checks": [],
                                "schema_artifact": None,
                                "no_data": [], "parameters": None, "executed_sql": None,
                                "rebound": None, "found": False}
        record = _query_record_of(sealed, str(query.node_id))
        if record is not None:
            artifact = str(record.get("artifact") or "")
            try:
                snap = load(artifact)
            except ValueError:
                snap = None
            if isinstance(snap, Mapping):
                material = snap.get("parameters") if isinstance(snap.get("parameters"), Mapping) else None
                item.update(
                    artifact=artifact, attempt=record.get("exec") if record.get("exec") is not None
                    else record.get("attempt"),
                    call_id=record.get("call_id"), via=record.get("via"),
                    # 这次实际执行的执行身份：同一次运行里同一条 SQL 跑两次（重跑、agent 两步）也各不相同，
                    # 按调用 id（agent）或这个节点第几次执行（run 内序列）区分（tools/registry.execution_ref）
                    execution_id=record.get("execution_id") or execution_id_of(
                        str(query.node_id), attempt=item.get("attempt"), call_id=record.get("call_id"),
                        run_id=sealed_run_id),
                    columns=[str(c) for c in snap.get("columns") or []],
                    row_count=len(snap.get("rows") or []),
                    truncated=bool(snap.get("truncated")), column_types=dict(snap.get("column_types") or {}),
                    data_source=record.get("source") or snap.get("source"),
                    data_version=snap.get("data_version"), tables=_tables_of(snap.get("sql")),
                    schema_artifact=snap.get("schema_artifact"),
                    checks=list(snap.get("checks") or []), no_data=list(snap.get("no_data") or []),
                    # 快照里的参数材料只留哈希与承载方式：源模板正文不外发（同试跑查询快照那一处）
                    parameters=({"inputs_hash": material.get("inputs_hash"),
                                 "template_hash": material.get("template_hash"),
                                 "adapter": material.get("adapter"), "binding": material.get("binding")}
                                if material else None),
                    executed_sql=snap.get("sql"), found=True)
        if plan is not None:
            # 运行期重绑定：实际执行的语句 / 它记下的参数材料必须和这次计划的一致（值只被模板替换一次）。要比对的是
            # 语句本身，所以它只活在这一层——**返回里不给 SQL 正文**（口径的 SQL 里可能写着遮罩值，同试跑查询快照
            # 那一处的分寸）：交回的是两边的哈希与工件引用，要读正文走证据接口（那里按遮罩处理）
            item["planned_statement_hash"] = plan.as_dict()["statement_hash"]
            item["planned_inputs_hash"] = plan.material.get("inputs_hash")
            item["rebound"] = _rebound(item, plan)
        item.pop("executed_sql", None)
        out.append(item)
    return out


def _query_record_of(sealed: Any, node_id: str) -> dict[str, Any] | None:
    """这个节点交回过的查询快照里**最后一次**的那一条（逐次身份，sealed.query_records）。

    按 node_id 从逐次记下的执行里取，不从按工件 id 去重的 `sealed.queries` 里取：两条不同的逻辑查询（两个节点写同一条
    SQL、参数一样、数据一样）会落到同一个工件上——那种字典只剩最后一条，早先那条就找不到自己那一次了。
    老运行、只回了 tool.end 的执行（没有台账条目）逐次记录里没有，退回按工件去重的那一份，认法和以前一样。"""
    records = [dict(r) for r in sealed.query_records_of(node_id) if r.get("artifact")]
    if not records:
        records = [{"artifact": str(artifact), **dict(info or {})}
                   for artifact, info in sealed.queries.items()
                   if str((info or {}).get("node_id") or "") == str(node_id)]
    return records[-1] if records else None


def _rebound(item: Mapping[str, Any], plan: _Plan) -> str | None:
    """这条查询执行时的语句/参数和计划对不上就是重绑定（返回一句原因，对上返回 None）。"""
    if not item.get("found"):
        return None
    executed = item.get("executed_sql")
    if isinstance(executed, str):
        item["executed_hash"] = _statement_hash(executed)
        if executed != plan.statement:
            return "实际执行的语句和这次计划渲染出来的不一致（运行期被重新绑定）"
    material = item.get("parameters")
    if isinstance(material, Mapping) and material.get("inputs_hash") \
            and material.get("inputs_hash") != plan.material.get("inputs_hash"):
        return "实际执行的参数输入哈希和这次计划的不一致（运行期被重新绑定）"
    return None


def _statement_hash(statement: Any) -> str:
    from app.ledger import params

    return params.rendered_hash(statement)


# --------------------------------------------------------------------------
# 结果
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Outcome:
    """一次固定执行的结果。`ok` 是「这次真的算成了」：执行成功、闭包里每条查询都有结果、点名的每个指标都有值（没被
    遮罩）——空指标、部分结果一律 ok=False。

    `run_id` 是**这次执行的那次运行**；`execute_prepared` 交回的 Outcome 里它是服务端起的父 Run（显式绑定、白名单、
    遮罩/只读与证据都在它上面），真正跑那几条 SQL 的子运行写在 `execution_run_id`（派生的口径固定执行）——其余字段
    （status / metrics / queries / problems / quality）说的都是**子运行那一次**算成了没有。别的入口 execution_run_id 为空。"""

    run_id: str
    status: str
    ok: bool
    workflow_id: str
    card_id: str
    version: int
    explicit_version: bool
    purpose: str
    publication: dict[str, Any]
    selection: dict[str, Any]
    parameters: dict[str, Any]
    metrics: tuple[dict[str, Any], ...]
    queries: tuple[dict[str, Any], ...]
    data: dict[str, Any]
    quality: dict[str, Any]
    problems: tuple[dict[str, Any], ...]
    trace: Any = None
    #: K13 的测量记录：这次执行量到的分段耗时与计数（见 Measurement）。量不到的字段是 None
    measurement: Measurement | None = None
    #: 这次调用派生出来真正执行的那次运行（execute_prepared 的子运行；别的入口为空）
    execution_run_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"run_id": self.run_id, "status": self.status, "ok": self.ok, "workflow_id": self.workflow_id,
                "card_id": self.card_id, "version": self.version, "explicit_version": self.explicit_version,
                "purpose": self.purpose, "publication": dict(self.publication),
                "selection": dict(self.selection), "parameters": dict(self.parameters),
                "metrics": [dict(m) for m in self.metrics], "queries": [dict(q) for q in self.queries],
                "data": dict(self.data), "quality": dict(self.quality),
                "problems": [dict(p) for p in self.problems],
                **({"trace": self.trace} if self.trace is not None else {}),
                **({"execution_run_id": self.execution_run_id} if self.execution_run_id else {}),
                **({"measurement": self.measurement.as_dict()} if self.measurement is not None else {})}


def _problem(code: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"code": code, "message": message, **{k: v for k, v in extra.items() if v not in (None, "")}}


# --------------------------------------------------------------------------
# K13：一次固定执行的测量（分段耗时与计数）
#
# 量的是「这次固定执行自己」：发布/定位、闭包/计划、参数、排队、数据库、证据各花多久，发出去几条 SQL、调了几个
# 模型、交回几条查询、有没有被截断、有没有出错或超时，以及 trace / run / query 的身份。测量只读时钟与这次运行自己
# 留下的记录与封存事件，不改执行路径：不加缓存、不改计划、不重试。
# --------------------------------------------------------------------------

#: 分段耗时的键（毫秒）：locate/closure/params/run/evidence 首尾相接，加起来就是 total；queue 与 database 是 run 里面
#: 的两段（各有出处，不是另加的时间）；量不到的一律 None，不填 0 冒充
PHASES = ("locate_ms", "closure_ms", "params_ms", "run_ms", "queue_ms", "database_ms", "evidence_ms", "total_ms")


class _Stage:
    """分段计时：段与段首尾相接（`mark` 结束当前段、开下一段），total 从建它那一刻算起。只读时钟，不改执行。"""

    def __init__(self) -> None:
        self._last = time.perf_counter()
        self._started = self._last
        self.ms: dict[str, int] = {}

    def mark(self, name: str) -> int:
        now = time.perf_counter()
        self.ms[name] = max(0, int((now - self._last) * 1000))
        self._last = now
        return self.ms[name]

    def total_ms(self) -> int:
        return max(0, int((time.perf_counter() - self._started) * 1000))


@dataclass(frozen=True)
class Measurement:
    """一次固定执行的测量记录（K13）。**只记这一次真的量到的**：没量到的一律 None，不填 0 冒充。

    phases（毫秒）：

    - `locate_ms` 发布/定位：版本解析（点名或推荐版）与固定、发布清单核验、用途判定；
    - `closure_ms` 闭包/计划：K08 的显式选择展开、预算与输出形状判定；
    - `params_ms` 参数：入口取值与校验（K01）、逐条查询的渲染与快照材料、探针图成形；
    - `run_ms` 起运行到收尾：**含** queue 与 database 两段，也含图引擎自己的开销；
    - `queue_ms` 排队：这次从要并发名额到拿到名额的时长（含发名额那一下的开销；没人在排队时就是那几毫秒），来自
      运行记录的 `usage.queue_ms`（engine/runner 在要名额前后量的那一段），**不是**等人审批的 `wait_ms`；老运行、
      还没收尾的运行没有这一项就是 None；
    - `database_ms` 数据库：这次取数节点自己的 `node.finished.duration_ms` 之和（同一个节点执行多次取最后一次）。它
      含着 SQL 往返、驱动与行映射，是这次执行里离数据库最近的一段，**不是**数据库内核时间；
    - `evidence_ms` 证据：读封存证据、取指标与查询引用、拼结果与质量报告；
    - `total_ms` 整次 `execute` 的墙钟。

    counts：`planned_queries`（闭包里要执行的查询数）、`executed_sql`（真的发出去的 SQL 条数，按封存里逐次记下的查询
    快照数，不是计划条数）、`llm_calls`（内部模型调用数，执行路径固定为 0）、`returned_queries` / `truncated_queries`
    （交回证据引用的查询条数 / 其中被截断的条数）、`problems` / `errors`（结果里的问题条数，errors 不含超时）、
    `timeouts`（到点没收尾 = 1）。

    ids：`run_id`、`trace`、`workflow_id` / `card_id` / `version`、`query_ids`、`execution_ids`——按其中任何一个都能把
    这次测量定位回那条运行、那条查询。
    """

    phases: dict[str, Any]
    counts: dict[str, Any]
    ids: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {"phases": dict(self.phases), "counts": dict(self.counts), "ids": dict(self.ids)}


def _queue_of(row: Any) -> int | None:
    """这次排队等了多久（毫秒）：运行记录上的 `usage.queue_ms`，由 runtime 在等并发名额前后量（engine/runner._drive）。

    没有这一项就是没量到（加这一项之前的运行、还没收尾的运行）：返回 None，**绝不拿等人审批的 wait_ms 冒充排队**。"""
    usage = getattr(row, "usage", None)
    if not isinstance(usage, Mapping):
        return None
    queued = usage.get("queue_ms")
    return int(queued) if isinstance(queued, int) and not isinstance(queued, bool) else None


def _from_evidence(sealed: Any, closure: Any) -> dict[str, Any]:
    """封存事件里量得到的几样：取数节点各自的执行时长、真发出去几条 SQL、内部模型调用数。

    取数节点的执行时长按 `node.finished.duration_ms` 求和（同一个节点执行过多次取最后一次，同「节点最后一次产出」
    的认法）；SQL 条数按封存里**逐次**记下的查询快照数（同一份内容被两条查询执行两次就是两条），不是闭包里的计划
    条数。模型调用数按 `llm.end` 事件数——执行路径 LLM=0，这一项是给「真的为 0」留个可核的数，不是估计。"""
    fetch_nodes = {str(query.node_id) for query in closure.queries}
    durations: dict[str, int] = {}
    calls = 0
    for _, kind, node_id, data in sealed.events:
        if kind == "llm.end":
            calls += 1
        if kind != "node.finished" or not isinstance(data, Mapping) or str(node_id) not in fetch_nodes:
            continue
        spent = data.get("duration_ms")
        if isinstance(spent, int) and not isinstance(spent, bool):
            durations[str(node_id)] = max(0, spent)
    return {"database_ms": sum(durations.values()), "llm_calls": calls,
            "executed_sql": sum(1 for record in sealed.query_records
                                if str(record.get("node_id") or "") in fetch_nodes)}


def _measurement(stages: _Stage, *, run_id: str, workflow_id: str, card_id: str, version: int, closure: Any,
                 status: str, problems: Sequence[Mapping[str, Any]], queries: Sequence[Mapping[str, Any]],
                 facts: Mapping[str, Any], trace: Any) -> Measurement:
    """把这次执行量到的收成一份记录。成功之后才读得到的（SQL 条数、数据库那一截、交回几条查询）没读到时是 None。"""
    stages.mark("evidence_ms")
    timeouts = sum(1 for problem in problems if str(problem.get("code")) == TIMEOUT)
    # 证据读到了才算量到「执行了几条 SQL / 交回几条查询」：没跑完（超时）一条都没有，也不能按计划条数顶上
    measured = status == "succeeded" and facts.get("executed_sql") is not None
    counts = {
        "planned_queries": len(closure.queries),
        "executed_sql": facts.get("executed_sql"),
        "llm_calls": facts.get("llm_calls"),
        "returned_queries": sum(1 for query in queries if query.get("found")) if measured else None,
        "truncated_queries": (sum(1 for query in queries if query.get("found") and query.get("truncated"))
                              if measured else None),
        "problems": len(problems),
        "errors": len(problems) - timeouts,
        "timeouts": timeouts,
    }
    phases = {name: stages.ms.get(name) for name in ("locate_ms", "closure_ms", "params_ms", "run_ms", "evidence_ms")}
    # queue 与 database 是 run 里面的两段，各有各的出处：一个来自运行记录的排队那一项，一个来自封存事件里取数节点
    # 自己的执行时长；两样都不在阶段计时器里
    phases["queue_ms"] = facts.get("queue_ms")
    phases["database_ms"] = facts.get("database_ms")
    phases["total_ms"] = stages.total_ms()
    assert set(phases) == set(PHASES), "分段耗时的键要和 PHASES 一致"
    identity = {
        "run_id": run_id, "trace": trace, "workflow_id": workflow_id, "card_id": card_id, "version": version,
        "query_ids": [str(query.query_id) for query in closure.queries],
        "execution_ids": [str(query["execution_id"]) for query in queries if query.get("execution_id")],
    }
    return Measurement(phases=phases, counts=counts, ids=identity)


# --------------------------------------------------------------------------
# 固定执行
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class _Prepared:
    """一次固定执行在**起 Run 之前**已经判完、备好的东西（`_prepared` 交回）。"""

    workflow: Any
    resolved: Any
    #: 这一版的图（清单核验、口径要读的源都按它认）
    graph: dict[str, Any]
    #: K08 的固定闭包与它那一版的口径卡（`derive.load_pinned`）
    loaded: Any
    closure: Any
    values: dict[str, Any]
    ignored: list[str]
    plans: dict[str, _Plan]
    publication_ref: dict[str, Any]
    #: 派生的探针图（真正跑的那张：入口、取数、口径卡、出口）
    graph_spec: dict[str, Any]


async def _prepared(session: AsyncSession, *, workflow_id: str, card_id: str, metric_ids: Sequence[str],
                    inputs: Mapping[str, Any] | None, version: int | None, purpose: str, actor: Any,
                    budget: Mapping[str, Any] | None, timeout_s: float | None,
                    stages: _Stage) -> tuple[_Prepared, dict[str, Any]]:
    """起运行之前判完这一次固定执行要用的东西：固定版本（并核普通使用者的资产授权）→ 核发布清单 → 解闭包 → 收参数。

    `execute` 与 `execute_prepared` 共用这一份（两条路在起 Run 之前该判的一模一样，不各抄一遍）：交回准备好的那一份
    和这次的预算（`max_queries` / `timeout_s`，给了的照用）。"""
    from app.apps.versions import is_published
    from app.db.models import Workflow
    from app.ledger import derive

    workflow_id, card_id = str(workflow_id or ""), str(card_id or "")
    wanted = [str(m) for m in (metric_ids or []) if str(m or "").strip()]
    if not wanted:
        raise QueryCoreRefused("metric_ids 是空的：这次一个指标也不选（不静默当整卡）", SELECTION_REFUSED,
                               status_code=422)
    attempted = _budget_of(budget)
    if timeout_s is not None:
        attempted["timeout_s"] = timeout_s

    # ---- 1) 版本：点名就固定到它；不点名读一次推荐版并固定（解析只发生这一次）----------------
    workflow = await session.get(Workflow, workflow_id)
    if workflow is None:
        raise QueryCoreRefused("工作流不存在，可能已被删除", NOT_FOUND, status_code=404)
    resolved = await publications.resolve(session, workflow, version=version, purpose=purpose)
    if resolved is None:
        detail = (f"「{workflow.name}」没有 v{version} 这个版本" if version is not None
                  else f"「{workflow.name}」现在没有推荐版（还没发布过）")
        raise QueryCoreRefused(detail, VERSION_MISSING, status_code=404)
    if not is_published(resolved.row, current=workflow.published_version) or resolved.row.retired_at is not None:
        raise QueryCoreRefused(f"「{workflow.name}」v{resolved.version} 没有发布或已退役，不能按固定口径执行",
                               NOT_PUBLISHED, status_code=409)
    # 版本已经固定：普通使用者这一档先核资产授权，没授权就当场拒（一条 SQL 都不发，也不起 Run）
    await _require_caliber(session, actor, purpose=purpose, workflow=workflow, version=resolved.version)
    # 卡在不在图里（图不存在时也要给出具体的一句话）由后面的闭包解析说；这里先把「清单必须就是这一版这一张图」核掉

    # ---- 2) K07：清单必须读得出来、就是这一版这一张图、约定与现在兼容（不现场重解释旧发布）-----
    graph = (resolved.row.graph or {}) if isinstance(resolved.row.graph, dict) else {}
    content = _verified_manifest(resolved, graph=graph)
    publication_ref = await _judged(session, workflow, resolved, purpose=purpose, content=content, graph=graph)
    stages.mark("locate_ms")

    # ---- 3) K08 闭包 + K01 参数语义（都在发起运行之前判完）--------------------------------
    try:
        loaded = await derive.load_pinned(session, resolved.workflow_id, resolved.version, card_id, metric_ids=wanted)
    except derive.NotDerivable as e:
        # 卡不在/不是口径卡：这一版里根本没有这张卡，谁能补也不该在这里补
        code = CARD_MISSING if e.code in (derive.GONE, derive.NOT_CARD) else (
            SELECTION_REFUSED if e.code == derive.SELECTION else e.code)
        raise QueryCoreRefused(e.message, code, status_code=409,
                               problems=[p.as_dict() for p in getattr(e, "problems", ())]) from None
    if loaded.closure is None or loaded.closure.whole_card:
        raise QueryCoreRefused("这次的点名没有解析成显式选择（闭包不完整）", SELECTION_REFUSED, status_code=409)
    closure = loaded.closure
    if len(closure.queries) > int(attempted["max_queries"]):
        raise QueryCoreRefused(
            f"这次要执行 {len(closure.queries)} 条查询，超过预算 {attempted['max_queries']}：不静默少算",
            BUDGET_REFUSED, status_code=429)
    _scalar_output(loaded, closure.metric_ids)
    stages.mark("closure_ms")
    # 入口取值与校验（K01）到逐条查询的渲染和探针图成形都算「参数」这一段
    values, ignored = _inputs_of(loaded, inputs or {})
    plans, parameter_problems = _plans(loaded, closure, values)
    if parameter_problems:
        first = parameter_problems[0]
        raise QueryCoreRefused(f"参数完整性不通过：{first.get('message') or first.get('code')}",
                               str(first.get("code") or PARAMETERS_REFUSED), status_code=422,
                               problems=parameter_problems)
    graph_spec = derive.caliber_probe(loaded).graph
    stages.mark("params_ms")
    return _Prepared(workflow=workflow, resolved=resolved, graph=graph, loaded=loaded, closure=closure,
                     values=values, ignored=ignored, plans={plan.node_id: plan for plan in plans},
                     publication_ref=publication_ref, graph_spec=graph_spec), attempted


async def _require_caliber(session: AsyncSession, actor: Any, *, purpose: str, workflow: Any, version: Any) -> None:
    """普通使用者按一份口径执行之前的那一道（K14）：资产授权到 use 及以上才放行，否则 403（起 Run 之前就拒）。

    - **每一个用途都过这一道**：compose（问数、交互入口、画布试跑、父 Run 的 compose 调用）与 formal（`execute_formal`、
      正式父 Run 派生的那一次执行）一样——正式运行也不能替使用者执行一份没授权给它的口径。formal 另**额外**走
      `publications.judge`（发布依据不齐照样拒），两道都过才执行：授权这一道在前，依据那一道在后；
    - 靠的是 `apps/access.caliber_role`：AppGrant（给本人、他所在的组、所有人，至少 use）加上应用**当前发布版**精确固定着
      这一版（AppVersion.pins），两样都在才算「授权给了这个主体」；
    - builder / admin / 关登录由**调用方**显式放行（`auth/chat_policy.restricted_actor`），这几档不写进资产授权函数；
    - actor 缺失、认不出是谁：按普通使用者算——资产授权查不到主体就是没有授权，照拒（不拿「认不出」当放行）。"""
    from app.apps.access import at_least, caliber_role
    from app.auth.chat_policy import effective_actor, restricted_actor

    who = effective_actor(actor)
    if not await restricted_actor(session, who):
        return
    version = int(version)
    role = await caliber_role(session, who, str(workflow.id), version)
    if at_least(role, "use"):
        return
    detail = "正式运行也一样要这一份授权。" if purpose != publications.PURPOSE_COMPOSE else ""
    raise QueryCoreRefused(
        f"没有权限：口径「{workflow.name}」v{version} 没有授权给你使用。" + detail
        + "用一份已发布的口径要有应用授权（让搭建者把固定着这一版口径的应用授权给你，至少「使用」），"
          "或者按数据源自己搭一张图",
        NOT_AUTHORIZED, status_code=403,
        problems=[{"code": NOT_AUTHORIZED, "workflow_id": str(workflow.id), "version": version,
                   "purpose": purpose, "role": role or ""}])


async def execute(
    session: AsyncSession,
    *,
    workflow_id: str,
    card_id: str,
    metric_ids: Sequence[str],
    inputs: Mapping[str, Any] | None = None,
    version: int | None = None,
    purpose: str = DEFAULT_PURPOSE,
    actor: str | None = None,
    timeout_s: float | None = None,
    budget: Mapping[str, Any] | None = None,
    trace: Any = None,
    parent_run_id: str | None = None,
    run_options: Mapping[str, Any] | None = None,
    data_versions: Mapping[str, Any] | None = None,
    emit: Any = None,
) -> Outcome:
    """按固定口径跑一次，交回类型化标量与证据引用。

    - `workflow_id` + `card_id` 定口径身份；`version` 点名就固定到它，不点名就**读一次推荐版并固定**（不反复解析）；
    - `metric_ids` 是这次要算的指标（显式选择，走 K08 闭包；空的不当整卡）；
    - `inputs` 是这次入口的类型化取值（不认识的字段拒，缺参数在发 SQL 之前拒）；
    - `purpose` 由**宿主常量**定（默认 `compose`；`execute_formal` 传 formal），客户端传不了；
    - `actor` 是认证主体（HTTP 入口从服务端身份取，宿主入口显式传），只记进运行来源，不从请求体读；普通使用者执行时
      （compose 与 formal 一样）**固定版本之后、起 Run 之前**还要核这一版口径的资产授权（`apps/access.caliber_role` 到
      use 及以上，否则 403 `querycore_not_authorized`，见 `_require_caliber`）；
    - `budget` 给 `max_queries` / `timeout_s`，`trace` 原样记进运行来源；
    - `parent_run_id` / `run_options` / `data_versions` / `emit` 是 K10 父 Run 交下来的上下文：派生出来的探针运行是
      **明确的子运行**（origin 里记 parent_run_id），照原样带上父 Run 的选项与固定的数据版本（遮罩、只读、上传/记录源
      版本固定一条不断）；这次口径那一版要读、父 Run 发起时又没算到的源，在这一层按**父 Run 的**类别与主体补固定
      （`pin_sources_for_parent`），`emit` 用来把这次补固定追加进父 Run 的事件流。

    拒（不发起）抛 QueryCoreRefused；发起了就交回 Outcome（含没跑完的 `status="running"`）。**被拒的这一次没有运行，也就
    没有测量记录**（那时一条 SQL 都没发，没有运行可以定位）。"""
    from app.auth.chat_policy import actor_name, effective_actor

    # K13：这次执行的分段计时从进这门就开始
    stages = _Stage()
    if purpose not in publications.PURPOSES:
        raise ValueError(f"不认识的用途：{purpose}")
    # 认证主体：判授权用它本身（给了的用它，没给就取当前请求的身份——HTTP 入口在中间件里写好了），角色与组不丢；
    # 记进运行、算行级范围用它的**名字**。两处是同一个主体，不能一处认得出、另一处认不出
    subject = effective_actor(actor)
    name = actor_name(actor)
    prepared, attempted = await _prepared(session, workflow_id=workflow_id, card_id=card_id, metric_ids=metric_ids,
                                         inputs=inputs, version=version, purpose=purpose, actor=subject,
                                         budget=budget, timeout_s=timeout_s, stages=stages)
    resolved, workflow = prepared.resolved, prepared.workflow
    loaded, closure = prepared.loaded, prepared.closure

    # ---- 4) 父 Run 上补固定这次要读的源（K10：晚解析的源固定在父 Run 上，不留给子运行）--------
    # 子运行里补（tools/datasource._pin_late_records）会按 exploratory 重算行级范围（正式父 Run 的 all 被收成
    # mine/none）、而且只写子运行那一行：同一次运行里的两次调用各固定各的版本。这里按父 Run 的类别与主体固定，
    # 记在父 Run 上，子运行照它继承。
    versions = data_versions
    if parent_run_id:
        versions = await pin_sources_for_parent(parent_run_id=str(parent_run_id), resolved=resolved,
                                                graph=prepared.graph, card_id=str(loaded.node_id),
                                                versions=data_versions, emit=emit)

    # ---- 5) 起一次只读的探索运行（进程内 await，超时交回 running）--------------------------
    run = await _start(prepared.graph_spec, values=prepared.values, workflow=workflow, resolved=resolved,
                       closure=closure, publication_ref=prepared.publication_ref, purpose=purpose, actor=name,
                       trace=trace, parent_run_id=parent_run_id, run_options=run_options,
                       data_versions=versions)
    status = await _await_run(run.id, timeout_s=float(attempted["timeout_s"]))
    stages.mark("run_ms")

    # ---- 6) 结果：类型化指标 + 证据引用 + 质量 --------------------------------------------
    return await _outcome(run.id, status=status, workflow=workflow, resolved=resolved, loaded=loaded, closure=closure,
                          plans=prepared.plans, values=prepared.values, ignored=prepared.ignored,
                          publication_ref=prepared.publication_ref, purpose=purpose, attempted=attempted,
                          trace=trace, stages=stages)


async def execute_formal(
    session: AsyncSession,
    *,
    workflow_id: str,
    card_id: str,
    metric_ids: Sequence[str],
    inputs: Mapping[str, Any] | None = None,
    version: int | None = None,
    actor: str | None = None,
    timeout_s: float | None = None,
    budget: Mapping[str, Any] | None = None,
    trace: Any = None,
    parent_run_id: str | None = None,
    run_options: Mapping[str, Any] | None = None,
    data_versions: Mapping[str, Any] | None = None,
    emit: Any = None,
) -> Outcome:
    """宿主入口（K10 父 Run 用）：purpose 固定成 formal，走**同一个** `publications.judge`——发布依据不齐（缺记录、
    不是 matched、没有认证审阅、只确认了一部分、清单不是这一版这一张图的、老资产没有清单）一律拒，不换路、不降级。

    这一层不发起正式运行（正式身份由父 Run 持有）：它只按 formal 的分寸要求同一份依据，执行本身仍是进程内的固定执行。
    **资产授权那一道照样要过**（`_require_caliber`，对每个用途都判）：普通使用者拿不到这一版口径的 use 及以上时，正式运行也
    不能替它执行——先是授权、再是发布依据，两道都过才跑。父 Run 交下来的上下文
    （parent_run_id / run_options / data_versions / emit）同 `execute`，原样带进派生的子运行，晚解析的源也照 `execute` 那一套
    固定在父 Run 上（行级范围取正式这一档：看全部）。"""
    return await execute(session, workflow_id=workflow_id, card_id=card_id, metric_ids=metric_ids, inputs=inputs,
                         version=version, purpose=HOST_PURPOSE, actor=actor, timeout_s=timeout_s, budget=budget,
                         trace=trace, parent_run_id=parent_run_id, run_options=run_options,
                         data_versions=data_versions, emit=emit)


# --------------------------------------------------------------------------
# 服务端替普通使用者准备的那一条（K14）：最小父图 + 真的从 caliber_query 工具走一次
# --------------------------------------------------------------------------

#: `execute_prepared` 那张最小父图的节点 id：入口、调用工具（口径查询）、出口
PREPARED_INPUT = "start"
PREPARED_NODE = "caliber"
PREPARED_OUTPUT = "out"


def prepared_graph(workflow_id: str, *, card_id: str, metric_ids: Sequence[str],
                   inputs: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """服务端替普通使用者搭的那张**最小父图**：入口 → 调用工具（`caliber_query`）→ 出口。

    **显式绑定**，不是模型挑的：口径身份（workflow + card）、要算的指标、这次入口取值都写死在节点上，用户不碰画布。这三个
    节点都在 user_chat_run 那一份白名单里（input / tool / output），跑起来照样带遮罩与只读；能碰哪些口径仍由 QueryCore 按
    资产授权核（`_require_caliber`，工具里那一次执行再核一遍）。"""
    def node(nid: str, ntype: str, label: str, config: dict[str, Any]) -> dict[str, Any]:
        return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0},
                "data": {"label": label, "config": config}}

    args: dict[str, Any] = {"workflow": str(workflow_id), "card": str(card_id),
                            "metric_ids": [str(m) for m in metric_ids], "inputs": dict(inputs or {})}
    return {
        "nodes": [node(PREPARED_INPUT, "input", "入口", {}),
                  node(PREPARED_NODE, "tool", "按口径取数", {"tool": CALIBER_QUERY, "args": args}),
                  node(PREPARED_OUTPUT, "output", "成果",
                       {"fields": [{"name": "口径", "value": f"{{{{ nodes.{PREPARED_NODE}.ok }}}}"}]})],
        "edges": [{"source": PREPARED_INPUT, "target": PREPARED_NODE},
                  {"source": PREPARED_NODE, "target": PREPARED_OUTPUT}],
    }


async def execute_prepared(
    session: AsyncSession,
    *,
    workflow_id: str,
    card_id: str,
    metric_ids: Sequence[str],
    inputs: Mapping[str, Any] | None = None,
    version: int | None = None,
    actor: Any = None,
    timeout_s: float | None = None,
    budget: Mapping[str, Any] | None = None,
    trace: Any = None,
    origin_kind: str = "chat",
    origin: Mapping[str, Any] | None = None,
    run_options: Mapping[str, Any] | None = None,
) -> Outcome:
    """普通使用者的一条**确定请求**：服务端替它搭最小父图、经 `user_chat_run` 起一次探索父 Run，实际从 `caliber_query` 工具走。

    与 `execute` 的区别只有一件：那条路是服务端自己起一次探针运行（用户看不到图），这一条是**真的发一次父 Run**——图里有
    一个显式绑定 `caliber_query` 的调用工具节点，执行的是同一个工具（同一份固定执行、同一个子运行），证据接在父 Run 上。
    普通使用者的「模型自己挑口径」那条路是它在自己的图里绑这个工具（chat/launch 自动绑定）；这一条用于确定请求直达，不经模型。

    - 起 Run 之前判的东西和 `execute` **一模一样**（`_prepared` 同一份）：固定版本、普通使用者的资产授权
      （`querycore_not_authorized`）、发布清单、K08 闭包、K01 参数——没授权/读不出来/点错了都在起任何 Run 之前拒；
    - 父 Run 选项照普通问数：遮罩（MASK_RESULTS_OPTION）+ 只读（READ_ONLY_OPTION），记忆范围是这位使用者自己的；这次口径
      查询的预算写进父 Run 的选项（`BUDGET_OPTION`），派生的子运行照它走；`origin_kind` / `origin` 由调用方给（问数据那一轮
      是 chat + 对话/轮次），普通使用者读得到这次运行（origin_kind=chat、遮罩、自己是发起人）；
    - 交回的 Outcome 与 `execute` 同形：`run_id` 是这次起的**父 Run**（显式绑定、白名单、遮罩/只读与每条 query artifact
      的证据都在它上面），`execution_run_id` 是它派生出来真正跑那几条 SQL 的子运行；metrics / queries / problems /
      quality / status / ok 说的都是子运行那一次算成了没有；
    - 父 Run 没跑成（工具被拒、运行失败、到点没收尾）：照实交回 ok=False + 一条问题（`status="running"` 时报超时），
      **绝不把「没跑成」说成成功**；这时没有子运行可定位，也就没有 metrics。
    - `version` 点名时按**点名的那一版**固定并授权这一道；工具那条路固定的是**推荐版**（`caliber_query` 的参数里没有版本，
      读一次推荐版并当场固定）——两者对不上（推荐版在这中间换过）时照实记一条 `querycore_version_moved` 并交回 ok=False，
      不把另一版的数当这一版交出去。

    拒（没发起任何运行）抛 QueryCoreRefused；发起了就交回 Outcome。"""
    from app.auth.chat_policy import actor_name, effective_actor
    from app.chat.launch import user_chat_run
    from app.engine.launch import LaunchRefused, launch_run

    stages = _Stage()
    # 判授权用主体本身（角色与组不丢），记进运行、当记忆范围用名字：和 execute 同一套
    subject = effective_actor(actor)
    name = actor_name(actor)
    prepared, attempted = await _prepared(session, workflow_id=workflow_id, card_id=card_id, metric_ids=metric_ids,
                                         inputs=inputs, version=version, purpose=DEFAULT_PURPOSE, actor=subject,
                                         budget=budget, timeout_s=timeout_s, stages=stages)
    # 图上的口径身份用**这一层固定下来的**那一份（卡节点是核对过的源头卡，指标是闭包收窄过的那几个）
    graph = prepared_graph(prepared.resolved.workflow_id, card_id=str(prepared.loaded.node_id),
                           metric_ids=list(prepared.closure.metric_ids), inputs=inputs or {})
    # 白名单、记忆范围、遮罩与只读：和 HTTP 发起一轮问数据共用同一份（chat/launch），不在这里另抄一条
    memory_scope, chat_options, graph = await user_chat_run(session, graph, name)
    options = {BUDGET_OPTION: dict(attempted), **dict(run_options or {}), **chat_options}
    try:
        parent = await launch_run(session, graph=graph, input_payload={}, memory_scope=memory_scope,
                                  run_class="exploratory", actor=name, options=options, origin_kind=origin_kind,
                                  origin=dict(origin) if origin is not None else None)
    except LaunchRefused as e:
        # 没发起任何运行：和服务层别的拒绝同一个形状（调用方只认 QueryCoreRefused 这一种）
        raise QueryCoreRefused(e.message, EXECUTION, status_code=e.status_code) from None
    status = await _await_run(parent.id, timeout_s=float(attempted["timeout_s"]))
    stages.mark("run_ms")
    return await _prepared_outcome(parent.id, status=status, prepared=prepared, attempted=attempted, trace=trace)


async def _prepared_outcome(parent_run_id: str, *, status: str, prepared: _Prepared, attempted: Mapping[str, Any],
                            trace: Any) -> Outcome:
    """把父 Run 里那次工具调用的结果收成一份与 `execute` 同形的 Outcome（见 `execute_prepared`）。

    结果取的是那次调用的**完整产出**（封存证据里按节点取它最后一次的产出工件），不是 tool.end 那个截到 2000 个字符的
    预览：一次口径查询可能交回好几条查询，预览读不全。产出不在（父 Run 没跑成、工具没交出结果）时照实报 ok=False。"""
    payload = await _tool_payload(parent_run_id)
    if payload is None:
        return _failed_outcome(parent_run_id, status=status, prepared=prepared, attempted=attempted, trace=trace)
    problems = [dict(p) for p in payload.get("problems") or []]
    # 固定的是发布物：子运行跑的必须就是这一层固定并授权过的那一版（推荐版在这中间换过就照实说，不把另一版当这一版交出）
    ran_version = int(payload.get("version") or prepared.resolved.version)
    if ran_version != int(prepared.resolved.version):
        problems.append(_problem(
            VERSION_MOVED, f"这次执行使用的是 v{ran_version}，不是这一层固定并授权过的 "
                           f"v{int(prepared.resolved.version)}：推荐版在中途换过，请重新发起"))
    return Outcome(
        run_id=parent_run_id, status=str(payload.get("status") or status),
        ok=bool(payload.get("ok")) and not any(str(p.get("code")) == VERSION_MOVED for p in problems),
        workflow_id=str(payload.get("workflow_id") or prepared.resolved.workflow_id),
        card_id=str(payload.get("card_id") or prepared.loaded.node_id), version=ran_version,
        explicit_version=bool(payload.get("explicit_version")), purpose=str(payload.get("purpose") or DEFAULT_PURPOSE),
        publication=dict(payload.get("publication") or {}), selection=dict(payload.get("selection") or {}),
        parameters=dict(payload.get("parameters") or {}),
        metrics=tuple(dict(m) for m in payload.get("metrics") or []),
        queries=tuple(dict(q) for q in payload.get("queries") or []),
        data=dict(payload.get("data") or {}), quality=dict(payload.get("quality") or {}),
        problems=tuple(problems), trace=trace if trace is not None else payload.get("trace"),
        measurement=_measurement_of(payload.get("measurement")),
        execution_run_id=str(payload.get("run_id") or "") or None)


def _failed_outcome(parent_run_id: str, *, status: str, prepared: _Prepared, attempted: Mapping[str, Any],
                    trace: Any) -> Outcome:
    """父 Run 没交出结果（失败、还挂着、工具没交回产出）：照实交回 ok=False 的那一份，不编造指标。"""
    if status == "running":
        problems = [_problem(TIMEOUT, f"这次执行在 {attempted['timeout_s']} 秒内没有收尾：状态仍是运行中，"
                                      "请按 run_id 去看运行结果。超时不算成功")]
    else:
        problems = [_problem(EXECUTION, "这次口径查询没有交回结果（运行状态：" + status + "）")]
    return Outcome(
        run_id=parent_run_id, status=status, ok=False, workflow_id=str(prepared.resolved.workflow_id),
        card_id=str(prepared.loaded.node_id), version=int(prepared.resolved.version), explicit_version=False,
        purpose=DEFAULT_PURPOSE, publication=dict(prepared.publication_ref),
        selection=_selection_of(prepared.closure), parameters={"values": dict(prepared.values)},
        metrics=(), queries=(), data={"as_of": None, "sources": [], "tables": [], "data_versions": {},
                                      "window": {}, "read": False},
        quality={"status": "unknown"}, problems=tuple(problems), trace=trace)


async def _tool_payload(run_id: str) -> dict[str, Any] | None:
    """父 Run 里那个口径查询节点交回的完整产出；不是口径查询的结果（没跑、跑成别的样子）返回 None。"""
    from app.engine.evidence_reader import read_sealed

    try:
        sealed = await read_sealed(run_id)
    except Exception:  # noqa: BLE001 - 证据读不出来只影响这一份结果的还原，不该让调用变成 500
        logger.exception("prepare 的父运行 %s 读不出封存证据", run_id)
        return None
    payload = sealed.payload_of([PREPARED_NODE])
    if not isinstance(payload, Mapping):
        return None
    if str(payload.get("kind") or payload.get("tool") or "") != CALIBER_QUERY:
        return None
    return dict(payload)


def _measurement_of(record: Any) -> Measurement | None:
    """子运行交回来的测量记录（dict）还原成 Measurement；形状不对就当没量到（None），不填 0 冒充。"""
    if not isinstance(record, Mapping):
        return None
    phases, counts, ids = record.get("phases"), record.get("counts"), record.get("ids")
    if not all(isinstance(part, Mapping) for part in (phases, counts, ids)):
        return None
    return Measurement(phases=dict(phases), counts=dict(counts), ids=dict(ids))


# --------------------------------------------------------------------------
# 内部：清单核验、判定、发起、等待、结果
# --------------------------------------------------------------------------


def _verified_manifest(resolved: Any, *, graph: Mapping[str, Any]) -> dict[str, Any]:
    """这一版的发布清单：读得出来、内容哈希对得上、**就是这一版这一张图**的，约定和现在这一套兼容。

    老资产（这一版没有发布事件）走 GET/trial 那条老路，这里明确拒：没有清单就没有「固定发布执行」可核。"""
    publication = resolved.publication
    if publication is None:
        raise QueryCoreRefused(
            f"「{resolved.workflow_id}」v{resolved.version} 没有发布清单（加发布依据机制之前发布的老资产）："
            "QueryCore 只按固定发布执行，老资产请走原来的试运行路径", MANIFEST_REQUIRED, status_code=409)
    try:
        content = publications.read(publication)
    except publications.PublicationUnreadable as e:
        raise QueryCoreRefused(e.message, MANIFEST_UNREADABLE, status_code=409,
                               problems=[{"code": e.code}]) from None
    problems = publications.compiler_problems(content)
    if problems:
        raise QueryCoreRefused("这一版的发布清单不是按现在这套编译器/绑定/参数/解析器约定物化的，不按新规则重解释旧发布："
                               + "；".join(str(p.get("message") or p.get("code")) for p in problems[:4]),
                               INCOMPATIBLE, status_code=409, problems=problems)
    problem = publications.manifest_problem(publication, graph=graph)
    if problem is not None:
        raise QueryCoreRefused(str(problem.get("message") or "发布清单和这一版对不上"),
                               publications.BASIS_MISMATCH, status_code=409, problems=[problem])
    return content


async def _judged(session: Any, workflow: Any, resolved: Any, *, purpose: str, content: Mapping[str, Any],
                  graph: Mapping[str, Any]) -> dict[str, Any]:
    """同一次判定（publications.judge，固定到刚解析出来的那一版）：formal 依据不齐就拒，别的用途只读身份。

    判定用的版本是 `resolved.version`（已经固定），不重新解析推荐版；交回的那一份把清单引用和内容里的确定信息一起带上。"""
    judged = await publications.judge(session, workflow, version=resolved.version, purpose=purpose)
    if judged is None:
        raise QueryCoreRefused(f"「{workflow.name}」v{resolved.version} 读不出来", VERSION_MISSING, status_code=404)
    refusal = judged.refusal
    if refusal is not None:
        # formal：发布依据不齐（缺记录/不是 matched/没有认证审阅/只确认一部分/清单错图缺卡……）。机器码用 judge 的原码，
        # 让入口和正式运行的说法一致；不换路、不降级成探索执行
        raise QueryCoreRefused(refusal[1], refusal[0], status_code=409, problems=list(judged.problems))
    return {**judged.as_dict(),
            "workflow_id": resolved.workflow_id, "graph_hash": str(content.get("graph_hash") or ""),
            "execution_hash": str(content.get("execution_hash") or ""), "format": publications.FORMAT,
            "compiler": dict(content.get("compiler") or {}), "level": str(content.get("level") or ""),
            "publication_id": str(getattr(resolved.publication, "id", "") or "")}


# --------------------------------------------------------------------------
# K10：晚解析的数据源固定在**父 Run** 上
# --------------------------------------------------------------------------


def target_key(workflow_id: str, version: Any, card_id: str) -> str:
    """这次执行钉住的固定目标：`<工作流>@<版本>:<卡>`。

    父 Run 记在补固定项上，用来认出「同一次运行里的这份固定是哪一份口径要的」——换了另一份口径（另一个工作流、
    另一版、另一张卡）之后源又换过版本，就要明确说冲突，不能悄悄读另一版。"""
    return f"{workflow_id}@{int(version)}:{card_id}"


def card_sources(graph: Mapping[str, Any], card_id: str) -> tuple[set[str], bool]:
    """这一版图里那张口径卡读的取数源名，以及认不认得出来（纯函数，无 IO）。

    认得出：`card_inputs` 给出的取数节点绑的 `db_query__` / `db_schema__` 源。认不出（不是口径卡、式子里认不出取数
    节点）就退回**这一版图里绑的全部数据源**（宁可多固定几个，也不漏），照实标成没认出来。"""
    from app.engine.launch import tool_bindings
    from app.engine.schema import GraphSpec
    from app.look.calibers import card_inputs
    from app.tools.datasource import source_of_tool

    inputs, _reason = card_inputs(dict(graph), card_id)
    tools = [str(item.get("tool") or "") for item in inputs if isinstance(item, dict)]
    sure = bool(tools)
    if not sure:
        tools = [name for _, name in tool_bindings(GraphSpec.model_validate(dict(graph)))]
    return {source for tool in tools if (source := source_of_tool(tool)) is not None}, sure


async def pin_sources_for_parent(*, parent_run_id: str, resolved: Any, graph: Mapping[str, Any],
                                 card_id: str, versions: Mapping[str, Any] | None,
                                 emit: Any = None) -> dict[str, Any]:
    """这次口径那一版读的源里，父 Run 还没固定的，按**父 Run 的**类别与主体补固定进父 Run 的记录（K10）。

    为什么不能在子运行里补（tools/datasource._pin_late_records）：那条路按子运行的类别（exploratory）与主体重算
    行级范围——正式父 Run 的 all 会被收成 mine/none；而且只写进子运行那一行：同一次运行里的两次调用各固定各的版本。

    这里固定的是**父 Run 自己已经授权的那一档**（`run_class` / `started_by`，同它发起时给别的源定范围的那一套），
    只记在父 Run 上，子运行照父 Run 那一份继承——不放大也不缩小。

    - 已经固定过的源照旧复用（同一次运行里两次调用读同一版），不重新固定；
    - 已经固定过、但那次固定属于**另一份口径目标**、而这个源现在的版本已经不是固定那一版：明确拒
      （`DATA_VERSION_CONFLICT`），不静默换一版；
    - 父 Run 的记录读不回来（直接调工具的单测、在别处调用）：原样交回，不固定也不报错。

    补固定本身在父 Run 上追加一条事件（`LATE_PIN_CODE` 的日志事件，带源、版本、口径目标、以及这次判定的行级范围）：
    父 Run 的 `run.started` 已经落库、封不进去，只能追加。"""
    from sqlalchemy import select

    from app.data import table_versions
    from app.db.base import SessionLocal
    from app.db.models import DataSource, Run
    from app.tools.datasource import _late_lock

    out = dict(versions or {})
    names, _sure = card_sources(graph, card_id)
    if not names:
        return out
    key = target_key(resolved.workflow_id, resolved.version, card_id)
    pinned: list[dict[str, Any]] = []
    async with _late_lock():
        # 上传源要读当前指针到写进运行记录，持着版本存储的锁（同 tools/datasource._pin_late）
        async with table_versions.pin_lock(), SessionLocal() as own:
            run = await own.get(Run, str(parent_run_id))
            if run is None:
                return out                      # 父 Run 的记录不在了：不在这里猜，交给调用方
            stored = dict(run.data_versions or {})
            for name in sorted(names):
                row = (await own.execute(select(DataSource).where(
                    DataSource.name == name, DataSource.enabled.is_(True)))).scalar_one_or_none()
                if row is None:
                    continue                    # 名字对不上（源被改名、停用、删了）：查询自己会照实报错
                entry = stored.get(row.id)
                if _pinned_version(entry) is not None:
                    _refuse_conflict(row, entry=entry, key=key, resolved=resolved, card_id=card_id,
                                     current=await _current_version(own, row))
                    out[row.id] = entry
                    continue
                entry = await _pin_now(own, row, run_class=str(run.run_class or "exploratory"),
                                       started_by=run.started_by)
                if entry is None:
                    continue                    # 登记或数据文件不在：不固定，查询时照实报错
                entry.update(late=True, caliber=key)
                stored[row.id] = entry
                out[row.id] = entry
                pinned.append({"source_id": row.id, "source": row.name, "entry": entry})
            if pinned:
                run.data_versions = stored
                await own.commit()
    for item in pinned:
        await _announce_pin(emit, **item, key=key, resolved=resolved)
    return out


def _pinned_version(entry: Any) -> Any:
    """这一项固定的是哪一版：上传源是快照 id，记录表是表的版本号。没固定（或形状不对）为 None。"""
    if not isinstance(entry, dict):
        return None
    if isinstance(entry.get("snapshot"), str) and entry["snapshot"]:
        return entry["snapshot"]
    if isinstance(entry.get("version"), int) and isinstance(entry.get("seq"), int):
        return int(entry["version"])
    return None


async def _current_version(session: AsyncSession, row: Any) -> Any:
    """这个源现在这一版是哪一版（和 `_pinned_version` 同一种取值）。"""
    if (getattr(row, "origin", None) or "manual") == "records":
        from app.records.tables import table_of_source

        table = await table_of_source(session, row.id)
        return int(table.version) if table is not None else None
    snapshot = getattr(row, "current_snapshot_id", None)
    return str(snapshot) if snapshot else None


async def _pin_now(session: AsyncSession, row: Any, *, run_class: str, started_by: str | None) -> dict[str, Any] | None:
    """现在把这一版固定下来（和发起时固定用的是同一套）：上传源取当前快照，记录表记高水位与**父 Run 的**行级范围。"""
    from app.records.views import pin_entry

    if (getattr(row, "origin", None) or "manual") == "records":
        return await pin_entry(session, row, run_class=run_class, started_by=started_by)
    snapshot = getattr(row, "current_snapshot_id", None)
    if not snapshot:
        return None
    return {"snapshot": str(snapshot), "name": row.name}


def _refuse_conflict(row: Any, *, entry: Mapping[str, Any], key: str, resolved: Any, card_id: str,
                     current: Any) -> None:
    """这一项固定属于另一份口径目标、而这个源现在已经是另一版：明确拒，不静默换一版（同一次运行不能读两版数据）。

    消息要短：节点把它交给 describe_exception 时会按 240 个字符截断，写太长机器码就被截掉了。精确的值放在
    problems 里（程序看那一份）。"""
    owned_by = str(entry.get("caliber") or "")
    if not owned_by or owned_by == key or _pinned_version(entry) == current:
        return
    raise QueryCoreRefused(
        f"数据源「{row.name}」在这次运行里固定的是 {_version_text(row, _pinned_version(entry))}，"
        f"这次要执行的口径「v{resolved.version}:{card_id}」引用的是 {_version_text(row, current)}："
        "同一次运行不能悄悄换一版数据。请重新发起运行，或让两份口径用同一版数据",
        DATA_VERSION_CONFLICT, status_code=409,
        problems=[{"code": DATA_VERSION_CONFLICT, "source": row.name, "pinned": _pinned_version(entry),
                   "current": current, "caliber": owned_by, "target": key}])


def _version_text(source: Any, value: Any) -> str:
    """报错里那一版的写法：上传源给快照 id 的前 12 位，记录表给版本号。"""
    if value is None:
        return "（读不出来）"
    if (getattr(source, "origin", None) or "manual") == "records":
        return f"记录表第 {value} 版"
    return f"快照 {str(value)[:12]}"


async def _announce_pin(emit: Any, *, source_id: str, source: str, entry: Mapping[str, Any], key: str,
                        resolved: Any) -> None:
    """把这次补固定写进父 Run 的事件流（`run.started` 已经落库，补不进去，只能追加）。"""
    import inspect

    if emit is None:
        return
    from app.core.events import EventType
    from app.tools.datasource import LATE_PIN_CODE

    pinned_text = (str(entry.get("snapshot")) if entry.get("snapshot")
                   else f"记录表第 {entry.get('version')} 版、高水位 {entry.get('seq')}")
    data = {"source_id": source_id, "source": source, "late": True, "caliber": key, "pinned_by": "caliber_query",
            "workflow_id": resolved.workflow_id, "version": resolved.version,
            **{k: entry.get(k) for k in ("snapshot", "seq", "rows", "viewer", "version") if entry.get(k) is not None}}
    note = (f"数据源「{source}」发起时没有固定版本，这次口径查询在父运行上固定为当时的那一版（{pinned_text}），"
            f"行级范围照父运行的授权（{entry.get('rows') or '不适用'}）；之后继续运行、审批恢复、同一次运行里别的调用"
            "都沿用这一版")
    try:
        out = emit(EventType.LOG, level="info", code=LATE_PIN_CODE, message=note, **data)
        if inspect.isawaitable(out):
            await out
    except Exception:  # noqa: BLE001 - 事件只是留痕，固定本身已经记进运行记录
        logger.warning("补固定的父 Run 事件没发出去（口径 %s@%s）", resolved.workflow_id, resolved.version,
                       exc_info=True)


async def _start(graph_spec: Mapping[str, Any], *, values: Mapping[str, Any], workflow: Any, resolved: Any,
                 closure: Any, publication_ref: Mapping[str, Any], purpose: str, actor: str | None,
                 trace: Any, parent_run_id: str | None = None, run_options: Mapping[str, Any] | None = None,
                 data_versions: Mapping[str, Any] | None = None) -> Any:
    """起一次只读的探索运行：来源按 engine/origin 的常量记（口径固定执行），
    origin 数据里带上这次固定的版本、清单、闭包身份、用途与 trace。

    K10 父 Run 那条路（`caliber_query`）：派生的探针运行是**明确的子运行**——origin 里记 `parent_run_id`，选项、固定的数据
    版本照父 Run 原样带下去（遮罩、数据目录视图、上传/记录源版本固定一条不断），只读边界照旧一律加上。"""
    from app.core.events import EventType
    from app.engine.origin import origin_note
    from app.engine.runner import run_manager
    from app.tools.datasource import READ_ONLY_OPTION

    origin = {
        "service": "querycore", "purpose": purpose, "workflow_id": resolved.workflow_id,
        "version": resolved.version, "node_id": closure.source.get("card_id"),
        "card": closure.source.get("card_id"),
        "manifest": publication_ref.get("manifest"), "identity": publication_ref.get("identity"),
        "selection_hash": closure.selection_hash, "closure_hash": closure.closure_hash,
        "metric_ids": list(closure.metric_ids), "query_ids": list(closure.query_ids),
        **({PARENT_RUN_KEY: str(parent_run_id)} if parent_run_id else {}),
        **({"trace": trace} if trace is not None else {}),
    }
    data = {**origin, "actor": actor}
    kind = origin_kind()
    notes = [(EventType.RUN_ORIGIN, None, origin_note(kind, **data))]
    # 父 Run 的选项照原样带下去（遮罩、数据目录视图……），只读边界一律加上：这次执行的取数一律只读
    options = {**dict(run_options or {}), READ_ONLY_OPTION: True}
    try:
        return await run_manager.start(
            graph=dict(graph_spec), input_payload=dict(values), workflow_id=resolved.workflow_id,
            workflow_name=str(workflow.name or ""), run_class="exploratory", version=resolved.version,
            version_hash=str(getattr(resolved.row, "graph_hash", "") or ""), started_by=actor,
            options=options, data_versions=dict(data_versions) if data_versions is not None else None,
            notes=notes, origin_kind=kind, origin=origin)
    except ValueError as e:
        raise QueryCoreRefused(f"这次固定执行的图过不了校验：{e}", EXECUTION, status_code=409) from None


async def _await_run(run_id: str, *, timeout_s: float) -> str:
    """进程内等这次运行收尾：先等执行任务（不阻塞事件循环），没完就看库里的状态，到点交回 running。

    **超时不算成功**：还没到终态就照实回 running，run_id 交给调用方自己去看（同试跑接口的分寸）。"""
    from app.db.base import SessionLocal
    from app.db.models import Run
    from app.engine.runner import run_manager

    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(0.0, float(timeout_s))
    while True:
        async with SessionLocal() as poll:
            row = await poll.get(Run, run_id)
        status = str(row.status) if row is not None else "unknown"
        if row is None or status in TERMINAL_RUN_STATUSES:
            return status
        remaining = deadline - loop.time()
        if remaining <= 0:
            return "running"
        await run_manager.wait_idle(run_id, timeout=min(remaining, 0.5))


async def _outcome(run_id: str, *, status: str, workflow: Any, resolved: Any, loaded: Any, closure: Any,
                   plans: Mapping[str, _Plan], values: Mapping[str, Any], ignored: Sequence[str],
                   publication_ref: Mapping[str, Any], purpose: str, attempted: Mapping[str, Any],
                   trace: Any, stages: _Stage) -> Outcome:
    """收尾：按运行状态取证据、拼类型化结果、判「算成了没有」，并把这次量到的收成一份测量记录（K13）。"""
    from app.db.base import SessionLocal
    from app.db.models import Run
    from app.engine.validation import validation_report

    problems: list[dict[str, Any]] = []
    metrics: list[dict[str, Any]] = []
    raw_metrics: dict[str, Any] = {}
    queries: list[dict[str, Any]] = []
    trusted = False
    #: 这一次收尾里几处共用的封存证据（见 _sealed_of）：同一份快照不读三遍
    memo: dict[str, Any] = {}
    async with SessionLocal() as poll:
        row = await poll.get(Run, run_id)
    finished = row.finished_at.isoformat() if row is not None and row.finished_at else None
    error = row.error if row is not None else None
    # K13：排队时长读运行记录上的那一项（老运行、还没收尾的运行没有它就是 None）；其余几样要读到执行证据才有
    facts: dict[str, Any] = {"queue_ms": _queue_of(row), "database_ms": None, "llm_calls": None, "executed_sql": None}
    if row is None:
        # 运行记录读不回来（被清掉之类）：交不回结果，也绝不冒充成功
        problems.append(_problem(NO_RUN, "这次执行的运行记录不在了：交不回结果"))
    elif status == "succeeded":
        try:
            metrics, raw = await _metrics_from(run_id, card_id=str(loaded.node_id), metric_ids=closure.metric_ids,
                                               memo=memo)
            trusted = bool(raw.get("trusted"))
            raw_metrics = raw.get("metrics") or {}
            if not trusted:
                problems.append(_problem(EVIDENCE, "这次运行的封存证据核验没通过：不能把结果当已核验的值交出"))
        except Exception:  # noqa: BLE001 - 证据读不出来（工件被改、读不回来）也不该让请求变成 500
            logger.exception("QueryCore 执行 %s 的指标证据读不出来", run_id)
            problems.append(_problem(EVIDENCE, "这次运行的指标证据读不出来：不能把结果交出"))
        try:
            queries = await _queries_from(run_id, closure=closure, plans=plans, memo=memo)
        except Exception:  # noqa: BLE001 - 查询快照读不出来：交不回证据引用，照实说
            logger.exception("QueryCore 执行 %s 的查询证据读不出来", run_id)
            problems.append(_problem(QUERY_MISSING, "这次运行的查询快照读不出来：证据引用交不回来"))
        try:
            # K13：数据库那一截（取数节点自己的执行时长）与真发出去的 SQL 条数按封存事件与逐次记录量。封存都得
            # 到这一步了还读不出来，就留 None——量不到是量不到，不拿计划条数或 0 顶上
            facts.update(_from_evidence(await _sealed_of(run_id, memo), closure))
        except Exception:  # noqa: BLE001 - 量不出来不该影响结果本身
            logger.exception("QueryCore 执行 %s 的执行量测不出来", run_id)
    elif status == "running":
        problems.append(_problem(TIMEOUT, f"这次执行在 {attempted['timeout_s']} 秒内没有收尾：状态仍是运行中，"
                                         "请按 run_id 去看运行结果。超时不算成功"))
    else:
        problems.append(_problem(EXECUTION, error or "这次执行没有成功完成", status=status))

    present = {str(m.get("id")) for m in metrics}
    if row is not None and status == "succeeded":
        # 指标/查询层面的问题只在「跑完了」时才说：没跑完谈不上缺指标，那条已经由 TIMEOUT/EXECUTION/NO_RUN 说了
        for metric_id in closure.metric_ids:
            if metric_id not in present:
                problems.append(_problem(METRIC_MISSING, f"点名的指标「{metric_id}」没有出现在结果里",
                                         metric_id=metric_id))
                continue
            if str(next(m for m in metrics if str(m.get("id")) == metric_id).get("status") or "") != "ok":
                problems.append(_problem(METRIC_NO_VALUE, f"指标「{metric_id}」这次没有可用的标量值",
                                         metric_id=metric_id))
        if any(m.get("masked") for m in metrics):
            problems.append(_problem(METRIC_MASKED, "有指标的值被遮罩：不把遮罩值当结果交出"))
        missing_queries = [q["query_id"] for q in queries if not q.get("found")]
        if missing_queries:
            problems.append(_problem(QUERY_MISSING, "这次要执行的查询没有留下快照：" + "、".join(missing_queries),
                                     query_ids=missing_queries))
        for query in queries:
            if query.get("rebound"):
                problems.append(_problem(REBOUND, f"查询「{query['query_id']}」：{query['rebound']}",
                                         query_id=query["query_id"]))
        if not metrics:
            problems.append(_problem(EMPTY, "这次执行没有任何指标结果：空结果不算成功"))

    quality = validation_report(list(raw_metrics.values()), execution=status, trusted=trusted)
    parameters = {
        "adapter": _plans_adapter(plans),
        "values": dict(values),
        "ignored": list(ignored),
        "contract": [dict(c) for c in _contract_of(loaded)],
        "queries": [plan.as_dict() for plan in plans.values()],
    }
    measurement = _measurement(stages, run_id=run_id, workflow_id=str(resolved.workflow_id),
                               card_id=str(loaded.node_id), version=int(resolved.version), closure=closure,
                               status=status, problems=problems, queries=queries, facts=facts, trace=trace)
    return Outcome(
        run_id=run_id, status=status, ok=not problems and status == "succeeded" and bool(metrics),
        workflow_id=str(resolved.workflow_id), card_id=str(loaded.node_id), version=int(resolved.version),
        explicit_version=bool(resolved.explicit), purpose=purpose, publication=dict(publication_ref),
        selection=_selection_of(closure), parameters=parameters, metrics=tuple(metrics), queries=tuple(queries),
        data=_data_of(queries, loaded=loaded, values=values, finished=finished),
        quality=quality, problems=tuple(problems), trace=trace, measurement=measurement)


def _plans_adapter(plans: Mapping[str, _Plan]) -> str:
    from app.ledger import params

    for plan in plans.values():
        if plan.material.get("adapter"):
            return str(plan.material["adapter"])
    return params.ADAPTER


def _contract_of(loaded: Any) -> list[dict[str, Any]]:
    from app.ledger import params

    return params.contract(loaded.entry_fields)


def _selection_of(closure: Any) -> dict[str, Any]:
    """这次的 selection/closure 身份：算哪些指标、执行哪几条查询（含共享 SQL 还在算谁）、解析器版本与两份哈希。"""
    return {**closure.summary(), "card_metric_ids": list(closure.card_metric_ids)}


def _data_of(queries: Sequence[Mapping[str, Any]], *, loaded: Any, values: Mapping[str, Any],
             finished: str | None) -> dict[str, Any]:
    """这次数据读了什么、什么时候读的：来源、表、上传源的数据版本、这次的时间窗口。"""
    from app.ledger import params

    sources: set[str] = set()
    tables: set[str] = set()
    versions: dict[str, Any] = {}
    for query in queries:
        if query.get("data_source"):
            sources.add(str(query["data_source"]))
        tables.update(str(t) for t in query.get("tables") or [])
        if query.get("data_version") and query.get("data_source"):
            versions[str(query["data_source"])] = query["data_version"]
    declared = params.declared(loaded.entry_fields)
    window = {str(name): value for name, value in values.items()
              if (params.field_type(declared.get(name) or {}) in _TIME_TYPES
                  or (isinstance(value, str) and _DATE_LIKE.match(value.strip())))}
    return {"as_of": finished, "sources": sorted(sources), "tables": sorted(tables),
            "data_versions": versions, "window": window, "read": bool(queries)}


# --------------------------------------------------------------------------
# K10：父 Run 的工具入口（caliber_query）
# --------------------------------------------------------------------------


class CaliberQueryArgs(BaseModel):
    """caliber_query 的参数：口径身份（workflow + card）、要算的指标、这次入口的类型化取值。

    **参数到此为止**：用途（formal/compose）、认证主体、父 Run 身份、固定的数据版本都由父 Run 的上下文定，参数里没有
    也传不了；点名版本留给别的调用方，父 Run 走的是「读一次推荐版并当场固定」。"""

    workflow: str = Field(description="口径所在的工作流：工作流 id，或唯一的工作流名字")
    card: str = Field(description="工作流里那张口径卡的节点 id：一次只算一张卡")
    metric_ids: list[str] = Field(min_length=1, description="这次要算的指标 id，可以多个；空的不当整卡")
    inputs: dict[str, Any] = Field(
        default_factory=dict,
        description="这次入口的取值：{字段：值}，按入口声明的类型校验（日期写 YYYY-MM-DD；跨源穿透传入对侧编码或标准实体键时由平台自动对齐换算）",
    )


@register(
    name=CALIBER_QUERY,
    category="口径",
    effect="read",
    description="按一份已发布的固定口径取一次数（工作流 + 口径卡 + 指定指标 + 入口取值），交回类型化的指标值和每条查询的"
                "证据引用——数出自哪一版、哪份发布清单、哪条 SQL 的执行都核得到。只读，不改数据。",
    args_schema=CaliberQueryArgs,
)
async def caliber_query(ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
    """父 Run 的工具入口（K10）：用**父 Run 的上下文**按固定口径执行一次，当场 await 收尾，把查询证据接回父 Run。

    - 用途由父 Run 定：`formal` 的父 Run 走 `execute_formal`（同一套 `publications.judge`，发布依据不齐就拒，不换路、
      不降级成探索），别的（交互、画布、试用）走 compose——拿不到正式身份；
    - 父 Run 固定的上传/记录源版本、选项（遮罩、只读、数据目录视图）与来源照原样带进派生的子运行（origin 里记
      `parent_run_id`），不失去父关系；超时、失败、遮罩、空指标照实交回 `ok=False`，绝不冒充成功；
    - 交回的结果与交互工作台同一种形状：metrics + queries 数组（每条带 query_id / execution_id / attempt、工件、来源、
      参数材料），节点据此把每条 query artifact 以合法引用记进父 Run 的证据。"""
    extra_entity_inputs = kwargs.pop("entity_inputs", None)
    extra_from_source = kwargs.pop("from_source", None)
    args = CaliberQueryArgs(**kwargs)
    from app.db.base import SessionLocal
    from app.db.models import Run

    parent = str(getattr(ctx, "run_id", "") or "")
    if not parent:
        raise QueryCoreRefused("caliber_query 只能在一次运行里调用：没有父运行，就没有证据可以接回去",
                               NO_PARENT, status_code=409)
    try:
        async with SessionLocal() as session:
            # 用途、主体、选项、固定的数据版本一律以**父运行的记录**为准（engine/runner 存下的那一份），ToolContext
            # 里那几项只当兜底：工具在哪条路上被调（调用工具节点、Agent、协作成员）都拿到同一个答案，不会被某个节点
            # 少的几项悄悄换掉。父运行记录读不回来（单测直接调工具）就按 ToolContext 给的算。
            parent_row = await session.get(Run, parent)
            if parent_row is not None:
                run_class = str(parent_row.run_class or "")
                options = dict(parent_row.options or {})
                versions = parent_row.data_versions
                actor = parent_row.started_by
                trace = parent_row.origin
            else:
                run_class = str(getattr(ctx, "run_class", "") or "")
                options = dict(getattr(ctx, "run_options", None) or {})
                versions = getattr(ctx, "data_versions", None)
                actor = getattr(ctx, "started_by", None)
                trace = getattr(ctx, "origin", None)
            purpose = HOST_PURPOSE if run_class == "formal" else DEFAULT_PURPOSE
            declared_budget = options.get(BUDGET_OPTION)
            workflow_id = await _workflow_id(session, args.workflow)
            effective_inputs = dict(args.inputs or {})
            alignments: list[dict[str, Any]] = []
            if effective_inputs or extra_entity_inputs:
                from app.auth.chat_policy import restricted_actor
                from app.tools.ontology import align_caliber_inputs

                is_restricted = await restricted_actor(session, actor)
                effective_inputs, alignments = await align_caliber_inputs(
                    session,
                    workflow_id=workflow_id,
                    card_id=args.card,
                    inputs=effective_inputs,
                    entity_inputs=extra_entity_inputs if isinstance(extra_entity_inputs, Mapping) else None,
                    from_source=str(extra_from_source) if extra_from_source else None,
                    restricted=is_restricted,
                )
            outcome = await execute(
                session, workflow_id=workflow_id, card_id=args.card, metric_ids=args.metric_ids,
                inputs=effective_inputs,
                purpose=purpose, actor=actor, parent_run_id=parent, run_options=options,
                data_versions=versions if isinstance(versions, Mapping) else None,
                budget=declared_budget if isinstance(declared_budget, Mapping) else None, trace=trace,
                # 晚解析的源（发布得晚、推荐版变了、动态引用）在父 Run 上补固定：把这次补固定追加进父 Run 的事件流
                emit=getattr(ctx, "emit", None))
    except QueryCoreRefused as e:
        # 拒（没发起）在工具这一层是一条失败，不是一份结果：机器码带在报错里，节点、Agent 的失败信息按它认这一档，
        # 不用去解析中文
        raise ValueError(f"{e.message}（{e.code}）") from None
    out_payload: dict[str, Any] = {"tool": CALIBER_QUERY, "kind": CALIBER_QUERY, **outcome.as_dict()}
    if alignments:
        out_payload["ontology_alignment"] = alignments
    violated = [m for m in out_payload.get("metrics") or [] if isinstance(m, Mapping) and m.get("contract_findings")]
    emit_fn = getattr(ctx, "emit", None)
    if violated and callable(emit_fn):
        import inspect
        from app.core.events import EventType

        first_finding = (violated[0].get("contract_findings") or [""])[0]
        detail = (
            str(first_finding.get("detail") or first_finding.get("code") or "触发指标数值契约异常")
            if isinstance(first_finding, Mapping)
            else (str(first_finding) or "触发指标数值契约异常")
        )
        emitted = emit_fn(
            EventType.LOG,
            level="warn",
            code="metric_contract_violated",
            workflow_id=out_payload.get("workflow_id"),
            card_id=out_payload.get("card_id"),
            metric=violated[0].get("id"),
            metrics=[m.get("id") for m in violated],
            metric_names=[m.get("name") or m.get("id") for m in violated],
            reason=detail,
            message="指标" + "".join(f"「{m.get('name') or m.get('id')}」" for m in violated) + f"触发数值契约告警：{detail}",
        )
        if inspect.isawaitable(emitted):
            await emitted
    return out_payload


async def _workflow_id(session: AsyncSession, ref: str) -> str:
    """工具参数里的工作流：先按 id 认；认不到再按名字认，且只有一个同名时才认（多个同名照实拒，不替它挑一个）。"""
    from sqlalchemy import select

    from app.db.models import Workflow

    ref = str(ref or "").strip()
    workflow = await session.get(Workflow, ref)
    if workflow is not None:
        return workflow.id
    found = list((await session.execute(select(Workflow).where(Workflow.name == ref))).scalars())
    if not found:
        raise QueryCoreRefused(f"找不到口径所在的工作流「{ref}」：可能已被删除，或名字写错了",
                               NOT_FOUND, status_code=404)
    if len(found) > 1:
        raise QueryCoreRefused(f"有 {len(found)} 个工作流都叫「{ref}」：请用工作流 id 点名要哪一份口径",
                               NOT_FOUND, status_code=409)
    return found[0].id


def query_entries(payload: Any) -> list[dict[str, Any]]:
    """`caliber_query` 交回的结果里的查询条目——**多条**（一张卡这一次可能执行好几条取数）。

    不是 caliber_query 的结果（名字不对、没有 queries 数组）返回空列表：调用方按工具名认身份，不看结果长得像不像。
    单条查询的数据源工具走 engine/evidence.query_entry_fields，不走这里；两支都由 capabilities.registry.query_items
    摊成同一种条目，再由 snapshot_owned 逐条认工件。"""
    data = payload
    if isinstance(data, str):
        text = data.lstrip()
        if not text.startswith("{"):
            return []
        try:
            data = json.loads(text)
        except ValueError:
            return []
    if not isinstance(data, dict) or str(data.get("kind") or data.get("tool") or "") != CALIBER_QUERY:
        return []
    out: list[dict[str, Any]] = []
    for item in data.get("queries") or []:
        if not isinstance(item, Mapping) or not item.get("artifact"):
            continue
        out.append({
            "artifact": str(item["artifact"]),
            # 数据源的源名：和数据源查询的条目写在同一个位置（证据面板、遮罩、数据目录按它认源）
            "source": item.get("data_source"),
            # 这条查询在这份口径里的出处（workflow + 版本 + 取数节点）：数据源条目没有这一项
            "origin": dict(item["source"]) if isinstance(item.get("source"), Mapping) else None,
            "columns": [str(c) for c in item.get("columns") or []],
            "rows": int(item.get("row_count") or 0), "truncated": bool(item.get("truncated")),
            "query_id": item.get("query_id"), "execution_id": item.get("execution_id"),
            "attempt": item.get("attempt"),
            **({"call_id": item["call_id"]} if item.get("call_id") else {}),
            # 复算要的材料：这次参数输入的哈希、源模板哈希、承载方式与绑定指纹（值本身与 SQL 正文都不在这里）
            **({"parameters": dict(item["parameters"])} if isinstance(item.get("parameters"), Mapping) else {}),
            **({"tables": [str(t) for t in item["tables"]]} if item.get("tables") else {}),
            **({"schema_artifact": item["schema_artifact"]} if item.get("schema_artifact") else {}),
        })
    return out


__all__ = ["BUDGET_OPTION", "BUDGET_REFUSED", "CALIBER_QUERY", "CARD_MISSING", "DEFAULT_MAX_QUERIES",
           "DEFAULT_PURPOSE", "DEFAULT_TIMEOUT_S", "EMPTY", "EVIDENCE", "EXECUTION", "HOST_PURPOSE", "INCOMPATIBLE",
           "INPUTS_REFUSED", "MANIFEST_REQUIRED", "MANIFEST_UNREADABLE", "METRIC_MASKED", "METRIC_MISSING",
           "METRIC_NO_VALUE", "NOT_AUTHORIZED", "NOT_FOUND", "NOT_PUBLISHED", "NO_PARENT", "NO_RUN",
           "OUTPUT_UNSUPPORTED", "PARAMETERS_REFUSED", "PARENT_RUN_KEY", "PHASES", "PREPARED_INPUT", "PREPARED_NODE",
           "PREPARED_OUTPUT", "QUERY_MISSING", "REBOUND", "REFUSAL_CODES", "SELECTION_REFUSED", "UNKNOWN_INPUT",
           "VERSION_MISSING", "VERSION_MOVED", "CaliberQueryArgs", "Measurement", "Outcome", "QueryCoreRefused",
           "caliber_query", "exact", "execute", "execute_formal", "execute_prepared", "origin_kind", "prepared_graph",
           "query_entries", "typed"]
