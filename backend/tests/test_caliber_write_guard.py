"""K02 卡级身份、客户端版本检查与写入幂等：卡要指明、写要带基准版本、同一个 request_id 只落一次。

- 读草稿给出权威口径卡（card_id）和基准版本（revision）；
- 已接管的草稿没带 expected_revision → 409 revision_required（老页面不能指望照旧写进去）；
- 两个页面基于同一版先后保存：后一个 409 stale_revision 并带上 current_version，前一个写的内容不被盖掉（E01）；
- 卡不在图上报 409 card_not_found；图上有好几张可写的卡又没指明目标报 409 card_required，指到哪张就改哪张（不取第一张）；
- request_id：同键同内容重放不新增版本（基准版本过期也照旧成功——幂等在版本检查之前判），同键不同内容 409
  idempotency_conflict；
- 没碰过的配置（取数 SQL、入口字段、报告节点、图上的扩展字段）保留；
- 画布、回滚、发布单的「保存版本」走同一条 CAS：旧写 409 stale_revision，出单之后画布又改过的发布单停在保存版本那一步；
- 没接管到工作流上的老草稿照旧只改 payload，不受这几条约束。

模型一律注入假模型（test_ledger_docdraft_api.fake_model），试跑换成写一行 succeeded 的运行（fake_trial），不发真实请求。
"""
from __future__ import annotations

import copy
import json
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.core.artifact_store import graph_hash
from app.db.base import SessionLocal
from app.db.models import CaliberDraft, Workflow, WorkflowVersion
from app.db.models_blueprints import Blueprint
from tests.test_ledger_docdraft_api import (  # noqa: F401 - client / documents / datasource / fake_trial 是 fixture
    client, datasource, documents, extract, fake_model, fake_trial, plan_reply,
)


def cfg(graph: dict, node_id: str) -> dict:
    return next(n for n in graph["nodes"] if n["id"] == node_id)["data"]["config"]


def _graph(label: str) -> dict:
    return {
        "nodes": [
            {"id": "start", "type": "input", "position": {"x": 0, "y": 0},
             "data": {"label": "输入", "config": {"fields": [{"name": "q"}]}}},
            {"id": "out", "type": "output", "position": {"x": 300, "y": 0},
             "data": {"label": label, "config": {"fields": [{"name": "r", "value": "{{ input.q }}"}]}}},
        ],
        "edges": [{"source": "start", "target": "out"}],
    }


async def _workflow(workflow_id: str) -> Workflow:
    async with SessionLocal() as session:
        return await session.get(Workflow, workflow_id)


async def _versions(workflow_id: str) -> list[int]:
    async with SessionLocal() as session:
        return list((await session.execute(
            select(WorkflowVersion.version).where(WorkflowVersion.workflow_id == workflow_id)
            .order_by(WorkflowVersion.version))).scalars())


async def _graph_of(workflow_id: str, version: int | None = None) -> dict:
    async with SessionLocal() as session:
        workflow = await session.get(Workflow, workflow_id)
        if version is None or version == workflow.version:
            return copy.deepcopy(workflow.graph)
        row = (await session.execute(select(WorkflowVersion).where(
            WorkflowVersion.workflow_id == workflow_id, WorkflowVersion.version == version))).scalars().one()
        return copy.deepcopy(row.graph)


async def ready_draft(client, documents, datasource, monkeypatch) -> dict:
    """一份起草好、已经接管到工作流上的口径草稿（GET 的形状，带 card_id / revision）。"""
    doc = await documents("guard-" + uuid.uuid4().hex)
    source_id = await datasource()
    fake_model(monkeypatch, plan_reply(f"{doc}-2"))
    created = await extract(client, doc, source_id)
    body = (await client.get(f"/api/ledger/drafts/{created['draft_id']}")).json()
    assert body["status"] == "ready", body
    assert body["workflow_id"], body
    return body


#: 这个文件自己建的工作流和蓝图都叫这个名字开头：跑完删掉，不给后面的用例留下一堆它们不认识的行
OWN_PREFIX = "K02 "


@pytest.fixture(autouse=True)
async def cleanup_own_rows():
    """本文件建的工作流（连同版本、幂等记录、草稿）和蓝图跑完删掉：别的用例会列它们、按计数断言。

    草稿本来由 documents fixture 收，这里只兜底自己的那几份（删过的再删一次是空操作）。
    """
    yield
    from app.db.models import CaliberWriteRequest

    async with SessionLocal() as session:
        workflow_ids = list((await session.execute(
            select(Workflow.id).where(Workflow.name.like(f"{OWN_PREFIX}%")))).scalars())
        if workflow_ids:
            for model in (CaliberWriteRequest, WorkflowVersion, CaliberDraft):
                rows = list((await session.execute(
                    select(model).where(model.workflow_id.in_(workflow_ids)))).scalars())
                for row in rows:
                    await session.delete(row)
            for workflow in list((await session.execute(
                    select(Workflow).where(Workflow.id.in_(workflow_ids)))).scalars()):
                await session.delete(workflow)
        for blueprint in list((await session.execute(
                select(Blueprint).where(Blueprint.name.like(f"{OWN_PREFIX}%")))).scalars()):
            await session.delete(blueprint)
        await session.commit()


def patch_url(draft_id: str) -> str:
    return f"/api/ledger/drafts/{draft_id}"


async def ready_draft_questions(client, documents, datasource, monkeypatch) -> dict:
    """一份带两个未决问题的草稿（问题挂在指标上，消掉它要落到图上）；消问题的路径用它。"""
    doc = await documents("guard-q-" + uuid.uuid4().hex)
    source_id = await datasource()
    plan = json.loads(plan_reply(f"{doc}-2"))
    plan["metrics"][0]["open_questions"] = ["按哪个时区结算", "按哪个门店口径"]
    fake_model(monkeypatch, json.dumps(plan, ensure_ascii=False))
    created = await extract(client, doc, source_id)
    body = (await client.get(f"/api/ledger/drafts/{created['draft_id']}")).json()
    assert sorted(body["metrics"][0]["open_questions"]) == ["按哪个时区结算", "按哪个门店口径"], body
    return body


# ==========================================================================
# 读草稿：卡和基准版本都是明确的
# ==========================================================================

async def test_the_draft_read_names_the_card_and_the_base_revision(client, documents, datasource, monkeypatch,
                                                                  fake_trial):
    body = await ready_draft(client, documents, datasource, monkeypatch)
    # 起草建出来的图只有一张可写的口径卡：读的时候直接给出它的 id，客户端不用猜
    assert body["card_id"] == "card"
    assert [card["id"] for card in body["cards"]] == ["card"]
    # revision 就是客户端要带回来的基准版本（Workflow.version）
    assert body["revision"] == body["version"] == 1
    assert (await _workflow(body["workflow_id"])).version == 1


async def test_a_draft_without_a_graph_keeps_the_plain_patch_path(client, monkeypatch):
    """没接管到工作流上的老草稿（连图都没有）照旧只改 payload：不要求基准版本，卡片也是空的。"""
    draft_id = uuid.uuid4().hex[:32]
    async with SessionLocal() as session:
        session.add(CaliberDraft(id=draft_id, title="老草稿", status="ready",
                                 payload={"metrics": [{"id": "m1", "name": "旧指标"}], "open_questions": []}))
        await session.commit()
    try:
        res = await client.patch(patch_url(draft_id), json={"metrics": [{"id": "m1", "name": "改过的"}]})
        assert res.status_code == 200, res.text
        assert res.json()["metrics"][0]["name"] == "改过的"
        assert res.json()["revision"] is None and res.json()["card_id"] is None
    finally:
        async with SessionLocal() as session:
            row = await session.get(CaliberDraft, draft_id)
            if row is not None:
                await session.delete(row)
                await session.commit()


# ==========================================================================
# 客户端版本检查：已接管的资产必须带基准版本，旧写不能盖新的
# ==========================================================================

async def test_a_taken_over_draft_refuses_a_write_without_a_base_revision(client, documents, datasource, monkeypatch,
                                                                        fake_trial):
    body = await ready_draft(client, documents, datasource, monkeypatch)
    metrics = copy.deepcopy(body["metrics"])
    metrics[0]["name"] = "没带基准版本"
    res = await client.patch(patch_url(body["id"]), json={"metrics": metrics})
    assert res.status_code == 409, res.text
    assert res.json()["code"] == "revision_required"
    assert res.json()["current_version"] == 1
    # 一个版本都没多出来，图也没改
    assert await _versions(body["workflow_id"]) == [1]
    assert cfg(await _graph_of(body["workflow_id"]), "card")["metrics"][0]["name"] == body["metrics"][0]["name"]


async def test_a_stale_page_cannot_overwrite_the_first_save(client, documents, datasource, monkeypatch, fake_trial):
    """E01：两个页面基于同一版先后保存，后一个旧写被拒，不覆盖已保存的内容。"""
    body = await ready_draft(client, documents, datasource, monkeypatch)
    first = copy.deepcopy(body["metrics"])
    first[0]["name"] = "先保存的"
    ok = await client.patch(patch_url(body["id"]),
                            json={"metrics": first, "expected_revision": body["revision"]})
    assert ok.status_code == 200, ok.text
    assert ok.json()["revision"] == 2 and ok.json()["metrics"][0]["name"] == "先保存的"

    # 第二个页面还拿着 v1：写不进去，服务端把当前版本交回，页面上的输入由客户端保留
    stale = copy.deepcopy(body["metrics"])
    stale[0]["name"] = "后保存的（基于旧页面）"
    old = await client.patch(patch_url(body["id"]),
                             json={"metrics": stale, "expected_revision": body["revision"]})
    assert old.status_code == 409, old.text
    assert old.json()["code"] == "stale_revision"
    assert old.json()["current_version"] == 2
    assert await _versions(body["workflow_id"]) == [1, 2]
    current = (await client.get(patch_url(body["id"]))).json()
    assert current["metrics"][0]["name"] == "先保存的" and current["revision"] == 2


async def test_the_canvas_cannot_write_a_taken_over_caliber_without_a_base_revision(
        client, documents, datasource, monkeypatch, fake_trial):
    """画布也是改同一份已接管资产的入口：同样要带基准版本，不能从这里绕过去。"""
    body = await ready_draft(client, documents, datasource, monkeypatch)
    graph = await _graph_of(body["workflow_id"])
    cfg(graph, "card")["metrics"][0]["name"] = "画布改的"
    url = f"/api/workflows/{body['workflow_id']}"
    refused = await client.patch(url, json={"graph": graph})
    assert refused.status_code == 409, refused.text
    assert refused.json()["code"] == "revision_required"
    assert await _versions(body["workflow_id"]) == [1]

    saved = await client.patch(url, json={"graph": graph, "expected_revision": body["revision"]})
    assert saved.status_code == 200, saved.text
    assert saved.json()["version"] == 2
    # 旧页面（还拿着 v1）再存一次：409 stale_revision，不覆盖
    cfg(graph, "card")["metrics"][0]["name"] = "又一个旧页面"
    again = await client.patch(url, json={"graph": graph, "expected_revision": body["revision"]})
    assert again.status_code == 409 and again.json()["code"] == "stale_revision"
    assert again.json()["current_version"] == 2

    # 没接管的（手建）工作流照旧可以不带基准版本保存，老客户端不受影响
    plain = (await client.post("/api/workflows", json={"name": "K02 手建 " + uuid.uuid4().hex[:6],
                                                       "graph": _graph("a")})).json()
    assert (await client.patch(f"/api/workflows/{plain['id']}", json={"graph": _graph("b")})).status_code == 200


# ==========================================================================
# 卡级目标：多张卡要指明，指到哪张改哪张
# ==========================================================================

async def test_a_named_card_is_the_one_that_gets_written(client, documents, datasource, monkeypatch, fake_trial):
    body = await ready_draft(client, documents, datasource, monkeypatch)
    wid = body["workflow_id"]
    # 画布上加了第二张可写的口径卡
    graph = await _graph_of(wid)
    metrics = cfg(graph, "card")["metrics"]
    graph["nodes"].append({"id": "card_2", "type": "metrics", "position": {"x": 0, "y": 200},
                           "data": {"label": "另一张卡", "config": {"metrics": [{**metrics[0], "id": "m9",
                                                                               "name": "另一张卡上的指标"}]}}})
    saved = await client.patch(f"/api/workflows/{wid}",
                               json={"graph": graph, "expected_revision": body["revision"]})
    assert saved.status_code == 200, saved.text

    # 读草稿仍然给出契约声明的那张卡（不是「第一张」的猜测）；候选两张都列出来
    fresh = (await client.get(patch_url(body["id"]))).json()
    assert fresh["card_id"] == "card"
    assert [card["id"] for card in fresh["cards"]] == ["card", "card_2"]

    # 多张可写的卡、又不指明目标：拒（不做「取第一张」）
    edited = copy.deepcopy(fresh["metrics"])
    edited[0]["name"] = "没指明目标"
    refused = await client.patch(patch_url(body["id"]),
                                 json={"metrics": edited, "expected_revision": fresh["revision"]})
    assert refused.status_code == 409, refused.text
    assert refused.json()["code"] == "card_required"
    assert [card["id"] for card in refused.json()["cards"]] == ["card", "card_2"]

    # 指到没有的卡：拒
    missing = await client.patch(patch_url(body["id"]),
                                 json={"metrics": edited, "expected_revision": fresh["revision"],
                                       "target": {"card_id": "card-nope"}})
    assert missing.status_code == 409 and missing.json()["code"] == "card_not_found"

    # 指到别的工作流：拒
    elsewhere = await client.patch(patch_url(body["id"]),
                                   json={"metrics": edited, "expected_revision": fresh["revision"],
                                         "target": {"workflow_id": "wf-elsewhere", "card_id": "card_2"}})
    assert elsewhere.status_code == 409 and elsewhere.json()["code"] == "target_mismatch"

    # 指明 card_2：改的是它，card 一个字段不动
    written = await client.patch(patch_url(body["id"]),
                                 json={"metrics": [{"id": "m9", "name": "改在第二张卡上"}],
                                       "expected_revision": fresh["revision"],
                                       "target": {"workflow_id": wid, "card_id": "card_2"}})
    assert written.status_code == 200, written.text
    latest = await _graph_of(wid)
    assert cfg(latest, "card_2")["metrics"][0]["name"] == "改在第二张卡上"
    assert cfg(latest, "card")["metrics"][0]["name"] == metrics[0]["name"]


# ==========================================================================
# 幂等：同一个 request_id 只落一次
# ==========================================================================

async def test_resolved_order_does_not_look_like_another_request(client, documents, datasource, monkeypatch,
                                                                fake_trial):
    """勾选顺序变一下不该被当成另一次请求：指纹按集合归一（草稿页也把勾选去空白、去重、排序之后再发）。"""
    body = await ready_draft_questions(client, documents, datasource, monkeypatch)
    questions = ["按哪个时区结算", "按哪个门店口径"]
    first = await client.patch(patch_url(body["id"]),
                               json={"resolved": questions, "expected_revision": body["revision"],
                                     "request_id": "resolved-0001"})
    assert first.status_code == 200, first.text
    assert first.json()["revision"] == 2 and first.json().get("replayed") is None

    # 同一批问题，顺序倒过来、还带一条重复的：同键同内容（重放），不是冲突、也不新增版本
    again = await client.patch(patch_url(body["id"]),
                               json={"resolved": [questions[1], questions[0], questions[1]],
                                     "expected_revision": body["revision"], "request_id": "resolved-0001"})
    assert again.status_code == 200, again.text
    assert again.json()["replayed"] is True
    assert await _versions(body["workflow_id"]) == [1, 2]


async def test_the_same_request_id_replays_without_a_new_version(client, documents, datasource, monkeypatch, fake_trial):
    body = await ready_draft(client, documents, datasource, monkeypatch)
    metrics = copy.deepcopy(body["metrics"])
    metrics[0]["name"] = "幂等改一次"
    request = {"metrics": metrics, "expected_revision": body["revision"], "request_id": "change-0001"}

    first = await client.patch(patch_url(body["id"]), json=request)
    assert first.status_code == 200, first.text
    assert first.json()["revision"] == 2 and first.json().get("replayed") is None

    # 同键同内容重发：基准版本这时已经过期，但幂等在版本检查之前判，照旧成功，且不再新增版本
    again = await client.patch(patch_url(body["id"]), json=request)
    assert again.status_code == 200, again.text
    assert again.json()["replayed"] is True and again.json()["revision"] == 2
    assert await _versions(body["workflow_id"]) == [1, 2]

    # 同键不同内容：冲突，也不新增版本
    other = await client.patch(patch_url(body["id"]),
                               json={**request, "metrics": [{**metrics[0], "name": "换了内容"}]})
    assert other.status_code == 409, other.text
    assert other.json()["code"] == "idempotency_conflict"
    assert await _versions(body["workflow_id"]) == [1, 2]

    # 换一个键、内容也有变化：照常写一版
    third = await client.patch(patch_url(body["id"]),
                               json={"metrics": [{**metrics[0], "name": "第二次修改"}],
                                     "expected_revision": 2, "request_id": "change-0002"})
    assert third.status_code == 200 and third.json()["revision"] == 3
    assert await _versions(body["workflow_id"]) == [1, 2, 3]


# ==========================================================================
# 没碰过的配置保留
# ==========================================================================

async def test_untouched_config_survives_a_metrics_patch(client, documents, datasource, monkeypatch, fake_trial):
    body = await ready_draft(client, documents, datasource, monkeypatch)
    wid = body["workflow_id"]
    before = await _graph_of(wid)
    # 图上先有别人加的扩展字段（前端不认识的那些）
    graph = copy.deepcopy(before)
    graph["extensions"] = {"unknown": True}
    cfg(graph, "fetch")["args"]["hints"] = {"keep": "me"}
    saved = await client.patch(f"/api/workflows/{wid}",
                               json={"graph": graph, "expected_revision": body["revision"]})
    assert saved.status_code == 200, saved.text

    metrics = copy.deepcopy((await client.get(patch_url(body["id"]))).json()["metrics"])
    metrics[0]["name"] = "只改名字"
    res = await client.patch(patch_url(body["id"]),
                             json={"metrics": metrics, "expected_revision": saved.json()["version"],
                                   "request_id": "keep-config"})
    assert res.status_code == 200, res.text
    after = await _graph_of(wid)
    assert cfg(after, "fetch") == cfg(graph, "fetch")
    assert cfg(after, "start") == cfg(graph, "start")
    assert cfg(after, "write") == cfg(graph, "write")
    assert after["extensions"] == {"unknown": True}
    assert cfg(after, "card")["metrics"][0]["name"] == "只改名字"
    assert cfg(after, "card")["metrics"][0]["expression"] == cfg(before, "card")["metrics"][0]["expression"]


# ==========================================================================
# 回滚与发布单：同一份资产、同一条 CAS
# ==========================================================================

async def test_restore_obeys_the_same_revision_rules(client):
    """回滚也是改图：已接管的资产要带基准版本，旧没带的、过期的都拒；带对了照旧回滚成新版本。"""
    wid = (await client.post("/api/workflows", json={"name": "K02 回滚 " + uuid.uuid4().hex[:6],
                                                     "graph": _graph("旧图")})).json()["id"]
    assert (await client.patch(f"/api/workflows/{wid}", json={"graph": _graph("新图")})).status_code == 200
    # 已经接管到工作流上（文档起草建出来的口径卡）
    async with SessionLocal() as session:
        workflow = await session.get(Workflow, wid)
        workflow.origin = {"kind": "doc_draft"}
        await session.commit()

    url = f"/api/workflows/{wid}/versions/1/restore"
    missing = await client.post(url)
    assert missing.status_code == 409, missing.text
    assert missing.json()["code"] == "revision_required" and missing.json()["current_version"] == 2

    stale = await client.post(url, json={"expected_revision": 1})
    assert stale.status_code == 409, stale.text
    assert stale.json()["code"] == "stale_revision" and stale.json()["current_version"] == 2
    assert await _versions(wid) == [1, 2]

    ok = await client.post(url, json={"expected_revision": 2})
    assert ok.status_code == 200, ok.text
    assert ok.json()["version"] == 3 and ok.json()["graph"] == _graph("旧图")
    assert ok.json()["status"] == "draft"
    assert await _versions(wid) == [1, 2, 3]


async def test_the_publish_plan_stops_when_the_canvas_moved_after_the_plan(client):
    """发布单的「保存版本」和画布同一条 CAS：出单之后画布又改过，就停在这一步，不把别人刚改的图盖掉。"""
    from app.blueprints.publish_plan import GRAPH_CHANGED, StepFailed, _Target, _do_save_version

    planned_graph = _graph("蓝图图")
    wid = (await client.post("/api/workflows", json={"name": "K02 发布单 " + uuid.uuid4().hex[:6],
                                                     "graph": _graph("一开始的图")})).json()["id"]
    async with SessionLocal() as session:
        session.add(Blueprint(id=uuid.uuid4().hex[:32], name="K02 发布单", scope="shared",
                              spec={"template": {"id": "board", "version": 1}}, graph=planned_graph,
                              graph_hash=graph_hash(planned_graph), workflow_id=wid))
        await session.commit()
        planned_at = (await session.get(Workflow, wid)).version  # 出单时工作流是这一版

    # 出单之后画布又存了一版：这次发布单要发的还是蓝图那张图，不能盖掉画布的修改
    saved = await client.patch(f"/api/workflows/{wid}",
                               json={"graph": _graph("画布改过的图"), "expected_revision": planned_at})
    assert saved.status_code == 200 and saved.json()["version"] == 2, saved.text
    async with SessionLocal() as session:
        workflow = await session.get(Workflow, wid)
        blueprint = (await session.execute(select(Blueprint).where(Blueprint.workflow_id == wid))).scalars().one()
        ctx = SimpleNamespace(bp=blueprint, options={"workflow_revision": planned_at},
                              target=_Target(graph=planned_graph, digest=graph_hash(planned_graph), app_spec={},
                                             schedule=None, workflow=workflow, version=workflow.version + 1,
                                             reused=False, name=workflow.name))
        with pytest.raises(StepFailed) as refused:
            await _do_save_version(session, ctx)
        assert refused.value.code == GRAPH_CHANGED
        await session.rollback()
    current = await _workflow(wid)
    assert current.version == 2 and current.graph == _graph("画布改过的图")
    assert await _versions(wid) == [1, 2]

    # 按现在这一版重新出单：照常存一版（蓝图那张图）
    async with SessionLocal() as session:
        workflow = await session.get(Workflow, wid)
        blueprint = (await session.execute(select(Blueprint).where(Blueprint.workflow_id == wid))).scalars().one()
        ctx = SimpleNamespace(bp=blueprint, options={"workflow_revision": workflow.version},
                              target=_Target(graph=planned_graph, digest=graph_hash(planned_graph), app_spec={},
                                             schedule=None, workflow=workflow, version=workflow.version + 1,
                                             reused=False, name=workflow.name))
        out = await _do_save_version(session, ctx)
    assert out == {"workflow_id": wid, "version": 3, "graph_hash": graph_hash(planned_graph), "reused": False}
    assert (await _workflow(wid)).graph == planned_graph
    assert await _versions(wid) == [1, 2, 3]


async def test_the_shared_cas_refuses_a_stale_base_revision(client):
    """save_graph / append_version 是同一条 CAS：基准版本过期、或者另一处先提交了新版本，都是 StaleRevision。"""
    from app.apps.versions import StaleRevision, append_version, save_graph

    wid = (await client.post("/api/workflows", json={"name": "K02 CAS " + uuid.uuid4().hex[:6],
                                                     "graph": _graph("a")})).json()["id"]
    async with SessionLocal() as session:
        workflow = await session.get(Workflow, wid)
        with pytest.raises(StaleRevision) as stale:
            await save_graph(session, workflow, _graph("b"), expected_revision=workflow.version + 1)
        assert stale.value.current == 1
        await session.rollback()

    async with SessionLocal() as first, SessionLocal() as second:
        stale_row = await first.get(Workflow, wid)
        current = await second.get(Workflow, wid)
        await append_version(second, current, _graph("先到的"), note="先到的")
        await second.commit()
        with pytest.raises(StaleRevision) as lost:
            await append_version(first, stale_row, _graph("后到的"), note="后到的")
        assert lost.value.current == 2
        await first.rollback()
    assert (await _workflow(wid)).graph == _graph("先到的")
    assert await _versions(wid) == [1, 2]
