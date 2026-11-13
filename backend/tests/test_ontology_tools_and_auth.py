"""Wave 2-a 单元测试：ontology_neighbors、source_health 工具、跨源实体键值对齐 align_caliber_inputs 与受限用户授权/脱敏边界。"""
from __future__ import annotations

import uuid
from datetime import date

import pytest
from sqlalchemy import delete

from app.api import deps
from app.api.deps import Principal
from app.api.runs import _Projection
from app.auth import chat_policy
from app.chat.launch import bind_caliber_tool, user_chat_run
from app.core import artifact_store
from app.core.events import EventType
from app.data.entities import create_entity
from app.db.base import SessionLocal
from app.db.models import Run
from app.db.models_ontology import CanonicalEntity
from app.engine.runner import run_manager
from app.ledger import querycore
from app.tools.analysis import SCOPE_KEY, AnalysisScope, health_event_view
from app.tools.ontology import (
    ONTOLOGY_GUIDE,
    ONTOLOGY_TOOLS,
    align_caliber_inputs,
    ontology_neighbors,
    source_health_tool,
)
from app.tools.registry import ToolContext
from tests.fakes import auth_env
from tests.fixtures.acceptance import multi_source_ontology as mso
from tests.test_caliber_query_tool import grant_caliber


@pytest.fixture(autouse=True)
async def _engine_and_cleanup():
    await run_manager.setup()
    yield
    await run_manager.shutdown()
    async with SessionLocal() as session:
        await session.execute(delete(CanonicalEntity))
        await session.commit()


def _as_principal(username: str, role: str):
    return deps.set_principal(Principal(username=username, role=role, verified=True, mode="local"))


async def test_ontology_neighbors_confirmed_only_and_restricted_user_filtering(tmp_path, scratch_source):
    seeded = await mso.seed_ontology_fixture(tmp_path, scratch_source, today=date(2026, 10, 22))
    wf_ops = seeded["workflow_ids"]["ops"]
    wf_equip = seeded["workflow_ids"]["equip"]
    wf_quality = seeded["workflow_ids"]["quality"]

    # 额外创建一个仅含 proposed 绑定的候选标准实体，验证运行期绝不混入已确认跨源桥
    async with SessionLocal() as session:
        await create_entity(
            session,
            {
                "key": "proposed_only_dim",
                "label": "候选未确认维度",
                "bindings": [
                    {
                        "source_name": mso.SOURCE_OPS,
                        "table_name": "shift_output",
                        "column_name": "line_code",
                        "source": "ai",
                    },
                    {
                        "source_name": mso.SOURCE_QUALITY,
                        "table_name": "scrap_ledger",
                        "column_name": "workcenter_code",
                        "source": "ai",
                    },
                ],
            },
            actor="builder",
        )
        await session.commit()

    # 1) 搭建者/管理员视角：查整卡 m_ops 能看到全部 3 条已确认邻居（factor / reconciles_with / upstream_driver），
    #    而 load_rate_pct 上那条 proposed 的 upstream_driver 边绝不暴露
    scope = AnalysisScope(node_id="agent_1")
    ctx = ToolContext(
        run_id=uuid.uuid4().hex,
        node_id="agent_1",
        started_by="builder",
        extra={SCOPE_KEY: scope},
    )
    res_card = await ontology_neighbors(ctx, workflow=wf_ops, card="m_ops", entity_filters={"production_line": "L2"})
    assert res_card["kind"] == "ontology_neighbors"
    assert len(res_card["neighbors"]) == 3
    neighbor_metrics = {n["target"]["metric_id"] for n in res_card["neighbors"]}
    assert neighbor_metrics == {"rated_hourly_qty", "unplanned_downtime_min", "ledger_scrap_units"}
    assert scope.ontology_neighbors and scope.focal_calibers

    # 单查 load_rate_pct：只返回已确认的 factor（rated_hourly_qty），排除同指标上的 proposed upstream_driver
    res_load = await ontology_neighbors(ctx, workflow=wf_ops, card="m_ops", metric_id="load_rate_pct")
    assert [n["target"]["metric_id"] for n in res_load["neighbors"]] == ["rated_hourly_qty"]

    # 搭建者能看到已确认的跨源标准实体桥 production_line，看不到 proposed_only_dim
    bridge_keys = {b["key"] for b in res_card["entity_bridges"]}
    assert "production_line" in bridge_keys
    assert "proposed_only_dim" not in bridge_keys

    # 2) 开启登录后受限普通使用者（user 角色）：只授权 ops 与 equip，未授权 quality
    admin = await auth_env.turn_on_local()
    try:
        await auth_env.make_user("uma_ont", "user")
        await grant_caliber(wf_ops, 1, "uma_ont", "use")
        await grant_caliber(wf_equip, 1, "uma_ont", "use")

        token = _as_principal("uma_ont", "user")
        try:
            # 查 ops 的 good_output_qty（含 upstream_driver -> equip，已授权）能返回 equip 邻居，且脱敏物理表列细节
            user_ctx = ToolContext(run_id=uuid.uuid4().hex, node_id="agent_u", started_by="uma_ont")
            res_authorized = await ontology_neighbors(
                user_ctx,
                workflow=wf_ops,
                card="m_ops",
                metric_id="good_output_qty",
                entity_filters={"production_line": "L2"},
            )
            assert len(res_authorized["neighbors"]) == 1
            nb = res_authorized["neighbors"][0]["target"]
            assert nb["workflow_id"] == wf_equip and nb["metric_id"] == "unplanned_downtime_min"
            assert "tables_by_source" not in nb
            assert "sources" not in nb
            assert all("bindings" not in b and "source_codes" not in str(b) for b in res_authorized["entity_bridges"])

            # 查 ops 的 shift_scrap_qty（reconciles_with -> 未授权的 quality）：未授权邻居被过滤并计入 omitted_unauthorized_count
            res_filtered = await ontology_neighbors(
                user_ctx,
                workflow=wf_ops,
                card="m_ops",
                metric_id="shift_scrap_qty",
            )
            assert res_filtered["neighbors"] == []
            assert res_filtered["omitted_unauthorized_count"] >= 1
            assert wf_quality not in str(res_filtered)

            # 直接点名未授权的焦点工作流 quality -> 明确拒绝 querycore_not_authorized
            with pytest.raises(ValueError, match="querycore_not_authorized"):
                await ontology_neighbors(
                    user_ctx,
                    workflow=wf_quality,
                    card="m_quality",
                    metric_id="ledger_scrap_units",
                )
        finally:
            deps.reset_principal(token)
    finally:
        await admin.aclose()
        await auth_env.reset()


async def test_source_health_tool_artifact_redaction_and_run_projection(tmp_path, scratch_source):
    seeded = await mso.seed_ontology_fixture(tmp_path, scratch_source, today=date(2026, 10, 22))
    wf_ops = seeded["workflow_ids"]["ops"]
    wf_quality = seeded["workflow_ids"]["quality"]
    yesterday = "2026-10-21"

    scope = AnalysisScope(node_id="agent_h")
    run_id = uuid.uuid4().hex
    ctx = ToolContext(run_id=run_id, node_id="agent_h", started_by="builder", extra={SCOPE_KEY: scope})
    res = await source_health_tool(
        ctx,
        workflow=wf_quality,
        card="m_quality",
        metric_id="ledger_scrap_units",
        start=yesterday,
        end=yesterday,
    )
    assert res["verdict"] == "anomalous"
    assert res["artifact"]
    alias = scope.add_health(res["result"], res["artifact"])
    assert alias == "H1"
    stored = artifact_store.load(res["artifact"])
    assert stored["verdict"] == "anomalous"
    assert "inspector_id" not in str(stored)
    assert "INSP-900" not in str(stored)

    # 验证 /api/runs 受限用户事件投影安全保留 health_artifact 摘要并剥离内部工件哈希
    ev_view = {"ref": alias, "artifact": res["artifact"], **health_event_view(res["result"], alias=alias)}
    ev_dict = {
        "type": str(EventType.TOOL_END),
        "node_id": "agent_h",
        "data": {"tool": "source_health", "health_artifact": ev_view, "preview": "H1"},
    }
    projected = _Projection({"nodes": []}).event(ev_dict)
    assert projected["data"]["health_artifact"]["ref"] == "H1"
    assert projected["data"]["health_artifact"]["verdict"] == "anomalous"
    assert "artifact" not in projected["data"]["health_artifact"]

    # 开启登录后普通使用者：允许按已授权口径查健康（脱敏物理表列），禁止查未授权数据源或未授权口径
    admin = await auth_env.turn_on_local()
    try:
        await auth_env.make_user("uma_health", "user")
        await grant_caliber(wf_ops, 1, "uma_health", "use")
        token = _as_principal("uma_health", "user")
        try:
            u_ctx = ToolContext(run_id=uuid.uuid4().hex, node_id="agent_u", started_by="uma_health")
            ok_health = await source_health_tool(
                u_ctx,
                workflow=wf_ops,
                card="m_ops",
                metric_id="load_rate_pct",
                start="2026-10-15",
                end=yesterday,
            )
            assert ok_health["verdict"] == "healthy"
            assert "shift_output" not in str(ok_health)

            with pytest.raises(ValueError, match="querycore_not_authorized"):
                await source_health_tool(u_ctx, source_name=mso.SOURCE_QUALITY)

            with pytest.raises(ValueError, match="querycore_not_authorized"):
                await source_health_tool(u_ctx, workflow=wf_quality, card="m_quality")
        finally:
            deps.reset_principal(token)
    finally:
        await admin.aclose()
        await auth_env.reset()


async def test_caliber_query_entity_inputs_alignment_and_chat_policy_whitelist(tmp_path, scratch_source):
    seeded = await mso.seed_ontology_fixture(tmp_path, scratch_source, today=date(2026, 10, 22))
    wf_ops = seeded["workflow_ids"]["ops"]
    wf_equip = seeded["workflow_ids"]["equip"]
    yesterday = "2026-10-21"

    # 1) chat_policy 白名单与 chat/launch 自动补绑验证
    assert ONTOLOGY_TOOLS.issubset(chat_policy.CHAT_BUILTIN_TOOLS)
    assert "ontology_neighbors" in ONTOLOGY_GUIDE.text and "source_health" in ONTOLOGY_GUIDE.text
    graph = {
        "nodes": [
            {"id": "in", "type": "input", "data": {"label": "入口", "config": {"fields": [{"name": "question"}]}}},
            {"id": "bot", "type": "agent", "data": {"label": "分析", "config": {"prompt": "{{ input.question }}", "tools": []}}},
            {"id": "out", "type": "output", "data": {"label": "出口", "config": {"fields": [{"name": "a", "value": "{{ nodes.bot.text }}"}]}}},
        ],
        "edges": [{"source": "in", "target": "bot"}, {"source": "bot", "target": "out"}],
    }
    admin = await auth_env.turn_on_local()
    try:
        await auth_env.make_user("uma_bind", "user")
        await grant_caliber(wf_ops, 1, "uma_bind", "use")
        async with SessionLocal() as session:
            bound = await bind_caliber_tool(session, graph, "uma_bind")
    finally:
        await admin.aclose()
        await auth_env.reset()
    bot_tools = next(n for n in bound["nodes"] if n["id"] == "bot")["data"]["config"]["tools"]
    assert {"caliber_query", "ontology_neighbors", "source_health"}.issubset(set(bot_tools))
    assert chat_policy.chat_graph_problem(bound) is None

    # 2) align_caliber_inputs：L2 -> EQ-L2 确定性翻译；未知值绝不模糊猜测
    async with SessionLocal() as session:
        aligned, records = await align_caliber_inputs(
            session,
            workflow_id=wf_equip,
            card_id="m_equip",
            inputs={"stat_day": yesterday},
            entity_inputs={"production_line": "L2"},
            from_source=mso.SOURCE_OPS,
        )
        assert aligned["equip_line"] == "EQ-L2"
        assert records and records[0]["target_value"] == "EQ-L2"

        # 未知产线编码 L99 绝不子串匹配成 L1/L2
        untouched, empty_records = await align_caliber_inputs(
            session,
            workflow_id=wf_equip,
            card_id="m_equip",
            inputs={"stat_day": yesterday},
            entity_inputs={"production_line": "L99"},
            from_source=mso.SOURCE_OPS,
        )
        assert "equip_line" not in untouched
        assert empty_records == []

    # 3) 真跑父 Run 下的 caliber_query(entity_inputs={"production_line": "L2"})，验证算出 EQ-L2 非计划停机 96.0 分钟
    async with SessionLocal() as session:
        parent = Run(
            status="running",
            run_class="exploratory",
            graph=bound,
            input={"question": "排查二线停机"},
            started_by="builder",
        )
        session.add(parent)
        await session.commit()
        parent_id = parent.id

    emitted_events: list[tuple] = []
    ctx = ToolContext(
        run_id=parent_id,
        node_id="bot",
        started_by="builder",
        emit=lambda etype, **kw: emitted_events.append((etype, kw)),
    )
    cq_res = await querycore.caliber_query(
        ctx,
        workflow=wf_equip,
        card="m_equip",
        metric_ids=["unplanned_downtime_min"],
        inputs={"stat_day": yesterday},
        entity_inputs={"production_line": "L2"},
        from_source=mso.SOURCE_OPS,
    )
    assert cq_res["ok"] is True
    assert cq_res["ontology_alignment"][0]["target_value"] == "EQ-L2"
    metrics_by_id = {m["id"]: m for m in cq_res["metrics"]}
    assert metrics_by_id["unplanned_downtime_min"]["value"] == pytest.approx(96.0)

    # 4) 验证 caliber_query 对越界指标（S1：L3 load_rate_pct = 135% > 100%）透传 contract_findings 并发 metric_contract_violated 日志
    cq_s1 = await querycore.caliber_query(
        ctx,
        workflow=wf_ops,
        card="m_ops",
        metric_ids=["load_rate_pct"],
        inputs={"shift_day": yesterday, "line_code": "L3"},
    )
    assert cq_s1["ok"] is True
    load_metric = next(m for m in cq_s1["metrics"] if m["id"] == "load_rate_pct")
    assert load_metric["value"] == pytest.approx(135.0)
    assert load_metric.get("contract_findings")
    assert any(kw.get("code") == "metric_contract_violated" for _t, kw in emitted_events)

    # 5) 验证 user_chat_run 对有授权口径的普通使用者自动补齐 ontology 工具
    admin = await auth_env.turn_on_local()
    try:
        await auth_env.make_user("uma_launch", "user")
        await grant_caliber(wf_ops, 1, "uma_launch", "use")
        token = _as_principal("uma_launch", "user")
        try:
            async with SessionLocal() as session:
                _mem, _opts, patched_graph = await user_chat_run(session, graph, "uma_launch")
            patched_tools = next(n for n in patched_graph["nodes"] if n["id"] == "bot")["data"]["config"]["tools"]
            assert {"caliber_query", "ontology_neighbors", "source_health"}.issubset(set(patched_tools))
        finally:
            deps.reset_principal(token)
    finally:
        await admin.aclose()
        await auth_env.reset()
