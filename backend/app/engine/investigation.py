"""异常调查的确定性完成条件。模型选择下一次查询，系统核对证据与停止原因。"""
from __future__ import annotations

import math
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

INVESTIGATION_VERSION = 2


class InvestigationPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = True
    max_rounds: int = Field(default=2, ge=0, le=5)
    minimum_depth: int = Field(default=1, ge=0, le=5)
    min_coverage: float = Field(default=0.8, ge=0, le=1)
    max_ontology_hops: int = Field(default=2, ge=0, le=5)
    max_cross_queries: int = Field(default=6, ge=0, le=20)
    check_source_health_first: bool = True


def _number(value: Any) -> float:
    try:
        number = float(value)
        return number if math.isfinite(number) else 0.0
    except (ValueError, TypeError, OverflowError):
        return 0.0


def _neighbor_queried(neighbor: dict[str, Any], queried_calibers: set[str], queried_sources: set[str],
                      focal_sources: set[str], ontology_hops: int) -> bool:
    if ontology_hops >= 1:
        target = neighbor.get("target") or {}
        wf_id = str(target.get("workflow_id") or "")
        card_id = str(target.get("card_id") or "")
        if wf_id and (f"{wf_id}:{card_id}" in queried_calibers or wf_id in queried_calibers):
            return True
        peer_sources = [str(s) for s in (target.get("sources") or []) if s]
        if any(s in queried_sources and s not in focal_sources for s in peer_sources):
            return True
        return not wf_id and not peer_sources
    return False


def assess(scope: Any, question: str, policy: InvestigationPolicy) -> dict[str, Any]:
    from app.analysis.intent import analysis_intent

    results = list(scope.results.values()) if scope is not None and hasattr(scope, "results") else []
    healths = [h for h in (getattr(scope, "source_healths", None) or {}).values() if isinstance(h, dict)]
    neighbors = [n for n in (getattr(scope, "ontology_neighbors", None) or []) if isinstance(n, dict)]
    contract_violations = list(getattr(scope, "contract_violations", None) or [])
    ontology_hops = int(getattr(scope, "ontology_hops", 0) or 0)
    cross_queries = int(getattr(scope, "cross_queries", 0) or 0)
    queried_calibers = set(getattr(scope, "queried_calibers", None) or ())
    queried_sources = set(getattr(scope, "queried_sources", None) or ())
    focal_sources = set(getattr(scope, "focal_sources", None) or ())

    has_health_anomaly = any(h.get("verdict") == "anomalous" for h in healths)
    has_reconcile_fault = any(
        r.get("status") == "ok"
        and r.get("op") == "reconcile"
        and (r.get("verdict") or {}).get("code") in ("diverged", "missing_counterpart")
        for r in results
    )
    intent = analysis_intent(question)
    active = bool(policy.enabled and scope is not None and (
        (intent is not None and intent.kind == "why")
        or any(
            r.get("status") == "ok" and (r.get("verdict") or {}).get("code") in ("outside_band", "far_outside_band")
            for r in results
        )
        or has_health_anomaly
        or has_reconcile_fault
        or bool(contract_violations)
    ))
    out: dict[str, Any] = {
        "version": INVESTIGATION_VERSION,
        "active": active,
        "status": "not_required",
        "depth": 0,
        "coverage": None,
        "query_count": len(scope.queries) if scope is not None and hasattr(scope, "queries") else 0,
        "analysis_count": len(results),
        "ontology_hops": ontology_hops,
        "source_health_checked": bool(healths),
        "reason": "",
        "next_step": "",
    }
    if not active:
        return out

    def pending(reason: str, next_step: str, stage: str) -> dict[str, Any]:
        return {**out, "status": "partial", "stage": stage, "reason": reason, "next_step": next_step}

    # 第一阶段：数据源健康度、跨源对账与物理契约检查（发现源表故障或对账超差立即早停）
    if has_health_anomaly:
        return {
            **out,
            "status": "complete",
            "stage": "health_and_contract",
            "reason": "关联数据源健康度核查发现断更、断档或数据骤降异常，已锁定数据质量问题，停止业务归因拆解",
        }
    if has_reconcile_fault:
        return {
            **out,
            "status": "complete",
            "stage": "health_and_contract",
            "reason": "跨源镜像口径对账超差或对侧缺失，已锁定跨源数据或单位不一致问题，停止业务归因拆解",
        }
    if contract_violations:
        if not neighbors:
            return pending(
                "指标触发契约或值域越界，尚未查询本体构成因子与关联数据源",
                "请先调用 ontology_neighbors 查看该指标的构成因子（factor）与跨源实体桥，再核查对侧基准值或数据源健康度。",
                "health_and_contract",
            )
        if ontology_hops < 1 and policy.max_ontology_hops > 0 and cross_queries < policy.max_cross_queries:
            return pending(
                "已发现构成因子或跨源实体桥，尚未穿透核对对侧主数据或因子口径",
                "请按 ontology_neighbors 的 recommended_caliber_query 或标准实体桥查询关联因子与对侧主数据，核实是否存在跨库失配。",
                "ontology_penetration",
            )
        return {
            **out,
            "status": "complete",
            "stage": "ontology_penetration",
            "reason": "已完成契约越界指标的跨源本体穿透与因子核对",
        }
    if (
        policy.check_source_health_first
        and any(n.get("kind") == "reconciles_with" for n in neighbors)
        and not healths
        and not any(r.get("op") == "reconcile" for r in results)
    ):
        return pending(
            "发现已确认的跨源对账镜像口径，尚未核查对侧数据源健康度或对账一致性",
            "请先调用 source_health 核查对侧数据源健康度，或调用 analysis_reconcile 核对镜像口径，排除断更与单位失配后再拆解业务维度。",
            "health_and_contract",
        )

    done = [r for r in results if r.get("status") == "ok"]
    decomposed = [r for r in done if r.get("op") == "decompose"]
    if not decomposed:
        refused = [r for r in results if r.get("status") == "refused"]
        if refused:
            return pending(str(refused[-1].get("reason") or "现有数据不满足分析条件"),
                           "检查缺数、截断、时间范围和口径；可以补齐时重新查询，无法补齐时明确证据缺口。", "data_quality")
        return pending("尚未对偏差进行贡献分解", "先核对口径、数据完整性和两期可比性，再选择业务维度或因子进行分解。", "decompose")
    last = decomposed[-1]
    params = last.get("params") or {}
    depth = int(params.get("depth") or 0)
    out["depth"] = depth
    chain = [last]
    seen = set()
    while (parent := (chain[-1].get("params") or {}).get("parent")):
        name = parent.get("of") if isinstance(parent, dict) else None
        previous = scope.results.get(name) if isinstance(name, str) else None
        if (name in seen or not isinstance(previous, dict) or previous.get("status") != "ok"
                or previous.get("op") != "decompose" or previous.get("mode") != last.get("mode")):
            return pending("下钻与上层结果的关联不完整", "重新取得上层分解，用 parent 关联真实的贡献项。", "lineage")
        seen.add(name)
        chain.append(previous)
    if depth != len(chain) - 1:
        return pending("下钻层数与证据链不一致", "从总指标开始，用 parent 逐层关联分解结果。", "lineage")
    caveats = {c.get("code") for result in chain for c in result.get("caveats") or [] if isinstance(c, dict)}
    code = (last.get("verdict") or {}).get("code")
    if any((r.get("verdict") or {}).get("code") == "unclosed" for r in chain) or "headline_basis" in caveats:
        return pending("分解尚未与总指标核对闭合", "取同一口径的总数作为 headline，核对分组和总数的差异，再继续解释。", "closure")
    if "comparability" in caveats:
        return pending("两期可比性尚未确认", "先核对两期的窗口、过滤条件和数据完整性；无法确认时保留有条件结论。", "comparability")
    comparable = [r for r in done if r.get("op") == "comparable"]
    # 算子已有内部可比性检查的结果不要求模型重复调用；缺少声明则不能据此证明可比。
    if comparable and (comparable[-1].get("verdict") or {}).get("code") != "comparable":
        return pending("可比性检查未通过", "先解决可比性缺口，再基于同口径数据重新分解。", "comparability")
    values = last.get("values") or {}
    coverage = max(_number(values.get("top_share")), _number(values.get("top_k_share")))
    if last.get("mode") == "additive":
        from app.analysis.inputs import same_value

        coverage = 1.0 if code in ("flat", "dispersed") else max(0.0, min(1.0, coverage))
        for child, previous in zip(chain, chain[1:]):
            key = child["params"]["parent"].get("key") or []
            table = previous.get("table") or {}
            columns = table.get("columns") or []
            row = next((r for r in table.get("rows") or [] if key and len(r) >= len(columns)
                        and all(same_value(k, v) for k, v in zip(key, r[:len(key)]))), None)
            if row is None or "share" not in columns:
                return pending("无法核对下钻贡献占整体偏差的比例", "保留上层贡献表及下钻的分组键，再核对整体覆盖程度。", "coverage")
            coverage *= max(0.0, min(1.0, _number(row[columns.index("share")])))
    out["coverage"] = coverage if last.get("mode") == "additive" else None
    if code in ("flat", "dispersed") and depth == 0:
        return {**out, "status": "complete", "stage": "contribution", "reason": "分解显示变化平稳或贡献分散"}
    if last.get("mode") != "factor" and depth < policy.minimum_depth:
        return pending("主要贡献项尚未进一步下钻", "对主要贡献项选择更细的业务维度，用 parent 关联上层结果继续下钻；没有合适维度时说明限制。", "drill")
    if last.get("mode") == "additive" and coverage < policy.min_coverage:
        return pending("主要贡献项的解释覆盖程度不足", "检查其他候选维度和剩余差异，保留未解释的部分，不把贡献分解写成因果证明。", "coverage")

    # 第三阶段：同源切片定位完成后，对已发现的 1 跳 factor / upstream_driver 本体邻居执行有界跨源穿透核查
    driver_neighbors = [n for n in neighbors if n.get("kind") in ("upstream_driver", "factor")]
    if driver_neighbors and policy.max_ontology_hops > 0:
        within_budget = ontology_hops < policy.max_ontology_hops and cross_queries < policy.max_cross_queries
        if not any(_neighbor_queried(n, queried_calibers, queried_sources, focal_sources, ontology_hops)
                   for n in driver_neighbors) and within_budget:
            return pending(
                "已完成同源切片定位，尚未沿本体穿透核查关联因子或上游驱动口径",
                "请将定位到的异常切片通过标准实体桥对齐后调用 caliber_query 查询邻居指标，验证跨源表现。",
                "ontology_penetration",
            )
        if any(n.get("kind") == "upstream_driver" for n in driver_neighbors):
            cochange_done = [r for r in done if r.get("op") == "cochange"]
            if not cochange_done:
                cochange_refused = [r for r in results if r.get("op") == "cochange" and r.get("status") == "refused"]
                if cochange_refused:
                    return pending(
                        str(cochange_refused[-1].get("reason") or "上游共变检验样本不足"),
                        "请检查两序列的时间窗口与有效点数；无法补齐时明确说明统计共变尚未证实，不写因果结论。",
                        "ontology_penetration",
                    )
                if cross_queries <= policy.max_cross_queries:
                    return pending(
                        "已获取跨源上游驱动数据，尚未执行同步共变统计检验",
                        "请调用 analysis_cochange 检验主指标与上游驱动序列是否存在同窗或滞后共变，注意相关性不等于因果。",
                        "ontology_penetration",
                    )
        return {
            **out,
            "status": "complete",
            "stage": "ontology_penetration",
            "reason": "已完成同源切片定位与跨源本体穿透核查，相关性结论仍需独立业务证据确认因果",
        }
    return {**out, "status": "complete", "stage": "contribution", "reason": "完成贡献分析，因果关系仍需独立证据"}
