"""快速直查通用性能优化、异构表/关联字典通用探查与零延迟数字证据防误报回归测试。"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import httpx
import openai
import pytest
import sqlglot
from langchain_core.messages import AIMessageChunk, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult

from app.chat.direct import build_direct_graph
from app.chat.direct_evidence import collect_fast_allow_numbers, normalize_and_bind_fast_citations
from app.chat.grounding import GroundingBundle, ground_question
from app.data import catalog
from app.data.guard import check as guard_check
from app.data.semantic_metrics import (
    compile_metric_sql,
    create_metric,
    find_companion_lookup_tables,
    probe_schema_lookup_and_discriminators,
)
from app.core.crypto import encrypt
from app.db.base import SessionLocal
from app.db.models import DataSource, Provider
from app.engine.evidence import compose_doc
from app.providers.factory import (
    ModelSpec,
    _NO_REASONING_HINT,
    _ReasoningAwareChatOpenAI,
    _openai_reasoning_hint,
    build_chat_model,
)
from app.tools.metrics import run_metric_query
from app.tools.registry import ToolContext, Versions
from tests.fixtures.sources import drop_source, sqlite_source


# ---------------------------------------------------------------------------
# 1. 跨模型网关关闭深度思考（thinking="off" / effort="low"）与 400 自动回退
# ---------------------------------------------------------------------------


def test_fast_direct_graph_disables_thinking_and_caps_steps() -> None:
    graph = build_direct_graph(bundle=GroundingBundle(), has_metrics=True)
    agent_node = next(n for n in graph["nodes"] if n["id"] == "agent1")
    cfg = agent_node["data"]["config"]
    assert cfg["thinking"] == "off"
    assert cfg["effort"] == "low"
    assert cfg["max_steps"] == 4
    assert cfg["parallel_tools"] is True


def test_provider_thinking_off_payloads_across_vendors() -> None:
    anth_prov = Provider(name="anth-test", kind="anthropic", api_key=encrypt("sk-test"), enabled=True)

    # Anthropic 普通模型：显式 disabled + effort=low
    anth_std = build_chat_model(
        anth_prov,
        ModelSpec(model="claude-sonnet-4-6", thinking="off", effort="low"),
    )
    assert anth_std.thinking == {"type": "disabled"}
    assert anth_std.output_config == {"effort": "low"}

    # Anthropic 强制思考模型（catalog 中标注 thinking='always'）：不发 disabled，仅设为隐藏式 adaptive + effort=low
    anth_req = build_chat_model(
        anth_prov,
        ModelSpec(model="claude-fable-5-1", thinking="off", effort="low"),
    )
    assert anth_req.thinking == {"type": "adaptive", "display": "omitted"}
    assert anth_req.output_config == {"effort": "low"}

    # OpenAI 兼容网关各主流家族推理开关映射
    off_spec = ModelSpec(thinking="off", effort="low")
    qwen_hint = _openai_reasoning_hint("openai_compatible", "qwen3-235b-a22b", off_spec)
    assert qwen_hint["extra_body"]["enable_thinking"] is False

    ds_hint = _openai_reasoning_hint("openai_compatible", "deepseek-v3.2", off_spec)
    assert ds_hint["extra_body"] == {"thinking": {"type": "disabled"}}

    gpt_hint = _openai_reasoning_hint("openai", "gpt-5", off_spec)
    assert gpt_hint == {"reasoning_effort": "low"}


@pytest.mark.asyncio
async def test_openai_compatible_gateway_strips_rejected_reasoning_hint_and_caches(monkeypatch: pytest.MonkeyPatch) -> None:
    _NO_REASONING_HINT.clear()
    seen_kwargs: list[dict[str, Any]] = []

    async def fake_agenerate(self: Any, messages: Any, stop: Any = None, run_manager: Any = None, **kwargs: Any) -> ChatResult:
        seen_kwargs.append(dict(kwargs))
        if "reasoning_effort" in kwargs or "extra_body" in kwargs:
            req = httpx.Request("POST", "https://strict-gateway.example/v1/chat/completions")
            resp = httpx.Response(
                400,
                request=req,
                json={"error": {"message": "Unrecognized request argument supplied: reasoning_effort"}},
            )
            raise openai.BadRequestError("Unrecognized request argument supplied: reasoning_effort", response=resp, body=resp.json())
        from langchain_core.messages import AIMessage

        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="ok"))])

    monkeypatch.setattr("langchain_openai.ChatOpenAI._agenerate", fake_agenerate)

    llm = _ReasoningAwareChatOpenAI(
        model="custom-corp-llm",
        api_key="sk-test",
        base_url="https://strict-gateway.example/v1",
        reasoning_hint={"reasoning_effort": "low"},
    )
    res1 = await llm._agenerate([HumanMessage(content="hi")])
    assert res1.generations[0].message.content == "ok"
    assert len(seen_kwargs) == 2
    assert seen_kwargs[0].get("reasoning_effort") == "low"
    assert "reasoning_effort" not in seen_kwargs[1]

    # 第二次调用命中进程级缓存，不再触发 400 重试
    seen_kwargs.clear()
    await llm._agenerate([HumanMessage(content="hi again")])
    assert len(seen_kwargs) == 1
    assert "reasoning_effort" not in seen_kwargs[0]


@pytest.mark.asyncio
async def test_openai_stream_strips_rejected_reasoning_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    _NO_REASONING_HINT.clear()
    calls = 0

    async def fake_astream(self: Any, messages: Any, stop: Any = None, run_manager: Any = None, **kwargs: Any):
        nonlocal calls
        calls += 1
        if "extra_body" in kwargs:
            req = httpx.Request("POST", "https://strict-stream.example/v1/chat/completions")
            resp = httpx.Response(
                422,
                request=req,
                json={"detail": "Extra inputs are not permitted: enable_thinking"},
            )
            raise openai.UnprocessableEntityError("Extra inputs are not permitted", response=resp, body=resp.json())
        yield ChatGenerationChunk(message=AIMessageChunk(content="fast-answer"))

    monkeypatch.setattr("langchain_openai.ChatOpenAI._astream", fake_astream)

    llm = _ReasoningAwareChatOpenAI(
        model="qwen3-coder",
        api_key="sk-test",
        base_url="https://strict-stream.example/v1",
        reasoning_hint={"extra_body": {"enable_thinking": False}},
    )
    chunks = [c async for c in llm._astream([HumanMessage(content="hello")])]
    assert calls == 2
    assert "".join(str(c.message.content) for c in chunks) == "fast-answer"


# ---------------------------------------------------------------------------
# 2. Oracle / PostgreSQL 分组指标 DESC NULLS LAST 方言修复与稀疏聚合验证
# ---------------------------------------------------------------------------


def test_compile_metric_sql_nulls_last_by_dialect() -> None:
    metric_def = {
        "key": "sparse_ratio",
        "name": "稀疏达成率",
        "calc_mode": "expression",
        "table_name": "fact_perf",
        "sql_expression": "ROUND(SUM(actual_val) * 100.0 / NULLIF(SUM(target_val), 0), 2)",
        "date_column": "stat_date",
        "filter_sql": "",
        "dimensions": [{"key": "unit_id", "label": "单元", "column": "unit_id"}],
    }
    schema = {
        "tables": {
            "fact_perf": {
                "qualified": "DW.fact_perf",
                "columns": [
                    {"name": "unit_id", "type": "VARCHAR2(32)"},
                    {"name": "stat_date", "type": "DATE"},
                    {"name": "actual_val", "type": "NUMBER"},
                    {"name": "target_val", "type": "NUMBER"},
                ],
            }
        }
    }

    ora_ds = DataSource(id="ora1", name="ora_dw", kind="oracle", schema_cache=schema)
    ora_sql, _ = compile_metric_sql(metric_def, source=ora_ds, group_by=["unit_id"], limit=50)
    assert "ORDER BY 2 DESC NULLS LAST" in ora_sql
    assert "WHERE ROWNUM <= 50" in ora_sql
    guard_check(ora_sql, readonly=True, dialect="oracle", source_name="ora_dw")
    sqlglot.parse_one(ora_sql, read="oracle")

    pg_ds = DataSource(id="pg1", name="pg_dw", kind="postgresql", schema_cache=schema)
    pg_sql, _ = compile_metric_sql(metric_def, source=pg_ds, group_by=["unit_id"], limit=50)
    assert "ORDER BY 2 DESC NULLS LAST LIMIT 50" in pg_sql
    sqlglot.parse_one(pg_sql, read="postgres")

    mysql_ds = DataSource(id="my1", name="my_dw", kind="mysql", schema_cache=schema)
    my_sql, _ = compile_metric_sql(metric_def, source=mysql_ds, group_by=["unit_id"], limit=50)
    assert "ORDER BY 2 DESC LIMIT 50" in my_sql
    assert "NULLS LAST" not in my_sql
    sqlglot.parse_one(my_sql, read="mysql")


# ---------------------------------------------------------------------------
# 3. 通用关联维表 / 字典配置表发现、低基数分类列实探与全空分组折叠诊断
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_general_companion_lookup_and_discriminator_probing_across_domains(tmp_path: Path) -> None:
    db_file = tmp_path / "general_erp.sqlite"
    conn = sqlite3.connect(db_file)
    conn.executescript(
        """
        CREATE TABLE fact_ledger_wide (
            tx_id TEXT,
            org_code TEXT,
            book_date TEXT,
            entry_category TEXT,
            secret_tag TEXT,
            val_01 REAL,
            val_02 REAL,
            val_03 REAL,
            val_04 REAL,
            val_05 REAL,
            val_06 REAL,
            val_07 REAL,
            val_08 REAL
        );
        CREATE TABLE meta_category_schema (
            entry_category TEXT,
            category_title TEXT,
            slot_col TEXT,
            slot_label TEXT,
            slot_unit TEXT
        );
        CREATE TABLE dim_org_unit (
            org_code TEXT,
            org_name TEXT,
            region_name TEXT
        );
        """
    )
    # 插入带前导零的分类码 '009' 与 '003'，以及 >20 种高基数流水号 tx_id（验证高基数列自动排除）
    for i in range(25):
        cat = "009" if i < 5 else "003"
        v1 = 100.0 if cat == "009" else None
        v2 = 12.0 if cat == "009" else None
        conn.execute(
            "INSERT INTO fact_ledger_wide VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, NULL, NULL, NULL)",
            (f"TX-{i:04d}", "2100" if i % 2 == 0 else "2200", "2026-11-11", cat, f"SEC-{i}", v1, v2),
        )
    conn.executemany(
        "INSERT INTO meta_category_schema VALUES (?, ?, ?, ?, ?)",
        [
            ("003", "能耗台账", "val_01", "总电量", "kWh"),
            ("009", "运营统计", "val_01", "应出勤秒数", "秒"),
            ("009", "运营统计", "val_02", "异常中断秒数", "秒"),
        ],
    )
    conn.executemany(
        "INSERT INTO dim_org_unit VALUES (?, ?, ?)",
        [("2100", "一分部", "华东"), ("2200", "二分部", "华东")],
    )
    conn.commit()
    conn.close()

    ds_id = await sqlite_source(
        "erp_general_test",
        db_file,
        options={"mask_columns": ["secret_tag"]},
        schema=True,
    )
    try:
        async with SessionLocal() as session:
            ds = await session.get(DataSource, ds_id)
            assert ds is not None
            schema_cache = ds.schema_cache

            # 1) 通用关联紧凑维表发现（无外键约束，纯靠高区分度共享键与结构识别）
            tables_map = schema_cache["tables"]
            masked = {"secret_tag"}
            companions = find_companion_lookup_tables(
                tables_map,
                ["fact_ledger_wide"],
                masked_set=masked,
            )
            comp_names = [c["table_name"] for c in companions]
            assert "meta_category_schema" in comp_names
            assert "dim_org_unit" in comp_names

            # 2) 低基数分类列与紧凑字典实探：保留 '009' 前导零，高基数 tx_id 与遮罩列 secret_tag 绝不混入
            hints = await probe_schema_lookup_and_discriminators(
                ds,
                tables_map,
                ["fact_ledger_wide"],
                masked_set=masked,
            )
            assert "'009'" in hints
            assert "'003'" in hints
            assert "异常中断秒数" in hints
            assert "TX-0001" not in hints
            assert "secret_tag" not in hints
            assert "SEC-" not in hints

            # 3) 语义预命中自动捎带紧凑关联配置表，且普通使用者模式不泄露物理表名
            metric_row, _ = await create_metric(
                session,
                {
                    "key": "erp_interrupt_rate",
                    "name": "异常中断率",
                    "source_id": ds.id,
                    "calc_mode": "expression",
                    "table_name": "fact_ledger_wide",
                    "sql_expression": "ROUND(SUM(val_02) * 100.0 / NULLIF(SUM(val_01), 0), 2)",
                    "date_column": "book_date",
                    "filter_sql": "",
                    "dimensions": [{"key": "tx_id", "label": "流水", "column": "tx_id"}],
                },
            )
            await catalog.write_entry(
                session,
                ds.id,
                "fact_ledger_wide",
                {
                    "dedup": {"value": "同表含多类台账记录，聚合前须限定 entry_category", "source": "human", "status": "confirmed"},
                },
                if_version=0,
                actor="test",
            )
            await session.commit()

            bundle = await ground_question(
                session,
                "查询 2026-11-11 的异常中断率",
                datasource_ids=[ds.id],
                restricted=False,
            )
            grounded_tbls = [t["table_name"] for t in bundle.matched_tables]
            assert "fact_ledger_wide" in grounded_tbls
            assert any(t in grounded_tbls for t in ("meta_category_schema", "dim_org_unit"))
            assert any(g["kind"] == "dedup" for g in bundle.guardrails)

            bundle_restricted = await ground_question(
                session,
                "查询 2026-11-11 的异常中断率",
                datasource_ids=[ds.id],
                restricted=True,
            )
            for ut in bundle_restricted.ui_summary["tables"]:
                assert "fact_ledger_wide" not in ut["table_name"]
                assert "meta_category_schema" not in ut["reason"]

            # 4) 当分组查询返回全 NULL 时，自动折叠重复 NULL 行并输出通用目录诊断提示 + 最新可用日期
            raw_tool_out = await run_metric_query(
                ToolContext(data_versions=Versions.CURRENT),
                metric=metric_row.key,
                date_from="2026-11-11",
                date_to="2026-11-11",
                group_by=["tx_id"],
                filters={"tx_id": [f"TX-{i:04d}" for i in range(5, 18)]},
                limit=50,
            )
            tool_payload = json.loads(raw_tool_out)
            assert tool_payload["all_metric_values_null"] is True
            assert tool_payload["null_group_row_count"] == 13
            assert len(tool_payload["rows"]) == 5
            assert tool_payload["latest_available_date"] == "2026-11-11"
            assert "diagnostic_hint" in tool_payload
            assert "meta_category_schema" in tool_payload["diagnostic_hint"]

            await session.delete(metric_row)
            await session.commit()
    finally:
        await drop_source(ds_id)


# ---------------------------------------------------------------------------
# 4. 快速直查零延迟数字证据封装：字符串编码、多表消歧与零误报验证
# ---------------------------------------------------------------------------


def test_fast_evidence_binds_string_codes_tables_and_preserves_hallucination_guard() -> None:
    snapshots: dict[str, dict[str, Any]] = {
        "art_dict": {
            "columns": ["cat_code", "cat_name"],
            "column_types": {"cat_code": "text", "cat_name": "text"},
            "rows": [
                ["003", "能耗数据"],
                ["7", "效率报表"],
                ["009", "班次统计"],
            ],
            "truncated": False,
        },
        "art_sum": {
            "columns": ["site_code", "unit_cnt", "stop_sec", "open_sec", "stop_rate", "unplanned_rate"],
            "column_types": {
                "site_code": "text",
                "unit_cnt": "number",
                "stop_sec": "number",
                "open_sec": "number",
                "stop_rate": "number",
                "unplanned_rate": "number",
            },
            "rows": [
                ["2100", 1, 7380, 72000, 10.25, 0.0],
                ["2200", 7, 18000, 500000, 3.6, 0.45],
            ],
            "truncated": False,
        },
        "art_dtl": {
            "columns": ["site_code", "unit_id", "unit_name", "open_sec", "stop_sec", "stop_rate", "unplanned_rate"],
            "column_types": {
                "site_code": "text",
                "unit_id": "text",
                "unit_name": "text",
                "open_sec": "number",
                "stop_sec": "number",
                "stop_rate": "number",
                "unplanned_rate": "number",
            },
            "rows": [
                ["2100", "U210001", "一号单元A", 72000, 7380, 10.25, 0.0],
                ["2200", "U220002", "二号单元B", 75000, 9900, 13.2, 2.2],
                ["2200", "U220001", "三号单元", 74000, 8100, 10.95, 1.1],
                ["2200", "U220003", "四号单元", 86400, 0, 0.0, 0.0],
                ["2200", "U220004", "五号单元", 86400, 0, 0.0, 0.0],
                ["2200", "U220005", "六号单元", 86400, 0, 0.0, 0.0],
                ["2200", "U220006", "七号单元", 86400, 0, 0.0, 0.0],
                ["2200", "U220007", "八号单元", 86400, 0, 0.0, 0.0],
            ],
            "truncated": False,
        },
    }
    cat = {
        "Q1": {"kind": "query", "artifact": "art_dict", "columns": snapshots["art_dict"]["columns"]},
        "Q2": {"kind": "query", "artifact": "art_sum", "columns": snapshots["art_sum"]["columns"]},
        "Q3": {"kind": "query", "artifact": "art_dtl", "columns": snapshots["art_dtl"]["columns"]},
    }
    loader = lambda aid: snapshots[aid]
    question = "2200和210011月11 的异常率"

    raw_answer = """### 2026-11-11 异常率（站点 2100 / 2200）

按「班次统计」（`cat_code='009'`）口径汇总 —— 异常率 = 异常时长 ÷ 应开时长 × 100.0%：

| 站点 | 单元数 | 异常时长(秒) | 应开时长(秒) | 异常率 | 计划外率 |
| --- | --- | --- | --- | --- | --- |
| 2100 | 1 | 7,380 | 72,000 | 10.25% | 0% |
| 2200 | 7 | 18,000 | 500,000 | 3.6% | 0.45% |

一句话结论：2100 当日异常率 10.25%（唯一单元一号单元A，异常 7,380 秒）；2200 异常率 3.6%，且 7 个单元中有 5 个为 0。

### 2200 单元级明细

| site_code | unit_id | unit_name | open_sec | stop_sec | stop_rate | unplanned_rate |
| --- | --- | --- | --- | --- | --- | --- |
| 2100 | U210001 | 一号单元A | 72,000 | 7,380 | 10.25 | 0 |
| 2200 | U220002 | 二号单元B | 75,000 | 9,900 | 13.2 | 2.2 |
| 2200 | U220001 | 三号单元 | 74,000 | 8,100 | 10.95 | 1.1 |
| 2200 | U220003 | 四号单元 | 86,400 | 0 | 0 | 0 |

说明：2026-11-11 两站点均已含两个班次记录；默认卡片混算了 003 与 7 分组，本次已按 009 分组计算。"""

    prepared = normalize_and_bind_fast_citations(raw_answer, cat, loader=loader)
    # 验证前导零字符串编码 '009' 与 '003' 被正确绑定且渲染保留前导零
    assert "[[v:Q1.r2.cat_code]]" in prepared
    assert "[[v:Q1.r0.cat_code]]" in prepared

    allow = collect_fast_allow_numbers(prepared, cat, question=question, loader=loader)
    doc = compose_doc(
        prepared,
        cat,
        node_id="report1",
        run_id="run_test_ok",
        allow_numbers=allow,
        cells_allowed=True,
        entities=False,
        loader=loader,
    )
    assert "009" in doc["markdown"]
    assert "003" in doc["markdown"]
    assert doc["stats"]["numbers_cited"] >= 15
    assert doc["stats"]["uncited_numbers"] == 0
    assert doc["violations"] == []

    # 反例：若模型在回答中凭空编造了一个快照与提问中都不存在的数字（如 99.87% 或额外编造的 100 次），必须照常标出 uncited_number！
    hallucinated_raw = raw_answer + "\n\n此外昨日异常率为 99.87%，累计发生 100 次故障。"
    prep_bad = normalize_and_bind_fast_citations(hallucinated_raw, cat, loader=loader)
    allow_bad = collect_fast_allow_numbers(prep_bad, cat, question=question, loader=loader)
    doc_bad = compose_doc(
        prep_bad,
        cat,
        node_id="report1",
        run_id="run_test_bad",
        allow_numbers=allow_bad,
        cells_allowed=True,
        entities=False,
        loader=loader,
    )
    uncited_msgs = [v["message"] for v in doc_bad["violations"] if v["code"] == "uncited_number"]
    assert any("99.87%" in m for m in uncited_msgs)
    assert any("100" in m for m in uncited_msgs)
