"""Document definitions -> reviewed semantic bindings -> one deterministic, authorized query.

Synthetic shifts only. No production connection or live model. Independent expected OEE:
(50 * 8 + 20 * 4) / 12 = 40, not the arithmetic mean (35).
"""
from __future__ import annotations

import copy
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import select

from app.db.base import SessionLocal
from app.db.models import Chunk, Document, Run
from app.ledger import nlchange, semantic
from app.ledger.semantic_import import inventory, implementation_guidance
from app.ledger.semantic_sql import scalar_problem
from tests import ledger_samples as S
from tests.publication_samples import publish
from tests.test_ledger_nlchange import (  # noqa: F401 - reuse isolated runtime and tracked cleanup
    client, cleanup_own_rows, engine_up, _auto_asset, _ops, _grant_app, _acting, _no_internals, runs_total,
)
from tests.test_ledger_docdraft_api import datasource, fake_trial, fake_model  # noqa: F401

SOURCE = Path(__file__).parent / 'fixtures' / 'oee_metric_definitions.md'
TIME = {"start": "start", "end": "end", "timezone": "Asia/Shanghai", "basis": "production_day",
        "periods": ["day"], "description": "按测试表已确认的生产日归属，夜班不按结束日期另算"}
SEMANTIC = {"version": 1, "enabled": True, "aliases": ["OEE"], "grain": "工厂、生产日单值",
            "aggregation": "按有效班次的实际开班时长加权；无有效值返回空；权重全缺时等权",
            "time": TIME, "entities": [{"field": "plant", "label": "工厂", "members": [
                {"value": "P100", "label": "测试工厂甲", "aliases": ["甲厂"]},
                {"value": "P200", "label": "测试工厂乙", "aliases": []}]}]}
SQL = """SELECT CASE WHEN COUNT(oee) = 1 THEN MAX(oee)
  WHEN COUNT(CASE WHEN oee IS NOT NULL THEN hours END) = 0 THEN AVG(oee)
  ELSE SUM(oee * hours) / NULLIF(SUM(CASE WHEN oee IS NOT NULL THEN hours END), 0) END AS oee
  FROM shifts WHERE plant = '{{ input.plant }}'
  AND production_day >= '{{ input.start }}' AND production_day < '{{ input.end }}'"""


def oee_graph(*, model=None, sql=SQL):
    return S.chain(
        S.node("start", "input", fields=[{"name": name, "type": "string", "required": True}
                                         for name in ("start", "end", "plant")]),
        S.node("fetch", "tool", tool=S.tool(), args={"sql": sql}),
        S.node("card", "metrics", caliber="合成 OEE 样例", metrics=[{
            "id": "oee", "name": "日 OEE（合成验证）", "unit": "%", "decimals": 2,
            "expression": "cell(nodes.fetch, 0, 'oee')", "contract": {"shape": "scalar"},
            "semantic": copy.deepcopy(model if model is not None else SEMANTIC)}]),
        S.node("report", "report", instructions="禁止在固定问数中运行本报告节点"),
        S.node("out", "output", fields=[{"name": "答案", "value": "{{ nodes.report.text }}"}]))


@pytest.fixture
def no_model(monkeypatch):
    from app.api import copilot
    from app.providers import mock_model

    def boom(*a, **kw):
        raise AssertionError("semantic fast path must not plan, discover schemas or write reports with a model")

    monkeypatch.setattr(copilot, "_op_model", boom)
    monkeypatch.setattr(mock_model.MockChatModel, "_decide", boom)
    monkeypatch.setattr(semantic, "now", lambda: datetime(2026, 11, 10, 2, tzinfo=timezone.utc))


@pytest.fixture
async def oee_asset(client, engine_up, tmp_path, no_model):
    with sqlite3.connect(tmp_path / "querycore.db") as db:
        db.execute("CREATE TABLE shifts (plant TEXT, production_day TEXT, oee REAL, hours REAL)")
        db.executemany("INSERT INTO shifts VALUES (?, ?, ?, ?)", [
            ("P100", "2026-11-09", 50, 8), ("P100", "2026-11-09", 20, 4),
            ("P100", "2026-11-09", None, 12),  # missing OEE must not dilute valid weights
            ("P200", "2026-11-09", 90, 8), ("P100", "2026-11-08", 99, 8),
            ("P100", "2026-11-07", 60, None), ("P100", "2026-11-07", 20, None),
        ])
    asset = await _auto_asset(graph=oee_graph())
    await publish(client, asset)
    return asset


def test_actual_document_is_inventory_not_37_invented_scalar_metrics():
    source = SOURCE.read_text()
    catalog = inventory([{"document_id": "doc", "content": source}])
    metrics = [i for i in catalog["items"] if i["kind"] == "metric"]
    symbols = [i for i in catalog["items"] if i["kind"] == "input"]
    assert len(symbols) == 19
    assert len(metrics) == 48
    assert len({i["id"] for i in catalog["items"]}) == len(catalog["items"])
    by_name = {i["name"]: i for i in metrics}
    assert by_name["人均效率 / TEEP 波动"]["status"] == "unavailable"
    assert "不回退" in by_name["有效 OEE（报表统一口径）"]["policy"]
    assert "0.5" in by_name["报废折算件数"]["policy"]
    for item in catalog["items"]:
        a, b = item["source"]["span"]
        assert source[a:b].strip() == item["source"]["quote"]
    guidance = implementation_guidance(catalog, [by_name["日 OEE（全天聚合）"]["id"]])
    assert "权重全缺" in guidance and "配置可覆盖" in guidance and "T_open_act" in guidance
    assert '"name": "空载率"' not in guidance


@pytest.mark.parametrize("sql", [
    "SELECT oee FROM shifts", "SELECT 86400 AS seconds FROM productionlines",
    "SELECT plant, SUM(oee) FROM shifts GROUP BY plant", "SELECT oee FROM shifts LIMIT 1",
    "SELECT oee FROM shifts WHERE ROWNUM = 1", "SELECT SUM(oee) OVER () FROM shifts",
    "SELECT SUM(oee) FROM shifts HAVING SUM(oee)>0", "SELECT SUM(oee), plant FROM shifts",
    "SELECT SUM(oee) FROM shifts UNION ALL SELECT SUM(oee) FROM shifts",
])
def test_detail_grouped_truncated_or_window_queries_cannot_be_scalar(sql):
    assert scalar_problem(sql)


@pytest.mark.parametrize("sql", [SQL, "SELECT COUNT(*) FROM shifts", "SELECT 86400 FROM dual",
    "WITH x AS (SELECT SUM(hours) AS hours FROM shifts) SELECT hours * 2 AS value FROM x",
    "SELECT SUM(good) / NULLIF(SUM(total),0) * 100 FROM shifts",
])
def test_single_row_aggregate_or_scalar_projection_is_accepted(sql):
    assert scalar_problem(sql) is None


@pytest.mark.parametrize("sql", ["SELECT 86400 AS seconds", "SELECT 86400 AS seconds FROM DUAL"])
def test_documented_constants_do_not_require_a_business_table_scan(sql):
    from app.ledger.draftplan import normalize_plan

    text = "自然时长按单天 86400 秒计算，不能按产线表复制多行"
    plan, problems = normalize_plan({"sql": sql, "metrics": [{
        "id": "natural", "name": "自然时长", "expression": "cell(nodes.fetch, 0, 'seconds')",
        "definition": {"citations": [{"chunk": "source", "quote": text}]},
    }]}, {"source": {"content": text, "document_id": "doc"}}, allowed_tables={"productionlines"}, strict_scalar=True)
    assert plan["sql"] == sql and not problems


def test_yesterday_uses_business_timezone_and_month_requires_its_own_permission():
    binding = semantic.TimeBinding.model_validate(TIME)
    # UTC still Nov 9; in Shanghai it is already Nov 10.
    at = datetime(2026, 11, 9, 17, tzinfo=timezone.utc)
    assert semantic.interval("昨天 P100 OEE", binding, at=at)[0] == ("2026-11-09", "2026-11-10")
    assert semantic.interval("2026年11月 OEE", binding)[0] is None
    binding.periods = ["month"]
    assert semantic.interval("2026年11月 OEE", binding)[0] == ("2026-11-01", "2026-12-01")
    assert semantic.interval("昨天和前天 OEE", binding)[0] is None
    assert not semantic.mention("P1001 的 OEE", "P100")
    assert not semantic.mention("昨天P100的OEEfoo", "OEE")


def test_authorized_agent_sees_the_same_published_entity_time_and_aggregation_rules():
    from app.look.calibers import metric_options, metric_briefs

    options = metric_options(oee_graph(), "card", workflow_id="wf", version=1)
    text = metric_briefs(options.selectable)
    assert "P100" in text and "工厂" in text and "Asia/Shanghai" in text
    assert "实际开班时长加权" in text and "production_day" in text


async def test_ordinary_question_reaches_querycore_with_one_query_and_no_llm(oee_asset, monkeypatch):
    import time
    from app.ledger import querycore

    executed = []
    original = querycore.execute

    async def measured(*args, **kwargs):
        outcome = await original(*args, **kwargs)
        executed.append(outcome)
        return outcome

    monkeypatch.setattr(querycore, "execute", measured)
    started = time.perf_counter()
    [final] = await _ops("给我昨天 P100 的 OEE")
    elapsed = time.perf_counter() - started
    assert final["op"] == "final", final
    result = final["execution"]
    assert result["via"] == "querycore" and result["ok"]
    assert result["metrics"][0]["value"] == 40
    assert len(executed) == 1 and len(executed[0].queries) == 1
    assert executed[0].measurement.counts["executed_sql"] == 1
    assert executed[0].measurement.counts["llm_calls"] == 0
    async with SessionLocal() as session:
        run = await session.get(Run, final["run_id"])
        assert run.input == {"start": "2026-11-09", "end": "2026-11-10", "plant": "P100"}
        assert run.origin["service"] == "querycore"
    print(f"\nSynthetic OEE ask-data: {elapsed:.3f}s; 1 query; 0 LLM; expected=40 actual=40")


@pytest.mark.parametrize("question", ["昨天 OEE", "昨天 P1001 的 OEE", "昨天 P100 和 P200 的 OEE",
    "昨天 P100 的 OEE 排除返工", "2026年11月 P100 的 OEE", "昨天前天 P100 的 OEE", "昨天产线P100的OEE"])
async def test_uncertain_semantic_request_clarifies_without_exploration(oee_asset, question):
    before = await runs_total()
    [reply] = await _ops(question)
    assert reply["op"] == "reply" and reply["route"]["mode"] == "clarify", reply
    assert await runs_total() == before


async def test_missing_day_stays_missing_and_weight_fallback_is_explicit(oee_asset):
    [empty] = await _ops("2026-11-06 P100 的 OEE")
    assert empty["execution"]["metrics"][0]["value"] is None
    [fallback] = await _ops("2026-11-07 P100 的 OEE")
    assert fallback["execution"]["metrics"][0]["value"] == 40


async def test_user_authorization_and_tool_parent_run_are_preserved(oee_asset):
    before = await runs_total()
    with _acting("semantic-denied"):
        [refused] = await _ops("昨天 P100 的 OEE")
    assert refused["op"] == "error" and refused["code"] == nlchange.ROUTE_NOT_PERMITTED
    assert await runs_total() == before
    await _grant_app(oee_asset["workflow_id"], 1, who="semantic-user")
    with _acting("semantic-user"):
        ops = await _ops("昨天 P100 的 OEE")
    assert ops[0]["op"] == "final", ops
    _no_internals(ops, workflow_id=oee_asset["workflow_id"])
    async with SessionLocal() as session:
        parent = await session.get(Run, ops[0]["run_id"])
        rows = (await session.execute(select(Run).where(Run.workflow_id == oee_asset["workflow_id"]))).scalars().all()
    assert "caliber_query" in str(parent.graph)
    assert any((r.origin or {}).get("parent_run_id") == parent.id for r in rows)


async def test_overlapping_oee_aliases_require_selection(client, oee_asset):
    other = oee_graph()
    other["nodes"][2]["data"]["config"]["metrics"][0]["name"] = "源系统 OEE（合成验证）"
    asset = await _auto_asset(graph=other)
    await publish(client, asset)
    before = await runs_total()
    [reply] = await _ops("昨天 P100 的 OEE")
    assert reply["op"] == "reply" and "多种" in reply["text"]
    assert await runs_total() == before


@pytest.mark.parametrize("case", ["disabled", "no_time", "unbound_entity"])
async def test_semantic_metadata_cannot_discard_a_date_or_unbound_filter(client, no_model, case):
    model = copy.deepcopy(SEMANTIC)
    if case == "disabled":
        model["enabled"] = False
    elif case == "no_time":
        model["time"] = None
    else:
        model["entities"][0]["field"] = "unused_plant"
    asset = await _auto_asset(graph=oee_graph(model=model))
    await publish(client, asset)
    before = await runs_total()
    [reply] = await _ops("2026-11-09 P100 的 OEE")
    assert reply["op"] == "reply" and reply["route"]["mode"] == "clarify"
    assert await runs_total() == before


async def test_semantic_edit_uses_existing_revision_and_invalidates_definition(client):
    asset = await _auto_asset(graph=oee_graph())
    before = copy.deepcopy(SEMANTIC)
    before["enabled"] = False
    body = {"operations": [{"op": "set", "metric_id": "oee", "path": "/semantic", "value": before}],
            "expected_revision": 1, "request_id": "semantic-edit", "target": {"card_id": "card"}}
    response = await client.patch(f"/api/ledger/drafts/{asset['draft_id']}", json=body)
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["revision"] == 2 and data["change"]["definition_hash"]["changed"]
    assert not data["metrics"][0]["semantic"]["enabled"]
    response = await client.patch(f"/api/ledger/drafts/{asset['draft_id']}", json={**body, "request_id": "stale"})
    assert response.status_code == 409


async def test_definitions_import_needs_no_database_or_model_and_preserves_unavailable(client, no_model):
    async with SessionLocal() as session:
        doc = Document(title="公式总表", content=SOURCE.read_text())
        session.add(doc)
        await session.commit()
        doc_id = doc.id
    response = await client.post("/api/ledger/drafts/definitions", json={"documents": [doc_id]})
    assert response.status_code == 201, response.text
    draft_id = response.json()["draft_id"]
    response = await client.get(f"/api/ledger/drafts/{draft_id}")
    assert response.status_code == 200, response.text
    draft = response.json()
    assert draft["workflow_id"] is None and draft["metrics"] == [] and draft["status"] == "path_pending"
    assert len(draft["semantic_inventory"]["items"]) == 67
    assert draft["definition_count"] == 48 and draft["metric_count"] == 0
    unavailable = next(i for i in draft["semantic_inventory"]["items"] if i["status"] == "unavailable")
    response = await client.post(f"/api/ledger/drafts/{draft_id}/implement",
                                 json={"selected": [unavailable["id"]], "datasource_id": "missing"})
    assert response.status_code == 422 and "无数据源" in response.text


@pytest.mark.parametrize("first_query", ["SELECT COUNT(*) AS count FROM orders", "SELECT amount AS count FROM orders"])
async def test_selected_definition_survives_implementation_and_rejects_duplicate_job(client, datasource, fake_trial, monkeypatch, first_query):
    import json
    from app.api import ledger

    source_id = await datasource()
    content = """# 测试口径
| 指标 | 公式 | 口径/说明 |
|---|---|---|
| 订单数 | COUNT(*) | 按日汇总，不把明细第一行当周期 |
| 人均效率 | 无数据源 | 不推算 |
"""
    async with SessionLocal() as session:
        doc = Document(title="测试定义表", content=content, chunk_count=1)
        session.add(doc)
        await session.flush()
        chunk = Chunk(document_id=doc.id, ordinal=1, content=content)
        session.add(chunk)
        await session.commit()
        doc_id, chunk_id = doc.id, chunk.id
    response = await client.post("/api/ledger/drafts/definitions", json={"documents": [doc_id]})
    draft_id = response.json()["draft_id"]
    draft = (await client.get(f"/api/ledger/drafts/{draft_id}")).json()
    item = draft["semantic_inventory"]["items"][0]
    calls = []

    async def queued(draft_id):
        calls.append(draft_id)

    monkeypatch.setattr(ledger.draft_jobs, "start", queued)
    body = {"selected": [item["id"]], "datasource_id": source_id}
    assert (await client.post(f"/api/ledger/drafts/{draft_id}/implement", json=body)).status_code == 202
    assert (await client.post(f"/api/ledger/drafts/{draft_id}/implement", json=body)).status_code == 409
    assert calls == [draft_id]
    generated = {
        "sql": "SELECT COUNT(*) AS count FROM orders", "entry_fields": [], "metrics": [{
            "id": "count", "name": "订单数", "expression": "cell(nodes.fetch, 0, 'count')",
            "definition": {"text": "订单数", "citations": [{"chunk": chunk_id, "quote": "按日汇总，不把明细第一行当周期"}]},
            "semantic": {"enabled": True, "grain": "日单值", "aggregation": "计数", "aliases": ["订单数量"]},
        }], "open_questions": [], "uncovered": [],
    }
    if first_query != generated["sql"]:
        # One document row may become separately named executable quantities (e.g. M_plan_slots / M_line_slots).
        generated["metrics"][0]["name"] = "订单计数量"
        generated["metrics"][0]["source_definition_id"] = item["id"]
    model = fake_model(monkeypatch, json.dumps({**generated, "sql": first_query}, ensure_ascii=False),
                       json.dumps(generated, ensure_ascii=False))
    await ledger._extract_in_background(draft_id)
    made = (await client.get(f"/api/ledger/drafts/{draft_id}")).json()
    assert made["status"] == "ready", (made.get("error"), made.get("dropped"), made.get("open_questions"))
    assert made["workflow_id"] and made["metric_count"] == 1
    assert made["selected_definition_ids"] == [item["id"]]
    assert made["semantic_inventory"] == draft["semantic_inventory"]
    assert made["metrics"][0]["semantic"]["enabled"] is False, "model cannot enable unreviewed query semantics"
    assert made["metrics"][0]["source_definition_id"] == item["id"] and not made["uncovered"]
    assert made["sql"] == "SELECT COUNT(*) AS count FROM orders"
    assert len(model.calls) == (1 if first_query == made["sql"] else 2)


async def test_changed_source_cannot_be_implemented_as_the_old_definition(client):
    async with SessionLocal() as session:
        doc = Document(title="会更新的总表", content=SOURCE.read_text())
        session.add(doc)
        await session.commit()
        doc_id = doc.id
    res = await client.post("/api/ledger/drafts/definitions", json={"documents": [doc_id]})
    draft_id = res.json()["draft_id"]
    draft = (await client.get(f"/api/ledger/drafts/{draft_id}")).json()
    item = next(i for i in draft["semantic_inventory"]["items"] if i["kind"] == "metric")
    async with SessionLocal() as session:
        doc = await session.get(Document, doc_id)
        doc.content += "\n修改公式"
        await session.commit()
    res = await client.post(f"/api/ledger/drafts/{draft_id}/implement",
                            json={"selected": [item["id"]], "datasource_id": "unused"})
    assert res.status_code == 409 and "原文已更新" in res.text
