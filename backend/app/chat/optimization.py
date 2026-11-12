"""Versioned optimizations for newly launched, simple ask-data runs.

Stored graphs/checkpoints keep their original behavior. No tools or permissions are added.
"""
from __future__ import annotations

from copy import deepcopy
import re


def simple_lookup(question: str) -> bool:
    return (0 < len(question.strip()) <= 120
            and bool(re.search(r"多少|查询|查一下|给我|是多少|昨天|今天|前天", question))
            and not re.search(r"分析|原因|为什么|趋势|对比|比较|预测|建议|报告|周报|月报|详细|解释|同比|环比", question))


def prepare(graph: dict, question: str) -> dict:
    graph = deepcopy(graph)
    nodes = graph.get("nodes") or []
    # Only the standard single-agent lookup chain. Custom analysis/report graphs retain their behavior.
    if not simple_lookup(question) or sorted(n.get("type", "") for n in nodes) != ["agent", "input", "output", "report"]:
        return graph
    by_type = {n["type"]: n for n in nodes}
    chain = [by_type[t]["id"] for t in ("input", "agent", "report", "output")]
    if {(e.get("source"), e.get("target")) for e in graph.get("edges", [])} != set(zip(chain, chain[1:])):
        return graph
    if by_type["output"].get("data", {}).get("config", {}).get("contract"):
        return graph
    for kind in ("agent", "report"):
        config = by_type[kind].setdefault("data", {}).setdefault("config", {})
        config["ask_data_optimization"] = 1
    by_type["report"]["data"]["config"]["reuse_answer_from"] = by_type["agent"]["id"]
    return graph


CONCISE_RULE = """这是一次简短问数。优先使用已授权且已绑定的 caliber_query 或 metric_query；不能绕过工具权限。
对象、日期与口径明确且已有完整结果时即可结束，不为补充背景反复查询。
最终答案简短写出结果、对象、时间归属与必要缺口：所有数值必须严格写成带行号的单元格证据引用（例如 [[v:Q1.r0.oee]]，必须带 .r0. 这类行号），严禁在引用外手写任何裸数字（包括百分比、行数、差值）；查询无返回记录时写「未返回记录」或「暂无数据」，不要写「0 行」。已有证据不足时明确说明，不把不同班次比率简单平均。
系统会核验这份答案，核验通过后直接作为答复。"""
