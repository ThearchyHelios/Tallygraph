"""问数据「快速直查（Fast Direct Ask-Data）」引擎。

对比「深度分析（Deep Analysis）」模式：
- 深度分析（ChatDriver）：LLM 选表 + 助手规划生成完整多节点画布工作流 → 执行（Agent + Report 二次撰写）→ 运行后 LLM 复核；
  适合复杂归因、跨源本体穿透、正式报告出具。
- 快速直查（DirectChatDriver）：毫秒级确定性语义预命中（Grounding：指标卡片 + 标准业务实体 + 数据目录防错规则，0 LLM）
  → 直接编译确定性直查图（q → agent1 → report1 → out1，开启 parallel_tools=True、内联当前东八区时间、单轮内联单元格引用）
  → 纯 Python 零模型直通封装 report_doc 数字证据并立即交付答案（0 次额外报告重写调用、0ms 事后复核），同时完整保留可点击数字证据、
  Run 审计记录、SQL 查询快照、只读守卫与敏感列遮罩。
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from app.chat.drive import Turn
from app.chat.grounding import GroundingBundle, ground_question
from app.chat.job import ChatDriver, ChatTurnExecution
from app.tools.datasource import QUERY_PREFIX, SCHEMA_PREFIX
from app.tools.metrics import METRIC_QUERY

_WEEKDAYS_ZH = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")


def _escape_template_braces(text: str) -> str:
    """转义预命中文本中可能来自数据库注释的模板括号，防止节点模板渲染器误解析。"""
    if not text:
        return ""
    return text.replace("{{", "{ {").replace("}}", "} }").replace("{%", "{ %").replace("%}", "% }")


def _shanghai_time_banner() -> str:
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    yesterday = (now.date() - timedelta(days=1)).isoformat()
    seven_days_ago = (now.date() - timedelta(days=6)).isoformat()
    weekday = _WEEKDAYS_ZH[now.weekday()]
    return (
        f"当前东八区时间：{now.strftime('%Y-%m-%d %H:%M:%S')}（{weekday}）；"
        f"今天日期：{now.date().isoformat()}，昨天日期：{yesterday}，近 7 日起点：{seven_days_ago}。"
        "（无需再调用时间查询工具）"
    )


DIRECT_SYSTEM_PROMPT = """你是 AgentLab 的「快速直查数据分析助手」。你的任务是基于已预命中的指标卡片、标准业务实体与数据目录，以最少的工具轮次、最快速度给出准确、清晰、结构化的 Markdown 数据回答。

【核心取数与防错纪律】
1. **{time_banner}**
2. **优先走标准语义指标**：若下方「已命中的标准语义指标卡片」直接对应用户所问指标，优先调用一次 `metric_query` 获取标准口径结果（支持 `date_from`、`date_to`、`group_by_date`、`group_by`、`filters`）。
3. **合并查询，杜绝碎片化往返**：
   - 手写 `db_query__<源名>` 时，尽量在 **1 条（最多不超过 2 条）SQL** 内一次性查全所需指标与近期对比窗口（例如查「今天怎么样」时，可直接查近 7 天按日汇总 `WHERE stat_date >= '<7日前>' ORDER BY stat_date DESC`，一条 SQL 同时看清今天是否有数、最新到哪一天、以及相比前几天的走势！）。
   - 已开启并行工具调用（`parallel_tools=True`）：确实需要查两张互不依赖的表时，在同一轮一次性并行发出工具调用。
4. **严守标准业务实体与数据目录护栏**：
   - 若下方给出了「标准业务实体与物理编码映射」，直接按映射出的列名与底层编码写过滤条件，不要发 `SELECT DISTINCT` 盲猜编码。
   - 严格遵守「数据目录强制防错护栏」中的业务时间列（`business_date`）、有效记录过滤条件（`valid_filter`）以及存量列（`stock`）不可跨期直接求和的约束。
   - 下方「预命中物理表结构」已包含完整字段与目录的表，直接写 SQL，不要重复调 `db_schema__` 查看；只有需要查找未列出的其他表时才用 `db_schema__`（支持传 `keyword` 检索或一次传多个表名）。
5. **诚实简洁作答与可点击数字证据**：
   - 每个数字都必须真实出自本轮工具返回结果，严禁编造或用数据剖析里的估算行数当查询答案。
   - 工具返回头部标有 `【证据 Q1】`、`【证据 Q2】` 等编号：凡是取自查询结果的数值，请直接用单元格引用标记 `[[v:Q1.r0.列名]]` **替代数字本身**（行号从 `r0` 起算；支持 `|万`、`|亿`、`|pct`、`|int`、`|.1` 等换算）。注意：`[[v:...]]` 会直接渲染成数值本身，正确写法为 `总客流 [[v:Q1.r0.net_visitors]] 人次`；**严禁**先写裸数字再在后面或句末重复附带 `[[v:...]]`！
   - 多行明细优先直接写整表标记 `[[table:Q1]]`（或 `[[table:Q1 cols=列1,列2 rows=0-9]]`），整表内的数字会自动生成可点击证据；真实物理表名用反引号包裹（如 `` `park_daily_traffic` ``）；日期统一写完整 `YYYY-MM-DD`（如 `2026-11-10`）。
   - 需要算合计、差值、占比或环比涨跌幅时，尽量在同一条 SQL 的 `SELECT` 中用 `ROUND(..., 1)` 直接算出结果列并用 `[[v:Q1.r0.列名]]` 引用（不要心算裸写百分比），让派生指标同样具备 SQL 级可点击证据。
   - 若当日数据尚未入库（查询未返回记录），明确写「暂无入库记录」或「未返回记录」（不要手写「0 行」），并主动给出已查到的最新可用日期数据供参考。"""


def build_direct_graph(
    *,
    bundle: GroundingBundle,
    has_metrics: bool = True,
) -> dict[str, Any]:
    """构建快速直查专用的确定性四节点执行图（input -> agent -> fast_report -> output，报告步 0 次模型调用）。"""
    sources = bundle.narrowed_sources or bundle.candidate_sources
    tools: list[str] = []
    for ds in sources:
        tools.append(f"{QUERY_PREFIX}{ds.name}")
        tools.append(f"{SCHEMA_PREFIX}{ds.name}")
    if has_metrics:
        tools.append(METRIC_QUERY)
    tools.extend(["calculator", "kb_search"])

    base_sys = DIRECT_SYSTEM_PROMPT.format(time_banner=_shanghai_time_banner())
    if bundle.system_context:
        full_sys = f"{base_sys}\n\n---\n\n{_escape_template_braces(bundle.system_context)}"
    else:
        full_sys = base_sys

    user_prompt = (
        "{% if input.history %}【前序对话参考】\n{{ input.history }}\n\n{% endif %}"
        "【当前问题】\n{{ input.question }}"
    )

    return {
        "nodes": [
            {
                "id": "q",
                "type": "input",
                "position": {"x": 80, "y": 160},
                "data": {
                    "label": "提问入口",
                    "config": {
                        "fields": [
                            {"name": "question", "label": "问题", "type": "text", "required": True},
                            {"name": "history", "label": "对话上下文", "type": "text", "required": False},
                        ]
                    },
                },
            },
            {
                "id": "agent1",
                "type": "agent",
                "position": {"x": 350, "y": 160},
                "data": {
                    "label": "快速直查 Agent",
                    "config": {
                        "system": full_sys,
                        "prompt": user_prompt,
                        "tools": tools,
                        "parallel_tools": True,
                        "max_steps": 8,
                        "ask_data_optimization": 2,
                        "emit_message": False,
                    },
                },
            },
            {
                "id": "report1",
                "type": "report",
                "position": {"x": 620, "y": 160},
                "data": {
                    "label": "零延迟证据核验",
                    "config": {
                        "ask_data_optimization": 2,
                        "reuse_answer_from": "agent1",
                        "numbers": "off",
                        "on_violation": "flag",
                        "max_repairs": 0,
                        "claims": "off",
                        "analysis_claims": "off",
                        "entities": "link",
                    },
                },
            },
            {
                "id": "out1",
                "type": "output",
                "position": {"x": 890, "y": 160},
                "data": {
                    "label": "直出回答",
                    "config": {
                        "fields": [
                            {"name": "answer", "value": "{{ nodes.report1.text }}"},
                        ]
                    },
                },
            },
        ],
        "edges": [
            {"id": "q->agent1", "source": "q", "target": "agent1"},
            {"id": "agent1->report1", "source": "agent1", "target": "report1"},
            {"id": "report1->out1", "source": "report1", "target": "out1"},
        ],
    }


class DirectChatDriver(ChatDriver):
    """快速直查驱动：跳过助手建图与事后复核，执行「毫秒级语义预命中 → 确定性直查图运行 → 即时落库」。"""

    def __init__(self, execution: ChatTurnExecution, *, question: str, datasource_ids: list[str]) -> None:
        super().__init__(execution, question=question, datasource_ids=datasource_ids)
        self.grounding_summary: dict[str, Any] | None = None

    async def launch_args(self, session: AsyncSession, graph: dict[str, Any]) -> dict[str, Any]:
        from app.api.deps import restricted
        from app.chat.launch import ChatRunRefused, user_chat_run
        from app.engine.launch import LaunchRefused

        args: dict[str, Any] = {
            "graph": graph,
            "actor": self.execution.actor,
            "origin_kind": "chat",
            "origin": {
                "conversation_id": self.execution.conversation_id,
                "turn_id": self.execution.turn_id,
                "depth": "fast",
            },
        }
        if restricted():
            try:
                args["memory_scope"], args["options"], args["graph"] = await user_chat_run(
                    session, graph, self.execution.actor
                )
            except ChatRunRefused as e:
                raise LaunchRefused(403, str(e)) from e
        return args

    async def drive(
        self,
        *,
        graph: dict[str, Any] | None = None,
        run_id: str | None = None,
        retry: bool = True,
        instruction: str | None = None,
        base_graph: dict[str, Any] | None = None,
    ) -> Turn:
        from app.api.conversations import run_history
        from app.api.deps import restricted
        from app.db.base import SessionLocal

        t = self.turn
        started = time.monotonic()
        try:
            if run_id is not None:
                await self._phase("run")
                t.current_run = run_id
                row = await self._follow(run_id)
                if row is not None:
                    t.path = "ran"
                return t

            if graph is not None:
                self.graph = graph
                await self._phase("run")
                row = await self._run(graph)
                if row is not None:
                    t.path = "ran"
                return t

            # 1. 毫秒级语义预命中（0 次 LLM 调用）
            await self._phase("ground")
            ground_t0 = time.monotonic()
            async with SessionLocal() as session:
                history = await run_history(
                    session,
                    self.execution.conversation_id,
                    before_seq=self.execution.turn_seq,
                )
                bundle = await ground_question(
                    session,
                    self.question,
                    datasource_ids=self.datasource_ids,
                    history_text=history,
                    restricted=restricted(),
                )
            t.timings["ground_ms"] = int((time.monotonic() - ground_t0) * 1000)
            self.grounding_summary = bundle.ui_summary

            # 2. 构建确定性直查图（含零延迟证据核验）并落库、推送预命中事件
            direct_graph = build_direct_graph(bundle=bundle, has_metrics=True)
            if instruction and instruction.strip() and instruction.strip() != self.question.strip():
                # 用户点「重试本轮」且仍保持快速直查时，把重试提示附进用户 prompt
                agent_node = next((n for n in direct_graph["nodes"] if n.get("type") == "agent"), None)
                if agent_node is not None:
                    agent_cfg = agent_node["data"]["config"]
                    agent_cfg["prompt"] = (
                        f"{agent_cfg['prompt']}\n\n【重试提示】\n{_escape_template_braces(instruction.strip())}"
                    )
            self.graph = direct_graph
            await self.execution.save(
                graph=direct_graph,
                explanation="快速直查（语义预命中直连取数）",
                meta={"depth": "fast", "grounding": bundle.ui_summary},
            )
            await self.execution.emit("op", {"op": "grounding", "grounding": bundle.ui_summary})

            # 3. 直接发起运行并流式跟随，结束后零延迟交付（不调报告重写与运行后复核）
            await self._phase("run")
            payload = {"question": self.question, "history": history}
            row = await self._run(direct_graph, payload)
            if row is not None:
                t.path = "ran"
            return t
        finally:
            t.timings["total_ms"] = int((time.monotonic() - started) * 1000)
