"""Wave 2-b 单元测试：三段式穿透充分性评估 investigation.assess、H{n} 源健康证据引用与离线包核验、analysis_claims V2 非因果护栏（D6）。"""
from __future__ import annotations

import json
from datetime import date
from typing import Any

import pytest

from sqlalchemy import delete

import app.analysis as analysis_ops
from app.analysis import contract as analysis_contract
from app.bundle.build import build_bundle
from app.core.artifact_store import canonical_json, content_hash, manifest_hash
from app.data import source_health as sh
from app.db.base import SessionLocal
from app.db.models_ontology import CanonicalEntity
from app.engine import analysis_claims, analysis_scope, evidence, investigation
from app.engine.investigation import InvestigationPolicy
from app.engine.runner import run_manager
from app.tools.analysis import AnalysisScope, input_snapshot
from tests.fixtures.acceptance import multi_source_ontology as mso
from tests.test_bundle_shown import NODE, doc_of, node, reseal


@pytest.fixture(autouse=True)
async def _setup_and_clean():
    await run_manager.setup()
    yield
    await run_manager.shutdown()
    async with SessionLocal() as session:
        await session.execute(delete(CanonicalEntity))
        await session.commit()


def _snap(ref: str, columns: list[str], rows: list[list], *, source: str = "ops_shift") -> dict[str, Any]:
    return input_snapshot(
        {
            "columns": columns,
            "rows": rows,
            "sql": f"SELECT {', '.join(columns)} FROM t",
            "source": source,
            "truncated": False,
        },
        ref=ref,
        artifact=ref.lower().ljust(64, "0")[:64],
        node_id="bot",
        masked=[],
    )


async def _probe(source: str, table: str, column: str) -> dict[str, Any]:
    return {
        "source": source,
        "table": table,
        "column": column,
        "min": "2026-10-01",
        "max": "2026-10-21",
        "basis": "live",
        "at": "2026-10-22T00:00:00+00:00",
    }


async def _run_op(req: dict[str, Any], scope: AnalysisScope, cfg: dict[str, Any]) -> dict[str, Any]:
    return await analysis_ops.run(
        req,
        scope.resolve,
        settings=cfg,
        context={"today": date(2026, 10, 22), "probe": _probe},
        refuse_data_errors=False,
    )


async def test_investigation_three_stage_s1_s2_s3_and_early_stop():
    cfg = analysis_contract.effective_settings({})
    policy = InvestigationPolicy(minimum_depth=0)

    # --- S1：契约越界（load_rate_pct > 100%），尚未做 ontology_neighbors 排查 -> 要求沿本体排查构成因子 ---
    s1_scope = AnalysisScope(node_id="bot", snapshot=cfg)
    s1_scope.record_query_target(
        sources=["ops_shift"],
        workflow_id="wf_ops",
        card_id="m_ops",
        violations=[{"metric_id": "load_rate_pct", "detail": "指标值超出声明的范围"}],
    )
    g_s1 = investigation.assess(s1_scope, "为什么昨天三线产线负荷率达到 135% 越界了？", policy)
    assert g_s1["status"] == "partial"
    assert "ontology_neighbors" in g_s1["next_step"] or "契约" in g_s1["reason"]

    # 发现 factor 邻居但尚未跨源核对 -> 提示按实体桥查询关联因子
    s1_scope.record_ontology_neighbors(
        [
            {
                "kind": "factor",
                "role": "denominator",
                "target": {
                    "workflow_id": "wf_equip",
                    "card_id": "m_equip",
                    "metric_id": "rated_hourly_qty",
                    "sources": ["equip_master"],
                },
            }
        ],
        focal_metrics=[{"workflow_id": "wf_ops", "card_id": "m_ops", "sources": ["ops_shift"]}],
    )
    g_s1_mid = investigation.assess(s1_scope, "为什么昨天三线产线负荷率达到 135% 越界了？", policy)
    assert g_s1_mid["status"] == "partial"
    assert g_s1_mid["stage"] == "ontology_penetration"

    # 完成 1 跳跨源核对后 -> S1 收口通过
    s1_scope.record_query_target(sources=["equip_master"], workflow_id="wf_equip", card_id="m_equip")
    g_s1_done = investigation.assess(s1_scope, "为什么昨天三线产线负荷率达到 135% 越界了？", policy)
    assert g_s1_done["status"] == "complete", g_s1_done
    assert g_s1_done["stage"] == "ontology_penetration"

    # --- S2：数据源健康检查发现断更（anomalous） -> 立即早停（complete），绝不误导继续做业务归因拆解！ ---
    s2_scope = AnalysisScope(node_id="bot", snapshot=cfg)
    s2_scope.add_query("Q1", _snap("Q1", ["dt", "scrap"], [["2026-10-20", 18], ["2026-10-21", 20]], source="ops_shift"))
    s2_scope.add_health(
        {
            "kind": "source_health",
            "verdict": "anomalous",
            "phrase": "数据源「quality_scrap」存在断更告警（最新日期停在 2026-10-19）",
            "sources": [{"source": "quality_scrap", "verdict": "anomalous"}],
            "issues": [{"kind": "date_stalled", "severity": "alert", "detail": "最新日期停在 2026-10-19"}],
        },
        artifact="a" * 64,
    )
    g_s2_health = investigation.assess(s2_scope, "为什么昨天质检台账报废数突然掉到 0，和车间对不上？", policy)
    assert g_s2_health["status"] == "complete"
    assert g_s2_health["stage"] == "health_and_contract"

    # --- S2 变体：reconcile 判定 diverged -> 同样在 health_and_contract 阶段立即早停收口 ---
    s2_rec_scope = AnalysisScope(node_id="bot", snapshot=cfg)
    q_left = _snap("Q1", ["line_code", "qty"], [["L1", 18], ["L2", 22]], source="ops_shift")
    q_right = _snap("Q2", ["line_code", "qty"], [["L1", 18], ["L2", 2]], source="quality_scrap")
    s2_rec_scope.add_query("Q1", q_left)
    s2_rec_scope.add_query("Q2", q_right)
    rec_res = await _run_op(
        {
            "op": "reconcile",
            "mode": None,
            "params": {"base_query": "Q1", "target_query": "Q2", "by": ["line_code"], "measure": "qty"},
        },
        s2_rec_scope,
        cfg,
    )
    assert rec_res["verdict"]["code"] == "diverged"
    s2_rec_scope.add_result(rec_res, artifact="b" * 64)
    g_s2_rec = investigation.assess(s2_rec_scope, "为什么两套报废数对不上？", policy)
    assert g_s2_rec["status"] == "complete"
    assert g_s2_rec["stage"] == "health_and_contract"

    # --- S3：源健康正常、同源基线异常 + 已拆解定位切片 L2，存在 upstream_driver 邻居未做跨源 cochange 验证 -> 提示跨源穿透 ---
    s3_scope = AnalysisScope(node_id="bot", snapshot=cfg)
    days = [f"2026-10-{d:02d}" for d in range(7, 22)]  # 15 天序列
    agg_vals = [1800, 1804, 1796, 1802, 1800, 1798, 1806, 1802, 1796, 1804, 1800, 1796, 1802, 1800, 1512]
    q_agg = _snap("Q1", ["dt", "good_qty"], [[d, v] for d, v in zip(days, agg_vals)], source="ops_shift")
    q_slice = _snap(
        "Q2",
        ["dt", "line_code", "good_qty"],
        [
            ["2026-10-20", "L1", 900],
            ["2026-10-20", "L2", 900],
            ["2026-10-21", "L1", 900],
            ["2026-10-21", "L2", 612],
        ],
        source="ops_shift",
    )
    s3_scope.add_query("Q1", q_agg)
    s3_scope.add_query("Q2", q_slice)
    s3_scope.record_query_target(sources=["ops_shift"], workflow_id="wf_ops", card_id="m_ops")

    base_res = await _run_op(
        {"op": "baseline", "mode": None, "params": {"query": "Q1", "period": "dt", "measure": "good_qty", "lag": 1}},
        s3_scope,
        cfg,
    )
    assert base_res["verdict"]["code"] in ("outside_band", "far_outside_band")
    decomp_res = await _run_op(
        {
            "op": "decompose",
            "mode": "additive",
            "params": {
                "query": "Q2",
                "period": "dt",
                "by": ["line_code"],
                "measure": "good_qty",
                "base": "2026-10-20",
                "target": "2026-10-21",
                "headline": {"base": "Q1.r13.good_qty", "target": "Q1.r14.good_qty"},
            },
        },
        s3_scope,
        cfg,
    )
    assert decomp_res["status"] == "ok"
    s3_scope.add_result(base_res, artifact="d" * 64)
    s3_scope.add_result(decomp_res, artifact="e" * 64)
    s3_scope.record_ontology_neighbors(
        [
            {
                "kind": "upstream_driver",
                "entity_key": "production_line",
                "target": {
                    "workflow_id": "wf_equip",
                    "card_id": "m_equip",
                    "metric_id": "unplanned_downtime_min",
                    "sources": ["equip_master"],
                },
            }
        ],
        focal_metrics=[{"workflow_id": "wf_ops", "card_id": "m_ops", "sources": ["ops_shift"]}],
    )
    g_s3_before = investigation.assess(s3_scope, "为什么昨天车间合格产量突然异常下跌？", policy)
    assert g_s3_before["status"] == "partial"
    assert g_s3_before["stage"] == "ontology_penetration"

    # 补上跨源驱动序列 + cochange 后 -> S3 完整收口！
    l2_vals = [900, 902, 898, 901, 900, 899, 903, 901, 898, 902, 900, 898, 901, 900, 612]
    drv_vals = [12.0, 12.5, 11.8, 12.2, 12.0, 11.6, 12.4, 12.1, 11.9, 12.3, 12.0, 11.7, 12.2, 12.0, 96.0]
    q_l2 = _snap("Q3", ["dt", "good_qty"], [[d, v] for d, v in zip(days, l2_vals)], source="ops_shift")
    q_driver = _snap("Q4", ["dt", "downtime_min"], [[d, v] for d, v in zip(days, drv_vals)], source="equip_master")
    s3_scope.add_query("Q3", q_l2)
    s3_scope.add_query("Q4", q_driver)
    s3_scope.record_query_target(sources=["equip_master"], workflow_id="wf_equip", card_id="m_equip")
    co_res = await _run_op(
        {
            "op": "cochange",
            "mode": None,
            "params": {
                "base_query": "Q3",
                "target_query": "Q4",
                "period": "dt",
                "measure": "good_qty",
                "driver_measure": "downtime_min",
            },
        },
        s3_scope,
        cfg,
    )
    assert co_res["verdict"]["code"] == "co_anomalous"
    s3_scope.add_result(co_res, artifact="f" * 64)
    g_s3_after = investigation.assess(s3_scope, "为什么昨天车间合格产量突然异常下跌？", policy)
    assert g_s3_after["status"] == "complete", g_s3_after
    assert g_s3_after["stage"] == "ontology_penetration"


async def test_analysis_claims_v2_reconcile_cochange_data_fault_and_d6_non_causal():
    lex_v2 = analysis_claims.lexicon(2)
    lex_v1 = analysis_claims.lexicon(1)
    v2_keys = {c.key for c in lex_v2.claims}
    v1_keys = {c.key for c in lex_v1.claims}
    assert {"reconcile_bad", "cochange", "data_fault"}.issubset(v2_keys)
    assert "reconcile_bad" not in v1_keys  # 老封存报告按 v1 复核不漂移

    cfg = analysis_contract.effective_settings({})
    scope = AnalysisScope(node_id="bot", snapshot=cfg)
    days = [f"2026-10-{d:02d}" for d in range(7, 22)]
    q_p = _snap(
        "Q1",
        ["dt", "good_qty"],
        [[d, v] for d, v in zip(days, [900, 902, 898, 901, 900, 899, 903, 901, 898, 902, 900, 898, 901, 900, 612])],
        source="ops_shift",
    )
    q_d = _snap(
        "Q2",
        ["dt", "downtime_min"],
        [[d, v] for d, v in zip(days, [12.0, 12.5, 11.8, 12.2, 12.0, 11.6, 12.4, 12.1, 11.9, 12.3, 12.0, 11.7, 12.2, 12.0, 96.0])],
        source="equip_master",
    )
    q_r = _snap("Q3", ["line_code", "scrap_qty"], [["L1", 18], ["L2", 22]], source="ops_shift")
    q_c = _snap("Q4", ["line_code", "scrap_units"], [["L1", 18], ["L2", 2]], source="quality_scrap")
    for alias, q in (("Q1", q_p), ("Q2", q_d), ("Q3", q_r), ("Q4", q_c)):
        scope.add_query(alias, q)

    co_res = await _run_op(
        {
            "op": "cochange",
            "mode": None,
            "params": {"base_query": "Q1", "target_query": "Q2", "period": "dt", "measure": "good_qty", "driver_measure": "downtime_min"},
        },
        scope,
        cfg,
    )
    rec_res = await _run_op(
        {
            "op": "reconcile",
            "mode": None,
            "params": {"base_query": "Q3", "target_query": "Q4", "by": ["line_code"], "measure": "scrap_qty", "target_measure": "scrap_units"},
        },
        scope,
        cfg,
    )
    h_res = {
        "kind": "source_health",
        "verdict": "anomalous",
        "phrase": "数据源「quality_scrap」存在断更告警（最新日期停在 2026-10-19）",
        "sources": [{"source": "quality_scrap", "verdict": "anomalous", "issue_count": 1}],
        "issues": [{"kind": "date_stalled", "severity": "alert", "detail": "最新日期停在 2026-10-19"}],
        "tables": [{"table": "scrap_ledger", "max_date": "2026-10-19"}],
    }

    store: dict[str, Any] = {}

    def put(obj: Any) -> str:
        sha = content_hash(canonical_json(obj).encode())
        store[sha] = obj
        return sha

    a1_art = put(co_res)
    a2_art = put(rec_res)
    h1_art = put(h_res)
    ledger = [
        analysis_contract.ledger_entry(co_res, node_id="bot", exec_no=1, artifact=a1_art, call_id="c1"),
        analysis_contract.ledger_entry(rec_res, node_id="bot", exec_no=1, artifact=a2_art, call_id="c2"),
        sh.ledger_entry(h_res, node_id="bot", exec_no=1, artifact=h1_art, call_id="h1"),
    ]
    catalog = evidence.build_catalog(nodes={}, ledger=ledger, loader=store.get)
    assert "H1" in catalog and catalog["H1"]["kind"] == sh.LEDGER_KIND

    # 1) D6 核心铁律：只挂 cochange（A1）、没有知识库/输入依据时，写「由于设备停机导致合格产量下降」必须被拦成 causal_unsupported！
    bad_doc = evidence.compose_doc(
        "星梭二线由于非计划停机时长上升导致合格产量下降[[see:A1]]。",
        catalog,
        node_id="write",
        run_id="r1",
        loader=store.get,
        analysis_claims="strict",
    )
    v_bad = analysis_claims.check_doc(bad_doc, catalog)
    assert any(v["code"] == analysis_claims.CAUSAL for v in v_bad), v_bad

    # 2) 合规写法：引用系统判定语 [[a:A1]] 并写统计伴随异动 -> 0 违规通过！
    good_cochange = "[[a:A1]]。两者在统计上伴随同步异动，具体工艺原因待结合现场检修记录核实[[see:A1]]。"
    doc_good = evidence.compose_doc(
        good_cochange,
        catalog,
        node_id="write",
        run_id="r1",
        loader=store.get,
        analysis_claims="strict",
    )
    assert analysis_claims.check_doc(doc_good, catalog) == []

    # 3) 数据源断更/对账不一致：挂 H1 与 A2 -> 0 违规通过；无依据空口写「数据源断更」被拦成 analysis_claim_uncited
    good_fault = "[[a:A2]]。[[h:H1]]，由于数据源断更导致双源报废数未对齐，不作业务滑坡归因[[see:A2,H1]]。"
    doc_fault = evidence.compose_doc(
        good_fault,
        catalog,
        node_id="write",
        run_id="r1",
        loader=store.get,
        analysis_claims="strict",
    )
    assert analysis_claims.check_doc(doc_fault, catalog) == []

    doc_unbacked = evidence.compose_doc(
        "质检台账昨天数据源断更，而且双源对账不一致。",
        catalog,
        node_id="write",
        run_id="r1",
        loader=store.get,
        analysis_claims="strict",
    )
    v_unbacked = analysis_claims.check_doc(doc_unbacked, catalog)
    assert any(v["code"] == analysis_claims.UNCITED for v in v_unbacked), v_unbacked

    # 4) verify_doc 复核通过且 analysis_scope.build 把 reconcile / cochange 收入 phenomena
    verify_res = evidence.verify_doc(doc_fault, catalog, loader=store.get)
    assert verify_res["ok"] is True, verify_res
    scope_sec = analysis_scope.build(catalog, sources={}, load=store.get)
    assert scope_sec is not None
    ops_in_phenomena = {item["op"] for item in scope_sec["phenomena"]}
    assert {"reconcile", "cochange"}.issubset(ops_in_phenomena)


@pytest.mark.skipif(NODE is None, reason="没有 node，无法执行离线包 verifier.js")
async def test_health_citation_evidence_chain_and_offline_bundle_verifier(tmp_path, scratch_source):
    seeded = await mso.seed_ontology_fixture(tmp_path, scratch_source, today=date(2026, 10, 22))
    yesterday = "2026-10-21"

    async with SessionLocal() as session:
        health_res = await sh.check_source_health(
            session,
            workflow_id=seeded["workflow_ids"]["quality"],
            card_id="m_quality",
            metric_id="ledger_scrap_units",
            start=yesterday,
            end=yesterday,
        )
    assert health_res["verdict"] == "anomalous"

    files: dict[str, bytes] = {}

    def put(obj: Any) -> str:
        raw = canonical_json(obj).encode()
        sha = content_hash(raw)
        files[sha] = raw
        return sha

    def load(sha: str) -> Any:
        raw = files.get(sha)
        return json.loads(raw) if raw is not None else None

    h_art = put(health_res)
    h_entry = sh.ledger_entry(health_res, node_id="bot", exec_no=1, artifact=h_art, call_id="call_h1")
    catalog = evidence.build_catalog(nodes={}, ledger=[h_entry], loader=load)
    assert "H1" in catalog

    # 报告正文使用 [[h:H1]] 渲染源健康判定语，并用 [[see:H1]] 挂接句末依据
    report_md = "## 跨源对账核查\n\n[[h:H1]]。因此昨日台账缺失属于源表同步断更，不做业务归因[[see:H1]]。"
    run_id = "9a1e0c3a5d9f4a2e8c6b4d2f0a1e3c5b"
    doc = evidence.compose_doc(report_md, catalog, node_id="write", run_id=run_id, loader=load)
    doc_id = put(doc)

    # 1) 服务端 verify_doc 校验通过
    checked = evidence.verify_doc(doc, catalog, loader=load)
    assert checked["ok"] is True, checked

    # 2) 构建离线可验证报告包并用 node 执行 verifier.js 逐字节核验
    run_meta = {
        "id": run_id,
        "workflow_id": "wf-ontology-s2",
        "workflow_name": "跨源健康排查",
        "version": 1,
        "version_hash": "ab" * 32,
        "run_class": "formal",
        "status": "succeeded",
        "started_at": "2026-10-22T01:00:00+00:00",
        "finished_at": "2026-10-22T01:01:00+00:00",
    }
    graph = {
        "nodes": [
            {"id": "bot", "type": "agent", "data": {"label": "本体排查Agent"}},
            {"id": "write", "type": "report", "data": {"label": "排查报告"}},
            {"id": "out", "type": "output", "data": {"label": "成果"}},
        ]
    }
    events: list[tuple[int, str, str | None, Any]] = [
        (1, "run.started", None, {"nodes": 3, "resumed": False, "tool_trust": "formal", "replay_protocol": 2}),
        (
            2,
            "tool.end",
            "bot",
            {
                "tool": "source_health",
                "duration_ms": 4,
                "health_artifact": {"ref": "H1", "artifact": h_art, "verdict": "anomalous", "phrase": health_res["phrase"]},
            },
        ),
        (3, "node.finished", "bot", {"duration_ms": 10, "attempt": 1, "evidence": [h_entry]}),
        (4, "report.checked", "write", {"doc_artifact": doc_id, "ok": True}),
        (5, "node.finished", "write", {"duration_ms": 8, "attempt": 1, "preview": {"text": "（报告正文）", "doc_artifact": doc_id}}),
        (6, "issuance", "out", {"tier": "formal", "mode": "citations"}),
        (7, "run.finished", None, {"output": {"报告": "（报告正文）"}}),
    ]
    bundle = build_bundle(
        run_meta,
        events,
        manifest_seq=len(events),
        manifest_hash=manifest_hash(events),
        read=files.get,
        exported_at="2026-10-22T02:00:00+00:00",
        graph=graph,
        source_masks={},
    )
    assert any(a["id"] == h_art and a["kind"] == sh.ARTIFACT_KIND for a in bundle["artifacts"])
    verdict = node("verify", data=bundle, seal=bundle["meta"]["manifest_hash"])
    assert verdict["overall"]["result"] == "pass", verdict

    # 3) 篡改报告里 [[h:H1]] 展示文字并重封事件哈希 -> verifier.js 必须精准检出健康判定语不一致！
    art, forged_doc = doc_of(bundle)
    seg = next(s for s in evidence.iter_segments(forged_doc) if s.get("ref") == "h:H1")
    fake_text = "数据源「quality_scrap」完全正常无任何延迟"
    forged_doc["markdown"] = forged_doc["markdown"].replace(seg["text"], fake_text, 1)
    seg["cite"]["rendered"] = seg["text"] = fake_text
    resealed = reseal(bundle, art, forged_doc)
    bad_verdict = node("verify", data=resealed, seal=resealed["meta"]["manifest_hash"])
    assert bad_verdict["overall"]["result"] == "fail", bad_verdict
