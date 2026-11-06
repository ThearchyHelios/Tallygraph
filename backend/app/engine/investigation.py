"""异常调查的确定性完成条件。模型选择下一次查询，系统核对证据与停止原因。"""
from __future__ import annotations

import math
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

INVESTIGATION_VERSION = 1


class InvestigationPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = True
    max_rounds: int = Field(default=2, ge=0, le=5)
    minimum_depth: int = Field(default=1, ge=0, le=5)
    min_coverage: float = Field(default=0.8, ge=0, le=1)


def _number(value: Any) -> float:
    try:
        number = float(value)
        return number if math.isfinite(number) else 0.0
    except (ValueError, TypeError, OverflowError):
        return 0.0


def assess(scope: Any, question: str, policy: InvestigationPolicy) -> dict[str, Any]:
    from app.analysis.intent import analysis_intent

    results = list(scope.results.values()) if scope is not None else []
    intent = analysis_intent(question)
    active = bool(policy.enabled and scope is not None and (
        (intent is not None and intent.kind == "why") or any(
            r.get("status") == "ok" and (r.get("verdict") or {}).get("code") in ("outside_band", "far_outside_band")
            for r in results)))
    out: dict[str, Any] = {"version": INVESTIGATION_VERSION, "active": active, "status": "not_required", "depth": 0, "coverage": None,
                           "query_count": len(scope.queries) if scope is not None else 0,
                           "analysis_count": len(results), "reason": "", "next_step": ""}
    if not active:
        return out

    def pending(reason: str, next_step: str, stage: str) -> dict[str, Any]:
        return {**out, "status": "partial", "stage": stage, "reason": reason, "next_step": next_step}

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
    return {**out, "status": "complete", "stage": "contribution", "reason": "完成贡献分析，因果关系仍需独立证据"}
