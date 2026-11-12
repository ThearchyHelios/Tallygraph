"""开登录后 user 角色的问数据运行能用什么（D-c，§3.12「问数据、助手」那一行，Q2）。

管理员开了「使用者可以问数据」之后，user 角色经 POST /api/runs 发起的只能是问数据当场搭的探索运行（api/runs.py 的
start_run 先查来源和运行类别）。图本身再按这里收窄，理由是同一条：对 user 角色来说遮罩就是边界（A7 第 6 条），它能
碰的只有「读数据、整理、撰写」这一类，不能执行代码、不能调外部系统、不能写任何东西：

- 节点只许：输入、模型、Agent、报告、输出、知识库检索、数据整形、条件分支、口径卡、合并查询、分析卡、调用工具。代码、协作、
  子工作流、循环、人工审批、记忆写入、校验、双盲复算、反方审稿一律不许——后几种不是危险，而是问数据用不到，多放一种
  就多一处要单独核对遮罩的地方；
- 工具（Agent 绑的、调用工具节点调的同一份白名单，engine/launch.tool_bindings 一并列出）只许数据源工具（db_query__ /
  db_schema__，查询结果按 MASK_RESULTS_OPTION 在工具边界遮）和只读的内置工具（计算、当前时间、JSON 取值、知识库检索、
  记忆读取、分析、口径查询）。分析工具不查库，只读本节点查过、在工具边界遮过的结果，遮罩列不当维度、度量（app/tools/analysis.py）；
  分析卡读上游查询节点的快照，同一套遮罩。**口径查询**（caliber_query，app/ledger/querycore.py）只读、按一份已发布口径取数：
  它按运行的类别选用途、用发起人当主体，派生的子运行照父 Run 的遮罩与只读边界走，**能碰哪些口径由 QueryCore 统一按资产授权
  核**（apps/access.caliber_role：普通使用者要 use 及以上，没有授权当场拒）——白名单放开它不等于放开数据，无权口径在这一层拦。
  自定义工具、MCP、文件、网页、HTTP、代码执行、平台的写入能力（records_append
  这类）一律不许；模板拼出来的工具名（{{ vars.tool }}）发起时不知道是谁，也不许。调用工具节点放行的理由：它和 Agent 调同一个查询工具等价——同一个
  ToolContext 带着遮罩、只读两个开关（engine/nodes/tools.py 的 _tool_ctx），参数由人写死反而比模型挑的更好核对；
  周期看板、异常处置这些模板的取数节点就是它（个人应用和问数据同一份白名单，blueprints/personal.policy_problem）；
- 数据源查询一律只读：可写源上的写语句由守卫拒掉，不交给审批（api/runs.py、个人应用刷新、个人蓝图试运行给运行打上
  READ_ONLY_OPTION）——Agent、调用工具节点的 approval 是发起人自己写在图里的，写成 never 就不问人了。能力治理里改成
  「写入」的工具在只读运行里同样不执行（调用工具节点在走动作流程之前拒，Agent 建工具时拒）；
- 记忆范围、知识库不由发起人挑：记忆和知识库里的内容不经遮罩，搭建者的 Agent 存进去的查询原文，换个范围就读到了。
  记忆固定为这位使用者自己的范围（chat_memory_scope；问数据里写不了记忆，它读到的只会是空的），知识库固定为设置里的
  运行默认值。节点上、图级 defaults 里、调用工具节点的参数里写了 memory_scope / collection 的图不许发起，请求体里带的
  同样拒（api/runs.py）。这只是发起前的一道：根子在运行时——遮罩、只读的运行里范围由运行定，节点配置改不了，kb_search
  的 collection 参数（参数名纠错改出来的、模型挑的）一律拒（tools/registry.ToolContext.fixed_scope）。

没在这里列出的节点类型、工具，以后加了也默认不许（白名单），要放开得先想清楚遮罩怎么守。

**谁是「普通使用者」（restricted_actor）。** 上面这些收窄只对开登录后的 user 角色成立：builder / admin / 关登录由各自的入口
显式放行，资产授权函数（apps/access.caliber_role / usable_calibers）不管这几档。发起运行、准备一轮问数据、读一份口径清单的
地方手上不都是 Principal（服务端任务只记着一个用户名），所以这里按 actor 判一次「是不是要按授权收窄的那一档」，判完再把
资产授权交给 apps/access——两件事一处是「谁」，一处是「有没有把这一版口径给他」。判定与 `api/deps.restricted()` 同一条规则、
同一个来源：**角色以 `Principal.role` 为准**（访问密钥认出来的身份封顶在使用者，不回库覆写；角色快照与身份不一致时以身份
为准），只有手上**只有一个用户名**时才回库里认角色与停用。
"""
from __future__ import annotations

from typing import Any

from app.analysis.contract import TOOL_NAMES
from app.ledger.querycore import CALIBER_QUERY
from app.tools.metrics import METRIC_QUERY
from app.tools.ontology import ONTOLOGY_TOOLS

#: 问数据运行里 user 角色能用的节点类型（engine/schema.py 的 NodeType 取值）
CHAT_NODE_TYPES = frozenset({"input", "llm", "agent", "report", "output", "retrieve", "transform", "branch",
                             "metrics", "merge", "analysis", "tool"})
#: Agent 能绑的只读内置工具（tools/builtin/）：分析工具也在内（contract.TOOL_NAMES，不查库），以及口径查询
#: （querycore.CALIBER_QUERY，只读、按资产授权收窄——见文件开头）、独立语义指标查询（METRIC_QUERY，只读、经数据源遮罩）
#: 和本体邻居/源健康探针（ONTOLOGY_TOOLS，只读、按授权与遮罩收窄）。工具名只从登记处取常量，不在这里另写一个字符串
CHAT_BUILTIN_TOOLS = frozenset({"calculator", "current_time", "json_query", "kb_search", "memory_recall",
                                CALIBER_QUERY, METRIC_QUERY, *ONTOLOGY_TOOLS, *TOOL_NAMES.values()})
#: 上面那几个内置工具给人看的说法（拒绝时的报错、提示词里都写它）
CHAT_BUILTIN_LABEL = "计算、当前时间、JSON 取值、知识库检索、记忆读取、分析、口径查询、指标查询、本体邻居、数据源健康"
#: 运行时改记忆范围、知识库的配置键（engine/nodes 的 ctx.cfg("memory_scope") / ctx.cfg("collection")，图级 defaults 同样生效）
SCOPE_KEYS = ("memory_scope", "collection")
SCOPE_REFUSED = "没有权限：使用者在问数据中不能指定记忆范围和知识库，使用的是管理员设好的默认值"


def chat_memory_scope(actor: str | None) -> str:
    """user 角色问数据运行的记忆范围：每人一份，和搭建者的不相通。"""
    return f"chat:{actor or 'anonymous'}"


def _tool_allowed(name: str) -> bool:
    from app.tools.datasource import source_of_tool

    return name in CHAT_BUILTIN_TOOLS or bool(source_of_tool(name))


def _set(value: Any) -> bool:
    """配置里填了值。和运行时 NodeContext.cfg 同一个规则：只有 None、空串算没填——全是空白的串运行时也照用。"""
    return value not in (None, "")


def chat_graph_problem(graph: Any, *, where: str = "问数据") -> str | None:
    """这张图 user 角色能不能在问数据里运行：能用返回 None，不能用交回一句给人看的原因（403 的 detail）。

    图不成形（GraphSpec 校验不过）时也返回 None：交给 launch_run 照常报校验问题——两边用的是同一个 GraphSpec，过不了
    校验的图本来就发起不了。

    where 是说法里的「在哪」：个人应用（blueprints/personal.py）用的是同一份白名单，只把「问数据」换成「个人应用」。"""
    from app.engine.launch import node_setting, tool_bindings
    from app.engine.schema import GraphSpec, type_label

    if not isinstance(graph, dict):
        return None
    try:
        spec = GraphSpec.model_validate(graph)
    except ValueError:
        return None
    scope_refused = SCOPE_REFUSED.replace("问数据", where)
    if any(_set(spec.defaults.get(key)) for key in SCOPE_KEYS):
        return scope_refused
    for node in spec.nodes:
        if str(node.type) not in CHAT_NODE_TYPES:
            return (f"没有权限：使用者在{where}中不能使用「{type_label(node.type)}」节点（「{node.title}」）。"
                    "如需这类处理，请联系搭建者做成应用")
        if any(_set(node.config.get(key)) for key in SCOPE_KEYS):
            return f"{scope_refused}（「{node.title}」）"
        # 调用工具节点的参数直接交给工具：kb_search 的 collection 这类参数写了，同样是自己挑了范围。取值和运行时同一个规则
        # （node_setting：节点上没写就取图级 defaults 里的 args）。参数名写错、运行时被纠成 collection 的，这里认不出——
        # 运行时那一道由工具自己拒（tools/registry.ToolContext.fixed_scope，kb_search 只能检索运行定下的知识库）
        args = node_setting(spec, node, "args") if str(node.type) == "tool" else None
        if isinstance(args, dict) and any(_set(args.get(key)) for key in SCOPE_KEYS):
            return f"{scope_refused}（「{node.title}」）"
    for who, name in tool_bindings(spec):
        if "{{" in name:
            return f"没有权限：{who}的工具名由模板拼成，发起前无法确定是哪个工具，使用者在{where}中不能这样绑定工具"
        if not _tool_allowed(name):
            return (f"没有权限：{who}绑定了工具「{name}」。使用者在{where}中只能使用数据源查询和只读的内置工具"
                    f"（{CHAT_BUILTIN_LABEL}）")
    return None


# --------------------------------------------------------------------------
# 谁是「普通使用者」：要按资产授权收窄的那一档
# --------------------------------------------------------------------------


def _name_of(actor: Any) -> str:
    """actor 是 Principal 就取它的用户名，是字符串就当用户名，别的按没有名字算。"""
    if isinstance(actor, str):
        return actor.strip()
    return str(getattr(actor, "username", "") or "").strip()


def effective_actor(actor: Any = None) -> Any:
    """这次是谁：调用方显式给的用它（用户名或 Principal），没给就取当前请求的身份（中间件写好的 `current_principal`）。

    调用方常常只拿得到一个记在运行上的名字（服务端任务、父 Run），HTTP 那边则什么也不用传——两处都要先归一成同一个
    主体，再判「是不是普通使用者」和「资产授权有没有把这一版口径给他」，不然两处判的会是两个人。"""
    if actor is not None:
        return actor
    from app.api.deps import current_principal

    return current_principal()


def actor_name(actor: Any = None) -> str | None:
    """这次是谁的**名字**（记进运行的发起人、当认证主体）：没给就取当前请求的身份；说不上来是 None。

    认不出的名字照样是一份「没有名字」的答案，不在这里当放行：`restricted_actor` 与资产授权都按普通使用者算，
    查不到主体就是没有授权（querycore 的 `_require_caliber` 当场拒）。"""
    return _name_of(effective_actor(actor)) or None


def _is_limited(principal: Any) -> bool:
    """一个**已经认出来的身份**要不要按资产授权收窄：和 `api/deps.restricted()` 同一条规则，角色只以 `Principal.role` 为准。

    不回库覆写它：访问密钥认出来的身份封顶在使用者（auth/keys.principal_for_key 一律 role="user"），拿它的用户名去库里查
    会查到那个人是管理员、把这一档又抬回去——那是绕过。说不上是谁（没有名字）也算收窄（宁可少给一点）。"""
    if not _name_of(principal):
        return True
    return principal.mode != "off" and not principal.at_least("builder")


async def _limited_name(session: Any, name: str) -> bool:
    """手上只有用户名时（服务端任务、父 Run 记下来的名字）回库里认：角色、停用。停用、认不出这个人都按收窄算。

    这一条只在**没有更好的身份**时走（`restricted_actor` 里那两个身份都在时取更严的那一个）。"""
    if not name:
        return True
    from sqlalchemy import select

    from app.db.models import User

    user = (await session.execute(select(User).where(User.username == name))).scalars().first()
    if user is None or user.disabled:
        return True
    return str(user.role) not in ("admin", "builder")


async def restricted_actor(session: Any, actor: Any = None) -> bool:
    """这次是不是「普通使用者」（开登录后不是 builder / admin 的那一档）：是的话，口径能不能用按资产授权判
    （apps/access.caliber_role / usable_calibers），问数据的图按这一份白名单收窄。

    - 关登录（auth_mode() == "off"）：全部放行，等同管理员，这里交回 False；
    - actor 是 Principal（这次请求的身份、访问密钥认出来的身份）时，**角色以它为准**（`Principal.role`），不回库覆写——
      访问密钥封顶在使用者这一档，不能因为那个人的用户名在库里是管理员就被抬成放行；角色快照与身份不一致时以身份为准；
    - actor 是用户名（服务端任务、父 Run 手上只有记在运行上的名字）时回库里认角色与停用；这种情况**另看当前请求的身份**：
      形参给的是名字、请求又是某个使用者（比如一把访问密钥）发来的，两个身份都在就取**更严**的那一个——名字在库里是管理员
      也绕不过请求那一档；
    - actor 没给就取当前请求的身份（`effective_actor`）；连身份都给不出、也认不出人时按普通使用者算——**宁可少给一点**：
      资产授权那边查不到就是没有授权，执行时会当场拒（querycore_not_authorized）。

    这几档的放行写在**调用方**这里，不写进资产授权函数（apps/access 只回答「授权有没有把这一版口径给到这个主体」）。"""
    from app.api.deps import Principal, auth_mode, current_principal

    if auth_mode() == "off":
        return False
    who = effective_actor(actor)
    if isinstance(who, Principal):
        return _is_limited(who)
    if await _limited_name(session, _name_of(who)):
        return True
    if actor is not None:
        # 形参给的是名字：当前请求的身份另算一道，取更严的那一个（见上面那条）
        request = current_principal()
        return bool(_name_of(request)) and _is_limited(request)
    return False


__all__ = ["CHAT_BUILTIN_LABEL", "CHAT_BUILTIN_TOOLS", "CHAT_NODE_TYPES", "SCOPE_REFUSED", "actor_name",
           "chat_graph_problem", "chat_memory_scope", "effective_actor", "restricted_actor"]
