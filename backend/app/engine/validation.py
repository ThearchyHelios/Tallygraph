"""执行状态与质量结论分开；试运行、正式出具和评测共用质量判定。"""
from __future__ import annotations

from typing import Any


def metric_quality(metric: dict[str, Any]) -> dict[str, Any]:
    import math

    checks: list[dict[str, str]] = []

    def add(code: str, status: str, detail: str) -> None:
        checks.append({"code": code, "status": status, "detail": detail})

    if metric.get("status") != "ok" or metric.get("value") is None:
        add("value", "fail", "指标没有可用值")
    value = metric.get("value")
    if isinstance(value, (int, float)) and not math.isfinite(value):
        add("value", "fail", "指标值不是有限数值")
    for key, reason in (("incomplete", "查询结果不完整"), ("no_data", "查询区间没有数据"),
                        ("sql_check_failed", "来源 SQL 未通过检查")):
        if metric.get(key):
            detail_key = "sql_check_reason" if key == "sql_check_failed" else f"{key}_reason"
            add(key, "fail", str(metric.get(detail_key) or reason))
    if metric.get("recompute_ok") is False:
        add("recompute", "fail", "指标复算不一致")
    external = metric.get("external")
    if isinstance(external, dict) and external.get("stale"):
        add("external_stale", "fail", "外部常量已过期")
    comparable = metric.get("comparability")
    if isinstance(comparable, dict) and comparable.get("verdict") != "comparable":
        add("comparability", "fail" if comparable.get("verdict") == "incomparable" else "unknown",
            str(comparable.get("phrase") or "两期可比性尚未验证"))
    if metric.get("comparability_skipped"):
        add("comparability", "unknown", "两期可比性检查未完成")
    for finding in metric.get("contract_findings") or []:
        add("metric_contract", "fail", str(finding))
    if metric.get("semantic_unverified"):
        add("semantic_definition", "unknown", "业务描述与取数条件的一致性仍需审阅")
    status = "fail" if any(c["status"] == "fail" for c in checks) else "unknown" if checks else "pass"
    return {"status": status, "checks": checks}


def validation_report(metrics: list[dict[str, Any]], *, execution: str, trusted: bool) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    if execution != "succeeded":
        checks.append({"code": "execution", "status": "fail" if execution == "failed" else "unknown",
                       "detail": "执行未成功完成"})
    if not trusted:
        checks.append({"code": "evidence", "status": "unknown", "detail": "运行证据尚未封存或完整性核对未通过"})
    if not metrics:
        checks.append({"code": "metrics", "status": "unknown", "detail": "没有可验证的指标结果"})
    for metric in metrics:
        quality = metric.get("quality") or metric_quality(metric)
        checks.extend({**c, "metric_id": metric.get("id"), "name": metric.get("name")} for c in quality["checks"])
    status = "fail" if any(c["status"] == "fail" for c in checks) else "unknown" if checks else "pass"
    return {"schema_version": 1, "execution": execution, "status": status, "checks": checks}


def additional_metric_gaps(metrics: list[dict[str, Any]]) -> list[str]:
    """既有出具检查之外的语义与可比性缺口。"""
    codes = {"metric_contract", "semantic_definition", "comparability", "recompute"}
    return [f"指标「{m.get('name') or m.get('id')}」：{c['detail']}" for m in metrics
            for c in metric_quality(m)["checks"] if c["code"] in codes
            and not (c["code"] == "comparability" and isinstance(m.get("comparability"), dict))]
