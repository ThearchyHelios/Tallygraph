"""问数据一轮里的几个「决定」（细案 01 做法 1，总方案 2.1）。

一轮问数据是：建图（助手的生成过程，api/copilot.answer_ops）→ 要不要运行 → 运行（输入怎么填）→ 复核 → 要不要带着原图
重来一次、重来时说什么。以前这几个决定都写在问数据页里（frontend/src/store/chat.ts 的 plan / launch / settle / retry），
后端跑不了一轮问数据：评测要么照着抄一份（迟早走样，量出来的不是用户拿到的那一轮），要么做不了；这也违反「界面层不放
逻辑」。现在决定写在这里，是纯函数：

- next_step：生成过程交出结局之后做什么（含生成过程一个操作都没给时自动重来一次）；
- fill_input：用户的问题填进这张图的入口字段；
- retry_after_review：复核之后要不要带着原图重来一次；
- retry_instruction：重来时交给助手的那句话（两种措辞）。

接口把决定交给问数据页：/copilot/generate-stream 的 final 带 next（run | ready | done | fail），error 带 next（retry | fail），
/copilot/review 的结果带 retry_instruction（要重来时是那句话，不重来是 null）。问数据页照后端给的走；老后端没有这两个字段时退回它自己那份同样的
规则（tests/test_chat_turn_policy.py 逐条对照两边的常量和措辞）。评测（第 3 波）用同一组函数把一轮串起来。

整轮编排后来也搬到了服务端（docs/design/orchestration-round §1）：流程在 chat/drive.py（问数据和评测共用），问数据的后台任务在
chat/job.py；页面只发起、只看，不再自己判这些。老服务端那条退路（页面自己那份规则）随之去掉。
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

#: 一轮最多自动试几次（含第一次）：第二次还不行多半是问题本身没说清，该让用户介入（同 chat.ts 的 MAX_ATTEMPTS）
MAX_ATTEMPTS = 2

#: final 之后的四种走法（final.next）：运行、只建不跑（摆着等用户点运行）、这一轮已经有了跑完的运行（固定执行）、判失败
NEXT_RUN, NEXT_READY, NEXT_DONE, NEXT_FAIL = "run", "ready", "done", "fail"
NEXT_STEPS = (NEXT_RUN, NEXT_READY, NEXT_DONE, NEXT_FAIL)
#: 别的结局：直接回答了（reply）、助手反问了等用户答（asked）、重来一次（retry：没自查过的老服务端交来没过检查的图，或者
#: 生成过程一个操作都没给）
NEXT_REPLY, NEXT_ASKED, NEXT_RETRY = "reply", "asked", "retry"

#: 生成过程一个操作都没给就结束了（error 操作的 code）：模型只写了思考（deepseek 这类推理模型偶发）、或者格式跑偏每一行都被
#: 跳过。这是偶发的，重来一次多半就好，按「重来一次」的规矩自动重来（最多 MAX_ATTEMPTS 次，含第一次）；被截断的不重来——额度
#: 不变，重来还是截断
EMPTY_GENERATION = "copilot_empty"
#: 自动重来的 error（code）
RETRY_ERRORS = frozenset({EMPTY_GENERATION})
#: error 操作的 message 前缀（api/copilot.answer_ops）：重来时交给助手的原因不带它
_ERROR_PREFIX = "助手本轮未完成："

#: 问题往哪个入口字段放：名字像「问题」的优先（同 chat.ts fillInput 的正则，大小写不敏感）
QUESTION_FIELD = re.compile(r"question|query|topic|input|问题|需求|主题", re.IGNORECASE)

#: 带着原图重来时交给助手的话：原图交回去改（调大步数、改参数这种事就是改一个字段，不是推倒重来）
RETRY_WITH_BASE = ("这个工作流上一次运行没有成功：{reason}。\n\n"
                   "请针对这个原因改它（比如调大最大步数、改正工具参数、换个查询方式），"
                   "改完继续回答原来的问题：{question}")
#: 没有图可交回（上一次连图都没建出来）时：原问题后面缀上原因，从零再建
RETRY_FROM_SCRATCH = "{question}\n\n（上一次没有成功：{reason}。请针对这个原因调整，重新给出完整的工作流。）"
#: 生成过程一个操作都没给（EMPTY_GENERATION）时自动重来交给助手的话：原问题后面缀上那句纠正（error 的 reason，见
#: copilot_gen.StreamDiagnosis.correction：只写了思考、格式跑偏、用了工具调用各有一句）。不原样重发——原样重发多半还是
#: 只写思考（部署上撞过两次都是空的）
RETRY_EMPTY = "{question}\n\n（上一次没有给出可用的操作：{reason}。）"
#: 没有纠正可用（老服务端只给了「模型没有给出任何修改」）时补的那句
EMPTY_FALLBACK = "这次请直接按输出格式给出操作（每行一个 JSON），要看表就先输出一行 look"
#: 用户点「重试本轮」时交给助手的话（以前在问数据页 retryTurn 里拼，一轮搬到服务端之后在这里）：有图时说的是「没有给出可用的
#: 结果」——上一次未必是运行失败（停止了、被驳回、结论不可用都算），原因是那一次的归档说法（chat/record.attempt_reason）；
#: 没有图的同 RETRY_FROM_SCRATCH
RETRY_USER_WITH_BASE = ("这个工作流上一次没有给出可用的结果：{reason}。\n\n"
                        "请针对这个原因改它（比如调大最大步数、改正工具参数、换个查询方式），"
                        "改完继续回答原来的问题：{question}")
#: 简单查数命中硬异常信号时，界面提供的一键本体排查提示按钮文案与升级重跑指令（P1）
ESCALATION_BUTTON_LABEL = "指标触发异常，沿本体排查关联数据源"
ESCALATION_WITH_BASE = (
    "上一次回答「{question}」时触发了指标或数据异常：{reason}。\n\n"
    "请升级为跨源本体穿透排查工作流（input → agent → report → output）："
    "给 agent 绑定 ontology_neighbors、source_health、caliber_query 及分析工具（analysis_comparable、"
    "analysis_baseline、analysis_trend、analysis_decompose、analysis_differ、analysis_reconcile、analysis_cochange），"
    "按「先调 ontology_neighbors 找已确认关联口径并调 source_health / analysis_reconcile 验数据源健康（发现断更、缺口、"
    "量级断崖或单位错位即早停归因数据故障并引用 [[h:H1]] / [[a:A1]]）→ 若触发数值契约越界则核查 factor 构成因子与跨库主数据 → "
    "若源健康通过则同源 decompose 锁定异常实体切片后沿标准实体桥调 caliber_query + analysis_cochange 检验上游驱动同步异动"
    "（严禁把统计共变写成确定因果）」排查异常并给出可核验报告。"
)
ESCALATION_FROM_SCRATCH = (
    "{question}\n\n（指标结果触发异常：{reason}。请沿业务本体排查关联口径与关联数据源健康：搭 input → agent → report → output，"
    "绑定 ontology_neighbors、source_health、caliber_query 与分析工具，先验源健康与跨源对账，再查构成因子与跨源驱动，"
    "报告引用 [[h:]] / [[a:]] 证据。）"
)


def _blockers(final: Mapping[str, Any]) -> list[str]:
    return [str(i.get("message") or "") for i in final.get("issues") or []
            if isinstance(i, Mapping) and i.get("level") == "error"]


def next_step(terminal: Mapping[str, Any] | None, *, self_checked: bool = True) -> str:
    """生成过程交出结局之后做什么。terminal 是最后那条结局操作（final / reply / error）；流结束时只有反问（ask）、没有
    别的结局的给 None。

    - reply：直接回答，这一轮到此为止；只有反问：等用户回答；
    - error：生成过程一个操作都没给（code 是 EMPTY_GENERATION）时重来一次（retry，次数由调用方按 MAX_ATTEMPTS 管，原因
      取 error_reason），别的判失败；
    - final：这一轮的运行**已经在生成这一轮里跑完了**（固定执行的 final 带 execution.run_id，app/ledger/nlchange 的两条
      接缝）就是 NEXT_DONE——不发起第二次，收下那次运行（chat/drive.TurnDriver 把 run_id 记进这一轮）；有 error 级问题时，
      服务端已经自查过（收到过 check 的结论）就判失败——没过的问题已经交回模型在同一轮里改过了，再整轮重来是再烧一次完整
      生成、改的还是同一批问题；没自查过（老服务端）才重来一次（retry）；模型说不运行（autorun=false：用户要的是流程本身、
      或者问了用户）只建不跑；其余运行。
    """
    if terminal is None:
        return NEXT_ASKED
    kind = terminal.get("op")
    if kind == "reply":
        return NEXT_REPLY
    if kind == "error":
        return NEXT_RETRY if terminal.get("code") in RETRY_ERRORS else NEXT_FAIL
    if kind != "final":
        return NEXT_FAIL
    # 这一轮的运行已经跑完了：运行已经发生，不能再按 ready / run 解释一遍（见 _completed_run）
    if _completed_run(terminal):
        return NEXT_DONE
    if _blockers(terminal):
        return NEXT_FAIL if self_checked else NEXT_RETRY
    if terminal.get("autorun") is False:
        return NEXT_READY
    return NEXT_RUN


def _completed_run(terminal: Mapping[str, Any]) -> str | None:
    """生成这一轮里就跑完的那次运行（final.execution.run_id）；不是固定执行那一档、没有运行 id 时为 None。"""
    execution = terminal.get("execution")
    if not isinstance(execution, Mapping):
        return None
    return str(execution.get("run_id") or "") or None


def error_reason(error: Mapping[str, Any]) -> str:
    """生成过程交出 error 时重来的原因（交给助手的那句话里用）：error 带的 reason，没有就是 message 去掉「助手本轮未完成：」。"""
    reason = str(error.get("reason") or "").strip()
    if reason:
        return reason
    message = str(error.get("message") or "").strip()
    return message[len(_ERROR_PREFIX):] if message.startswith(_ERROR_PREFIX) else message


def blocked_reason(final: Mapping[str, Any]) -> str:
    """没过检查的图重来时的原因（只有老服务端那条路用得到）。"""
    return "生成的工作流没有通过校验：" + "；".join(_blockers(final))


def fill_input(graph: Mapping[str, Any] | None, question: str, history: str = "") -> dict[str, Any]:
    """用户的问题填进这张图的入口字段（同 chat.ts 的 fillInput）。

    入口没声明字段：{question, history}（下游可能用 input.question）。声明了：每个字段先填默认值（没有就空串），问题放进
    名字像「问题」的那个，其次第一个必填的，再不然第一个；history（前几轮压成的文字）照样带上——图跑起来时模型得知道
    「它们」指的是上一轮那批数据。
    """
    nodes = (graph or {}).get("nodes") or []
    entry = next((n for n in nodes if isinstance(n, Mapping) and n.get("type") == "input"), None)
    config = ((entry or {}).get("data") or {}).get("config") or {}
    fields = [f for f in (config.get("fields") or []) if isinstance(f, Mapping)]
    if not fields:
        return {"question": question, "history": history}
    payload: dict[str, Any] = {"history": history}
    for f in fields:
        if f.get("name"):
            payload[str(f["name"])] = f["default"] if f.get("default") is not None else ""
    named = next((f for f in fields if QUESTION_FIELD.search(str(f.get("name") or ""))), None)
    target = named or next((f for f in fields if f.get("required")), None) or fields[0]
    if target.get("name"):
        payload[str(target["name"])] = question
    return payload


def review_reason(review: Mapping[str, Any]) -> str:
    """复核判不可用时重来的原因：各条信号的说明，没有就用给用户看的那句说明。"""
    details = [str(s.get("detail") or "") for s in review.get("signals") or [] if isinstance(s, Mapping)]
    return "；".join(d for d in details if d) or str(review.get("note") or "")


def retry_after_review(review: Mapping[str, Any] | None, attempt: int) -> bool:
    """复核之后要不要带着原图重来一次：复核说换个配置重跑大概率就好（retry）、答案不可信（severity=broken）或者有值得
    重建一次的缺口（engine/review.REBUILD：查询区间里这张表没有数据，换一张有这段数据的表大概率就好）、这一轮还没试满
    （attempt 从 0 数，attempt + 1 < MAX_ATTEMPTS）。"""
    from app.engine.review import REBUILD

    if not review:
        return False
    rebuild = any(isinstance(s, Mapping) and s.get("kind") in REBUILD for s in review.get("signals") or [])
    return bool(review.get("retry")) and (review.get("severity") == "broken" or rebuild) \
        and attempt + 1 < MAX_ATTEMPTS


def retry_instruction(question: str, reason: str, has_base: bool) -> str:
    """重来时交给助手的话。has_base：有上一次建出的图可以交回去改。"""
    template = RETRY_WITH_BASE if has_base else RETRY_FROM_SCRATCH
    return template.format(question=question, reason=reason)


def empty_retry_instruction(question: str, reason: str) -> str:
    """生成过程一个操作都没给、自动重来时交给助手的话：问题后面附那句纠正（reason）；纠正里没说怎么做的补 EMPTY_FALLBACK。"""
    reason = reason.strip().rstrip("。")
    if "look" not in reason:
        reason = f"{reason}；{EMPTY_FALLBACK}" if reason else EMPTY_FALLBACK
    return RETRY_EMPTY.format(question=question, reason=reason)


def user_retry_instruction(question: str, reason: str, has_base: bool) -> str:
    """用户点「重试本轮」时交给助手的话。has_base：有上一次建出的图可以交回去改。"""
    template = RETRY_USER_WITH_BASE if has_base else RETRY_FROM_SCRATCH
    return template.format(question=question, reason=reason)


def escalation_instruction(question: str, reason: str, has_base: bool) -> str:
    """指标触发硬异常信号后，沿业务本体排查关联数据源的诊断升级指令（P1）。"""
    template = ESCALATION_WITH_BASE if has_base else ESCALATION_FROM_SCRATCH
    return template.format(question=question, reason=reason)


def escalate_after_review(review: Mapping[str, Any] | None, attempt: int) -> bool:
    """异常自动升级模式（anomaly_escalation_mode == 'auto'）下最多自动升级 1 轮（attempt == 0）。"""
    if not review or attempt != 0:
        return False
    esc = review.get("anomaly_escalation")
    return isinstance(esc, Mapping) and bool(esc.get("available")) and bool(esc.get("auto_escalate"))


def has_nodes(graph: Any) -> bool:
    """一张图有没有节点（有才交回去改）。"""
    return isinstance(graph, Mapping) and bool(graph.get("nodes"))


__all__ = ["EMPTY_FALLBACK", "EMPTY_GENERATION", "ESCALATION_BUTTON_LABEL", "ESCALATION_FROM_SCRATCH",
           "ESCALATION_WITH_BASE", "MAX_ATTEMPTS", "NEXT_ASKED", "NEXT_DONE", "NEXT_FAIL", "NEXT_READY",
           "NEXT_REPLY", "NEXT_RETRY", "NEXT_RUN", "NEXT_STEPS", "QUESTION_FIELD", "RETRY_EMPTY", "RETRY_ERRORS",
           "RETRY_FROM_SCRATCH", "RETRY_USER_WITH_BASE", "RETRY_WITH_BASE", "blocked_reason",
           "empty_retry_instruction", "error_reason", "escalate_after_review", "escalation_instruction",
           "fill_input", "has_nodes", "next_step", "retry_after_review",
           "retry_instruction", "review_reason", "user_retry_instruction"]
