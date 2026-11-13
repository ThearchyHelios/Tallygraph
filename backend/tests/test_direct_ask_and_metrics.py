"""快速直查问数引擎（M3）、毫秒级确定性语义预命中（M2）与独立语义指标库 + metric_query 工具（M1）端到端测试。"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from langchain_core.messages import AIMessage, ToolMessage
from sqlalchemy import delete, select

from app.capabilities.registry import is_platform_query, snapshot_owned
from app.chat import job as chat_job
from app.chat import log as turn_log
from app.chat.grounding import ground_question
from app.data import catalog, catalog_assistant
from app.data.catalog import make_item
from app.data.engine import CATALOG_PROFILE_OPTION, MASK_COLUMNS_OPTION
from app.db.base import SessionLocal
from app.db.models import Artifact, ConversationTurn, Document, Run, RunEvent
from app.db.models_chat import ChatTurnJob
from app.db.models_metrics import SemanticMetric
from app.db.models_ontology import CanonicalEntity
from app.engine.evidence import iter_segments
from app.engine.runner import run_manager
from app.main import app
from app.providers import mock_model
from app.tools.metrics import METRIC_QUERY
from tests import eval_samples as es
from tests.fixtures.sources import drop_source, sqlite_source


@pytest.fixture(autouse=True)
async def _engine_lifecycle():
    await run_manager.setup()
    yield
    await chat_job.shutdown()
    await run_manager.shutdown()
    async with SessionLocal() as session:
        await session.execute(delete(SemanticMetric))
        await session.execute(delete(CanonicalEntity).where(CanonicalEntity.key.like("park_test_%")))
        await session.execute(delete(Document).where(Document.source == "test_metric_formula.md"))
        await session.commit()


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def _create_park_fixture(tmp_path: Path) -> dict[str, str]:
    db_path = tmp_path / f"park_demo_{uuid.uuid4().hex[:6]}.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(
            """
            CREATE TABLE park_traffic_daily (
                id INTEGER PRIMARY KEY,
                stat_date TEXT NOT NULL,
                zone_code TEXT NOT NULL,
                net_visitors INTEGER NOT NULL,
                crowd_density INTEGER NOT NULL,
                secret_phone TEXT,
                status INTEGER NOT NULL DEFAULT 1
            );
            INSERT INTO park_traffic_daily (stat_date, zone_code, net_visitors, crowd_density, secret_phone, status) VALUES
                ('2026-11-09', 'Z_WEST_04', 1200, 85, '13800001111', 1),
                ('2026-11-09', 'Z_EAST_01', 800, 60, '13800002222', 1),
                ('2026-11-10', 'Z_WEST_04', 1500, 92, '13800003333', 1),
                ('2026-11-10', 'Z_EAST_01', 900, 65, '13800004444', 1),
                ('2026-11-10', 'Z_WEST_04', 9999, 99, '13800005555', 0);
            """
        )
        conn.commit()
    finally:
        conn.close()

    src_name = f"park_demo_{uuid.uuid4().hex[:6]}"
    sid = await sqlite_source(
        src_name,
        db_path,
        options={
            CATALOG_PROFILE_OPTION: {"enabled": True},
            MASK_COLUMNS_OPTION: "secret_phone",
        },
        schema=True,
    )

    notes = {
        "label": make_item("园区日度客流汇总表", "human"),
        "grain": make_item("每日期每区域一行", "human"),
        "business_date": make_item({"column": "stat_date", "rule": "按园区营业归属日统计（T+1 更新）"}, "human"),
        "valid_filter": make_item("status = 1", "human"),
        "columns": {
            "net_visitors": {"label": make_item("系统去重客流", "human"), "unit": make_item("人次", "human"), "measure": make_item("flow", "human")},
            "crowd_density": {"label": make_item("瞬时在园峰值密度", "human"), "measure": make_item("stock", "human")},
            "zone_code": {"label": make_item("区域编码", "human")},
            "secret_phone": {"label": make_item("敏感手机号绝密备注", "human")},
        },
    }
    async with SessionLocal() as session:
        await catalog.write_entry(session, sid, "park_traffic_daily", notes, if_version=0, actor="tester")
        ent = CanonicalEntity(
            key="park_test_zone",
            label="园区区域",
            description="星河园区标准商业区域实体",
            bindings=[
                {
                    "id": "b1",
                    "source_id": sid,
                    "source_name": src_name,
                    "table_name": "park_traffic_daily",
                    "column_name": "zone_code",
                    "status": "confirmed",
                    "confidence": 1.0,
                }
            ],
            members=[
                {
                    "id": "m1",
                    "value": "WEST_04",
                    "label": "西区四号楼",
                    "aliases": ["W4", "四号楼"],
                    "source_codes": {src_name: "Z_WEST_04"},
                },
                {
                    "id": "m2",
                    "value": "EAST_01",
                    "label": "东区一号楼",
                    "aliases": ["E1"],
                    "source_codes": {src_name: "Z_EAST_01"},
                },
            ],
        )
        session.add(ent)
        await session.commit()

    return {"id": sid, "name": src_name, "entity_key": "park_test_zone"}


async def test_semantic_metric_crud_and_sql_guardrails(client: AsyncClient, tmp_path: Path):
    fx = await _create_park_fixture(tmp_path)
    try:
        # 1. SQL 护栏校验：拦截分号多语句、嵌套子查询、不存在的物理列、以及敏感遮罩列
        for bad_expr, note in [
            ("SUM(net_visitors); DROP TABLE park_traffic_daily", "分号多语句"),
            ("(SELECT SUM(net_visitors) FROM park_traffic_daily)", "嵌套子查询"),
            ("SUM(non_existent_col)", "不存在列"),
            ("COUNT(secret_phone)", "敏感遮罩列"),
        ]:
            r = await client.post(
                "/api/semantic-metrics",
                json={
                    "key": "park_bad_metric",
                    "name": "非法指标",
                    "source_id": fx["id"],
                    "table_name": "park_traffic_daily",
                    "sql_expression": bad_expr,
                },
            )
            assert r.status_code == 422, f"{note} 未被拦截: {r.text}"

        # 2. 正常创建指标卡片，即存即生效
        r = await client.post(
            "/api/semantic-metrics",
            json={
                "key": "park_daily_visitors",
                "name": "星河园区日客流（系统去重）",
                "aliases": ["客流", "总客流"],
                "unit": "人次",
                "description": "按系统去重统计有效客流",
                "source_id": fx["id"],
                "table_name": "park_traffic_daily",
                "sql_expression": "SUM(net_visitors)",
                "date_column": "stat_date",
                "filter_sql": "status = 1",
                "dimensions": [
                    {"key": "zone", "label": "园区区域", "column": "zone_code", "entity_key": fx["entity_key"]}
                ],
            },
        )
        assert r.status_code == 201, r.text
        created = r.json()["metric"]
        metric_id = created["id"]
        assert created["version"] == 1 and created["aggregation_type"] == "sum"

        # 3. 列表查询与乐观锁版本冲突保护
        listed = (await client.get("/api/semantic-metrics", params={"q": "客流"})).json()
        assert listed["total"] == 1 and listed["items"][0]["key"] == "park_daily_visitors"

        upd = await client.put(
            f"/api/semantic-metrics/{metric_id}",
            json={"unit": "人", "expected_version": 1},
        )
        assert upd.status_code == 200 and upd.json()["metric"]["version"] == 2

        stale = await client.put(
            f"/api/semantic-metrics/{metric_id}",
            json={"unit": "人次", "expected_version": 1},
        )
        assert stale.status_code == 409, stale.text

        # 4. 删除
        deleted = await client.delete(f"/api/semantic-metrics/{metric_id}")
        assert deleted.status_code == 200 and deleted.json()["ok"] is True
        assert (await client.get(f"/api/semantic-metrics/{metric_id}")).status_code == 404
    finally:
        await drop_source(fx["id"])


async def test_metric_preview_entity_translation_and_latest_date_probe(client: AsyncClient, tmp_path: Path):
    fx = await _create_park_fixture(tmp_path)
    try:
        r = await client.post(
            "/api/semantic-metrics",
            json={
                "key": "park_visitors",
                "name": "星河园区客流",
                "aliases": ["客流量"],
                "unit": "人次",
                "source_id": fx["id"],
                "table_name": "park_traffic_daily",
                "sql_expression": "SUM(net_visitors)",
                "date_column": "stat_date",
                "filter_sql": "status = 1",
                "dimensions": [
                    {"key": "zone", "label": "园区区域", "column": "zone_code", "entity_key": fx["entity_key"]}
                ],
            },
        )
        assert r.status_code == 201, r.text
        metric_id = r.json()["metric"]["id"]

        # 1. 按标准实体中文别名「四号楼」筛选 + 按日趋势分组：自动翻译为底层编码 Z_WEST_04 并过滤掉 status=0 脏数据
        prev = await client.post(
            "/api/semantic-metrics/preview",
            json={
                "metric_id": metric_id,
                "date_from": "2026-11-09",
                "date_to": "2026-11-10",
                "group_by_date": True,
                "filters": {"zone": "四号楼"},
            },
        )
        assert prev.status_code == 200, prev.text
        body = prev.json()
        assert body["row_count"] == 2
        assert [row[1] for row in body["rows"]] == [1200, 1500]
        assert body["entity_alignments"] and body["entity_alignments"][0]["translated"] == ["Z_WEST_04"]

        # 2. T+1 场景：查尚未入库的日期 2026-11-12 时，一次返回 latest_available_date = '2026-11-10'
        empty_prev = await client.post(
            "/api/semantic-metrics/preview",
            json={
                "metric_id": metric_id,
                "date_from": "2026-11-12",
                "date_to": "2026-11-12",
            },
        )
        assert empty_prev.status_code == 200, empty_prev.text
        eb = empty_prev.json()
        assert eb["latest_available_date"] == "2026-11-10"
    finally:
        await drop_source(fx["id"])


async def test_ai_suggest_metrics_from_catalog_and_kb_doc(client: AsyncClient, monkeypatch, tmp_path: Path):
    fx = await _create_park_fixture(tmp_path)
    try:
        async with SessionLocal() as session:
            doc = Document(
                collection="default",
                title="指标公式总表",
                source="test_metric_formula.md",
                content="园区有效客流 = SUM(net_visitors)，仅统计 status=1 的记录。",
                status="ready",
            )
            session.add(doc)
            await session.commit()
            doc_id = doc.id

        class _FakeSuggestLLM:
            async def ainvoke(self, messages):
                prompt_text = str(messages[-1].content)
                assert "指标公式总表" in prompt_text
                assert "已绑定标准实体：park_test_zone" in prompt_text
                payload = {
                    "metrics": [
                        {
                            "key": "park_doc_visitors",
                            "name": "文档有效客流",
                            "aliases": ["有效客流"],
                            "unit": "人次",
                            "description": "依据指标公式总表提取",
                            "table_name": "park_traffic_daily",
                            "sql_expression": "SUM(net_visitors)",
                            "date_column": "stat_date",
                            "filter_sql": "status = 1",
                            "dimensions": [{"key": "zone", "label": "区域", "column": "zone_code", "entity_key": "park_test_zone"}],
                        },
                        {
                            "key": "park_hallucinated_col",
                            "name": "幻觉字段指标",
                            "table_name": "park_traffic_daily",
                            "sql_expression": "SUM(fake_not_in_table)",
                        },
                    ]
                }
                return AIMessage(content=json.dumps(payload, ensure_ascii=False))

        async def _fake_resolve(_session):
            return _FakeSuggestLLM(), "mock-suggest"

        monkeypatch.setattr(catalog_assistant, "resolve_model", _fake_resolve)

        sug = await client.post(
            "/api/semantic-metrics/suggest",
            json={"source_id": fx["id"], "document_ids": [doc_id], "note": "提取客流核心指标"},
        )
        assert sug.status_code == 200, sug.text
        sb = sug.json()
        assert len(sb["candidates"]) == 1 and sb["candidates"][0]["key"] == "park_doc_visitors"
        assert len(sb["skipped"]) == 1 and sb["skipped"][0]["key"] == "park_hallucinated_col"

        batch = await client.post("/api/semantic-metrics/batch", json={"items": sb["candidates"]})
        assert batch.status_code == 201 and len(batch.json()["created"]) == 1
    finally:
        await drop_source(fx["id"])


async def test_millisecond_deterministic_grounding_and_redaction(client: AsyncClient, tmp_path: Path):
    fx = await _create_park_fixture(tmp_path)
    try:
        await client.post(
            "/api/semantic-metrics",
            json={
                "key": "park_visitors_card",
                "name": "星河园区日客流",
                "aliases": ["客流"],
                "unit": "人次",
                "source_id": fx["id"],
                "table_name": "park_traffic_daily",
                "sql_expression": "SUM(net_visitors)",
                "date_column": "stat_date",
                "filter_sql": "status = 1",
                "dimensions": [{"key": "zone", "label": "园区区域", "column": "zone_code", "entity_key": fx["entity_key"]}],
            },
        )

        async with SessionLocal() as session:
            t0 = time.perf_counter()
            bundle = await ground_question(
                session,
                f"{fx['name']} 西区四号楼今天客流咋样？",
                datasource_ids=None,
                restricted=False,
            )
            elapsed_ms = (time.perf_counter() - t0) * 1000

            assert elapsed_ms < 250, f"确定性预命中耗时过长: {elapsed_ms:.1f}ms"
            assert any(m["key"] == "park_visitors_card" for m in bundle.ui_summary["metrics"])
            ent_hits = [e for e in bundle.ui_summary["entities"] if e["entity_key"] == fx["entity_key"]]
            assert ent_hits and ent_hits[0]["matched_member"]["target_codes"][fx["name"]] == ["Z_WEST_04"]
            guardrail_kinds = {g["kind"] for g in bundle.ui_summary["guardrails"]}
            assert {"business_date", "valid_filter", "stock_columns"} <= guardrail_kinds
            assert "crowd_density" in bundle.system_context
            assert "已遮罩" in bundle.system_context and "敏感手机号绝密备注" not in bundle.system_context

            # restricted=True（普通使用者视图）不泄露底层 SQL 表达式与物理编码
            restricted_bundle = await ground_question(
                session,
                f"{fx['name']} 西区四号楼今天客流咋样？",
                datasource_ids=None,
                restricted=True,
            )
            assert "sql_expression" not in restricted_bundle.ui_summary["metrics"][0]
            assert "target_codes" not in (restricted_bundle.ui_summary["entities"][0]["matched_member"] or {})
    finally:
        await drop_source(fx["id"])


async def test_fast_direct_ask_turn_with_metric_query_and_switch_to_deep(
    client: AsyncClient, monkeypatch, tmp_path: Path,
):
    fx = await _create_park_fixture(tmp_path)
    try:
        assert is_platform_query(METRIC_QUERY) is True

        await client.post(
            "/api/semantic-metrics",
            json={
                "key": "park_visitors_fast",
                "name": "星河园区客流",
                "aliases": ["客流"],
                "unit": "人次",
                "source_id": fx["id"],
                "table_name": "park_traffic_daily",
                "sql_expression": "SUM(net_visitors)",
                "date_column": "stat_date",
                "filter_sql": "status = 1",
                "dimensions": [{"key": "zone", "label": "园区区域", "column": "zone_code", "entity_key": fx["entity_key"]}],
            },
        )

        # 脚本化直查 Agent：第 1 步看到语义预命中注入的 park_visitors_fast，调用 metric_query；第 2 步直接输出结论
        def _fast_decide(self, messages):
            if any(isinstance(m, ToolMessage) for m in messages):
                tool_msgs = [m for m in messages if isinstance(m, ToolMessage)]
                raw_tool = str(tool_msgs[-1].content)
                assert "【证据" in raw_tool
                payload = json.loads(raw_tool[raw_tool.index("{") :])
                val = payload["rows"][0][0]
                return AIMessage(content=f"西区四号楼 2026-11-10 有效客流为 {val} 人次。")
            sys_prompt = str(messages[0].content)
            assert "park_visitors_fast" in sys_prompt and "Z_WEST_04" in sys_prompt
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": METRIC_QUERY,
                        "args": {
                            "metric_key": "park_visitors_fast",
                            "date_from": "2026-11-10",
                            "date_to": "2026-11-10",
                            "filters": {"zone": "西区四号楼"},
                        },
                        "id": "tc_metric_1",
                    }
                ],
            )

        monkeypatch.setattr(mock_model.MockChatModel, "_decide", _fast_decide)

        conv_res = await client.post("/api/conversations", json={"kind": "chat", "title": "星河园区快速直查测试"})
        assert conv_res.status_code == 201
        conv_id = conv_res.json()["id"]

        # 1. 发起 depth="fast" 快速直查轮次
        start_res = await client.post(
            "/api/chat/turns",
            json={
                "conversation_id": conv_id,
                "question": f"{fx['name']} 西区四号楼 2026-11-10 客流咋样？",
                "datasource_ids": [fx["id"]],
                "depth": "fast",
            },
        )
        assert start_res.status_code == 202, start_res.text
        turn_id = start_res.json()["turn_id"]

        await chat_job.manager.wait(turn_id, timeout=30.0)
        async with SessionLocal() as session:
            job = await session.get(ChatTurnJob, turn_id)
            turn_row = await session.get(ConversationTurn, turn_id)
            run_row = await session.get(Run, job.run_id)
            snaps = list((await session.execute(select(Artifact).where(Artifact.run_id == job.run_id))).scalars().all())
            run_events = list(
                (await session.execute(select(RunEvent).where(RunEvent.run_id == job.run_id).order_by(RunEvent.seq)))
                .scalars()
                .all()
            )

        assert job is not None and job.status == "succeeded"
        assert turn_row is not None and turn_row.status == "done"
        assert "1,500" in (turn_row.answer or "") or "1500" in (turn_row.answer or ""), turn_row.answer
        assert turn_row.meta["depth"] == "fast"
        assert turn_row.meta["queries"] == 1
        assert turn_row.meta.get("evidence") is True, "快速直查数字自动绑定为可点击溯源证据"
        assert turn_row.meta["grounding"]["metrics"][0]["key"] == "park_visitors_fast"
        assert turn_row.review == {}, "快速直查模式跳过冗余的复核与报告重写"
        assert [n["id"] for n in turn_row.graph["nodes"]] == ["q", "agent1", "report1", "out1"]
        assert run_row is not None and run_row.status == "succeeded"
        assert run_row.output["_evidence"]["report_node"] == "report1"
        assert run_row.output["_evidence"]["fields"] == ["answer"]
        checked_evts = [e for e in run_events if e.type == "report.checked"]
        assert len(checked_evts) == 1 and checked_evts[0].data["ok"] is True
        assert [e for e in run_events if e.type == "llm.start" and e.node_id == "report1"] == [], (
            "快速直查证据封装绝不发起额外 LLM 调用"
        )
        assert any(e.data.get("code") == "report_answer_reused" for e in run_events)
        q_snaps = [s for s in snaps if s.kind == "query_snapshot"]
        assert q_snaps and await snapshot_owned(q_snaps[0].id, job.run_id, METRIC_QUERY) is True

        ev_res = await client.get(f"/api/runs/{job.run_id}/evidence")
        assert ev_res.status_code == 200
        ev_payload = ev_res.json()
        assert ev_payload["mode"] == "cited", ev_payload
        assert len(ev_payload["reports"]) == 1

        verify_res = await client.get(f"/api/runs/{job.run_id}/verify")
        assert verify_res.status_code == 200
        assert verify_res.json()["ok"] is True, verify_res.json()

        events = await turn_log.read(turn_id, 0, limit=500)
        stages = [e["data"]["stage"] for e in events if e["type"] == "stage"]
        assert stages == ["ground", "run"], stages
        grounding_ops = [e["data"] for e in events if e["type"] == "op" and e["data"].get("op") == "grounding"]
        assert len(grounding_ops) == 1

        # 2. 单轮一键「切换深度分析」（retry_of + depth="deep"）：归档快速直查结果，重建完整工作流 + 报告 + 复核
        class _DeepCopilot:
            async def astream(self, messages):
                msg_text = str(messages[1][1])
                if "上线前自查" in msg_text:
                    yield es._line({"op": "done"})
                    return
                sql = "SELECT SUM(net_visitors) AS gmv FROM park_traffic_daily WHERE stat_date = '2026-11-10' AND zone_code = 'Z_WEST_04' AND status = 1"
                for op in es.graph_ops(fx["name"], sql):
                    yield es._line(op)

        es.use_copilot(monkeypatch, _DeepCopilot())
        es.use_report(monkeypatch, "深度分析报告：西区四号楼有效客流为 [[v:Q1.r0.gmv]] 人次。")

        upgrade_res = await client.post(
            "/api/chat/turns",
            json={
                "conversation_id": conv_id,
                "retry_of": turn_id,
                "mode": "retry",
                "depth": "deep",
            },
        )
        assert upgrade_res.status_code == 202, upgrade_res.text
        await chat_job.manager.wait(turn_id, timeout=30.0)

        async with SessionLocal() as session:
            upgraded = await session.get(ConversationTurn, turn_id)

        assert upgraded is not None and upgraded.status == "done"
        assert "深度分析报告" in (upgraded.answer or "")
        assert "1,500" in (upgraded.answer or "") or "1500" in (upgraded.answer or "")
        assert upgraded.meta["depth"] == "deep"
        assert len(upgraded.meta.get("attempts") or []) == 1, "切换深度分析后，原快速直查答案完整归档在 attempts 中"
        prev_ans = upgraded.meta["attempts"][0].get("answer") or ""
        assert "1,500" in prev_ans or "1500" in prev_ans
        assert [n["id"] for n in upgraded.graph["nodes"]] != ["q", "agent1", "report1", "out1"], (
            "切换深度分析后重新构建了完整工作流（而非沿用直查图）"
        )
    finally:
        await drop_source(fx["id"])


async def test_fast_direct_ask_clickable_evidence_shorthand_autolink_and_mask_guard(
    client: AsyncClient, monkeypatch, tmp_path: Path,
):
    """验证快速直查在零额外 LLM 调用下，支持显式标记归一、裸数字按行键消歧自动回绑、表名链接与遮罩列防护。"""
    fx = await _create_park_fixture(tmp_path)
    try:
        sql_tool_name = f"db_query__{fx['name']}"

        def _decide_with_mixed_citations(self, messages):
            if any(isinstance(m, ToolMessage) for m in messages):
                # 混合输出：
                # 1) 显式无 r 前缀 + 大写列名：[[v:Q1.0.NET_VISITORS]]（对应 2026-11-09 的 1,200）
                # 2) 裸数字 + 同行日期行键消歧：2026-11-10 的峰值密度 92、客流 1500
                # 3) 试图引用遮罩列：[[v:Q1.r0.secret_phone]]（必须拦截为「已遮罩」）
                # 4) 坏引用标记与句中 [[see:Q1]]：[[v:Q99.r0.ghost]] 不得向前端泄露 ⟦?v:...⟧
                # 5) 反引号物理表名：`park_traffic_daily` 自动转为实体链接
                return AIMessage(
                    content=(
                        "根据 [[see:Q1]] 查询 `park_traffic_daily` 结果：\n"
                        "- 2026-11-09 西区四号楼客流为 [[v:Q1.0.NET_VISITORS]] 人次（联系电话 [[v:Q1.r0.secret_phone]]）；\n"
                        "- 2026-11-10 西区四号楼客流为 1500 人次，峰值密度 92；\n"
                        "- 异常占位核验：[[v:Q99.r0.ghost]]。"
                    )
                )
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": sql_tool_name,
                        "args": {
                            "sql": (
                                "SELECT stat_date, zone_code, net_visitors, crowd_density, secret_phone "
                                "FROM park_traffic_daily WHERE zone_code = 'Z_WEST_04' AND status = 1 "
                                "ORDER BY stat_date ASC"
                            )
                        },
                        "id": "tc_sql_mixed",
                    }
                ],
            )

        monkeypatch.setattr(mock_model.MockChatModel, "_decide", _decide_with_mixed_citations)

        conv_res = await client.post("/api/conversations", json={"kind": "chat", "title": "快速直查溯源边界测试"})
        conv_id = conv_res.json()["id"]

        start_res = await client.post(
            "/api/chat/turns",
            json={
                "conversation_id": conv_id,
                "question": f"{fx['name']} 西区四号楼近两天客流与密度明细",
                "datasource_ids": [fx["id"]],
                "depth": "fast",
            },
        )
        assert start_res.status_code == 202
        turn_id = start_res.json()["turn_id"]
        await chat_job.manager.wait(turn_id, timeout=30.0)

        async with SessionLocal() as session:
            job = await session.get(ChatTurnJob, turn_id)
            turn_row = await session.get(ConversationTurn, turn_id)
            run_row = await session.get(Run, job.run_id)

        assert turn_row is not None and turn_row.status == "done"
        ans = turn_row.answer or ""
        assert "[[v:" not in ans and "⟦?" not in ans, f"不应泄露原始标记或未解析占位符: {ans}"
        assert "13800001111" not in ans and "13800003333" not in ans, "遮罩列严禁泄露真实手机号"
        assert "已遮罩" in ans
        assert "1,200" in ans and "1,500" in ans and "92" in ans

        doc_art_id = run_row.output["_evidence"]["doc_artifact"]
        doc_res = await client.get(f"/api/artifacts/{doc_art_id}")
        assert doc_res.status_code == 200
        doc = doc_res.json()["content"]
        assert doc["stats"]["numbers_cited"] >= 3, doc["stats"]
        all_segs = list(iter_segments(doc))
        det_cols = {
            ((s.get("cite") or {}).get("locator", {}).get("row"), (s.get("cite") or {}).get("locator", {}).get("column"))
            for s in all_segs
            if s.get("kind") == "number" and s.get("state") == "deterministic"
        }
        # r0.net_visitors (1200) 来自显式简写归一；r1.net_visitors (1500) 与 r1.crowd_density (92) 来自裸数字自动回绑
        assert {(0, "net_visitors"), (1, "net_visitors"), (1, "crowd_density")} <= det_cols
        assert any(s.get("kind") == "entity" and "park_traffic_daily" in (s.get("text") or "") for s in all_segs)

        verify_res = await client.get(f"/api/runs/{job.run_id}/verify")
        assert verify_res.status_code == 200 and verify_res.json()["ok"] is True
    finally:
        await drop_source(fx["id"])


def test_direct_evidence_unit_markdown_table_pct_wan_and_shorthand():
    """纯内存单元测试：单行无 r0 简写、百分比/万单位换算、手写 Markdown 表头按列消歧与 0 行数量词保护。"""
    from app.chat.direct_evidence import normalize_and_bind_fast_citations

    snaps = {
        "art_q1": {
            "columns": ["zone_name", "gmv", "target_gmv", "conv_rate", "zero_diff"],
            "column_types": ["TEXT", "NUMERIC", "NUMERIC", "REAL", "INTEGER"],
            "rows": [
                ["西区", 125000, 125000, 0.126, 0],
            ],
            "mask_columns": [],
        }
    }
    catalog_map = {"Q1": {"kind": "query", "artifact": "art_q1", "eid": "ev:query:art_q1"}}
    raw = (
        "核心摘要：西区转化率达 12.6%，总成交 [[v:Q1.gmv|万]] 元（共返回 0 行异常记录，对比两天均值）。\n\n"
        "| zone_name | gmv | target_gmv |\n"
        "| --- | --- | --- |\n"
        "| 西区 | 125000 | 125000 |\n"
    )
    bound = normalize_and_bind_fast_citations(raw, catalog_map, loader=lambda aid: snaps.get(aid))
    # 1) 单行省略 .r0. 的 [[v:Q1.gmv|万]] 自动修复为 [[v:Q1.r0.gmv|万]]
    assert "[[v:Q1.r0.gmv|万]]" in bound
    # 2) 12.6% 自动识别小数比率列 conv_rate 并加 |pct
    assert "[[v:Q1.r0.conv_rate|pct]]" in bound
    # 3) 「0 行」不被误绑到 zero_diff=0，「两天」中文数字词不被误绑
    assert "0 行异常记录" in bound and "两天" in bound
    # 4) Markdown 表格中 gmv 与 target_gmv 同为 125000，按表头列名各自精准绑定到对应列
    assert "| 西区 | [[v:Q1.r0.gmv]] | [[v:Q1.r0.target_gmv]] |" in bound

    # 5) 防止模型误将 [[v:...]] 当作脚注追加在裸数字后或句末造成二次重复渲染
    snaps2 = {
        "art_q2": {
            "columns": ["net_visitors", "north_gate_visitors"],
            "column_types": ["INTEGER", "INTEGER"],
            "rows": [[12340, 8560]],
            "mask_columns": [],
        }
    }
    cat2 = {"Q1": {"kind": "query", "artifact": "art_q2", "eid": "ev:query:art_q2"}}
    dup_raw = (
        "当日总客流 12,340 人次，北门街区 8,560 人次。[[v:Q1.r0.net_visitors]] [[v:Q1.r0.north_gate_visitors]]\n"
        "紧邻重复测试：12,340 [[v:Q1.r0.net_visitors]] 与 8,560（[[v:Q1.r0.north_gate_visitors]]）。"
    )
    deduped = normalize_and_bind_fast_citations(dup_raw, cat2, loader=lambda aid: snaps2.get(aid))
    assert deduped == (
        "当日总客流 [[v:Q1.r0.net_visitors]] 人次，北门街区 [[v:Q1.r0.north_gate_visitors]] 人次。\n"
        "紧邻重复测试：[[v:Q1.r0.net_visitors]] 与 [[v:Q1.r0.north_gate_visitors]]。"
    )
