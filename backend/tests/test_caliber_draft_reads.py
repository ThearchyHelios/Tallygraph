"""已接管草稿按图读、老草稿如实说没有来源基线（K03/K05 的前置边界）。

试跑（POST /api/ledger/drafts/{id}/trial）对 docattach 那种已接管草稿（tags 有 doc_draft、origin 里没有 kind）读的是
**当前图版本**和**当前图里的入口字段**，不是 payload 里起草/接上那一刻的快照：

- 图上有、payload 里没有的字段能填；payload 里的旧字段名现在是「不认识的入口字段」（422）；
- 必填字段没给值 → needs_input，**一条 SQL 都不发**（运行表里不多一行）；
- 数来自当前版本（v2）的 SQL，不是 payload 里写的那一版；
- 跑完试跑不判 stale（它记的就是当前这一版）。

来源基线（K05 的读取侧）：草稿详情对没有可还原快照的老草稿如实给 baseline_unknown（不拿它现在的内容冒充「上次被
接受的导入值」）；payload 里已经有的快照和基线照原样交回，不重算、不另补。

真引擎跑一次（run_manager.setup + tests/ledger_samples 的真实 SQLite 店铺库）：探针图里没有报告节点，不发模型请求。
"""
from __future__ import annotations

import copy
import uuid

import pytest
from sqlalchemy import delete, func, select

from app.core.artifact_store import graph_hash
from app.db.base import SessionLocal
from app.db.models import CaliberDraft, Run, Workflow, WorkflowVersion
from app.engine.runner import run_manager
from tests import ledger_samples as S
from tests.test_ledger_docdraft_api import client  # noqa: F401 - fixture 和起草用例共用

#: 这个文件自己建的工作流（连同版本、草稿）跑完删掉
OWN_PREFIX = "K03 读真源 "


@pytest.fixture(autouse=True)
async def cleanup_own_rows():
    yield
    async with SessionLocal() as session:
        workflow_ids = list((await session.execute(
            select(Workflow.id).where(Workflow.name.like(f"{OWN_PREFIX}%")))).scalars())
        if workflow_ids:
            for model in (WorkflowVersion, CaliberDraft):
                await session.execute(delete(model).where(model.workflow_id.in_(workflow_ids)))
            await session.execute(delete(Workflow).where(Workflow.id.in_(workflow_ids)))
        await session.commit()


#: 取数 SQL 里的那个加数：v2 比 v1 多 1000，跑出来的数一眼看得出是哪一版
BUMP = 1000.0


def graph(sql: str, *, fields: list[dict] | None = None) -> dict:
    """一份能派生探针的口径图：入口 → 取数 → 口径卡（入口字段照给，缺省是 start / end 两个必填）。"""
    return S.chain(
        S.node("start", "input", fields=fields if fields is not None else [
            {"name": "start", "required": True, "description": "开始日期"},
            {"name": "end", "required": True, "description": "结束日期"}]),
        S.node("fetch", "tool", tool=S.tool(), args={"sql": sql}),
        S.node("card", "metrics", caliber="周销售", caliber_version="v1",
               metrics=[{"id": "gmv", "name": "销售额", "unit": "元", "decimals": 2,
                         "expression": "cell(nodes.fetch, 0, 'gmv')"}]),
    )


async def attached_draft(*, graph: dict, payload: dict) -> dict:
    """一份像 docattach 产物那样已接管、但 payload 还停在旧快照上的草稿（tags 有 doc_draft、origin 没有 kind）。"""
    workflow_id = f"wf-{uuid.uuid4().hex[:12]}"
    draft_id = uuid.uuid4().hex[:32]
    async with SessionLocal() as session:
        session.add(Workflow(id=workflow_id, name=f"{OWN_PREFIX}{uuid.uuid4().hex[:6]}", status="draft", graph=graph,
                             version=1, tags=["doc_draft"],
                             origin={"run_id": "run-1", "freeze": True, "sources": [{"run_id": "run-1"}]}))
        session.add(WorkflowVersion(workflow_id=workflow_id, version=1, graph=graph, graph_hash=graph_hash(graph)))
        await session.flush()          # 草稿的外键指着工作流：先落工作流那一行
        session.add(CaliberDraft(id=draft_id, title="接上来的口径", status="ready", workflow_id=workflow_id,
                                 payload={"version": 1, "workflow_id": workflow_id, **copy.deepcopy(payload)}))
        await session.commit()
    return {"id": draft_id, "workflow_id": workflow_id, "revision": 1, "card_id": "card"}


async def runs_of(workflow_id: str) -> int:
    async with SessionLocal() as session:
        return int(await session.scalar(select(func.count(Run.id)).where(Run.workflow_id == workflow_id)) or 0)


# ==========================================================================
# 试跑：当前图版本、当前入口字段、缺参不发 SQL
# ==========================================================================

async def test_the_trial_of_a_taken_over_draft_reads_the_current_graph_not_the_payload(client, tmp_path):
    """docattach 那种已接管草稿：试跑按当前图版本和当前入口字段跑，payload 里的旧快照一个字都不算数。"""
    await run_manager.setup()
    try:
        await S.fresh_source(tmp_path / "shop.db")
        # 起草/接上那一刻的 payload：入口字段是 store，版本停在 1，SQL 是另一句（跑到它身上一眼看得出）
        body = await attached_draft(
            graph=graph(S.TEMPLATE), payload={
                "sql": "SELECT 1 AS gmv", "entry_fields": [{"name": "store", "description": "旧快照里的字段"}],
                "metrics": [{"id": "m1", "name": "旧指标", "expression": "cell(nodes.fetch, 0, 'gmv')"}]})

        # 画布上改了取数（加了 1000）：新的图版本就是当前这一版
        saved = await client.patch(f"/api/workflows/{body['workflow_id']}", json={
            "graph": graph(S.TEMPLATE.replace("SELECT SUM(amount) AS gmv", f"SELECT SUM(amount) + {BUMP} AS gmv")),
            "expected_revision": body["revision"]})
        assert saved.status_code == 200, saved.text
        assert saved.json()["version"] == 2

        # 入口字段按图里的：payload 里那个旧字段现在是不认识的字段（不回退旧快照）
        stale_field = await client.post(f"/api/ledger/drafts/{body['id']}/trial",
                                        json={"inputs": {"store": "east", "start": "2026-09-01", "end": "2026-09-08"}})
        assert stale_field.status_code == 422, stale_field.text
        assert "不认识的入口字段" in stale_field.text and "store" in stale_field.text

        # 必填没给：表单校验就拒（422，和别的入口同一套），一条 SQL 都不发（运行表里不多行）
        before = await runs_of(body["workflow_id"])
        missing = await client.post(f"/api/ledger/drafts/{body['id']}/trial", json={"inputs": {}})
        assert missing.status_code == 422, missing.text
        assert "start" in missing.text and "end" in missing.text
        assert await runs_of(body["workflow_id"]) == before

        # 补齐入口：真的跑一次，数来自当前版本（170.5 + 1000）
        done = await client.post(f"/api/ledger/drafts/{body['id']}/trial",
                                 json={"inputs": {"start": "2026-09-01", "end": "2026-09-08"}})
        assert done.status_code == 200, done.text
        trial = done.json()
        assert trial["status"] == "succeeded", trial.get("error")
        assert trial["version"] == 2 and trial["error"] == ""
        assert float(trial["metrics"][0]["value"]) == 1170.5
        assert [query["node_id"] for query in trial["queries"]] == ["fetch"]

        # 草稿详情和试跑都认这一版：图里的入口字段和 SQL 就是现在这一版，试跑不算 stale
        made = (await client.get(f"/api/ledger/drafts/{body['id']}")).json()
        assert [field["name"] for field in made["entry_fields"]] == ["start", "end"]
        assert "1000" in made["sql"] and made["version"] == 2
        assert made["trial"]["status"] == "succeeded"
        assert (await client.get(f"/api/ledger/drafts/{body['id']}/trial")).json()["status"] == "succeeded"
    finally:
        await run_manager.shutdown()


async def test_the_trial_of_a_taken_over_draft_reports_a_derivation_problem_without_running(client, tmp_path):
    """图本身派生不出来（入口字段没声明、模板引用了它）时照实说，也不发起运行。"""
    await run_manager.setup()
    try:
        await S.fresh_source(tmp_path / "shop.db")
        sql = S.TEMPLATE.replace("{{ input.end }}", "{{ input.undefined_param }}")
        body = await attached_draft(graph=graph(sql), payload={"entry_fields": [], "sql": "SELECT 1 AS gmv"})
        before = await runs_of(body["workflow_id"])
        res = await client.post(f"/api/ledger/drafts/{body['id']}/trial",
                                json={"inputs": {"start": "2026-09-01", "end": "2026-09-08"}})
        assert res.status_code == 200, res.text
        assert res.json()["status"] == "failed" and "undefined_param" in res.json()["error"]
        assert await runs_of(body["workflow_id"]) == before
    finally:
        await run_manager.shutdown()


async def test_a_parameter_with_no_value_stops_before_any_sql(client, tmp_path):
    """可选、但没声明缺省值的入口被 SQL 用着：这次没给就是缺参数（needs_input），一条 SQL 都不发。

    和 K01 的参数完整性同一套：声明了 `""` 缺省值的（「空值不过滤」那种写法）照缺省值渲染，不算缺参。
    """
    await run_manager.setup()
    try:
        await S.fresh_source(tmp_path / "shop.db")
        fields = [{"name": "start", "required": True}, {"name": "end", "required": True},
                  {"name": "region", "type": "string", "description": "区域（可选；没声明缺省值）"}]
        sql = ("SELECT SUM(amount) AS gmv FROM orders WHERE status = 'paid' "
               "AND paid_at >= '{{ input.start }}' AND paid_at < '{{ input.end }}' "
               "AND ('{{ input.region }}' = '' OR region = '{{ input.region }}')")
        body = await attached_draft(graph=graph(sql, fields=fields), payload={"sql": "SELECT 1 AS gmv"})
        before = await runs_of(body["workflow_id"])
        res = await client.post(f"/api/ledger/drafts/{body['id']}/trial",
                                json={"inputs": {"start": "2026-09-01", "end": "2026-09-08"}})
        assert res.status_code == 200, res.text
        assert res.json()["status"] == "needs_input" and "region" in res.json()["error"]
        assert await runs_of(body["workflow_id"]) == before

        # 声明 `""` 缺省值的可选入口（空值不过滤）：不给也能跑，SQL 照缺省值渲染
        open_graph = graph(sql, fields=[{**fields[0]}, {**fields[1]}, {**fields[2], "default": ""}])
        async with SessionLocal() as session:
            workflow = await session.get(Workflow, body["workflow_id"])
            workflow.graph, workflow.version = open_graph, 2
            session.add(WorkflowVersion(workflow_id=body["workflow_id"], version=2, graph=open_graph,
                                        graph_hash=graph_hash(open_graph)))
            await session.commit()
        done = await client.post(f"/api/ledger/drafts/{body['id']}/trial",
                                 json={"inputs": {"start": "2026-09-01", "end": "2026-09-08"}})
        assert done.status_code == 200, done.text
        assert done.json()["status"] == "succeeded" and float(done.json()["metrics"][0]["value"]) == 170.5
    finally:
        await run_manager.shutdown()


async def test_a_really_attached_draft_runs_its_trial_from_the_current_graph(client, monkeypatch, tmp_path):
    """真走一次 docattach：接上来的草稿再试跑，读的是接上之后图上又改过的那一版，payload 的旧字段不算数。

    和上面那条一样的几条断言，但工作流是 `docattach.attach` 真建出来的（问数据 → 比对 → 接上），不是手搭的形状。
    """
    from app.ledger import docattach
    from tests.test_caliber_docdraft_attach import ask, document, payload as attached

    await run_manager.setup()
    try:
        await S.fresh_source(tmp_path / "shop.db")
        run = await ask(monkeypatch)
        doc_id, chunk_id = await document()
        draft_id = uuid.uuid4().hex[:32]
        definition = attached(doc_id, chunk_id)
        async with SessionLocal() as session:
            session.add(CaliberDraft(id=draft_id, title="周销售口径", status="ready", payload=definition))
            await session.commit()
        async with SessionLocal() as session:
            made = await docattach.attach(session, payload=definition, name="周销售口径", run_id=run.id,
                                          draft_id=draft_id)
        workflow_id = made["workflow_id"]

        # 接上来之后画布又改过：图上加了入口字段 region，取数按它筛（这一版才是当前版）
        async with SessionLocal() as session:
            workflow = await session.get(Workflow, workflow_id)
            current = copy.deepcopy(workflow.graph)
            draft = await session.get(CaliberDraft, draft_id)
            # payload 停在接上那一刻的旧快照：一个旧字段名、版本 1
            draft.payload = {**draft.payload, "workflow_id": workflow_id, "version": 1,
                             "entry_fields": [{"name": "store", "description": "旧快照里的字段"}]}
            draft.workflow_id, draft.status = workflow_id, "attached"
            await session.commit()
        region_sql = ("SELECT SUM(amount) AS gmv FROM orders WHERE status = 'paid' "
                      "AND region = '{{ input.region }}'")
        for node in current["nodes"]:
            if node["type"] == "input":
                node["data"]["config"]["fields"] = [{"name": "region", "type": "string", "required": True,
                                                     "description": "地区"}]
            elif node["type"] == "tool":
                node["data"]["config"]["args"]["sql"] = region_sql
        saved = await client.patch(f"/api/workflows/{workflow_id}", json={"graph": current, "expected_revision": 1})
        assert saved.status_code == 200 and saved.json()["version"] == 2, saved.text

        # 入口字段按图里的：payload 里的旧字段名是不认识的字段
        stale_field = await client.post(f"/api/ledger/drafts/{draft_id}/trial",
                                        json={"inputs": {"store": "east"}})
        assert stale_field.status_code == 422, stale_field.text
        assert "不认识的入口字段" in stale_field.text and "store" in stale_field.text

        before = await runs_of(workflow_id)
        missing = await client.post(f"/api/ledger/drafts/{draft_id}/trial", json={"inputs": {}})
        assert missing.status_code == 422 and "region" in missing.text        # 必填没给：不发 SQL
        assert await runs_of(workflow_id) == before

        done = await client.post(f"/api/ledger/drafts/{draft_id}/trial", json={"inputs": {"region": "east"}})
        assert done.status_code == 200, done.text
        assert done.json()["status"] == "succeeded", done.json().get("error")
        assert done.json()["version"] == 2 and float(done.json()["metrics"][0]["value"]) == 156.5
        assert (await client.get(f"/api/ledger/drafts/{draft_id}")).json()["trial"]["status"] == "succeeded"
    finally:
        await run_manager.shutdown()


# ==========================================================================
# 来源基线：老草稿如实说 unknown，已记的快照照原样
# ==========================================================================

async def test_an_old_draft_says_its_source_baseline_is_unknown(client):
    """没有可还原快照的老草稿：草稿详情给 baseline_unknown，也不凭空补一条快照。"""
    draft_id = uuid.uuid4().hex[:32]
    async with SessionLocal() as session:
        session.add(CaliberDraft(id=draft_id, title="老草稿", status="path_pending",
                                 payload={"metrics": [{"id": "m1", "name": "旧指标"}], "sql": "SELECT 1 AS n",
                                          "version": 1, "workflow_id": None}))
        await session.commit()
    try:
        body = (await client.get(f"/api/ledger/drafts/{draft_id}")).json()
        assert body["metrics"][0]["name"] == "旧指标"
        assert "source_snapshot" not in body
        assert body["source_baseline"]["status"] == "baseline_unknown"
        assert "无法还原" in body["source_baseline"]["reason"] and "artifact_id" not in body["source_baseline"]
    finally:
        async with SessionLocal() as session:
            row = await session.get(CaliberDraft, draft_id)
            if row is not None:
                await session.delete(row)
                await session.commit()


async def test_a_baseline_is_recomputed_from_the_artifact_and_never_read_from_the_payload(client):
    """反例（复核）：payload 里写着 known、工件却不在（或被人改过）时，读草稿要照实说 baseline_unknown。

    来源基线是**读工件现算**的：工件读得回来才 known（readable=true，段落以工件为准），读不回来就是 unknown
    （readable=false，带上那个工件 id 和原因），不照抄 payload 里存过的状态，也不抛出去把页面弄崩。"""
    from app.core.artifact_store import put_json
    from app.core.config import settings
    from app.ledger import docdraft

    draft_id = uuid.uuid4().hex[:32]
    snapshot = docdraft.source_snapshot(documents=[{"document_id": "d1", "content": "退款率按已支付订单统计"}],
                                        chunks={"d1-0": {"document_id": "d1", "ordinal": 0,
                                                         "content": "退款率按已支付订单统计"}})
    artifact_id = await put_json(snapshot, kind=docdraft.SNAPSHOT_KIND)
    ref = docdraft.snapshot_ref(artifact_id, snapshot)
    # payload 里存着的那份状态是「当时算出来的」，工件真伪由读的时候现验
    stale = {"status": "known", "artifact_id": artifact_id, "version": 1, "chunk_ids": ["d1-0"], "note": "起草时写的"}
    async with SessionLocal() as session:
        session.add(CaliberDraft(id=draft_id, title="有基线的草稿", status="ready",
                                 payload={"metrics": [{"id": "m1", "name": "退款率"}],
                                          "source_snapshot": copy.deepcopy(ref),
                                          "source_baseline": copy.deepcopy(stale)}))
        await session.commit()
    path = settings.data_dir / "artifacts" / artifact_id[:2] / f"{artifact_id}.json"
    original = path.read_bytes()
    try:
        body = (await client.get(f"/api/ledger/drafts/{draft_id}")).json()
        assert body["source_snapshot"] == ref
        # 工件读得回来：known（readable=true），段落以工件为准，不照抄 payload 里那份（note 不再出现）
        assert body["source_baseline"] == {"status": "known", "readable": True, "artifact_id": artifact_id,
                                           "version": docdraft.SNAPSHOT_VERSION, "chunk_ids": ["d1-0"]}
        assert docdraft.source_baseline(body) == body["source_baseline"]

        path.write_text('{"kind": "caliber_source_snapshot", "tampered": true}', encoding="utf-8")
        tampered = (await client.get(f"/api/ledger/drafts/{draft_id}")).json()
        assert tampered["source_baseline"]["status"] == "baseline_unknown"
        assert tampered["source_baseline"]["readable"] is False
        assert tampered["source_baseline"]["artifact_id"] == artifact_id      # 说得出是哪一个工件读不回来
        assert "读不回来" in tampered["source_baseline"]["reason"]
        assert (await client.get(f"/api/ledger/drafts/{draft_id}/trial")).status_code in (200, 404)
        assert (await client.get(f"/api/ledger/drafts/{draft_id}/validations")).status_code == 200

        path.unlink()
        gone = (await client.get(f"/api/ledger/drafts/{draft_id}")).json()
        assert gone["source_baseline"]["status"] == "baseline_unknown"
        assert gone["source_baseline"]["readable"] is False
    finally:
        path.write_bytes(original)
        async with SessionLocal() as session:
            row = await session.get(CaliberDraft, draft_id)
            if row is not None:
                await session.delete(row)
                await session.commit()
    assert (await client.get(f"/api/ledger/drafts/{draft_id}")).status_code == 404
