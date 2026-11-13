"""多表 / 参数化 SQL 模板语义指标（calc_mode="sql_template"）与后台口径文档一键拆解工作流（SemanticDecomposeJob）端到端测试。"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import uuid
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from langchain_core.messages import AIMessage
from sqlalchemy import delete, select

from app.chat import job as chat_job
from app.chat.grounding import ground_question
from app.data import catalog, catalog_assistant, semantic_decompose
from app.data.engine import CATALOG_PROFILE_OPTION, MASK_COLUMNS_OPTION
from app.data.semantic_metrics import get_metric, metric_to_dict
from app.db.base import SessionLocal
from app.db.models import ConversationTurn, Document, Run
from app.db.models_chat import ChatTurnJob
from app.db.models_metrics import SemanticDecomposeJob, SemanticMetric
from app.db.models_ontology import CanonicalEntity
from app.engine.evidence import iter_segments
from app.engine.runner import run_manager
from app.main import app
from app.providers import mock_model
from app.tools.metrics import METRIC_QUERY
from tests.fixtures.sources import drop_source, sqlite_source


@pytest.fixture(autouse=True)
async def _lifecycle():
    await run_manager.setup()
    yield
    await semantic_decompose.shutdown()
    await chat_job.shutdown()
    await run_manager.shutdown()
    async with SessionLocal() as session:
        await session.execute(delete(SemanticDecomposeJob))
        await session.execute(delete(SemanticMetric))
        await session.execute(delete(CanonicalEntity).where(CanonicalEntity.key.like("mes_test_%")))
        await session.execute(delete(Document).where(Document.title.like("%OEE%")))
        await session.commit()


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def _create_mes_fixture(tmp_path: Path) -> dict[str, str]:
    """构造还原工业 OEE / TEEP 跨表场景的 SQLite 测试库（盲列 EXT_VAL_01..34 + 不合格记录表 + 折算系数表 + 产线节拍表 + 遮罩列）。"""
    db_path = tmp_path / f"mes_oee_{uuid.uuid4().hex[:6]}.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(
            """
            CREATE TABLE mes_shift_daily (
                id INTEGER PRIMARY KEY,
                stat_date TEXT NOT NULL,
                line_code TEXT NOT NULL,
                prod_code TEXT NOT NULL,
                EXT_VAL_01 REAL NOT NULL,   -- 计划开班时长（秒）
                EXT_VAL_02 REAL NOT NULL,   -- 计划内停机时长（秒）
                EXT_VAL_03 REAL NOT NULL,   -- 故障与换模损失时长（秒）
                EXT_VAL_10 REAL NOT NULL,   -- 合格入库数（件）
                EXT_VAL_34 REAL NOT NULL,   -- 良品率（底表直接存百分数，如 98.5）
                secret_phone TEXT,
                is_valid INTEGER NOT NULL DEFAULT 1
            );

            CREATE TABLE fact_scrap_record (
                id INTEGER PRIMARY KEY,
                stat_date TEXT NOT NULL,
                line_code TEXT NOT NULL,
                scrap_qty REAL NOT NULL
            );

            CREATE TABLE dim_carrier_factor (
                id INTEGER PRIMARY KEY,
                prod_code TEXT NOT NULL,
                conv_factor REAL NOT NULL
            );

            CREATE TABLE dim_prod_line (
                line_code TEXT PRIMARY KEY,
                line_name TEXT NOT NULL,
                beat REAL NOT NULL           -- 标准节拍（秒/件）
            );

            INSERT INTO dim_prod_line (line_code, line_name, beat) VALUES
                ('L1', '一号冲压产线', 10.0),
                ('L2', '二号焊接产线', 20.0);

            INSERT INTO dim_carrier_factor (prod_code, conv_factor) VALUES
                ('P100', 1.2);
            -- P200 故意不在折算系数表中，验证 COALESCE(cf.conv_factor, 0.5) 兜底系数

            INSERT INTO mes_shift_daily (
                stat_date, line_code, prod_code,
                EXT_VAL_01, EXT_VAL_02, EXT_VAL_03, EXT_VAL_10, EXT_VAL_34,
                secret_phone, is_valid
            ) VALUES
                ('2026-03-01', 'L1', 'P100', 28800, 3600, 1200, 1800, 96.0, '13900001111', 1),
                ('2026-03-01', 'L2', 'P200', 28800, 1800, 2000, 2000, 95.0, '13900002222', 1),
                ('2026-03-02', 'L1', 'P100', 28800, 3600, 600,  2000, 98.0, '13900003333', 1),
                ('2026-03-02', 'L1', 'P100', 99999, 0,    0,    9999, 100.0,'13900004444', 0);

            INSERT INTO fact_scrap_record (stat_date, line_code, scrap_qty) VALUES
                ('2026-03-01', 'L1', 200),
                ('2026-03-01', 'L2', 100),
                ('2026-03-02', 'L1', 50);
            """
        )
        conn.commit()
    finally:
        conn.close()

    src_name = f"mes_prod_{uuid.uuid4().hex[:6]}"
    sid = await sqlite_source(
        src_name,
        db_path,
        options={
            CATALOG_PROFILE_OPTION: {"enabled": True},
            MASK_COLUMNS_OPTION: "secret_phone",
        },
        schema=True,
    )
    ent_key = f"mes_test_line_{uuid.uuid4().hex[:4]}"
    async with SessionLocal() as session:
        session.add(
            CanonicalEntity(
                key=ent_key,
                label="制造产线",
                aliases=["产线", "生产线"],
                members=[
                    {
                        "id": "m_l1",
                        "value": "LINE_01",
                        "label": "一号冲压产线",
                        "aliases": ["一号产线", "1线"],
                        "source_codes": {src_name: "L1"},
                    },
                    {
                        "id": "m_l2",
                        "value": "LINE_02",
                        "label": "二号焊接产线",
                        "aliases": ["二号产线", "2线"],
                        "source_codes": {src_name: "L2"},
                    },
                ],
                bindings=[
                    {
                        "id": "b_line",
                        "source_id": sid,
                        "source_name": src_name,
                        "table_name": "mes_shift_daily",
                        "column_name": "line_code",
                        "status": "confirmed",
                        "source": "human",
                    }
                ],
                version=1,
            )
        )
        await session.commit()

    return {"source_id": sid, "source_name": src_name, "entity_key": ent_key}


@pytest.mark.asyncio
async def test_multi_table_sql_template_metric_crud_preview_and_guardrails(client: AsyncClient, tmp_path: Path):
    """验证多表 JOIN + CTE + COALESCE + {{period_days}} 参数化模板指标的创建、安全拦截、实体转码试算与 restricted 脱敏。"""
    fx = await _create_mes_fixture(tmp_path)
    sid = fx["source_id"]
    ent_key = fx["entity_key"]
    try:
        # 1) 安全红线拦截：写操作、未知占位符、不存在物理表、敏感遮罩列均必须 422 拒绝
        for bad_tpl in (
            "DELETE FROM mes_shift_daily",
            "SELECT {{evil_slot}} SUM(s.EXT_VAL_10) FROM mes_shift_daily s",
            "SELECT SUM(s.EXT_VAL_10) FROM mes_nonexistent_tbl s",
            "SELECT COUNT(s.secret_phone) FROM mes_shift_daily s",
        ):
            r_bad = await client.post(
                "/api/semantic-metrics",
                json={
                    "key": "bad_tpl_metric",
                    "name": "非法模板测试",
                    "source_id": sid,
                    "calc_mode": "sql_template",
                    "sql_template": bad_tpl,
                },
            )
            assert r_bad.status_code == 422, f"未拦截非法模板: {bad_tpl} -> {r_bad.text}"

        # 2) 创建多表 OEE 精算指标（CTE + 多表 LEFT JOIN + COALESCE 0.5 模具兜底系数 + 参数化槽位）
        oee_tpl = """
        WITH cf AS (
            SELECT prod_code, AVG(conv_factor) AS conv_factor
            FROM dim_carrier_factor
            GROUP BY prod_code
        ),
        sc AS (
            SELECT stat_date, line_code, SUM(scrap_qty) AS bad_qty
            FROM fact_scrap_record
            GROUP BY stat_date, line_code
        )
        SELECT
            {{select_dims}}
            ROUND(
                SUM((s.EXT_VAL_10 + COALESCE(sc.bad_qty, 0)) * pl.beat * COALESCE(cf.conv_factor, 0.5))
                * 100.0
                / NULLIF(SUM(s.EXT_VAL_01 - s.EXT_VAL_02), 0)
                * (SUM(s.EXT_VAL_10) * 1.0 / NULLIF(SUM(s.EXT_VAL_10 + COALESCE(sc.bad_qty, 0)), 0)),
                2
            ) AS mes_oee_multi
        FROM mes_shift_daily s
        JOIN dim_prod_line pl ON pl.line_code = s.line_code
        LEFT JOIN cf ON cf.prod_code = s.prod_code
        LEFT JOIN sc ON sc.stat_date = s.stat_date AND sc.line_code = s.line_code
        WHERE 1=1 {{and_where}}
        {{group_by}}
        {{order_by}}
        """.strip()

        r_create = await client.post(
            "/api/semantic-metrics",
            json={
                "key": "mes_oee_multi",
                "name": "设备综合效率 OEE（跨表精算）",
                "aliases": ["OEE", "综合效率"],
                "unit": "%",
                "source_id": sid,
                "calc_mode": "sql_template",
                "sql_template": oee_tpl,
                "date_column": "s.stat_date",
                "filter_sql": "s.is_valid = 1",
                "dimensions": [
                    {"key": "line_code", "label": "产线", "column": "s.line_code", "entity_key": ent_key},
                ],
            },
        )
        assert r_create.status_code == 201, r_create.text
        created_metric = r_create.json()["metric"]
        assert created_metric["calc_mode"] == "sql_template"
        assert created_metric["table_name"] == "mes_shift_daily"  # 自动从模板首张物理表识别
        assert "[多表模板]" in created_metric["sql_expression"]

        # restricted=True 下必须隐藏底层 sql_template
        async with SessionLocal() as session:
            row = await get_metric(session, created_metric["id"])
            restricted_dict = metric_to_dict(row, restricted=True)
            assert "sql_template" not in restricted_dict
            assert "table_name" not in restricted_dict

        # 3) 在线试算：按产线分组 + 实体中文别名过滤（「一号产线」-> L1，且过滤掉 is_valid=0 脏数据）
        r_prev_l1 = await client.post(
            "/api/semantic-metrics/preview",
            json={
                "metric_id": created_metric["id"],
                "date_from": "2026-03-01",
                "date_to": "2026-03-01",
                "filters": {"line_code": "一号产线"},
            },
        )
        assert r_prev_l1.status_code == 200, r_prev_l1.text
        prev_l1 = r_prev_l1.json()
        assert prev_l1["entity_alignments"][0]["resolved_code"] == "L1"
        # L1 2026-03-01: (1800+200)*10*1.2 / (28800-3600) * (1800/2000) * 100 = 24000 / 25200 * 0.9 * 100 = 85.71
        assert prev_l1["rows"] == [[85.71]]

        # L2 2026-03-01 走 COALESCE(cf.conv_factor, 0.5) 兜底：(2000+100)*20*0.5 / (28800-1800) * (2000/2100) * 100 = 20000 / 27000 * 100 = 74.07
        r_prev_group = await client.post(
            "/api/semantic-metrics/preview",
            json={
                "metric_id": created_metric["id"],
                "date_from": "2026-03-01",
                "date_to": "2026-03-01",
                "group_by": ["line_code"],
            },
        )
        assert r_prev_group.status_code == 200, r_prev_group.text
        grouped_rows = {r[0]: r[1] for r in r_prev_group.json()["rows"]}
        assert grouped_rows == {"L1": 85.71, "L2": 74.07}

        # 4) 验证 {{period_days}} 动态自然周期天数模板（TEEP 日历秒数 = {{period_days}} * 86400 * 产线条数）
        teep_tpl = """
        SELECT
            {{select_dims}}
            ROUND(
                SUM(s.EXT_VAL_01 - s.EXT_VAL_02 - s.EXT_VAL_03) * 100.0
                / NULLIF(({{period_days}}) * 86400.0 * COUNT(DISTINCT s.line_code), 0),
                2
            ) AS mes_teep
        FROM mes_shift_daily s
        WHERE 1=1 {{and_where}}
        {{group_by}}
        {{order_by}}
        """.strip()

        r_teep = await client.post(
            "/api/semantic-metrics",
            json={
                "key": "mes_teep",
                "name": "全员有效生产率 TEEP",
                "unit": "%",
                "source_id": sid,
                "calc_mode": "sql_template",
                "sql_template": teep_tpl,
                "date_column": "s.stat_date",
                "filter_sql": "s.is_valid = 1",
                "dimensions": [{"key": "line_code", "label": "产线", "column": "s.line_code"}],
            },
        )
        assert r_teep.status_code == 201, r_teep.text
        teep_id = r_teep.json()["metric"]["id"]

        # 7 天区间（2026-03-01 ~ 2026-03-07）：{{period_days}} 必须确定性编译为 7
        r_teep_7d = await client.post(
            "/api/semantic-metrics/preview",
            json={
                "metric_id": teep_id,
                "date_from": "2026-03-01",
                "date_to": "2026-03-07",
            },
        )
        assert r_teep_7d.status_code == 200, r_teep_7d.text
        assert "(7) * 86400.0" in r_teep_7d.json()["sql"]

        # 按日趋势（group_by_date=True）：单日每行 {{period_days}} 必须确定性编译为 1
        r_teep_daily = await client.post(
            "/api/semantic-metrics/preview",
            json={
                "metric_id": teep_id,
                "date_from": "2026-03-01",
                "date_to": "2026-03-02",
                "group_by_date": True,
            },
        )
        assert r_teep_daily.status_code == 200, r_teep_daily.text
        assert "(1) * 86400.0" in r_teep_daily.json()["sql"]
        assert len(r_teep_daily.json()["rows"]) == 2
    finally:
        await drop_source(sid)


@pytest.mark.asyncio
async def test_fast_direct_ask_with_sql_template_metric_produces_clickable_evidence(
    client: AsyncClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """验证快速直查（depth='fast'）命中多表 sql_template 指标后，经 metric_query 取数并生成可点击数字证据。"""
    fx = await _create_mes_fixture(tmp_path)
    sid = fx["source_id"]
    ent_key = fx["entity_key"]
    try:
        oee_tpl = (
            "WITH cf AS (SELECT prod_code, AVG(conv_factor) AS conv_factor FROM dim_carrier_factor GROUP BY prod_code), "
            "sc AS (SELECT stat_date, line_code, SUM(scrap_qty) AS bad_qty FROM fact_scrap_record GROUP BY stat_date, line_code) "
            "SELECT {{select_dims}} ROUND(SUM((s.EXT_VAL_10 + COALESCE(sc.bad_qty, 0)) * pl.beat * COALESCE(cf.conv_factor, 0.5)) * 100.0 "
            "/ NULLIF(SUM(s.EXT_VAL_01 - s.EXT_VAL_02), 0) "
            "* (SUM(s.EXT_VAL_10) * 1.0 / NULLIF(SUM(s.EXT_VAL_10 + COALESCE(sc.bad_qty, 0)), 0)), 2) AS mes_oee_fast "
            "FROM mes_shift_daily s "
            "JOIN dim_prod_line pl ON pl.line_code = s.line_code "
            "LEFT JOIN cf ON cf.prod_code = s.prod_code "
            "LEFT JOIN sc ON sc.stat_date = s.stat_date AND sc.line_code = s.line_code "
            "WHERE 1=1 {{and_where}} {{group_by}} {{order_by}}"
        )
        r_create = await client.post(
            "/api/semantic-metrics",
            json={
                "key": "mes_oee_fast",
                "name": "产线跨表OEE",
                "aliases": ["跨表OEE"],
                "unit": "%",
                "source_id": sid,
                "calc_mode": "sql_template",
                "sql_template": oee_tpl,
                "date_column": "s.stat_date",
                "filter_sql": "s.is_valid = 1",
                "dimensions": [{"key": "line_code", "label": "产线", "column": "s.line_code", "entity_key": ent_key}],
            },
        )
        assert r_create.status_code == 201

        # 验证语义预命中包含 calc_mode="sql_template" 提示
        async with SessionLocal() as session:
            bundle = await ground_question(session, question="查询 2026-03-01 一号产线的跨表OEE")
            assert any(m["key"] == "mes_oee_fast" and m["calc_mode"] == "sql_template" for m in bundle.matched_metrics)
            assert "多表参数化 SQL 模板" in bundle.system_context

        def _decide_oee(self: Any, messages: list[Any]) -> AIMessage:
            has_tool_res = any(getattr(m, "type", "") == "tool" for m in messages)
            if not has_tool_res:
                return AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": "call_oee_tpl",
                            "name": METRIC_QUERY,
                            "args": {
                                "metric_key": "mes_oee_fast",
                                "date_from": "2026-03-01",
                                "date_to": "2026-03-01",
                                "filters": {"line_code": "一号产线"},
                            },
                        }
                    ],
                )
            return AIMessage(
                content="2026-03-01 一号冲压产线的跨表精算 OEE 为 **[[v:Q1.r0.mes_oee_fast]]**%。"
            )

        monkeypatch.setattr(mock_model.MockChatModel, "_decide", _decide_oee)

        conv_res = await client.post("/api/conversations", json={"kind": "chat", "title": "跨表OEE直查测试"})
        conv_id = conv_res.json()["id"]

        r_turn = await client.post(
            "/api/chat/turns",
            json={
                "conversation_id": conv_id,
                "question": "查询 2026-03-01 一号产线的跨表OEE",
                "datasource_ids": [sid],
                "depth": "fast",
            },
        )
        assert r_turn.status_code == 202, r_turn.text
        turn_id = r_turn.json()["turn_id"]
        await chat_job.manager.wait(turn_id, timeout=30.0)

        async with SessionLocal() as session:
            job = await session.get(ChatTurnJob, turn_id)
            turn_row = await session.get(ConversationTurn, turn_id)
            run_row = await session.get(Run, job.run_id)

        assert turn_row is not None and turn_row.status == "done"
        assert "85.71" in (turn_row.answer or "")
        assert "[[v:" not in (turn_row.answer or "")

        doc_art_id = run_row.output["_evidence"]["doc_artifact"]
        doc = (await client.get(f"/api/artifacts/{doc_art_id}")).json()["content"]
        assert doc["stats"]["numbers_cited"] >= 1
        segs = list(iter_segments(doc))
        assert any(s.get("kind") == "number" and "85.71" in str(s.get("text") or "") for s in segs)

        verify_res = await client.get(f"/api/runs/{job.run_id}/verify")
        assert verify_res.status_code == 200 and verify_res.json()["ok"] is True
    finally:
        await drop_source(sid)


@pytest.mark.asyncio
async def test_background_document_decompose_workflow_and_one_click_apply(
    client: AsyncClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """验证业务口径文档一键拆解后台任务（阶段进度 + 字段符号链式注入 + 知识库归档 + 一键应用四层资产）。"""
    fx = await _create_mes_fixture(tmp_path)
    sid = fx["source_id"]
    src_name = fx["source_name"]

    captured_prompts: list[tuple[str, str]] = []

    class _FakeDecomposeLLM:
        async def ainvoke(self, msgs: list[Any]) -> AIMessage:
            sys_txt = str(msgs[0].content)
            usr_txt = str(msgs[1].content)
            captured_prompts.append((sys_txt, usr_txt))

            if "数据目录与字段字典拆解专家" in sys_txt:
                payload = {
                    "tables": [
                        {
                            "table": "mes_shift_daily",
                            "changes": [
                                {"path": "label", "value": "产线班次日度生产宽表", "reason": "文档总表说明"},
                                {
                                    "path": "business_date",
                                    "value": {"column": "stat_date", "rule": "按班次归属自然日"},
                                    "reason": "文档第 0 节",
                                },
                                {"path": "valid_filter", "value": "is_valid = 1", "reason": "仅统计有效班次"},
                                {"path": "columns.ext_val_01.label", "value": "计划开班时长", "reason": "文档符号 T_open"},
                                {"path": "columns.ext_val_01.unit", "value": "秒", "reason": "文档符号 T_open"},
                                {"path": "columns.ext_val_01.measure", "value": "flow", "reason": "时长可累加"},
                                {"path": "columns.EXT_VAL_34.label", "value": "班次良品率", "reason": "文档符号 Q_rate"},
                                {"path": "columns.EXT_VAL_34.unit", "value": "%", "reason": "百分数"},
                                {"path": "columns.EXT_VAL_34.measure", "value": "stock", "reason": "比率不可直接跨日相加"},
                                {
                                    "path": "columns.EXT_VAL_34.note",
                                    "value": "底表直接存百分数如 98.5，计算时需除以 100，切勿重复乘 100",
                                    "reason": "文档存储陷阱提示",
                                },
                                # 故意混入遮罩列测试自动拦截
                                {"path": "columns.secret_phone.label", "value": "手机号", "reason": "测试遮罩过滤"},
                            ],
                        }
                    ],
                    "guardrail_notes": [
                        {
                            "title": "TEEP 物理红线",
                            "rule": "TEEP 超过 100% 必属配置或源数据错误；OEE 超过 200% 多为折算系数异常。",
                            "category": "redline",
                        }
                    ],
                }
                return AIMessage(content=json.dumps(payload, ensure_ascii=False))

            if "语义指标建模专家" in sys_txt:
                payload = {
                    "metrics": [
                        {
                            "key": "mes_time_utilization",
                            "name": "时间稼动率（单表）",
                            "aliases": ["时间稼动率"],
                            "description": "实际开机时长除以负荷时长",
                            "unit": "%",
                            "calc_mode": "expression",
                            "table_name": "mes_shift_daily",
                            "sql_expression": "ROUND(SUM(EXT_VAL_01 - EXT_VAL_02 - EXT_VAL_03) * 100.0 / NULLIF(SUM(EXT_VAL_01 - EXT_VAL_02), 0), 2)",
                            "sql_template": "",
                            "date_column": "stat_date",
                            "filter_sql": "is_valid = 1",
                            "dimensions": [{"key": "line_code", "label": "产线", "column": "line_code"}],
                        },
                        {
                            "key": "mes_teep_doc",
                            "name": "全员有效生产率 TEEP（文档拆解）",
                            "aliases": ["TEEP"],
                            "description": "联合产线节拍与动态周期天数计算 TEEP",
                            "unit": "%",
                            "calc_mode": "sql_template",
                            "table_name": "mes_shift_daily",
                            "sql_expression": "",
                            "sql_template": (
                                "SELECT {{select_dims}} "
                                "ROUND(SUM(s.EXT_VAL_10 * pl.beat) * 100.0 / NULLIF(({{period_days}}) * 86400.0 * COUNT(DISTINCT s.line_code), 0), 2) AS mes_teep_doc "
                                "FROM mes_shift_daily s JOIN dim_prod_line pl ON pl.line_code = s.line_code "
                                "WHERE 1=1 {{and_where}} {{group_by}} {{order_by}}"
                            ),
                            "date_column": "s.stat_date",
                            "filter_sql": "s.is_valid = 1",
                            "dimensions": [{"key": "line_code", "label": "产线", "column": "s.line_code"}],
                        },
                        {
                            # 故意混入不存在列的脏指标，验证自动过滤进 skipped
                            "key": "mes_hallucinated",
                            "name": "臆造列指标",
                            "calc_mode": "expression",
                            "table_name": "mes_shift_daily",
                            "sql_expression": "SUM(NON_EXISTENT_COL)",
                        },
                    ]
                }
                return AIMessage(content=json.dumps(payload, ensure_ascii=False))

            # 标准业务实体拆解专家
            ent_payload = {
                "operations": [
                    {
                        "op": "upsert_entity",
                        "entity_key": "mes_test_product_model",
                        "label": "产品型号",
                        "description": "车间生产产品型号标准实体",
                        "aliases": ["品号", "机种"],
                        "reason": "多表通过 prod_code 关联折算系数",
                    },
                    {
                        "op": "upsert_binding",
                        "entity_key": "mes_test_product_model",
                        "entity_label": "产品型号",
                        "source_name": src_name,
                        "table_name": "mes_shift_daily",
                        "column_name": "prod_code",
                        "note": "班次产品编码",
                        "reason": "关联折算系数表",
                    },
                ]
            }
            return AIMessage(content=json.dumps(ent_payload, ensure_ascii=False))

    async def _fake_resolve_model(_session: Any):
        return _FakeDecomposeLLM(), "mock/decompose-pro"

    monkeypatch.setattr(catalog_assistant, "resolve_model", _fake_resolve_model)

    spec_md = """# TEEP / OEE 及相关指标公式总表
## 0. 字段符号对照
- `EXT_VAL_01`：计划开班时长（秒）
- `EXT_VAL_34`：班次良品率（底表存百分数，如 98.5）
## 1. 跨表 TEEP 公式
自然日历时间 = 统计周期天数 × 86400 × 产线条数
## 2. 判异红线
TEEP > 100% 必属源数据错误。
"""
    try:
        # 1) 测试上传附件接口可用
        r_att = await client.post(
            "/api/semantic-metrics/attachments",
            files={"file": ("oee_extra_notes.md", b"# Extra Note\nprod_code P100 = Model A\n", "text/markdown")},
        )
        assert r_att.status_code == 200, r_att.text
        att_id = r_att.json()["attachment_id"]

        # 2) 发起后台一键拆解任务（202 Accepted）
        r_start = await client.post(
            "/api/semantic-metrics/decompose-jobs",
            json={
                "source_id": sid,
                "scopes": ["catalog", "metrics", "entities"],
                "attachment_ids": [att_id],
                "doc_title": "TEEP 与 OEE 指标公式总表",
                "doc_text": spec_md,
                "save_to_kb": True,
                "note": "重点提取单表稼动率与跨表 TEEP",
            },
        )
        assert r_start.status_code == 202, r_start.text
        job_id = r_start.json()["job"]["id"]

        # 3) 轮询直至后台任务完成（验证进度 100% 与 4 个阶段全部成功）
        final_job: dict[str, Any] | None = None
        for _ in range(50):
            r_job = await client.get(f"/api/semantic-metrics/decompose-jobs/{job_id}")
            assert r_job.status_code == 200
            j = r_job.json()["job"]
            if j["status"] in ("succeeded", "failed", "cancelled"):
                final_job = j
                break
            await asyncio.sleep(0.04)

        assert final_job is not None
        assert final_job["status"] == "succeeded", final_job.get("error")
        assert final_job["progress"] == 100
        assert [s["status"] for s in final_job["steps"]] == ["succeeded", "succeeded", "succeeded", "succeeded"]

        # 验证 active 横幅接口返回最新待审阅任务
        r_active = await client.get("/api/semantic-metrics/decompose-jobs/active", params={"source_id": sid})
        assert r_active.status_code == 200
        assert r_active.json()["latest"]["id"] == job_id

        # 核心断言：阶段 2 提取出的 EXT_VAL_01（计划开班时长，大写对齐！）与 EXT_VAL_34 存储说明已在内存里链式传给阶段 3！
        metric_stage_prompt = next(u for s, u in captured_prompts if "语义指标建模专家" in s)
        assert "EXT_VAL_01 (REAL) — 计划开班时长，单位：秒，度量：flow" in metric_stage_prompt
        assert "底表直接存百分数" in metric_stage_prompt

        res = final_job["result"]
        assert len(res["kb_documents"]) >= 1
        assert len(res["guardrail_notes"]) == 1
        assert len(res["metric_candidates"]) == 2
        assert len(res["metric_skipped"]) == 1
        # 确认 secret_phone 遮罩列被自动拦截，ext_val_01 大小写已自动对齐为 EXT_VAL_01
        cat_paths = [c["path"] for c in res["catalog_patches"][0]["changes"]]
        assert "columns.EXT_VAL_01.label" in cat_paths
        assert all("secret_phone" not in p for p in cat_paths)
        assert len(res["entity_changes"]) == 2

        # 4) 一键应用审阅结果
        r_apply = await client.post(
            f"/api/semantic-metrics/decompose-jobs/{job_id}/apply",
            json={"overwrite_existing_metrics": True},
        )
        assert r_apply.status_code == 200, r_apply.text
        applied = r_apply.json()["applied"]
        assert applied["catalog_applied_count"] >= 8
        assert applied["metrics_created_count"] == 2
        assert applied["entities_applied_count"] == 2

        # 5) 核实三层资产均已真实落库且可立即执行在线试算
        async with SessionLocal() as session:
            cat_entry = await catalog.read_entry(session, sid, "mes_shift_daily")
            assert cat_entry is not None
            vis = catalog.visible_notes(cat_entry.notes)
            assert vis["columns"]["EXT_VAL_01"]["label"]["value"] == "计划开班时长"
            assert vis["columns"]["EXT_VAL_34"]["measure"]["value"] == "stock"

            ent_row = (
                await session.execute(select(CanonicalEntity).where(CanonicalEntity.key == "mes_test_product_model"))
            ).scalar_one_or_none()
            assert ent_row is not None
            assert ent_row.label == "产品型号"

        r_try_teep = await client.post(
            "/api/semantic-metrics/preview",
            json={
                "metric_id": "mes_teep_doc",
                "date_from": "2026-03-01",
                "date_to": "2026-03-02",
            },
        )
        assert r_try_teep.status_code == 200, r_try_teep.text
        assert r_try_teep.json()["row_count"] == 1
        assert r_try_teep.json()["rows"][0][0] > 0
    finally:
        await drop_source(sid)


async def test_oracle_dialect_sql_compilation_and_validation(client: AsyncClient) -> None:
    """回归验证 Oracle 方言下的多表 WITH CTE 模板、单表表达式、ROWNUM 行数限制、标识符不误加小写双引号与 DATE 字面量。"""
    import sqlglot

    from app.data.guard import check as guard_check
    from app.data.semantic_metrics import compile_metric_sql, create_metric
    from app.db.models import DataSource

    async with SessionLocal() as session:
        ora_ds = DataSource(
            name=f"oracle_mes_{uuid.uuid4().hex[:6]}",
            kind="oracle",
            host="127.0.0.1",
            port=1521,
            database="ORCL",
            username="mes_ro",
            password="",
            readonly=True,
            options={"schema": "MES_PROD"},
            schema_cache={
                "schema": "MES_PROD",
                "tables": {
                    "mes_kpi_shift_fact": {
                        "qualified": "MES_PROD.mes_kpi_shift_fact",
                        "schema": "MES_PROD",
                        "columns": [
                            {"name": "factory_code", "type": "VARCHAR2(32 CHAR)", "nullable": False},
                            {"name": "object_id", "type": "VARCHAR2(32 CHAR)", "nullable": False},
                            {"name": "start_date_time", "type": "DATE", "nullable": False},
                            {"name": "ext_val_01", "type": "NUMBER(18, 2)", "nullable": True},
                            {"name": "ext_val_05", "type": "NUMBER(18, 2)", "nullable": True},
                            {"name": "ext_val_14", "type": "NUMBER(18, 2)", "nullable": True},
                            {"name": "ext_val_15", "type": "NUMBER(18, 2)", "nullable": True},
                            {"name": "ext_val_54", "type": "NUMBER(18, 2)", "nullable": True},
                            {"name": "lower_quoted_col", "type": "VARCHAR2(16 CHAR)", "nullable": True, "quote_name": True},
                        ],
                    },
                    "iot_prod_line_dim": {
                        "qualified": "MES_PROD.iot_prod_line_dim",
                        "schema": "MES_PROD",
                        "columns": [
                            {"name": "line_code", "type": "VARCHAR2(32 CHAR)", "nullable": False},
                            {"name": "beat", "type": "VARCHAR2(32 CHAR)", "nullable": True},
                        ],
                    },
                },
            },
        )
        session.add(ora_ds)
        await session.flush()

        tpl = (
            "WITH ln AS (SELECT line_code, AVG(TO_NUMBER(beat)) AS m_line FROM MES_PROD.iot_prod_line_dim GROUP BY line_code) "
            "SELECT {{select_dims}} "
            "ROUND(SUM((k.ext_val_14 + k.ext_val_15) * k.ext_val_54) * 100.0 / NULLIF(SUM(ln.m_line * k.ext_val_54), 0) "
            "* (SUM(GREATEST(k.ext_val_01 - k.ext_val_05, 0)) / NULLIF({{period_days}} * 86400 * COUNT(DISTINCT k.object_id), 0)), 2) AS u_plan "
            "FROM MES_PROD.mes_kpi_shift_fact k LEFT JOIN ln ON ln.line_code = k.object_id "
            "WHERE 1=1 {{and_where}} {{group_by}} {{order_by}}"
        )
        metric_row, _ = await create_metric(
            session,
            {
                "key": "ora_u_plan",
                "name": "计划综合利用率（Oracle跨表）",
                "source_id": ora_ds.id,
                "calc_mode": "sql_template",
                "table_name": "mes_kpi_shift_fact",
                "sql_template": tpl,
                "date_column": "k.start_date_time",
                "dimensions": [
                    {"key": "factory_code", "label": "工厂", "column": "k.factory_code"},
                    {"key": "lower_col", "label": "特殊小写列", "column": "k.lower_quoted_col"},
                ],
            },
        )

        # 1) 按工厂维度分组（无日期区间）：验证无 LIMIT、无 CAST AS TEXT、无小写引号 "k"."factory_code"、WITH 保持在最外层
        sql_grouped, _ = compile_metric_sql(
            metric_row,
            source=ora_ds,
            group_by=["factory_code"],
            limit=30,
        )
        assert "LIMIT" not in sql_grouped.upper()
        assert "CAST(" not in sql_grouped.upper()
        assert '"k"."factory_code"' not in sql_grouped
        assert "k.factory_code" in sql_grouped
        assert "TO_CHAR(k.start_date_time, 'YYYY-MM-DD')" in sql_grouped
        assert sql_grouped.startswith("WITH ln AS")
        assert "WHERE ROWNUM <= 30" in sql_grouped
        guard_check(sql_grouped, readonly=True, dialect="oracle", source_name=ora_ds.name)
        sqlglot.parse_one(sql_grouped, read="oracle")

        # 2) 带日期区间 + quote_name=True 的特殊小写列分组：验证 Oracle DATE 字面量与跨天 < DATE 'next_day'
        sql_dated, _ = compile_metric_sql(
            metric_row,
            source=ora_ds,
            date_from="2026-11-01",
            date_to="2026-11-07",
            group_by=["lower_col"],
            limit=20,
        )
        assert "k.start_date_time >= DATE '2026-11-01'" in sql_dated
        assert "k.start_date_time < DATE '2026-11-08'" in sql_dated
        assert 'k."lower_quoted_col"' in sql_dated
        assert "7 * 86400" in sql_dated
        sqlglot.parse_one(sql_dated, read="oracle")

        # 3) 单表 expression 模式：验证自动补齐 MES_PROD. 前缀、按表达式而非 GROUP BY 1 分组、套外层 ROWNUM
        expr_row, _ = await create_metric(
            session,
            {
                "key": "ora_open_hours",
                "name": "计划开班总秒数",
                "source_id": ora_ds.id,
                "calc_mode": "expression",
                "table_name": "mes_kpi_shift_fact",
                "sql_expression": "SUM(ext_val_01)",
                "date_column": "start_date_time",
                "dimensions": [{"key": "factory_code", "label": "工厂", "column": "factory_code"}],
            },
        )
        sql_expr, _ = compile_metric_sql(
            expr_row,
            source=ora_ds,
            group_by_date=True,
            group_by=["factory_code"],
            limit=15,
        )
        assert "FROM MES_PROD.mes_kpi_shift_fact" in sql_expr
        assert "GROUP BY 1" not in sql_expr
        assert "GROUP BY TO_CHAR(start_date_time, 'YYYY-MM-DD'), factory_code" in sql_expr
        assert "LIMIT" not in sql_expr.upper()
        assert "WHERE ROWNUM <= 15" in sql_expr
        sqlglot.parse_one(sql_expr, read="oracle")
        await session.rollback()

