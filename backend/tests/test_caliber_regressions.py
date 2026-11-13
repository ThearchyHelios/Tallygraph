"""口径审查回归：走真实保存/发布接口和隔离数据库，验证版本、只读试跑、质量门禁和组合复用。"""
from __future__ import annotations

import copy
import json
import sqlite3
import uuid

import pytest
from sqlalchemy import select

from app.api import ledger as api_ledger
from app.db.base import SessionLocal
from app.db.models import CaliberDraft, DataSource, Run, Workflow, WorkflowVersion
from app.engine.runner import run_manager
from app.engine.schema import GraphSpec, validate_graph
from app.ledger import draftplan
from tests import ledger_samples as S
from tests.test_ledger_docdraft_api import (
    REAL_RUN_TRIAL, client, datasource, documents, extract, fake_model, fake_trial, groups_reply, plan_reply,
)
from tests.test_runtime_sqlcheck import (
    FANOUT_SQL, _drop_created_sources, _source, engine_up, output_of,
)
from tests.test_metric_no_data import plant, board as no_data_board


def cfg(graph, nid):
    return next(n for n in graph["nodes"] if n["id"] == nid)["data"]["config"]


async def draft(client, documents, datasource, monkeypatch, *, multi=False):
    doc = await documents("regression-" + uuid.uuid4().hex)
    source = await datasource()
    one = json.loads(plan_reply(doc + "-2"))
    if multi:
        two = copy.deepcopy(one)
        two["metrics"][0].update(id="m2", name="订单金额", expression="cell(nodes.fetch, 0, 'gmv')")
        two["sql"] = "SELECT SUM(amount) AS gmv FROM orders WHERE store = '{{ input.store }}'"
        reply = groups_reply(one, two)
    else:
        reply = json.dumps(one, ensure_ascii=False)
    fake_model(monkeypatch, reply)
    created = await extract(client, doc, source)
    got = await client.get("/api/ledger/drafts/" + created["draft_id"])
    assert got.status_code == 200, got.text
    body = got.json()
    assert body["status"] == "ready", body
    return body


async def snapshots(wid):
    async with SessionLocal() as session:
        workflow = await session.get(Workflow, wid)
        versions = list((await session.execute(select(WorkflowVersion).where(
            WorkflowVersion.workflow_id == wid).order_by(WorkflowVersion.version))).scalars())
        return workflow, versions


async def test_draft_edit_preserves_published_snapshot_and_updates_canvas(client, documents, datasource, monkeypatch):
    body = await draft(client, documents, datasource, monkeypatch)
    wid, did = body["workflow_id"], body["id"]
    published = await client.post(f"/api/workflows/{wid}/publish", json={"level": "published"})
    assert published.status_code == 200 and published.json()["ok"], published.text
    _, before = await snapshots(wid)
    metrics = copy.deepcopy(body["metrics"])
    metrics[0]["expression"] += " * 100"
    changed = await client.patch(f"/api/ledger/drafts/{did}",
                                 json={"metrics": metrics, "expected_revision": body["revision"]})
    assert changed.status_code == 200, changed.text
    workflow, after = await snapshots(wid)
    assert [v.version for v in after] == [1, 2]
    assert after[0].level == "published" and after[0].graph == before[0].graph
    assert after[0].graph_hash == before[0].graph_hash
    assert after[1].level is None and workflow.published_version == 1 and workflow.status == "draft"
    assert cfg(after[1].graph, "card")["metrics"][0]["expression"].endswith("* 100")
    assert workflow.graph == after[1].graph and changed.json()["version"] == 2


async def test_trial_is_invalidated_by_edit_and_late_results_cannot_replace_new_trial(
        client, documents, datasource, monkeypatch):
    body = await draft(client, documents, datasource, monkeypatch)
    did = body["id"]
    metrics = copy.deepcopy(body["metrics"])
    metrics[0]["expression"] += " * 100"
    changed = (await client.patch(f"/api/ledger/drafts/{did}",
                                  json={"metrics": metrics, "expected_revision": body["revision"]})).json()
    assert changed["trial"]["status"] == "stale"
    assert not next(c for c in changed["checks"] if c["key"] == "trial")["ok"]
    old = (await client.get(f"/api/ledger/drafts/{did}/trial")).json()
    assert old["status"] == "stale" and old["metrics"] == [] and old["queries"] == []
    first = (await client.post(f"/api/ledger/drafts/{did}/trial", json={"inputs": {}})).json()
    latest = (await client.post(f"/api/ledger/drafts/{did}/trial", json={"inputs": {"store": "east"}})).json()
    assert first["version"] == latest["version"] == 2
    assert first["inputs_hash"] != latest["inputs_hash"]
    await api_ledger._save_trial(did, body["trial"])
    await api_ledger._save_trial(did, first)
    current = (await client.get(f"/api/ledger/drafts/{did}")).json()
    assert current["trial"]["run_id"] == latest["run_id"]
    assert current["trial"]["status"] == "succeeded"
    assert next(c for c in current["checks"] if c["key"] == "trial")["status"] == "unknown"


async def test_pending_trial_failure_cannot_replace_a_newer_attempt(client, documents, datasource, monkeypatch):
    body = await draft(client, documents, datasource, monkeypatch)
    first = {**body["trial"], "trial_id": "first", "run_id": "", "status": "running"}
    second = {**first, "trial_id": "second"}
    assert await api_ledger._save_trial(body["id"], first, replace=True)
    assert await api_ledger._save_trial(body["id"], second, replace=True)
    assert not await api_ledger._save_trial(body["id"], {**first, "status": "failed"})
    current = (await client.get(f"/api/ledger/drafts/{body['id']}/trial")).json()
    assert current["trial_id"] == "second" and current["status"] == "running"


async def test_metric_edits_preserve_canvas_sql_and_node_ids(client, documents, datasource, monkeypatch):
    body = await draft(client, documents, datasource, monkeypatch, multi=True)
    wid, did = body["workflow_id"], body["id"]
    workflow, _ = await snapshots(wid)
    graph = copy.deepcopy(workflow.graph)
    cfg(graph, "fetch_2")["args"]["sql"] = "SELECT SUM(amount) * 2 AS gmv FROM orders"
    cfg(graph, "start")["fields"].append({"name": "region", "type": "string", "required": True})
    saved = await client.patch(f"/api/workflows/{wid}",
                               json={"graph": graph, "expected_revision": workflow.version})
    assert saved.status_code == 200, saved.text
    kept = copy.deepcopy(body["metrics"][1])
    kept["name"] = "编辑第二组"
    patched = await client.patch(f"/api/ledger/drafts/{did}",
                                 json={"metrics": [kept], "expected_revision": saved.json()["version"]})
    assert patched.status_code == 200, patched.text
    workflow, versions = await snapshots(wid)
    latest = versions[-1].graph
    assert workflow.version == 3 and workflow.graph == latest
    assert cfg(latest, "fetch_2") == cfg(graph, "fetch_2")
    assert cfg(latest, "start") == cfg(graph, "start")
    assert cfg(latest, "card")["metrics"][0]["expression"] == kept["expression"]
    assert validate_graph(GraphSpec.model_validate(latest)).ok
    assert cfg(latest, "out")["contract"]["required"] == [kept["id"]]
    empty = await client.patch(f"/api/ledger/drafts/{did}",
                               json={"metrics": [], "expected_revision": patched.json()["revision"]})
    assert empty.status_code == 200 and empty.json()["metrics"] == [], empty.text


async def test_draft_list_counts_current_canvas_metrics(client, documents, datasource, monkeypatch):
    body = await draft(client, documents, datasource, monkeypatch)
    workflow, _ = await snapshots(body["workflow_id"])
    graph = copy.deepcopy(workflow.graph)
    metrics = cfg(graph, "card")["metrics"]
    metrics.append({**metrics[0], "id": "m2", "name": "另一个指标"})
    saved = await client.patch(f"/api/workflows/{workflow.id}",
                               json={"graph": graph, "expected_revision": workflow.version})
    assert saved.status_code == 200
    items = (await client.get("/api/ledger/drafts")).json()["drafts"]
    assert next(item for item in items if item["id"] == body["id"])["metric_count"] == 2


@pytest.mark.parametrize("draft_trial", [False, True])
async def test_trials_never_write_even_when_source_is_writable_and_approval_disabled(
        client, tmp_path, engine_up, monkeypatch, draft_trial):
    path = tmp_path / "writable.db"
    name = await S.fresh_source(path)
    async with SessionLocal() as session:
        source = (await session.execute(select(DataSource).where(DataSource.name == name))).scalar_one()
        source.readonly = False
        await session.commit()
    graph = S.caliber_graph(sql="UPDATE orders SET amount = amount + 1 WHERE id = 1 RETURNING amount AS gmv")
    cfg(graph, "fetch")["approval"] = "never"
    wid = await S.publish(name="writable caliber " + uuid.uuid4().hex, graph=graph, level="published")
    published = await client.post(f"/api/workflows/{wid}/publish", json={"level": "published"})
    assert published.status_code == 200 and published.json()["ok"], published.text
    if draft_trial:
        async with SessionLocal() as session:
            workflow = await session.get(Workflow, wid)
            workflow.origin = {"kind": "doc_draft"}
            row = CaliberDraft(title="只读回归", workflow_id=wid, status="ready", payload={"version": 1})
            session.add(row)
            await session.commit()
            did = row.id
        monkeypatch.setattr(api_ledger, "_run_trial", REAL_RUN_TRIAL)
        url = f"/api/ledger/drafts/{did}/trial"
    else:
        url = f"/api/ledger/calibers/{wid}/1/card/trial"
    result = await client.post(url, json={"inputs": {"start": "2026-09-01", "end": "2026-09-08"}})
    assert result.status_code == 200, result.text
    assert result.json()["status"] == "failed", result.text
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT amount FROM orders WHERE id = 1").fetchone()[0] == 100
    async with SessionLocal() as session:
        run = await session.get(Run, result.json()["run_id"])
    assert run.options["read_only_data"] is True


def definition(kind, path="cell(nodes.fetch, 0, 'gmv')"):
    if kind == "rule":
        return {"kind": "rule", "rule": {"inputs": {"x": path},
                "all": [{"input": "x", "op": "gte", "value": 50}], "then": 1, "else": 0}}
    if kind == "assessment":
        return {"kind": "assessment", "assessment": {
            "dimensions": [{"name": "x", "input": path, "min": 0, "max": 100000000}],
            "levels": [{"name": "normal", "min": 0}]}}
    return {"expression": path}


@pytest.mark.parametrize("kind", ["formula", "rule", "assessment"])
async def test_all_metric_kinds_degrade_when_the_query_fails_sql_checks(scenic_db, engine_up, kind):
    _, source = await _source(scenic_db)
    graph = S.chain(
        S.node("start", "input"),
        S.node("fetch", "tool", tool=f"db_query__{source}", args={"sql": FANOUT_SQL}),
        S.node("card", "metrics", caliber="review", metrics=[{"id": "m", "name": "metric", **definition(kind)}]),
        S.node("out", "output", fields=[{"name": "value", "value": "{{ nodes.card.text }}"}],
               contract={"metrics_from": ["card"], "required": ["m"]}),
    )
    started = await run_manager.start(graph=graph, input_payload={})
    run = await S.wait(started.id)
    assert run.status == "succeeded", run.error
    metric = (await output_of(run.id, "card"))["metrics"][0]
    assert metric["sql_check_failed"] is True
    assert run.output["_issuance"]["tier"] == "degraded"
    assert any("SQL 检查" in gap for gap in run.output["_issuance"]["gaps"])


@pytest.mark.parametrize("first,second", [(0, 100), (False, True), ("2026-09-01", "2026-10-01")])
def test_conflicting_defaults_require_explicit_input_even_after_more_groups(first, second):
    blocks = [{"entry_fields": [{"name": "scope", "type": "string", "default": value}]}
              for value in (first, second, second)]
    fields, notes = draftplan.merge_entry_fields(blocks)
    assert "default" not in fields[0] and fields[0]["required"] is True
    assert any("默认值不一致" in note for note in notes)
    graph = draftplan.build_graph(blocks, name="conflict")
    assert cfg(graph, "start")["fields"] == fields
    fields, notes = draftplan.merge_entry_fields([blocks[0], blocks[0]])
    assert fields[0]["default"] == first and not notes


async def test_real_trial_runs_current_canvas_version(client, documents, datasource, monkeypatch, tmp_path, engine_up):
    body = await draft(client, documents, datasource, monkeypatch)
    path = tmp_path / "draft.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE orders (status TEXT, store TEXT, amount REAL, paid_at TEXT)")
        db.execute("INSERT INTO orders VALUES ('paid', '', 5, '2026-01-01')")
    async with SessionLocal() as session:
        source = await session.get(DataSource, body["datasource_id"])
        source.database, source.readonly, source.enabled = str(path), True, True
        await session.commit()
    workflow, _ = await snapshots(body["workflow_id"])
    graph = copy.deepcopy(workflow.graph)
    cfg(graph, "card")["metrics"][0]["expression"] += " * 100"
    saved = await client.patch("/api/workflows/" + body["workflow_id"],
                               json={"graph": graph, "expected_revision": workflow.version})
    assert saved.status_code == 200 and saved.json()["version"] == 2, saved.text
    monkeypatch.setattr(api_ledger, "_run_trial", REAL_RUN_TRIAL)
    result = await client.post("/api/ledger/drafts/" + body["id"] + "/trial", json={"inputs": {}})
    assert result.status_code == 200 and result.json()["status"] == "succeeded", result.text
    assert result.json()["metrics"][0]["value"] == 100
    async with SessionLocal() as session:
        run = await session.get(Run, result.json()["run_id"])
    assert run.origin["version"] == result.json()["version"] == 2
    current = (await client.get("/api/ledger/drafts/" + body["id"])).json()
    assert next(c for c in current["checks"] if c["key"] == "trial")["status"] == "pass"


@pytest.mark.parametrize("kind", ["rule", "assessment"])
async def test_prefixed_reuse_preserves_nonformula_inputs(kind, tmp_path, engine_up):
    from app.ledger.derive import assemble, caliber_block, caliber_probe, load_pinned

    await S.fresh_source(tmp_path / (kind + ".db"))
    graph = S.caliber_graph(sql="SELECT amount AS gmv FROM orders WHERE id = 1")
    cfg(graph, "card")["metrics"] = [{"id": "m", "name": "metric", **definition(kind)}]
    # 编辑器切类型会保留旧公式；依赖分析只认当前类型，不把未使用的旧输入也拷进来。
    cfg(graph, "card")["metrics"][0]["expression"] = "cell(nodes.former_fetch, 0, 'gmv')"
    original_graph = copy.deepcopy(graph)
    wid = await S.publish(name="regression " + kind, graph=graph)
    async with SessionLocal() as session:
        pinned = await load_pinned(session, wid, 1, "card")
    inputs = {"start": "2026-09-01", "end": "2026-09-08"}
    original = await run_manager.start(graph=caliber_probe(pinned).graph, input_payload=inputs)
    direct = await S.wait(original.id)
    combined = assemble("caliber_probe", [caliber_block(pinned, prefix="c1_")], fields=pinned.entry_fields)
    renamed = await run_manager.start(graph=combined.graph, input_payload=inputs)
    reused = await S.wait(renamed.id)
    assert direct.status == reused.status == "succeeded", reused.error
    before = (await output_of(direct.id, "card"))["metrics"][0]
    after = (await output_of(reused.id, "c1_card"))["metrics"][0]
    assert after["value"] == before["value"]
    assert all("c1_fetch" in item["path"] for item in after["inputs"])
    _, versions = await snapshots(wid)
    assert versions[0].graph == original_graph


@pytest.mark.parametrize("kind", ["rule", "assessment"])
async def test_nonformula_metrics_keep_no_data_gaps(kind, plant, engine_up):
    graph = no_data_board(plant)
    cfg(graph, "card")["metrics"] = [{"id": "m", "name": "metric",
        **definition(kind, "cell(nodes.q_kpi, 0, '上周到访人数')")}]
    cfg(graph, "out")["contract"]["required"] = ["m"]
    started = await run_manager.start(graph=graph, input_payload={})
    run = await S.wait(started.id)
    assert run.status == "succeeded", run.error
    metric = (await output_of(run.id, "card"))["metrics"][0]
    assert metric["no_data"] is True and metric["no_data_sources"]
    assert run.output["_issuance"]["tier"] == "degraded"


@pytest.mark.parametrize("kind", ["rule", "assessment"])
@pytest.mark.parametrize("copied", [False, True])
async def test_nonformula_metrics_keep_truncation_gaps(kind, copied, tmp_path, engine_up):
    await S.fresh_source(tmp_path / "truncated.db")
    nodes = [S.node("start", "input"),
             S.node("fetch", "tool", tool=S.tool(), args={"sql": "SELECT id FROM orders ORDER BY id", "limit": 2}),
             S.node("parse", "transform", mode="json", template="{{ nodes.fetch }}", assign_to="rows")]
    path = "len(vars.rows.rows)"
    if copied:
        nodes.append(S.node("upstream", "metrics", metrics=[{"id": "n", "expression": path}]))
        path = "nodes.upstream.metrics[0].value"
    nodes.extend([
        S.node("card", "metrics", caliber="truncated", metrics=[{"id": "m", "name": "metric", **definition(kind, path)}]),
        S.node("out", "output", fields=[{"name": "value", "value": "{{ nodes.card.text }}"}],
               contract={"metrics_from": ["card"], "required": ["m"]}),
    ])
    started = await run_manager.start(graph=S.chain(*nodes), input_payload={})
    run = await S.wait(started.id)
    assert run.status == "succeeded", run.error
    metric = (await output_of(run.id, "card"))["metrics"][0]
    assert metric["incomplete"] is True and metric["truncated_sources"]
    assert run.output["_issuance"]["tier"] == "degraded"


async def test_a_stale_writer_cannot_overwrite_a_new_workflow_revision():
    from app.apps.versions import WorkflowChanged, save_graph

    graph = S.chain(S.node("start", "input"), S.node("out", "output"))
    wid = await S.publish(name="concurrent " + uuid.uuid4().hex, graph=graph)
    async with SessionLocal() as first, SessionLocal() as second:
        stale = await first.get(Workflow, wid)
        current = await second.get(Workflow, wid)
        changed = copy.deepcopy(graph)
        cfg(changed, "out")["fields"] = [{"name": "value", "value": "latest"}]
        assert await save_graph(second, current, changed)
        await second.commit()
        with pytest.raises(WorkflowChanged):
            await save_graph(first, stale, {**graph, "viewport": {"x": 1, "y": 1}})
        await first.rollback()
    workflow, versions = await snapshots(wid)
    assert workflow.graph == changed and len(versions) == 2
