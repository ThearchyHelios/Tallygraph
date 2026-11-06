"""验证口径约束、执行身份和调查完成条件，覆盖会造成错误业务结论的输入。"""
from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest

from app.core.artifact_store import execution_hash, graph_hash
from app.data.caliber_identity import definition_hash, semantic_hash
from app.engine.investigation import InvestigationPolicy, assess
from app.engine.metric_spec import MetricContract, contract_findings, metric_expressions
from app.engine.validation import metric_quality, validation_report
from tests import ledger_samples as S


def test_execution_and_semantic_hashes_distinguish_layout_from_sql():
    graph = S.caliber_graph()
    layout = copy.deepcopy(graph)
    layout["nodes"][1]["position"] = {"x": 999, "y": 777}
    layout["nodes"].reverse()
    assert graph_hash(layout) != graph_hash(graph)  # 历史封存算法不改写
    assert execution_hash(layout) == execution_hash(graph)
    assert semantic_hash(layout, "card") == semantic_hash(graph, "card")
    sql = copy.deepcopy(graph)
    fetch = next(n for n in sql["nodes"] if n["id"] == "fetch")
    fetch["data"]["config"]["args"]["sql"] = "SELECT SUM(amount) AS gmv FROM orders WHERE status='paid'"
    assert semantic_hash(sql, "card") != semantic_hash(graph, "card")
    assert execution_hash(sql) != execution_hash(graph)


@pytest.mark.parametrize("key,value", [("grain", "order"), ("filters", ["status=paid"]),
                                      ("time_field", "paid_at"), ("contract", {"shape": "scalar"})])
def test_declared_semantics_are_part_of_definition_identity(key, value):
    metric = {"id": "n", "expression": "input.n"}
    assert definition_hash("", "", [metric]) != definition_hash("", "", [{**metric, key: value}])


def test_inactive_formula_is_not_a_rule_dependency():
    assert metric_expressions({"kind": "rule", "expression": "nodes.old.value",
                               "rule": {"inputs": {"n": "input.n"}}}) == ["input.n"]


def test_contract_catches_multiline_first_cell_and_duplicate_grain():
    snapshot = {"columns": ["shop", "gmv"], "rows": [["east", 4], ["east", 7]]}
    metric = {"value": 4}
    bad = contract_findings({"contract": {"shape": "scalar"}}, metric, [snapshot])
    assert any("恰好一行" in problem for problem in bad)
    bad = contract_findings({"contract": {"shape": "table", "grain": ["shop"]}}, metric, [snapshot])
    assert any("重复行" in problem for problem in bad)


@pytest.mark.parametrize("rows,value,problem", [([[2, 10]], .2, None), ([[2, 0]], 0, "分母"),
                                               ([[12, 10]], 1.2, "子集"), ([[2, 10]], 20, "倍率"),
                                               ([[None, 10]], .2, "有限")])
def test_ratio_contract_verifies_value_and_subset(rows, value, problem):
    definition = {"contract": {"ratio": {"numerator": "refunds", "denominator": "orders"}}}
    found = contract_findings(definition, {"value": value}, [{"columns": ["refunds", "orders"], "rows": rows}])
    assert any(problem in p for p in found) if problem else not found


def test_contract_rejects_unknown_fields_and_nonfinite_bounds():
    for raw in ({"minimum": float("nan")}, {"shape": "scalar", "typo": True}, {"minimum": 4, "maximum": 3}):
        with pytest.raises(ValueError):
            MetricContract.model_validate(raw)


def test_value_range_contract_can_validate_non_query_inputs():
    definition = {"expression": "input.score", "contract": {"minimum": 0, "maximum": 100}}
    assert contract_findings(definition, {"value": 85}, []) == []
    assert contract_findings(definition, {"value": float("nan")}, [])
    assert contract_findings({"contract": {"shape": "scalar"}}, {"value": 85}, [])


@pytest.mark.parametrize("flag", ["incomplete", "no_data", "sql_check_failed"])
def test_successful_execution_does_not_hide_data_quality_failure(flag):
    metric = {"id": "n", "status": "ok", "value": 0, flag: True}
    report = validation_report([metric], execution="succeeded", trusted=True)
    assert report["status"] == "fail" and report["execution"] == "succeeded"


def test_unknown_comparability_and_unsealed_evidence_never_pass():
    metric = {"id": "n", "status": "ok", "value": 0}
    assert validation_report([metric], execution="succeeded", trusted=True)["status"] == "pass"
    assert validation_report([metric], execution="succeeded", trusted=False)["status"] == "unknown"
    assert metric_quality({**metric, "comparability": {"verdict": "conditional"}})["status"] == "unknown"
    assert metric_quality({**metric, "value": float("nan")})["status"] == "fail"


def scope(*results):
    return SimpleNamespace(queries={"Q1": {}}, results={f"A{i}": r for i, r in enumerate(results)})


def decomposition(*, depth=0, code="driver", caveats=None, share="0.9"):
    return {"status": "ok", "op": "decompose", "mode": "additive", "params": {
                "depth": depth, **({"parent": {"of": "A0", "key": ["east"]}} if depth else {})},
            "table": {"columns": ["region", "share"], "rows": [["east", share]]},
            "verdict": {"code": code}, "values": {"top_share": share}, "caveats": caveats or []}


def test_investigation_requires_closure_and_drill_before_completion():
    policy = InvestigationPolicy()
    question = "销售额为什么下降"
    assert assess(scope(), question, policy)["stage"] == "decompose"
    assert assess(scope(decomposition(code="unclosed")), question, policy)["stage"] == "closure"
    assert assess(scope(decomposition()), question, policy)["stage"] == "drill"
    done = assess(scope(decomposition(), decomposition(depth=1)), question, policy)
    assert done["status"] == "complete" and done["coverage"] == pytest.approx(.81)
    unknown = assess(scope(decomposition(), decomposition(depth=1, caveats=[{"code": "comparability"}])), question, policy)
    assert unknown["status"] == "partial" and unknown["stage"] == "comparability"


def test_local_drill_coverage_does_not_count_as_coverage_of_the_whole_change():
    found = assess(scope(decomposition(share="0.6"), decomposition(depth=1)), "销售额为什么下降", InvestigationPolicy())
    assert found["status"] == "partial" and found["stage"] == "coverage"
    assert found["coverage"] == pytest.approx(.54)
    unclosed = assess(scope(decomposition(code="unclosed"), decomposition(depth=1)), "为什么下降", InvestigationPolicy())
    assert unclosed["stage"] == "closure"
    unlinked = assess(scope(decomposition(depth=1)), "为什么下降", InvestigationPolicy())
    assert unlinked["stage"] == "lineage"


def test_anomaly_activates_investigation_but_plain_value_request_does_not():
    policy = InvestigationPolicy()
    assert not assess(scope(), "本周销售额是多少", policy)["active"]
    anomaly = {"op": "baseline", "status": "ok", "verdict": {"code": "outside_band"}}
    assert assess(scope(anomaly), "本周销售额是多少", policy)["active"]
    assert not assess(scope(anomaly), "本周销售额是多少", InvestigationPolicy(enabled=False))["active"]


def test_eval_fingerprint_tracks_investigation_policy_changes(monkeypatch):
    from app.engine import investigation
    from app.eval.runner import prompt_fingerprint

    before = prompt_fingerprint()
    monkeypatch.setattr(investigation, "INVESTIGATION_VERSION", investigation.INVESTIGATION_VERSION + 1)
    assert prompt_fingerprint() != before


def test_investigation_eval_checks_depth_budget_and_stopping_reason():
    from app.eval.cases import _expectation
    from app.eval.score import Answer, score_case

    exp = _expectation({"id": "i", "kind": "investigation", "minimum_depth": 1, "max_queries": 4},
                       0, gold_kind=None, taken=set())
    answer = Answer("run", investigations=[{"status": "complete", "depth": 1, "query_count": 3}])
    case = {"expectations": [exp]}
    assert score_case(case, None, answer, path="ran").verdict == "pass"
    answer.investigations[0]["query_count"] = 5
    assert score_case(case, None, answer, path="ran").verdict == "fail"
    answer.investigations = [{"status": "partial", "depth": 0, "query_count": 2, "stop_reason": "evidence_insufficient"}]
    partial = _expectation({"id": "i", "kind": "investigation", "status": "partial",
                            "stop_reason": "evidence_insufficient"}, 0, gold_kind=None, taken=set())
    assert score_case({"expectations": [partial]}, None, answer, path="ran").verdict == "pass"
