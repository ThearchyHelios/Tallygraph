"""指标的公共契约：依赖提取、可执行约束和质量结论共用这里的字段。"""
from __future__ import annotations

import math
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class RatioContract(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    numerator: str = Field(min_length=1)
    denominator: str = Field(min_length=1)
    scale: float = Field(default=1, gt=0)
    subset: bool = True


class MetricContract(BaseModel):
    """显式约束查询结果和指标值；自然语言定义仍需人工审阅。"""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    shape: Literal["scalar", "table"] = "scalar"
    grain: list[str] = Field(default_factory=list)
    non_null: list[str] = Field(default_factory=list)
    unit: str | None = None
    minimum: float | None = None
    maximum: float | None = None
    ratio: RatioContract | None = None

    @model_validator(mode="after")
    def valid(self) -> "MetricContract":
        if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
            raise ValueError("minimum 不能大于 maximum")
        if any(not c.strip() for c in self.grain + self.non_null):
            raise ValueError("粒度和非空列名不能为空")
        if len(set(self.grain)) != len(self.grain):
            raise ValueError("粒度列不能重复")
        return self


def metric_expressions(metric: Any) -> list[str] | None:
    """只读取当前类型的有效输入；隐藏的旧编辑字段不产生依赖。"""
    if not isinstance(metric, dict):
        return None
    kind = metric.get("kind") or "formula"
    if kind == "external":
        return []
    if kind == "formula":
        return [metric["expression"]] if isinstance(metric.get("expression"), str) else None
    if kind == "rule":
        inputs = (metric.get("rule") or {}).get("inputs") if isinstance(metric.get("rule"), dict) else None
        return list(inputs.values()) if isinstance(inputs, dict) and all(isinstance(v, str) for v in inputs.values()) else None
    if kind == "assessment":
        dims = (metric.get("assessment") or {}).get("dimensions") if isinstance(metric.get("assessment"), dict) else None
        if not isinstance(dims, list) or any(not isinstance(d, dict) or not isinstance(d.get("input"), str) for d in dims):
            return None
        return [d["input"] for d in dims]
    return None


def contract_findings(definition: dict[str, Any], metric: dict[str, Any], snapshots: list[dict[str, Any]]) -> list[str]:
    """检查明确声明的约束，不把自然语言字段解释为已经执行的过滤条件。"""
    raw = definition.get("contract")
    if raw is None:
        return []
    contract = MetricContract.model_validate(raw)
    problems: list[str] = []
    if contract.unit is not None and contract.unit != str(definition.get("unit") or ""):
        problems.append("指标单位与约束声明不一致")
    value = metric.get("value")
    if contract.minimum is not None or contract.maximum is not None:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            problems.append("无法检查取值范围：结果不是有限数值")
        elif ((contract.minimum is not None and value < contract.minimum)
              or (contract.maximum is not None and value > contract.maximum)):
            problems.append("指标值超出声明的范围")
    needs_snapshot = bool("shape" in contract.model_fields_set or contract.grain or contract.non_null or contract.ratio)
    if needs_snapshot and not snapshots:
        problems.append("无法检查结果形状：没有可验证的查询快照")
    for snap in snapshots:
        rows, columns = snap.get("rows"), snap.get("columns")
        if not isinstance(rows, list) or not isinstance(columns, list):
            problems.append("无法检查结果形状：查询快照不完整")
            continue
        if contract.shape == "scalar" and len(rows) != 1:
            problems.append(f"标量指标要求取数结果恰好一行，实际为 {len(rows)} 行")
        if snap.get("truncated"):
            problems.append("查询已截断，无法验证完整结果的粒度和行数")
        required = set(contract.grain + contract.non_null)
        if missing := required - set(columns):
            problems.append("查询缺少约束列：" + "、".join(sorted(missing)))
            continue
        if any(not isinstance(row, list) or len(row) != len(columns) for row in rows):
            problems.append("查询行与列定义不一致")
            continue
        if any(row[columns.index(col)] is None for row in rows for col in contract.non_null):
            problems.append("声明非空的列含有空值")
        if contract.grain:
            from app.core.artifact_store import canonical_json

            keys = [canonical_json([row[columns.index(col)] for col in contract.grain]) for row in rows]
            if len(keys) != len(set(keys)):
                problems.append("查询结果在声明的粒度上存在重复行")
    if contract.ratio is not None:
        ratio = contract.ratio
        candidates = [s for s in snapshots if ratio.numerator in (s.get("columns") or [])
                      and ratio.denominator in (s.get("columns") or [])]
        if not candidates:
            problems.append("比率约束要求同一查询结果提供分子和分母列")
        else:
            for snap in candidates:
                columns, rows = snap["columns"], snap["rows"]
                if len(rows) != 1 or not isinstance(rows[0], list) or len(rows[0]) != len(columns):
                    problems.append("比率约束要求分子和分母来自同一行汇总结果")
                    continue
                numerator, denominator = (rows[0][columns.index(c)] for c in (ratio.numerator, ratio.denominator))
                if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)
                       for v in (numerator, denominator)):
                    problems.append("分子和分母必须是有限数值")
                elif denominator <= 0:
                    problems.append("比率分母必须大于零")
                elif ratio.subset and not 0 <= numerator <= denominator:
                    problems.append("分子作为分母的子集，不能小于零或大于分母")
                else:
                    expected = numerator / denominator * ratio.scale
                    decimals = metric.get("decimals")
                    if isinstance(decimals, int) and not isinstance(decimals, bool):
                        expected = round(expected, decimals)
                    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isclose(value, expected, rel_tol=1e-9, abs_tol=1e-12):
                        problems.append("指标值与声明的分子、分母和换算倍率不一致")
    return list(dict.fromkeys(problems))
