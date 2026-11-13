"""Wave 2-c 单元测试：look 口径/表本体提示（按角色与授权脱敏）、Copilot 分析与本体提示词契约、P1 异常告警一键升级与最多 1 轮自动升级。"""
from __future__ import annotations

import uuid
from datetime import date

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select

import app.api.copilot as copilot
from app.analysis import contract as analysis_contract
from app.api import deps
from app.api.deps import Principal
from app.api.settings import anomaly_escalation_mode
from app.chat import drive as chat_drive
from app.chat import turn
from app.core.events import EventType
from app.db.base import SessionLocal
from app.db.models import DataSource, Run, RunEvent, Setting
from app.db.models_ontology import CanonicalEntity
from app.engine.runner import run_manager
from app.look.calibers import CaliberKind
from app.look.tables import TableKind
from app.main import app
from tests.fakes import auth_env
from tests.fixtures.acceptance import multi_source_ontology as mso
from tests.test_caliber_query_tool import grant_caliber


@pytest.fixture(autouse=True)
async def _cleanup():
    await run_manager.setup()
    yield
    await run_manager.shutdown()
    async with SessionLocal() as session:
        await session.execute(delete(CanonicalEntity))
        row = await session.get(Setting, "run")
        if row is not None:
            await session.delete(row)
        await session.commit()


def _as_principal(username: str, role: str):
    return deps.set_principal(Principal(username=username, role=role, verified=True, mode="local"))


async def test_look_caliber_and_table_ontology_hints_respect_confirmed_and_user_boundaries(tmp_path, scratch_source):
    seeded = await mso.seed_ontology_fixture(tmp_path, scratch_source, today=date(2026, 10, 22))
    wf_ops = seeded["workflow_ids"]["ops"]
    wf_equip = seeded["workflow_ids"]["equip"]
    wf_quality = seeded["workflow_ids"]["quality"]

    # 1) 搭建者（builder）查看口径详情：展示 1 跳已确认本体关联指标与底层数据源，绝不展示 proposed 边
    async with SessionLocal() as session:
        cal_builder = await CaliberKind.load(session, compose=True, actor="builder")
        found, missing = await cal_builder.resolve([{"source": wf_ops, "name": "good_output_qty"}])
        assert found and not missing
        detail_builder, _ = await cal_builder.detail(found, budget=8000)
        assert "本体关联指标" in detail_builder
        assert "unplanned_downtime_min" in detail_builder
        assert mso.SOURCE_EQUIP in detail_builder

        # load_rate_pct 上有 1 条 confirmed factor + 1 条 proposed upstream_driver：只显示 rated_hourly_qty
        f_load, _ = await cal_builder.resolve([{"source": wf_ops, "name": "load_rate_pct"}])
        detail_load, _ = await cal_builder.detail(f_load, budget=8000)
        assert "rated_hourly_qty" in detail_load
        assert "unplanned_downtime_min" not in detail_load

        # 查看数据表详情：搭建者看到已确认的跨源标准实体桥（production_line: ops_shift <-> equip_master / quality_scrap）
        src_rows = list((await session.execute(select(DataSource).where(
            DataSource.name.in_([mso.SOURCE_OPS, mso.SOURCE_EQUIP])
        ))).scalars())
    tbl_builder = await TableKind.load(src_rows, trail={"actor": "builder"})
    t_refs, t_missing = await tbl_builder.resolve([{"source": mso.SOURCE_OPS, "name": "shift_output"}])
    assert t_refs and not t_missing
    tbl_detail_builder, _ = await tbl_builder.detail(t_refs, budget=8000)
    assert "跨源标准实体桥" in tbl_detail_builder
    assert "production_line" in tbl_detail_builder
    assert f"{mso.SOURCE_EQUIP}:line_specs.equip_line_id" in tbl_detail_builder

    # 2) 普通受限使用者（user）：口径详情只列已授权邻居且隐去底层数据源名；表详情不泄露跨源物理表桥
    admin = await auth_env.turn_on_local()
    try:
        await auth_env.make_user("uma_look", "user")
        await grant_caliber(wf_ops, 1, "uma_look", "use")
        await grant_caliber(wf_equip, 1, "uma_look", "use")
        # 注意：不给 uma_look 授权 wf_quality

        token = _as_principal("uma_look", "user")
        try:
            async with SessionLocal() as session:
                cal_user = await CaliberKind.load(session, query=True, actor="uma_look")
                # good_output_qty -> unplanned_downtime_min（已授权）：可见邻居但隐去物理数据源名
                f_rate, _ = await cal_user.resolve([{"source": wf_ops, "name": "good_output_qty"}])
                detail_rate, _ = await cal_user.detail(f_rate, budget=8000)
                assert "unplanned_downtime_min" in detail_rate
                assert mso.SOURCE_EQUIP not in detail_rate

                # shift_scrap_qty -> ledger_scrap_units（未授权 wf_quality）：绝不泄露未授权邻居
                f_rep, _ = await cal_user.resolve([{"source": wf_ops, "name": "shift_scrap_qty"}])
                detail_rep, _ = await cal_user.detail(f_rep, budget=8000)
                assert "ledger_scrap_units" not in detail_rep
                assert wf_quality not in detail_rep

            tbl_user = await TableKind.load(src_rows, trail={"actor": "uma_look"})
            u_refs, _ = await tbl_user.resolve([{"source": mso.SOURCE_OPS, "name": "shift_output"}])
            tbl_detail_user, _ = await tbl_user.detail(u_refs, budget=8000)
            assert "跨源标准实体桥" not in tbl_detail_user
            assert "equip_line_id" not in tbl_detail_user
        finally:
            deps.reset_principal(token)
    finally:
        await admin.aclose()
        await auth_env.reset()


def test_copilot_prompt_contracts_for_ontology_and_analysis_off_switch():
    ref_on = copilot.node_reference(analysis_tools=True)
    for tool_name in analysis_contract.TOOL_NAMES.values():
        assert tool_name in ref_on
        assert tool_name in copilot.ANALYSIS_SHAPE_PROMPT
    assert "ontology_neighbors" in ref_on and "source_health" in ref_on
    assert "[[h:H1]]" in ref_on
    assert "ontology_neighbors" in copilot._CHAT_USER_PROMPT and "inputs" in copilot._CHAT_USER_PROMPT

    # 评测关掉分析工具那一臂（analysis_tools=False）：所有 analysis_* 与分析段落干净剥离
    ref_off = copilot.node_reference(analysis_tools=False)
    for tool_name in analysis_contract.TOOL_NAMES.values():
        assert tool_name not in ref_off
    assert "analysis_" not in ref_off
    assert "[[a:" not in ref_off


async def test_anomaly_escalation_prompt_default_and_auto_single_rerun_cap(tmp_path, scratch_source):
    await mso.seed_ontology_fixture(tmp_path, scratch_source, today=date(2026, 10, 22))
    simple_graph = {
        "nodes": [
            {"id": "in", "type": "input", "data": {"label": "入口", "config": {"fields": [{"name": "question"}]}}},
            {"id": "card", "type": "metrics", "data": {"label": "指标卡", "config": {"metrics": []}}},
            {"id": "out", "type": "output", "data": {"label": "出口", "config": {"fields": [{"name": "结果", "value": "135%"}]}}},
        ],
        "edges": [{"source": "in", "target": "card"}, {"source": "card", "target": "out"}],
    }

    async with SessionLocal() as session:
        assert await anomaly_escalation_mode(session) == "prompt"
        run1 = Run(
            id=uuid.uuid4().hex,
            status="succeeded",
            graph=simple_graph,
            output={"结果": "昨日星梭三线产线负荷率为 135.0%。"},
        )
        session.add(run1)
        await session.flush()
        session.add(RunEvent(
            run_id=run1.id,
            seq=1,
            type=EventType.LOG,
            node_id="card",
            ts=1.0,
            data={
                "level": "warn",
                "code": "metric_contract_violated",
                "metric": "load_rate_pct",
                "reason": "指标值超出声明的范围",
                "message": "指标「产线负荷率」触发数值契约告警：指标值超出声明的范围",
            },
        ))
        await session.commit()
        run1_id = run1.id

    # 1) 默认 prompt 模式：返回正常复核说明 + anomaly_escalation 提示按钮，retry_instruction 为 None（不自动重跑烧额外 token）
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r_prompt = await client.post("/api/copilot/review", json={"run_id": run1_id, "question": "昨天三线产线负荷率是多少"})
        assert r_prompt.status_code == 200, r_prompt.text
        body_prompt = r_prompt.json()
        assert body_prompt["retry_instruction"] is None
        esc = body_prompt["anomaly_escalation"]
        assert esc["available"] is True
        assert esc["mode"] == "prompt"
        assert esc["auto_escalate"] is False
        assert esc["button_label"] == turn.ESCALATION_BUTTON_LABEL
        assert "metric_contract_violated" in esc["signals"]
        assert "ontology_neighbors" in esc["follow_up_instruction"]

        # 校验设置接口拒绝非法的 anomaly_escalation_mode，接受 "auto"
        bad_put = await client.put("/api/settings", json={"values": {"run": {"anomaly_escalation_mode": "infinite"}}})
        assert bad_put.status_code == 422
        ok_put = await client.put("/api/settings", json={"values": {"run": {"anomaly_escalation_mode": "auto"}}})
        assert ok_put.status_code == 200

        # 2) 切换到 auto 模式：attempt=0 给出升级指令且 escalation=True；attempt=1 严格封顶为 None（最多自动升级 1 轮！）
        r_auto_0 = await client.post("/api/copilot/review", json={"run_id": run1_id, "question": "昨天三线产线负荷率是多少", "attempt": 0})
        body_auto_0 = r_auto_0.json()
        assert body_auto_0["escalation"] is True
        assert body_auto_0["anomaly_escalation"]["auto_escalate"] is True
        assert body_auto_0["retry_instruction"] == body_auto_0["anomaly_escalation"]["follow_up_instruction"]
        assert turn.escalate_after_review(body_auto_0, 0) is True
        assert turn.escalate_after_review(body_auto_0, 1) is False

        r_auto_1 = await client.post("/api/copilot/review", json={"run_id": run1_id, "question": "昨天三线产线负荷率是多少", "attempt": 1})
        body_auto_1 = r_auto_1.json()
        assert body_auto_1["anomaly_escalation"]["auto_escalate"] is False
        assert body_auto_1["retry_instruction"] is None

    # 3) 验证 TurnDriver 在 auto 模式下端到端最多自动触发 1 次升级重跑（总共建图 2 次、跑图 2 次后停止）
    seen_instructions: list[str] = []
    retry_kinds: list[str] = []

    class _FakeDriver(chat_drive.TurnDriver):
        async def _generate(self, instruction: str, base_graph: dict | None):
            seen_instructions.append(instruction)
            return [{"op": "final", "graph": simple_graph, "issues": [], "autorun": True, "next": "run"}], None

        async def _run(self, graph: dict, payload: dict | None = None):
            async with SessionLocal() as s:
                r = Run(
                    id=uuid.uuid4().hex,
                    status="succeeded",
                    graph=graph,
                    output={"结果": "135.0%"},
                )
                s.add(r)
                await s.flush()
                s.add(RunEvent(
                    run_id=r.id,
                    seq=1,
                    type=EventType.LOG,
                    node_id="card",
                    ts=1.0,
                    data={
                        "level": "warn",
                        "code": "metric_contract_violated",
                        "metric": "load_rate_pct",
                        "reason": "指标值超出声明的范围",
                        "message": "指标「产线负荷率」触发数值契约告警：指标值超出声明的范围",
                    },
                ))
                await s.commit()
                self.turn.runs.append(r.id)
                self.turn.run_id = r.id
                self.last_run = r
                return r

        async def on_retry(self, reason: str, *, kind: str, base_graph: dict | None, detail: str | None = None):
            retry_kinds.append(kind)

    drv = _FakeDriver("昨天三线产线负荷率是多少", datasource_ids=[])
    settled_turn = await drv.drive()
    assert settled_turn.path == "ran"
    assert settled_turn.attempts == 1
    assert len(seen_instructions) == 2
    assert "ontology_neighbors" in seen_instructions[1]
    assert retry_kinds == ["escalation"]
