"""Wave 1-c 单元测试：三源虚构制造库确定性生成、ontology_suite.json 导入、零人工填数门禁、gold 试算与调查过程评分。"""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import delete

from app.db.base import SessionLocal
from app.db.models_ontology import CanonicalEntity
from app.engine.runner import run_manager
from app.eval import cases, gold, judgements, score, transfer
from tests.fixtures.acceptance import multi_source_ontology as mso

SUITE_PATH = Path(__file__).resolve().parent / "fixtures" / "eval" / "ontology_suite.json"


@pytest.fixture(autouse=True)
async def _engine_lifecycle():
    await run_manager.setup()
    yield
    await run_manager.shutdown()
    async with SessionLocal() as session:
        await session.execute(delete(CanonicalEntity))
        await session.commit()


def test_multi_source_ontology_builder_is_byte_deterministic(tmp_path):
    a = mso.build(tmp_path / "run_a", today=date(2026, 10, 22))
    b = mso.build(tmp_path / "run_b", today=date(2026, 10, 22))
    assert a.ops_path.read_bytes() == b.ops_path.read_bytes()
    assert a.equip_path.read_bytes() == b.equip_path.read_bytes()
    assert a.quality_path.read_bytes() == b.quality_path.read_bytes()


def test_human_value_keys_and_invalid_investigation_fields_are_rejected():
    # 严禁在 reconcile / cochange / investigation 期望里手填判定、数值或驱动段（评测-D4）
    for bad_exp in (
        {"kind": "verdict", "op": "reconcile", "params": {"measure": "a", "target_measure": "b"}, "verdict": "diverged"},
        {"kind": "verdict", "op": "cochange", "params": {"period": "d", "measure": "a", "driver_measure": "b"}, "judgement": "co_anomalous"},
        {"kind": "investigation", "status": "complete", "expected": "equip_master"},
    ):
        with pytest.raises(cases.CaseInvalid):
            cases.normalize_expectations([bad_exp], gold_kind="sql", case_kind="ask")

    # investigation 新增字段的类型与枚举范围校验
    with pytest.raises(cases.CaseInvalid):
        cases.normalize_expectations(
            [{"kind": "investigation", "min_ontology_hops": -1}], gold_kind=None, case_kind="ask"
        )
    with pytest.raises(cases.CaseInvalid):
        cases.normalize_expectations(
            [{"kind": "investigation", "diagnosis_stage": "guesswork"}], gold_kind=None, case_kind="ask"
        )


async def test_ontology_suite_imports_and_computes_all_six_gold_cases(tmp_path, scratch_source):
    await mso.seed_ontology_fixture(tmp_path, scratch_source, today=date(2026, 10, 22))
    raw_suite = json.loads(SUITE_PATH.read_text(encoding="utf-8"))

    async with SessionLocal() as session:
        plan = await transfer.plan_import(session, raw_suite)
    assert len(plan.cases) == 6
    for planned in plan.cases:
        assert planned.fields is not None, f"第 {planned.seq} 题导入校验未通过：{planned.reason}"

    # 固定截至时间 2026-10-22 10:00 +08:00（yesterday = 2026-10-21），逐题用真实探针算 gold
    ctx = gold.Context(as_of=datetime(2026, 10, 22, 2, 0, 0, tzinfo=timezone.utc), tz="Asia/Shanghai")
    outcomes = {}
    for planned in plan.cases:
        out = await gold.compute_gold(planned.fields, ctx)
        assert out.ok, f"第 {planned.seq} 题算期望失败：{out.status} - {out.reason}"
        outcomes[planned.seq] = out.sets[0].values

    # Case 1 (S1): EQ-L3 过期额定每小时产量 = 80.0
    assert outcomes[1]["stale_spec"]["value"] == 80.0
    # Case 2 (S2a): quality_scrap 昨日断更返回 0 行
    assert outcomes[2]["stalled_zero_rows"] == {"kind": "none", "rows": 0}
    # Case 3 (S2b): 1000 倍量纲换算后 L4 超差 -> diverged
    assert outcomes[3]["reconcile_energy"]["judgement"] == "diverged"
    assert outcomes[3]["reconcile_energy"]["verdict"] == "diverged"
    # Case 4 (S3): EQ-L2 有效运转率与非计划停机呈同期伴随异动 -> co_anomalous
    assert outcomes[4]["cochange_l2"]["judgement"] == "co_anomalous"
    assert outcomes[4]["cochange_l2"]["verdict"] == "co_anomalous"
    # Case 5 (Control 1): L1 常规波动 -> normal (within_band)
    assert outcomes[5]["normal_l1"]["judgement"] == "normal"
    assert outcomes[5]["normal_l1"]["verdict"] == "within_band"
    # Case 6 (Control 2): EQ-L4 主指标下滑但设备非计划停机平稳 -> target_normal
    assert outcomes[6]["upstream_normal_l4"]["judgement"] == "target_normal"
    assert outcomes[6]["upstream_normal_l4"]["verdict"] == "target_normal"


def test_score_grades_ontology_investigation_source_health_and_new_verdicts():
    exps = cases.normalize_expectations(
        [
            {
                "id": "inv",
                "kind": "investigation",
                "status": "complete",
                "minimum_depth": 1,
                "min_ontology_hops": 1,
                "require_source_health": True,
                "diagnosis_stage": "ontology_penetration",
                "max_queries": 6,
            },
            {
                "id": "co",
                "kind": "verdict",
                "op": "cochange",
                "params": {"period": "日期", "measure": "有效运转率", "driver_measure": "非计划停机分钟"},
            },
        ],
        gold_kind="sql",
        case_kind="ask",
    )
    gold_values = {
        "co": {
            "kind": "verdict",
            "status": "ok",
            "op": "cochange",
            "mode": None,
            "method": "伴随异动",
            "verdict": "co_anomalous",
            "judgement": "co_anomalous",
            "subjects": [],
            "direction": None,
            "phrase": "有效运转率超出常规波动，非计划停机分钟在同期同样超出常规波动（统计伴随关系，非因果断言）",
            "result": {"op": "cochange", "status": "ok", "verdict": {"code": "co_anomalous", "subjects": [], "direction": None}},
        }
    }

    # 1) 达标且引用了 A1 结论、台账带有 H1 健康检查记录 -> traced 通过
    good_answer = score.Answer(
        run_id="r_good",
        text="L2 有效运转率下滑，且上游非计划停机同步异动。",
        analyses=[
            {
                "artifact": "a" * 64,
                "alias": "A1",
                "op": "cochange",
                "mode": None,
                "method": "伴随异动",
                "status": "ok",
                "verdict": "co_anomalous",
                "judgement": "co_anomalous",
                "subjects": [],
                "direction": None,
                "phrase": "同期伴随异动",
                "cited": ["phrase"],
                "inputs": [],
            }
        ],
        investigations=[
            {
                "status": "complete",
                "depth": 2,
                "ontology_hops": 1,
                "diagnosis_stage": "ontology_penetration",
                "query_count": 4,
            }
        ],
        source_healths=[
            {"artifact": "h" * 64, "node_id": "bot", "verdict": "healthy", "sources": ["equip_master"], "phrase": "正常"}
        ],
    )
    good_scores = score._expectation_scores(exps, gold_values, good_answer, "ran", None, {})
    assert [s["outcome"] for s in good_scores] == ["traced", "traced"]

    # 2) 未做健康检查、跳数 0、阶段不符且把伴随异动判成 target_normal -> 两项均判 wrong 并写明原因
    bad_answer = score.Answer(
        run_id="r_bad",
        text="未查出关联异动。",
        analyses=[
            {
                **good_answer.analyses[0],
                "verdict": "target_normal",
                "judgement": "target_normal",
            }
        ],
        investigations=[
            {
                "status": "complete",
                "depth": 1,
                "ontology_hops": 0,
                "diagnosis_stage": "slice_localization",
                "query_count": 2,
            }
        ],
        source_healths=[],
    )
    bad_scores = score._expectation_scores(exps, gold_values, bad_answer, "ran", None, {})
    assert [s["outcome"] for s in bad_scores] == ["wrong", "wrong"]
    assert "未达到要求的本体穿透跳数" in bad_scores[0]["reason"]
    assert "未执行关联数据源健康度检查" in bad_scores[0]["reason"]
    assert "诊断阶段不符合期望" in bad_scores[0]["reason"]
    assert judgements.judgement_code("reconcile", None, "missing_counterpart") == "diverged"


def test_ontology_tools_variant_preset_fingerprint_and_copilot_ablation():
    """W3-b.1：ontology_tools 预设对照、指纹兼容（开着时与历史完全一致）与助手关断门禁。"""
    from app.api import copilot, evals
    from app.eval import compare, variants

    on = variants.normalize_variant({})
    off = variants.normalize_variant({"ontology": {"tools": "off"}})
    assert on["ontology"] == {"tools": "on"} and off["ontology"] == {"tools": "off"}
    models = {"assistant": "mock-fast", "writer": None}
    legacy = {k: v for k, v in on.items() if k != "ontology"}
    assert variants.variant_key(on, models) == variants.variant_key(legacy, models)
    assert variants.variant_key(off, models) != variants.variant_key(on, models)
    assert "本体穿透关" in variants.describe(off, models)
    assert "本体穿透关" not in variants.describe(on, models)

    arms = variants.ontology_tools_preset({**on, "repeats": 3})
    assert [(a["label"], a["ontology"]["tools"], a["repeats"]) for a in arms] == [
        ("本体穿透开", "on", 3),
        ("本体穿透关", "off", 3),
    ]
    assert "ontology_tools" in evals.PRESETS

    # 老指纹无 ontology_tools 与新默认 on 比较不报伪差异；on -> off 报「本体穿透：开 -> 关」
    assert compare.fingerprint_diff({"assistant_model": "m1"}, {"assistant_model": "m1", "ontology_tools": "on"}) == []
    diff = compare.fingerprint_diff({"ontology_tools": "on"}, {"ontology_tools": "off"})
    assert diff == [{"key": "ontology_tools", "label": "本体穿透", "before": "开", "after": "关"}]

    # copilot 工具清单与自查门禁在 ontology_tools=False 时彻底屏蔽 ontology_neighbors / source_health
    cat_on = copilot._tool_catalog([], ontology_tools=True)
    cat_off = copilot._tool_catalog([], ontology_tools=False)
    assert "ontology_neighbors" in cat_on and "source_health" in cat_on
    assert "ontology_neighbors" not in cat_off and "source_health" not in cat_off
    ref_off = copilot.node_reference(analysis_tools=True, ontology_tools=False)
    assert "ontology_neighbors" not in ref_off and "source_health" not in ref_off

    nodes = {
        "in1": {"id": "in1", "type": "input", "data": {"label": "输入", "config": {"fields": []}}},
        "ag": {
            "id": "ag",
            "type": "agent",
            "data": {"label": "排查", "config": {"prompt": "hi", "tools": ["ontology_neighbors", "source_health"]}},
        },
        "out1": {"id": "out1", "type": "output", "data": {"label": "输出", "config": {"fields": [{"name": "a", "value": "1"}]}}},
    }
    edges = [{"source": "in1", "target": "ag"}, {"source": "ag", "target": "out1"}]
    issues = copilot._blocking_issues(nodes, edges, ontology_tools=False)
    assert issues and any(i["code"] == copilot.ONTOLOGY_TOOLS_OFF for i in issues)


def test_template_twelve_validates_and_publishes_as_governed():
    """W3-b.2：内置模板「⑫ 跨源指标异动与对账诊断」通过图校验并可直接按受管级别发布。"""
    import copy

    from app.engine.governance import publish_issues
    from app.engine.schema import GraphSpec, validate_graph
    from app.seed import TEMPLATES

    name = "⑫ 跨源指标异动与对账诊断"
    names = [t["name"] for t in TEMPLATES]
    assert names.count(name) == 1 and names[-1] == name
    tpl = next(t for t in TEMPLATES if t["name"] == name)
    spec = GraphSpec.model_validate(copy.deepcopy({"nodes": tpl["nodes"], "edges": tpl["edges"]}))
    report = validate_graph(spec)
    assert report.ok, [i.message for i in report.issues if i.level == "error"]
    errors = [i for i in publish_issues(spec, level="governed") if i.level == "error"]
    assert errors == [], [i.message for i in errors]
    agent_cfg = next(n for n in tpl["nodes"] if n["type"] == "agent")["data"]["config"]
    assert set(agent_cfg["tools"]) >= {
        "caliber_query",
        "ontology_neighbors",
        "source_health",
        "analysis_reconcile",
        "analysis_cochange",
    }
