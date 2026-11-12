"""Read-only migration audit. Findings never rewrite formulas or activate a metric."""
from __future__ import annotations

from app.ledger import closure
from app.ledger.semantic import QuerySemantics
from app.ledger.semantic_sql import scalar_problem


def audit(graph: dict, *, workflow_id: str, version: int, sources: set[str]) -> dict:
    nodes = {n["id"]: n for n in graph.get("nodes", [])}
    results = []
    for card in nodes.values():
        cfg = card.get("data", {}).get("config", {})
        if card.get("type") != "metrics" or cfg.get("caliber_from"):
            continue
        for metric in cfg.get("metrics", []):
            findings = []

            def add(code, message):
                if not any(f["code"] == code and f["message"] == message for f in findings):
                    findings.append({"code": code, "message": message})

            selected = closure.build(graph, workflow_id, version, card["id"], metric_ids=[metric["id"]])
            for problem in selected.unresolved:
                add("dependency", problem["message"])
            raw = metric.get("semantic")
            sem = None
            if not raw:
                add("semantic_missing", "补齐问数定义：常用叫法、业务粒度、汇总规则、对象映射和日期归属")
            else:
                try:
                    sem = QuerySemantics.model_validate(raw)
                except ValueError:
                    add("semantic_invalid", "问数定义不完整，请在指标的问数设置中修复")
                if sem and not sem.enabled:
                    add("semantic_disabled", "问数定义尚未启用，验证并确认后才能自动查询")
            fields = {f["name"]: f for f in selected.required_entry_fields}
            if sem:
                mapped = {e.field for e in sem.entities}
                if sem.time:
                    mapped |= {sem.time.start, sem.time.end}
                for name in sorted(mapped - fields.keys()):
                    add("parameter_unused", f"问数参数 {name} 未接入这项指标的取数依赖")
                for name, field in fields.items():
                    if field.get("required") and field.get("default") is None and name not in mapped:
                        add("parameter_missing", f"必填参数 {name} 尚未绑定对象或日期")
            for field in fields.values():
                desc = str(field.get("description") or "")
                if any(word in desc for word in ("含当天", "包含结束", "含结束")):
                    add("inclusive_end", "旧结束日期包含当天；接入自动日期前，需将取数和说明统一为左闭右开，防止多算或漏算一天")
            for query in selected.queries:
                qcfg = nodes[query.node_id].get("data", {}).get("config", {})
                sql = (qcfg.get("args") or {}).get("sql", "")
                tool = query.tool
                source = tool.removeprefix("db_query__")
                if source not in sources:
                    add("source_missing", f"数据源 {source} 不在本机可用目录中，尚不能做真实取数验证")
                if (metric.get("contract") or {}).get("shape", "scalar") == "scalar":
                    if problem := scalar_problem(sql):
                        add("scalar_unproven", f"取数 {query.node_id}：{problem}")
            if "%" in str(metric.get("unit", "")) or "OEE" in str(metric.get("name", "")).upper():
                note = "比率需核对分子、分母与权重，不能通过 SUM、平均值或截取首行机械修复"
            else:
                note = "先确认业务粒度，再选择求和、去重计数或常量取数；缺数据与真实零值分开处理"
            results.append({"card_id": card["id"], "metric_id": metric["id"], "name": metric.get("name", metric["id"]),
                            "findings": findings, "query_count": len(selected.queries), "guidance": note})
    return {"workflow_id": workflow_id, "revision": version, "metrics": results,
            "metrics_total": len(results), "metrics_needing_work": sum(bool(m["findings"]) for m in results),
            "runtime_verified": False,
            "note": "这是静态迁移检查，不代表查询结果已验证。原定义、已发布版本和权限保持不变；修复后需用独立期望试运行。"}
