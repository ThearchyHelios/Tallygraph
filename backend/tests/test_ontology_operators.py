"""Wave 1-b 单元测试：数据源健康度检查 check_source_health、跨源对账算子 reconcile、伴随异动算子 cochange 及 Agent 工具接入。"""
from __future__ import annotations

import asyncio
from datetime import date

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from sqlalchemy import delete, select

import app.analysis as analysis_ops
from app.analysis import contract
from app.analysis.contract import CallError
from app.core import artifact_store
from app.data import source_changes as sc
from app.data import source_health as sh
from app.db.base import SessionLocal
from app.db.models import DataSource, Run, RunEvent
from app.db.models_ontology import CanonicalEntity
from app.engine.runner import run_manager
from app.tools.analysis import input_snapshot
from tests.fixtures.acceptance import multi_source_ontology as mso


@pytest.fixture(autouse=True)
async def _engine_lifecycle():
    await run_manager.setup()
    yield
    await run_manager.shutdown()
    async with SessionLocal() as session:
        await session.execute(delete(CanonicalEntity))
        await session.commit()


def _snap(ref: str, columns: list[str], rows: list[list], *, masked: list[str] | None = None,
          truncated: bool = False, source: str = "ops_shift", artifact: str | None = None) -> dict:
    return input_snapshot(
        {
            "columns": columns,
            "rows": rows,
            "sql": f"SELECT {', '.join(columns)} FROM t",
            "source": source,
            "truncated": truncated,
        },
        ref=ref,
        artifact=artifact or (ref.lower().ljust(64, "0")[:64]),
        node_id="bot",
        masked=masked or [],
    )


async def test_source_health_verdicts_masking_and_caliber_reverse_lineage(tmp_path, scratch_source, monkeypatch):
    seeded = await mso.seed_ontology_fixture(tmp_path, scratch_source, today=date(2026, 10, 22))
    yesterday = "2026-10-21"

    async with SessionLocal() as session:
        # 1) ops_shift 数据最新覆盖到昨天 -> healthy，且探查绝不触碰遮罩列 operator_phone
        healthy = await sh.check_source_health(
            session,
            source_name=mso.SOURCE_OPS,
            tables=["shift_output"],
            start="2026-10-15",
            end=yesterday,
        )
        assert healthy["verdict"] == sh.HEALTHY
        assert "operator_phone" not in str(healthy)
        assert "138-0010" not in str(healthy)

        # 2) quality_scrap 在昨天断更（SourceChange date_stalled + 现查最大日期 2026-10-19 < 2026-10-21）-> anomalous
        anomalous = await sh.check_source_health(
            session,
            source_name=mso.SOURCE_QUALITY,
            tables=["scrap_ledger"],
            start=yesterday,
            end=yesterday,
        )
        assert anomalous["verdict"] == sh.ANOMALOUS
        assert any(i["kind"] == sc.DATE_STALLED for i in anomalous["issues"])
        assert "inspector_id" not in str(anomalous)
        assert "INSP-900" not in str(anomalous)

        # 3) restricted=True 受限视角隐去物理表名、物理列名与底层数据源标识
        restricted_res = await sh.check_source_health(
            session,
            source_name=mso.SOURCE_QUALITY,
            tables=["scrap_ledger"],
            start=yesterday,
            end=yesterday,
            restricted=True,
        )
        assert restricted_res["verdict"] == sh.ANOMALOUS
        assert "scrap_ledger" not in str(restricted_res)
        assert "inspect_date" not in str(restricted_res)

        # 4) 按口径卡反向血缘自动解析底层数据源与物理表
        via_caliber = await sh.check_source_health(
            session,
            workflow_id=seeded["workflow_ids"]["quality"],
            card_id="m_quality",
            metric_id="ledger_scrap_units",
            start=yesterday,
            end=yesterday,
        )
        assert via_caliber["verdict"] == sh.ANOMALOUS
        assert via_caliber["caliber_ref"]["workflow_id"] == seeded["workflow_ids"]["quality"]

        # 5) codes_new 新码值提醒 -> degraded
        equip_src = await session.get(DataSource, seeded["source_ids"][mso.SOURCE_EQUIP])
        await sc.record(
            session,
            equip_src,
            sc.NewChange(kind=sc.CODES_NEW, table="downtime_events", column="fault_category", severity="warning", key="new_code"),
            sc.Detected(job_kind="profile", trigger="manual", actor="tester"),
        )
        await session.commit()
        degraded = await sh.check_source_health(
            session,
            source_name=mso.SOURCE_EQUIP,
            tables=["downtime_events"],
            start="2026-10-15",
            end=yesterday,
        )
        assert degraded["verdict"] == sh.DEGRADED

        # 6) 工件存储与台账条目形状校验
        art_id = await sh.store_source_health(anomalous, run_id=None, node_id="bot")
        loaded = artifact_store.load(art_id)
        assert sh.is_health_result(loaded)
        entry = sh.ledger_entry(anomalous, node_id="bot", exec_no=1, artifact=art_id, call_id="c1")
        assert entry["kind"] == "source_health" and entry["verdict"] == sh.ANOMALOUS

        # 7) 实时探查异常降级为 unverified 而不抛未捕获异常
        async def _boom(*_a, **_kw):
            raise TimeoutError("simulated timeout")

        monkeypatch.setattr(sh, "run_query", _boom)
        fallback = await sh.check_source_health(session, source_name=mso.SOURCE_OPS, tables=["shift_output"])
        assert fallback["verdict"] == sh.UNVERIFIED


async def _run_op(req: dict, resolve, *, refuse_data_errors: bool = False) -> dict:
    return await analysis_ops.run(
        req,
        resolve,
        settings=contract.DEFAULTS,
        context={},
        refuse_data_errors=refuse_data_errors,
    )


async def test_reconcile_operator_verdicts_errors_and_byte_identical_recompute():
    q_left = _snap("Q1", ["line_code", "scrap_qty", "operator_phone"], [["L1", 20, "***"], ["L2", 50, "***"]], masked=["operator_phone"])
    q_right_ok = _snap("Q2", ["wc_line", "scrap_units"], [["L1", 20], ["L2", 50]], source="quality_scrap")
    q_right_div = _snap("Q2", ["wc_line", "scrap_units"], [["L1", 20], ["L2", 35]], source="quality_scrap")
    q_right_miss = _snap("Q2", ["wc_line", "scrap_units"], [["L1", 20]], source="quality_scrap")

    # 1) 完全对齐 -> aligned
    res_aligned = await _run_op(
        {
            "op": "reconcile",
            "mode": None,
            "params": {
                "base_query": "Q1",
                "target_query": "Q2",
                "by": ["line_code"],
                "target_by": ["wc_line"],
                "measure": "scrap_qty",
                "target_measure": "scrap_units",
                "label": "报废件数",
            },
        },
        lambda name: {"Q1": q_left, "Q2": q_right_ok}.get(name),
    )
    assert res_aligned["status"] == "ok" and res_aligned["verdict"]["code"] == "aligned"

    # 2) 超差 -> diverged，且双盲复算重算逐字节一致（contract.same）
    req_div = {
        "op": "reconcile",
        "mode": None,
        "params": {
            "base_query": "Q1",
            "target_query": "Q2",
            "by": ["line_code"],
            "target_by": ["wc_line"],
            "measure": "scrap_qty",
            "target_measure": "scrap_units",
            "label": "报废件数",
        },
    }
    res_div = await _run_op(req_div, lambda name: {"Q1": q_left, "Q2": q_right_div}.get(name))
    res_div_again = await _run_op(req_div, lambda name: {"Q1": q_left, "Q2": q_right_div}.get(name))
    assert res_div["status"] == "ok" and res_div["verdict"]["code"] == "diverged"
    assert res_div["verdict"]["subjects"] == [["L2"]]
    assert contract.same(res_div, res_div_again)

    # 3) 对侧缺失 -> missing_counterpart
    res_miss = await _run_op(
        {**req_div},
        lambda name: {"Q1": q_left, "Q2": q_right_miss}.get(name),
    )
    assert res_miss["status"] == "ok" and res_miss["verdict"]["code"] == "missing_counterpart"

    # 4) 单份查询 + unit_factor 换算（千瓦时 vs 兆瓦时 * 1000）
    q_single = _snap("Q3", ["line_code", "kwh", "mwh"], [["L1", 12000, 12.0], ["L2", 13000, 13.0]])
    res_unit = await _run_op(
        {
            "op": "reconcile",
            "mode": None,
            "params": {"query": "Q3", "by": ["line_code"], "measure": "kwh", "target_measure": "mwh", "unit_factor": 1000},
        },
        lambda name: q_single if name == "Q3" else None,
    )
    assert res_unit["status"] == "ok" and res_unit["verdict"]["code"] == "aligned"

    # 5) 遮罩列拒收 + 截断数据在 refuse_data_errors=True 时返回 refused
    with pytest.raises(CallError):
        await _run_op(
            {
                "op": "reconcile",
                "mode": None,
                "params": {"base_query": "Q1", "target_query": "Q2", "by": ["operator_phone"], "measure": "scrap_qty"},
            },
            lambda name: {"Q1": q_left, "Q2": q_right_ok}.get(name),
        )
    q_trunc = _snap("QT", ["line_code", "kwh", "mwh"], [["L1", 12000, 10.0]], truncated=True)
    res_trunc = await _run_op(
        {"op": "reconcile", "mode": None, "params": {"query": "QT", "by": ["line_code"], "measure": "kwh", "target_measure": "mwh"}},
        lambda name: q_trunc if name == "QT" else None,
        refuse_data_errors=True,
    )
    assert res_trunc["status"] == "refused"


async def test_cochange_operator_verdicts_lag_non_causal_phrase_and_recompute():
    days = [f"2026-10-{d:02d}" for d in range(1, 16)]
    # 前 14 期对称平稳小幅波动，第 15 期主指标暴跌、驱动指标暴涨
    p_base = [100, 102, 99, 101, 100, 98, 103, 101, 99, 102, 100, 98, 101, 100]
    d_base = [12.0, 12.5, 11.8, 12.2, 12.0, 11.6, 12.4, 12.1, 11.9, 12.3, 12.0, 11.7, 12.2, 12.0]
    p_vals = p_base + [68]
    d_spike = d_base + [85.0]
    d_flat = d_base + [12.1]

    q_primary = _snap("Q1", ["shift_date", "good_qty"], [[d, v] for d, v in zip(days, p_vals)])
    q_driver_spike = _snap("Q2", ["event_date", "unplanned_min"], [[d, v] for d, v in zip(days, d_spike)], source="equip_master")
    q_driver_flat = _snap("Q2", ["event_date", "unplanned_min"], [[d, v] for d, v in zip(days, d_flat)], source="equip_master")

    req_co = {
        "op": "cochange",
        "mode": None,
        "params": {
            "base_query": "Q1",
            "target_query": "Q2",
            "period": "shift_date",
            "target_period": "event_date",
            "measure": "good_qty",
            "driver_measure": "unplanned_min",
            "label": "合格产量",
        },
    }
    res_co = await _run_op(req_co, lambda name: {"Q1": q_primary, "Q2": q_driver_spike}.get(name))
    res_co_2 = await _run_op(req_co, lambda name: {"Q1": q_primary, "Q2": q_driver_spike}.get(name))
    assert res_co["status"] == "ok" and res_co["verdict"]["code"] == "co_anomalous"
    assert contract.same(res_co, res_co_2)
    # 决策 D6：系统判定语严禁输出因果词（导致、引起、造成）
    for forbidden in ("导致", "引起", "造成", "归因于"):
        assert forbidden not in res_co["phrase"]

    # 上游平稳 -> target_normal
    res_norm_target = await _run_op(req_co, lambda name: {"Q1": q_primary, "Q2": q_driver_flat}.get(name))
    assert res_norm_target["status"] == "ok" and res_norm_target["verdict"]["code"] == "target_normal"

    # 主指标平稳 -> primary_normal
    q_primary_steady = _snap("Q1", ["shift_date", "good_qty"], [[d, v] for d, v in zip(days, p_base + [101])])
    res_norm_primary = await _run_op(
        req_co, lambda name: {"Q1": q_primary_steady, "Q2": q_driver_spike}.get(name)
    )
    assert res_norm_primary["status"] == "ok" and res_norm_primary["verdict"]["code"] == "primary_normal"

    # lag=1 领先期检验：驱动指标在第 14 期飙升，主指标在第 15 期下跌 -> co_anomalous
    d_lead = d_base[:-1] + [88.0, 12.0]
    q_driver_lead = _snap("Q2", ["event_date", "unplanned_min"], [[d, v] for d, v in zip(days, d_lead)], source="equip_master")
    res_lag = await _run_op(
        {**req_co, "params": {**req_co["params"], "lag": 1}},
        lambda name: {"Q1": q_primary, "Q2": q_driver_lead}.get(name),
    )
    assert res_lag["status"] == "ok" and res_lag["verdict"]["code"] == "co_anomalous"

    # 历史不足 -> refused (history_short)
    q_short = _snap("QS", ["shift_date", "good_qty", "unplanned_min"], [[days[i], p_vals[i], d_spike[i]] for i in range(5)])
    res_short = await _run_op(
        {
            "op": "cochange",
            "mode": None,
            "params": {"query": "QS", "period": "shift_date", "measure": "good_qty", "driver_measure": "unplanned_min"},
        },
        lambda name: q_short if name == "QS" else None,
    )
    assert res_short["status"] == "refused" and res_short["reason_code"] == "history_short"


async def test_agent_executes_analysis_reconcile_and_cochange_and_records_ledger(tmp_path, scratch_source, monkeypatch):
    from app.providers import mock_model

    await mso.seed_ontology_fixture(tmp_path, scratch_source, today=date(2026, 10, 22))
    calls = [
        ("db_query__ops_shift", {"sql": "SELECT line_code, meter_kwh, settled_mwh FROM energy_readings WHERE shift_date = '2026-10-21' ORDER BY line_code"}),
        (
            "analysis_reconcile",
            {"query": "Q1", "by": ["line_code"], "measure": "meter_kwh", "target_measure": "settled_mwh", "unit_factor": 1000, "label": "产线能耗"},
        ),
        ("db_query__equip_master", {"sql": "SELECT event_date, net_run_ratio, unplanned_min FROM downtime_events WHERE equip_line_id = 'EQ-L2' ORDER BY event_date"}),
        (
            "analysis_cochange",
            {"query": "Q2", "period": "event_date", "measure": "net_run_ratio", "driver_measure": "unplanned_min", "target": "2026-10-21", "label": "有效运转率"},
        ),
    ]

    def decide(model, messages):
        done = sum(isinstance(m, ToolMessage) for m in messages)
        if done < len(calls):
            name, args = calls[done]
            return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": f"call_{done}"}])
        return AIMessage(content="对账与伴随异动核查已完成。")

    monkeypatch.setattr(mock_model.MockChatModel, "_decide", decide)

    nodes = [
        {"id": "start", "type": "input", "position": {"x": 0, "y": 0}, "data": {"label": "入口", "config": {}}},
        {
            "id": "bot",
            "type": "agent",
            "position": {"x": 220, "y": 0},
            "data": {
                "label": "诊断助手",
                "config": {
                    "prompt": "执行跨源能耗对账与设备停机伴随异动检验",
                    "tools": ["db_query__ops_shift", "db_query__equip_master", "analysis_reconcile", "analysis_cochange"],
                    "approval": "never",
                },
            },
        },
        {
            "id": "out",
            "type": "output",
            "position": {"x": 460, "y": 0},
            "data": {"label": "出口", "config": {"fields": [{"name": "answer", "value": "{{ nodes.bot.text }}"}]}},
        },
    ]
    graph = {"nodes": nodes, "edges": [{"source": "start", "target": "bot"}, {"source": "bot", "target": "out"}]}
    run = await run_manager.start(graph=graph, input_payload={})
    for _ in range(300):
        await asyncio.sleep(0.05)
        async with SessionLocal() as session:
            row = await session.get(Run, run.id)
        if row.status in ("succeeded", "failed") and row.manifest_seq is not None:
            break
    assert row.status == "succeeded", row.error

    async with SessionLocal() as session:
        ev = (
            await session.execute(
                select(RunEvent).where(RunEvent.run_id == run.id, RunEvent.type == "node.finished", RunEvent.node_id == "bot")
            )
        ).scalar_one()
    analysis_entries = [e for e in ev.data["evidence"] if e.get("kind") == "analysis"]
    assert [(e["op"], e["verdict"]) for e in analysis_entries] == [
        ("reconcile", "diverged"),
        ("cochange", "co_anomalous"),
    ]
