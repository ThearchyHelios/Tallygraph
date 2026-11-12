"""Legacy ask-data optimization through real query tools, evidence sealing and migration APIs."""
from __future__ import annotations

import copy

import pytest
from langchain_core.messages import AIMessage

from app.chat.optimization import prepare
from app.db.base import SessionLocal
from app.db.models import CaliberDraft, DataSource, Document, Workflow
from app.engine.guards import call_key
from app.engine.runner import run_manager, verify_manifest
from app.ledger.migration import audit
from tests import ledger_samples as S
from tests.test_semantic_pipeline import (  # noqa: F401
    SOURCE, client, cleanup_own_rows, engine_up, no_model, oee_asset, oee_graph, _ops,
)
from tests.test_report_node import events, report_of


def ask_graph():
    return S.chain(S.node("start", "input"),
                   S.node("work", "agent", prompt="昨天P100的OEE是多少", tools=[S.tool()], approval="never"),
                   S.node("write", "report", instructions="简短回答查询问题并引用证据"),
                   S.node("out", "output", fields=[{"name": "答案", "value": "{{ nodes.write.text }}"}]))


def test_only_simple_lookup_graphs_are_opted_in_without_mutating_saved_graph():
    graph = ask_graph()
    before = copy.deepcopy(graph)
    optimized = prepare(graph, "昨天P100的OEE是多少")
    assert optimized["nodes"][1]["data"]["config"]["ask_data_optimization"] == 1
    assert optimized["nodes"][2]["data"]["config"]["reuse_answer_from"] == "work"
    assert graph == before
    assert prepare(graph, "分析昨天P100的OEE为什么下降") == graph
    graph["nodes"][-1]["data"]["config"]["contract"] = {"required_metrics": ["oee"]}
    assert prepare(graph, "昨天P100的OEE是多少") == graph


def test_normalized_calls_preserve_literals_sources_and_parameters():
    key = lambda sql, **args: call_key("db_query__shop", {"sql": sql, **args}, canonical=True)
    assert key("SELECT  x  FROM t") == key("SELECT\nx FROM\nt")
    assert key("SELECT 'a  b'") != key("SELECT 'a b'")
    assert key('SELECT "a  b" FROM t') != key('SELECT "a b" FROM t')
    assert key("SELECT x FROM t -- a\nWHERE id=1") != key("SELECT x FROM t WHERE id=1")
    assert key("SELECT x FROM t", limit=1) != key("SELECT x FROM t", limit=100)
    assert call_key("db_query__shop", {"sql": "SELECT  x"}) != call_key("db_query__shop", {"sql": "SELECT x"})
    assert call_key("db_schema__shop", {"table": ["a", "b"]}, canonical=True) == call_key(
        "db_schema__shop", {"table": "b,a"}, canonical=True)
    assert call_key("db_schema__shop", {"table": ["a"]}, canonical=True) != call_key(
        "db_schema__other", {"table": ["a"]}, canonical=True)


@pytest.mark.parametrize("mode, expected_calls", [("clean", 0), ("bare", 1), ("wrong_ref", 1), ("formal", 1)])
async def test_agent_answer_reuse_still_checks_evidence_and_seals(oee_asset, monkeypatch, mode, expected_calls):
    from app.providers.mock_model import MockChatModel

    sql = "SELECT SUM(oee * hours) / SUM(hours) AS oee FROM shifts WHERE oee IS NOT NULL AND plant='P100' AND production_day='2026-11-09'"
    calls = {"agent": 0, "report": 0}

    def decide(self, messages):
        if not self.tools:
            calls["report"] += 1
            return AIMessage(content="OEE 为 [[v:Q1.r0.oee]]%。[[see:Q1]]")
        calls["agent"] += 1
        step = calls["agent"]
        if step <= 2:
            return AIMessage(content="", tool_calls=[{"name": S.tool(), "id": f"call_{step}",
                "args": {"sql": sql if step == 1 else sql.replace(" FROM ", "\n  FROM  ")}}])
        return AIMessage(content={"bare": "OEE 为 999%。", "wrong_ref": "OEE 为 [[v:Q99.r0.oee]]%。"}.get(
            mode, "OEE 为 [[v:Q1.r0.oee]]%。[[see:Q1]]"))

    monkeypatch.setattr(MockChatModel, "_decide", decide)
    run = await run_manager.start(graph=prepare(ask_graph(), "昨天P100的OEE是多少"), input_payload={},
                                  run_class="formal" if mode == "formal" else "exploratory")
    row = await S.wait(run.id)
    assert row.status == "succeeded", row.error
    assert calls == {"agent": 3, "report": expected_calls}
    tool_ends = [e for e in await events(run.id) if e.type == "tool.end"]
    assert len(tool_ends) == 1, [e.type for e in await events(run.id)]
    checked, doc = await report_of(run.id)
    assert checked["ok"] and "40" in doc["markdown"] and "999" not in doc["markdown"]
    assert (await verify_manifest(run.id))["ok"]
    assert bool([e for e in await events(run.id) if e.data.get("code") == "report_answer_reused"]) == (mode == "clean")


def test_migration_reports_grain_source_and_boundary_without_rewriting():
    graph = oee_graph()
    graph["nodes"][1]["data"]["config"]["args"]["sql"] = "SELECT oee FROM shifts"
    metric = graph["nodes"][2]["data"]["config"]["metrics"][0]
    metric.pop("semantic")
    before = copy.deepcopy(graph)
    result = audit(graph, workflow_id="legacy", version=3, sources=set())
    codes = {f["code"] for f in result["metrics"][0]["findings"]}
    assert {"semantic_missing", "source_missing", "scalar_unproven"} <= codes
    assert not result["runtime_verified"] and graph == before


async def test_migration_draft_is_independent_idempotent_and_keeps_source(oee_asset, client):
    async with SessionLocal() as session:
        workflow = await session.get(Workflow, oee_asset["workflow_id"])
        old_graph, old_revision, old_published = copy.deepcopy(workflow.graph), workflow.version, workflow.published_version
        doc = Document(title="指标公式总表.md", source="migration-test", content=SOURCE.read_text(), status="ready")
        session.add(doc)
        await session.flush()
        draft = CaliberDraft(title="迁移测试", status="questions_open", workflow_id=workflow.id,
                             origin={"documents": [doc.id], "fixed_filters": "仅测试范围"}, payload={})
        session.add(draft)
        await session.commit()
        draft_id = draft.id
    check = await client.get(f"/api/ledger/drafts/{draft_id}/migration")
    assert check.status_code == 200, check.text
    assert check.json()["metrics_total"] == 1 and check.json()["can_rebuild"]
    response = await client.post(f"/api/ledger/drafts/{draft_id}/migration", json={"expected_revision": old_revision})
    assert response.status_code == 201, response.text
    again = await client.post(f"/api/ledger/drafts/{draft_id}/migration", json={"expected_revision": old_revision})
    assert response.json() == again.json()
    fresh = (await client.get(f"/api/ledger/drafts/{response.json()['draft_id']}" )).json()
    assert fresh["workflow_id"] is None and fresh["definition_count"] == 48
    assert fresh["origin"]["fixed_filters"] == "仅测试范围"
    async with SessionLocal() as session:
        wf = await session.get(Workflow, oee_asset["workflow_id"])
        assert wf.graph == old_graph and wf.version == old_revision and wf.published_version == old_published
    stale = await client.post(f"/api/ledger/drafts/{draft_id}/migration", json={"expected_revision": old_revision + 1})
    assert stale.status_code == 409


async def test_explicit_source_scope_cannot_be_bypassed_by_fast_route(oee_asset):
    from app.api.copilot import GenerateIn, _auto_route_ops
    from tests.fixtures.sources import drop_source

    async with SessionLocal() as session:
        source = DataSource(name="only_this_scope", kind="sqlite", database=":memory:")
        session.add(source)
        await session.commit()
        source_id = source.id
    try:
        # A caliber from another database is not a candidate for this scoped question.
        # Return control to scoped exploration, rather than executing it or hijacking the turn with clarification.
        ops = await _auto_route_ops(GenerateIn(instruction="昨天P100的OEE是多少", intent="answer",
                                               datasource_ids=[source_id]))
        assert ops is None
    finally:
        await drop_source(source_id)


async def test_migration_requires_builder_even_for_read_only_audit(client):
    from tests.fakes import auth_env

    await auth_env.reset()
    admin = await auth_env.turn_on_local()
    await auth_env.make_user("migration-user", role="user")
    user = await auth_env.login("migration-user")
    try:
        response = await user.get("/api/ledger/drafts/absent/migration")
        write = await user.post("/api/ledger/drafts/absent/migration", json={"expected_revision": 1})
        assert response.status_code == write.status_code == 403
    finally:
        await user.aclose()
        await admin.aclose()
        await auth_env.reset()


async def test_matching_and_repeated_look_do_not_call_picker_again():
    from tests.test_look_caliber_definition import _view
    from app.look.calibers import CaliberKind
    from app.look.service import LookService

    class Picker:
        async def pick(self, **kwargs):
            raise AssertionError("known metric retrieval must not need a model")

    kind = CaliberKind([_view(name=f"无关指标{i}", metric_id=f"x{i}") for i in range(50)] + [
        _view(name="装配线 OEE", metric_id="assembly"), _view(name="成型线 OEE", metric_id="foam")], picker=Picker())
    result, reason = await kind.pick("昨天P100的OEE是多少", None)
    assert reason is None and {v.metric_id for v, _ in result} == {"assembly", "foam"}
    service = LookService([kind])
    request = {"need": "昨天P100的OEE是多少", "targets": [{"kind": "caliber"}]}
    first = await service.look(request)
    assert first.record["kinds"] == ["caliber"]

    async def no_more(*args, **kwargs):
        raise AssertionError("duplicate look must short circuit before pick/detail")
    kind.pick = no_more
    second = await service.look(request)
    assert second.record is None


async def test_analysis_request_is_not_blocked_by_disabled_or_ambiguous_semantics(monkeypatch):
    from app.ledger import nlchange

    async def cards(_):
        return [{"workflow_id": "one", "version": 1, "workflow_name": "OEE口径", "caliber": "OEE",
                 "card_title": "OEE", "card_id": "card", "metrics": [
                     {"id": "m1", "name": "装配线 OEE", "semantic": {"enabled": False, "aliases": ["OEE"]}},
                     {"id": "m2", "name": "成型线 OEE", "semantic": {"enabled": False, "aliases": ["OEE"]}}]}]
    monkeypatch.setattr(nlchange, "_auto_cards", cards)
    for question in ("分析昨天P100的OEE", "昨天P100的OEE为什么下降", "查一下昨天P100的OEE，解释原因"):
        assert await nlchange.auto_route(None, question=question) is None
    # Analysis does not grant access to an otherwise inaccessible matched asset.
    refused = await nlchange.auto_route(None, question="分析昨天P100的OEE", allowed=set())
    assert refused.mode == nlchange.ROUTE_REFUSED
