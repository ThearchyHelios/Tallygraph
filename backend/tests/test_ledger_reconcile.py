"""口径对账：差多少（next-round 02 做法 6，app/ledger/reconcile.py、ledger/derive.reconcile_graph）。

真的跑探索运行（虚构店铺库、mock 模型写报告）、等后台比对做完，再点开对账：
- 动作：有相关口径的查询给「看差多少」；参数是按「同一列、同一运算符」猜的，要先核对（needs_check）；
- 不核对直接发起回 409（要核对参数）；核对过的参数发起：派生图是确定的、过 validate、出口契约 metrics_from 指向对账卡、
  不加报告；跑出来的这次、口径、差值、相对差都对，差值的出处链点得开；GET 账本列出对过的账；统计 E2 数到它；
- 认不出哪一列对哪个指标（口径的指标式不是取数的一格）：要人选，选了照记进 origin；
- 多行结果：被引用的行按维度列经合并查询对齐，每行一组对账指标；维度是遮罩列、合并键不唯一时照实拒绝。
数据是虚构店铺库。
"""
from __future__ import annotations

import contextlib
from pathlib import Path
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete

from app.db.base import SessionLocal, utcnow
from app.db.models import Run, Workflow, WorkflowVersion
from app.engine.runner import run_manager
from app.engine.schema import GraphSpec, validate_graph
from app.ledger import match as ledger_match
from app.ledger import reconcile as ledger_reconcile
from app.main import app
from tests import ledger_samples as S

BY_REGION = ("SELECT region AS 区域, SUM(amount) AS gmv FROM orders WHERE status = 'paid' "
             "AND paid_at >= '{{ input.start }}' AND paid_at < '{{ input.end }}' GROUP BY region ORDER BY region")
BY_REGION_ALL = ("SELECT region AS 区域, SUM(amount) AS gmv FROM orders WHERE paid_at >= '2026-09-01' "
                 "AND paid_at < '2026-09-08' GROUP BY region ORDER BY region")
CHECKED = {"start": "2026-09-01", "end": "2026-09-08"}


@pytest.fixture(autouse=True)
async def engine_up(tmp_path):
    await run_manager.setup()
    path = tmp_path / "shop.db"
    await S.fresh_source(path)
    yield path
    await ledger_match.drain()
    await run_manager.shutdown()


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def ledger_of(client, run_id: str) -> dict:
    res = await client.get(f"/api/runs/{run_id}/ledger")
    assert res.status_code == 200, res.text
    return res.json()


async def card_of(run_id: str, node_id: str = "reconcile") -> dict:
    from app.api.evidence import _sealed, _sealed_cards

    sealed = await _sealed(run_id)
    [(_, card)] = [(a, c) for n, a, c in _sealed_cards(sealed) if n == node_id]
    return {m["id"]: m for m in card["metrics"]}


async def reconcile(client, run_id: str, body: dict) -> Run:
    res = await client.post(f"/api/ledger/answers/{run_id}/reconcile", json=body)
    assert res.status_code == 201, res.text
    return await S.wait(res.json()["run_id"])


async def test_reconcile_the_refunds_answer_against_the_weekly_caliber(client, monkeypatch):
    wf = await S.publish()
    row = await S.explore(monkeypatch, S.WITH_REFUNDS)
    body = await ledger_of(client, row.id)
    [action] = [a for a in body["actions"] if a["kind"] == "reconcile"]
    assert action["workflow_id"] == wf and action["version"] == 1 and action["node_id"] == "fetch"
    assert action["needs_check"] is True and action["check_reason"] == ledger_reconcile.CHECK_REASON
    assert action["binding"] == [{"field": "end", "value": "2026-09-08"}, {"field": "start", "value": "2026-09-01"}]
    query = action["query"]

    # 参数是猜的：不核对就发起，照实拒绝
    res = await client.post(f"/api/ledger/answers/{row.id}/reconcile", json={"query": query, "workflow_id": wf,
                                                                              "version": 1})
    assert res.status_code == 409 and res.json()["code"] == ledger_reconcile.CHECK_INPUTS
    assert res.json()["detail"] == ledger_reconcile.CONFIRM_FIRST

    done = await reconcile(client, row.id, {"query": query, "workflow_id": wf, "version": 1, "inputs": CHECKED,
                                            "confirm": True})
    assert done.status == "succeeded", done.error
    assert done.origin_kind == "reconcile" and done.run_class == "exploratory" and done.workflow_id is None
    origin = done.origin
    assert origin["source_run"] == row.id and origin["query"] == query and origin["inputs_from"] == "checked"
    assert origin["pairs"] == [{"ours": "gmv", "metric": "gmv"}] and origin["caliber"] == f"{wf}@1"
    assert done.input == {"start": "2026-09-01", "end": "2026-09-08", "store": "all"}

    graph = done.graph
    ids = [n["id"] for n in graph["nodes"]]
    assert ids == ["derived_in", "ours_q1", "fetch", "card", "reconcile", "derived_out"]
    nodes = {n["id"]: n for n in graph["nodes"]}
    assert nodes["ours_q1"]["data"]["config"]["args"]["sql"] == S.WITH_REFUNDS
    assert "{{ input.start }}" in nodes["fetch"]["data"]["config"]["args"]["sql"], "口径的取数保留模板，运行时按入口渲染"
    assert nodes["derived_out"]["data"]["config"]["contract"] == {"metrics_from": "reconcile"}
    assert not [n for n in graph["nodes"] if n["type"] in ("report", "llm", "agent")]
    assert not [i for i in validate_graph(GraphSpec.model_validate(graph)).issues if i.level == "error"]

    metrics = await card_of(done.id)
    assert metrics["ours_0"]["value"] == 221.0 and metrics["cal_0"]["value"] == 170.5
    assert metrics["diff_0"]["value"] == 50.5 and metrics["rel_0"]["value"] == 0.2962
    assert metrics["rel_0"]["rendered"] == "29.62%" and metrics["diff_0"]["rendered"] == "50.50元"
    # 三个数各自点得开：差值的出处链追到两边的查询
    res = await client.get(f"/api/runs/{done.id}/evidence/metrics/reconcile/diff_0")
    assert res.status_code == 200, res.text
    inputs = [s for s in res.json()["chain"] if s["step"] == "input"]
    assert {i["node_id"] for i in inputs} == {"ours_q1", "card"}

    listed = (await ledger_of(client, row.id))["reconciled"]
    assert [r["run_id"] for r in listed] == [done.id] and listed[0]["inputs_from"] == "checked"

    # 统计 E2：对账运行数到了（它不进使用统计，E2 专门数它）
    stats = (await client.get("/api/ledger/stats")).json()["e2"]
    assert stats["launched"] >= 1 and stats["succeeded"] >= 1


async def test_the_derived_graph_is_deterministic():
    from app.ledger import derive

    wf = await S.publish()
    async with SessionLocal() as session:
        pinned = await derive.load_pinned(session, wf, 1, "card")
    plan = ledger_reconcile.Plan(pairs=[ledger_reconcile.Pair(ours="gmv", metric="gmv", index=0, name="销售额",
                                                              unit="元", decimals=2)])
    a = ledger_reconcile.build(pinned, sql=S.WITH_REFUNDS, tool=S.tool(), plan=plan, inputs=CHECKED, fetch="fetch")
    b = ledger_reconcile.build(pinned, sql=S.WITH_REFUNDS, tool=S.tool(), plan=plan, inputs=CHECKED, fetch="fetch")
    assert a.graph == b.graph and a.kind == "reconcile"
    assert a.origin["from"][0] == {"kind": "executed", "node_id": "ours_q1"} and a.origin["from"][1]["node_id"] == "card"


async def test_metrics_that_are_not_a_plain_cell_need_a_person_to_pick(client, monkeypatch):
    wf = await S.publish(graph=S.caliber_graph(expression="cell(nodes.fetch, 0, 'gmv') * 1"))
    row = await S.explore(monkeypatch, S.WITH_REFUNDS)
    [action] = [a for a in (await ledger_of(client, row.id))["actions"] if a["kind"] == "reconcile"]
    body = {"query": action["query"], "workflow_id": wf, "version": 1, "inputs": CHECKED, "confirm": True}
    res = await client.post(f"/api/ledger/answers/{row.id}/reconcile", json=body)
    assert res.status_code == 409 and res.json()["code"] == ledger_reconcile.NEEDS_PAIRS
    bad = await client.post(f"/api/ledger/answers/{row.id}/reconcile",
                            json={**body, "pairs": [{"ours": "nope", "metric": "gmv"}]})
    assert bad.status_code == 422 and "没有列「nope」" in bad.json()["detail"]
    done = await reconcile(client, row.id, {**body, "pairs": [{"ours": "gmv", "metric": "gmv"}]})
    assert done.status == "succeeded", done.error
    assert done.origin["pairs_chosen"] is True
    assert (await card_of(done.id))["diff_0"]["value"] == 50.5


async def test_multi_row_answers_are_aligned_by_their_dimensions(client, monkeypatch):
    wf = await S.publish(graph=S.caliber_graph(BY_REGION))
    row = await S.explore(monkeypatch, BY_REGION_ALL, text="东区 [[v:Q1.r0.gmv]]，西区 [[v:Q1.r1.gmv]]。")
    [action] = [a for a in (await ledger_of(client, row.id))["actions"] if a["kind"] == "reconcile"]
    done = await reconcile(client, row.id, {"query": action["query"], "workflow_id": wf, "version": 1,
                                            "inputs": CHECKED, "confirm": True})
    assert done.status == "succeeded", done.error
    assert done.origin["rows"] == [0, 1] and done.origin["pairs"] == [{"ours": "gmv", "caliber": "gmv"}]
    align = next(n for n in done.graph["nodes"] if n["id"] == "align")
    assert align["type"] == "merge" and align["data"]["config"]["inputs"] == {"o": "ours_q1", "c": "fetch"}
    # 对齐语句是服务端拼的字面 SQL：带参数契约（哈希与这条语句绑定），运行时不再当模板渲染（值里的 {{ }} 是数据）
    from app.ledger import params

    assert params.literal_keys(align["data"]["config"], "merge") == ("sql",)
    metrics = await card_of(done.id)
    assert metrics["ours_0_0"]["name"] == "区域=east · gmv · 这次"
    assert (metrics["ours_0_0"]["value"], metrics["cal_0_0"]["value"], metrics["diff_0_0"]["value"]) == (163.0, 150.5, 12.5)
    assert (metrics["ours_1_0"]["value"], metrics["cal_1_0"]["value"], metrics["diff_1_0"]["value"]) == (58.0, 20.0, 38.0)


async def test_masked_dimensions_and_duplicate_keys_are_refused(client, monkeypatch, engine_up):
    wf = await S.publish(graph=S.caliber_graph(BY_REGION))
    duplicated = ("SELECT region AS 区域, amount AS gmv FROM orders WHERE paid_at >= '2026-09-01' "
                  "AND paid_at < '2026-09-08' ORDER BY id")
    row = await S.explore(monkeypatch, duplicated, text="第一单 [[v:Q1.r0.gmv]]，第二单 [[v:Q1.r1.gmv]]。")
    [action] = [a for a in (await ledger_of(client, row.id))["actions"] if a["kind"] == "reconcile"]
    res = await client.post(f"/api/ledger/answers/{row.id}/reconcile",
                            json={"query": action["query"], "workflow_id": wf, "version": 1, "inputs": CHECKED,
                                  "confirm": True})
    assert res.status_code == 409 and "有重复的行" in res.json()["detail"]

    await S.set_source(engine_up, options={"mask_columns": ["region"]})
    row = await S.explore(monkeypatch, BY_REGION_ALL, text="东区 [[v:Q1.r0.gmv]]，西区 [[v:Q1.r1.gmv]]。")
    [action] = [a for a in (await ledger_of(client, row.id))["actions"] if a["kind"] == "reconcile"]
    res = await client.post(f"/api/ledger/answers/{row.id}/reconcile",
                            json={"query": action["query"], "workflow_id": wf, "version": 1, "inputs": CHECKED,
                                  "confirm": True})
    assert res.status_code == 409 and "设置了遮罩" in res.json()["detail"]


def test_align_sql_keeps_the_referenced_rows_in_order():
    pairs = [ledger_reconcile.Pair(ours="gmv", column="销售额")]
    sql = ledger_reconcile.align_sql([("区域", "区域")], pairs, [("west",), ("o'east",)])
    assert sql == ('SELECT o."区域" AS "区域", o."gmv" AS "ours_0", c."销售额" AS "cal_0" FROM o LEFT JOIN c ON '
                   'o."区域" = c."区域" WHERE (o."区域" = \'west\') OR (o."区域" = \'o\'\'east\') '
                   'ORDER BY CASE WHEN (o."区域" = \'west\') THEN 0 WHEN (o."区域" = \'o\'\'east\') THEN 1 END')


def test_the_align_node_carries_a_literal_parameter_contract():
    """对齐语句由服务端按维度值拼成字面 SQL，值直接来自查询结果（可能就带 {{ }}）：节点带参数契约（与这条语句绑定哈希），
    运行时的模板渲染不再碰它（K01：渲染一遍就是把数据当模板解释）。"""
    from app.ledger import derive, params

    plan = ledger_reconcile.Plan(pairs=[ledger_reconcile.Pair(ours="gmv", column="gmv")], rows=[0],
                                 keys=[("区域", "区域")], key_values=[("{{ vars.region }}",)])
    [align] = [n for n in ledger_reconcile.tail_nodes(plan, card_id="card", fetch_id="fetch")
               if n["id"] == derive.ALIGN_ID]
    config = align["data"]["config"]
    assert "'{{ vars.region }}'" in config["sql"]
    assert params.mode_of(config) == params.LITERAL_ARGS
    # 核对过哈希才算数：这条语句和契约注解的就是同一条
    assert params.literal_keys(config, "merge") == ("sql",)
    # 改过 SQL 之后契约作废（复制后编辑那条路）
    edited = {**config, "sql": config["sql"] + " -- 改过"}
    assert params.literal_keys(edited, "merge") == ()


async def test_users_reconcile_their_own_answers_read_only_and_masked(client, monkeypatch):
    """拍板 L5：对账（只读的探索运行）使用者都可以，带上问数据同一套 MASK_RESULTS / READ_ONLY；别人的答案读不到就不给对。"""
    from app.api.deps import Principal
    from app.tools.datasource import MASK_RESULTS_OPTION, READ_ONLY_OPTION

    wf = await S.publish()
    row = await S.explore(monkeypatch, S.WITH_REFUNDS)
    async with SessionLocal() as session:
        (await session.get(Run, row.id)).started_by = "carol"
        await session.commit()
    [action] = [a for a in (await ledger_of(client, row.id))["actions"] if a["kind"] == "reconcile"]
    req = ledger_reconcile.Request(query=action["query"], workflow_id=wf, version=1, inputs=CHECKED, confirm=True)
    dave = Principal(username="dave", role="user", verified=True, mode="local")
    with pytest.raises(ledger_reconcile.ReconcileRefused) as e:
        await ledger_reconcile.reconcile(row.id, req, principal=dave)
    assert e.value.status_code == 403
    carol = Principal(username="carol", role="user", verified=True, mode="local")
    run = await ledger_reconcile.reconcile(row.id, req, principal=carol)
    assert run.started_by == "carol"
    assert run.options[MASK_RESULTS_OPTION] is True and run.options[READ_ONLY_OPTION] is True
    done = await S.wait(run.id)
    assert done.status == "succeeded", done.error


async def test_users_read_their_reconcile_runs_only_through_the_evidence(client, monkeypatch):
    """使用者发起的对账运行带 MASK_RESULTS、记在他名下，但派生图里有口径取数节点的 SQL 模板（口径的 SQL 可能写着遮罩值）：
    运行的原始记录、事件、运行时的图（/api/runs/{id}、/events、/graph）只给他自己的问数据运行，对账运行经证据接口读
    （那里按遮罩渲染）。要让对账真的跑起来，他先得有那一版口径的资产授权（K14：缺了在发起前就 403，见下面那几条用例）。"""
    from app.tools.datasource import MASK_RESULTS_OPTION
    from tests.fakes import auth_env
    from tests.test_caliber_query_tool import grant_caliber

    wf = await S.publish()
    row = await S.explore(monkeypatch, S.WITH_REFUNDS, origin="chat")
    # 当作开登录后使用者 carol 问的：问数据运行在工具边界遮
    async with SessionLocal() as session:
        source = await session.get(Run, row.id)
        source.started_by, source.options = "carol", {MASK_RESULTS_OPTION: True}
        await session.commit()
    [action] = [a for a in (await ledger_of(client, row.id))["actions"] if a["kind"] == "reconcile"]
    await auth_env.reset()
    app_id = ""
    try:
        admin = await auth_env.turn_on_local()
        await auth_env.make_user("carol", "user")
        assert (await admin.put("/api/auth/settings", json={"user_can_chat": True})).status_code == 200
        carol = await auth_env.login("carol")
        # 对账要执行的就是这一版口径：先把这一版的资产授权给它（K14）
        app_id = await grant_caliber(wf, 1, "carol", "use")
        res = await carol.post(f"/api/ledger/answers/{row.id}/reconcile", json={
            "query": action["query"], "workflow_id": wf, "version": 1, "inputs": CHECKED, "confirm": True})
        assert res.status_code == 201, res.text
        done = await S.wait(res.json()["run_id"])
        assert done.status == "succeeded" and done.started_by == "carol" and done.options[MASK_RESULTS_OPTION] is True
        for rest in ("", "/events", "/graph"):
            assert (await carol.get(f"/api/runs/{done.id}{rest}")).status_code == 403, rest
            assert (await carol.get(f"/api/runs/{row.id}{rest}")).status_code == 200, f"自己的问数据运行照旧：{rest}"
        assert (await carol.get(f"/api/runs/{done.id}/evidence")).status_code == 200
    finally:
        await auth_env.reset()
        if app_id:
            from app.db.models_apps import App

            async with SessionLocal() as session:
                await session.execute(delete(App).where(App.id == app_id))
                await session.commit()


async def test_the_reconcile_view_gives_the_panel_what_it_draws(client, monkeypatch):
    """界面层不放逻辑（W4-c 账本前端）：「看差多少」那一项带上口径的入口字段（核对参数的表单照它画：哪几项、必填、有没有
    缺省值）；认不出列怎么配时 409 另带可选的列和口径卡上的指标（选对照的那一步照它画）；对过的账列出对账卡和每一组的
    这次 / 口径 / 差值 / 相对差是哪几个指标（三个数各自点得开，界面不按命名规则去拼指标 id）。"""
    wf = await S.publish(graph=S.caliber_graph(expression="cell(nodes.fetch, 0, 'gmv') * 1"))
    row = await S.explore(monkeypatch, S.WITH_REFUNDS)
    [action] = [a for a in (await ledger_of(client, row.id))["actions"] if a["kind"] == "reconcile"]
    assert action["fields"] == [{"name": "start", "required": True, "has_default": False},
                                {"name": "end", "required": True, "has_default": False},
                                {"name": "store", "required": False, "has_default": True}]
    body = {"query": action["query"], "workflow_id": wf, "version": 1, "inputs": CHECKED, "confirm": True}
    res = await client.post(f"/api/ledger/answers/{row.id}/reconcile", json=body)
    assert res.status_code == 409 and res.json()["code"] == ledger_reconcile.NEEDS_PAIRS
    # suggested：逐格证明了的预选（这条查询只引用了一列，没有逐格比，空）
    assert res.json()["options"] == {"columns": ["gmv"], "metrics": [{"id": "gmv", "name": "销售额", "unit": "元"}],
                                     "suggested": []}
    done = await reconcile(client, row.id, {**body, "pairs": [{"ours": "gmv", "metric": "gmv"}]})
    [listed] = (await ledger_of(client, row.id))["reconciled"]
    assert listed["run_id"] == done.id and listed["card"] == "reconcile"
    assert listed["groups"] == [{"label": "销售额", "ours": "ours_0", "caliber": "cal_0", "diff": "diff_0", "rel": "rel_0"}]


async def test_multi_row_reconcile_groups_carry_the_row_labels(client, monkeypatch):
    wf = await S.publish(graph=S.caliber_graph(BY_REGION))
    row = await S.explore(monkeypatch, BY_REGION_ALL, text="东区 [[v:Q1.r0.gmv]]，西区 [[v:Q1.r1.gmv]]。")
    [action] = [a for a in (await ledger_of(client, row.id))["actions"] if a["kind"] == "reconcile"]
    await reconcile(client, row.id, {"query": action["query"], "workflow_id": wf, "version": 1, "inputs": CHECKED,
                                     "confirm": True})
    [listed] = (await ledger_of(client, row.id))["reconciled"]
    assert [g["label"] for g in listed["groups"]] == ["区域=east · gmv", "区域=west · gmv"]
    assert listed["groups"][1] == {"label": "区域=west · gmv", "ours": "ours_1_0", "caliber": "cal_1_0", "diff": "diff_1_0",
                                   "rel": "rel_1_0"}


async def test_a_related_query_prefills_the_pairing_from_the_cells_that_were_proven(client, monkeypatch):
    """要人选对照时，按逐格比过的结论预选：这次的 gmv 那一列单独比是和口径取数的 gmv 等价的（逐格证明了），口径卡上读这一列的
    指标就是它最可能的对照，options.suggested 给出来，界面预选上、人可以改；单数那一列没有证明，不替人猜。"""
    wf = await S.publish(graph=S.caliber_graph(expression="cell(nodes.fetch, 0, 'gmv') * 1"))
    sql = ("SELECT SUM(amount) AS gmv, COUNT(*) AS 单数 FROM orders WHERE status = 'paid' "
           "AND paid_at >= '2026-09-01' AND paid_at < '2026-09-08'")
    row = await S.explore(monkeypatch, sql, text="本周销售额 [[v:Q1.r0.gmv]]，共 [[v:Q1.r0.单数]] 单。")
    [action] = [a for a in (await ledger_of(client, row.id))["actions"] if a["kind"] == "reconcile"]
    body = {"query": action["query"], "workflow_id": wf, "version": 1, "inputs": CHECKED, "confirm": True}
    res = await client.post(f"/api/ledger/answers/{row.id}/reconcile", json=body)
    assert res.status_code == 409 and res.json()["code"] == ledger_reconcile.NEEDS_PAIRS, res.text
    options = res.json()["options"]
    assert options["suggested"] == [{"ours": "gmv", "metric": "gmv"}], options


# ==========================================================================
# 普通使用者的资产授权（K14）：对账要执行的那一版口径缺授权时，一条运行都不起
# ==========================================================================


async def _answer_by(monkeypatch, who: str, sql: str = S.WITH_REFUNDS) -> Run:
    """这个人自己发起的一次探索运行（对账要读它，相关口径也记在它的比对工件里）：等封存、等后台比对做完。"""
    from app.engine.runner import run_manager
    from app.ledger import match as ledger_match

    S.script(monkeypatch, "本周销售额 [[v:Q1.r0.gmv]]。")
    run = await run_manager.start(graph=S.explore_graph(sql), input_payload={}, run_class="exploratory",
                                  origin_kind="chat", started_by=who)
    row = await S.wait(run.id)
    assert row.status == "succeeded", row.error
    await ledger_match.drain()
    return row


async def _actions(client, run_id: str) -> list[dict]:
    res = await client.get(f"/api/runs/{run_id}/ledger")
    assert res.status_code == 200, res.text
    return res.json()["actions"]


async def _reconcile_action(run_id: str) -> dict:
    """这一条运行上「看差多少」那一项（按**搭建者**的账本读：普通使用者那一份视图里不出去口径的内部身份）。

    用例照它发起请求（workflow_id / version 是请求体要的；普通使用者那一份视图按 K14 只留人读的名字，见 ledger/view）。"""
    async with SessionLocal() as session:
        from app.ledger.view import run_view

        body = await run_view(run_id)
    [action] = [a for a in body["actions"] if a["kind"] == "reconcile"]
    return action


def _body(action: dict) -> dict:
    return {"query": action["query"], "workflow_id": action["workflow_id"], "version": action["version"],
            "inputs": CHECKED, "confirm": True}


async def _reconciled_runs(run_id: str) -> list[Run]:
    async with SessionLocal() as session:
        return await ledger_reconcile.reconciled_runs(session, run_id)


@contextlib.asynccontextmanager
async def _sandbox():
    """开登录的本地环境 + 一个普通使用者 uma：自己建的工作流、应用（授权）、运行退出时清掉，不给别的用例留授权。"""
    from app.db.models_apps import App, AppGrant
    from tests.fakes import auth_env
    from tests.test_caliber_query_tool import grant_caliber

    made = {"workflows": [], "apps": []}

    async def publish(*, version: int = 1, workflow_id: str | None = None) -> str:
        wf = await S.publish(version=version, workflow_id=workflow_id)
        if wf not in made["workflows"]:
            made["workflows"].append(wf)
        return wf

    async def grant(workflow_id: str, version: int, who: str, role: str = "use") -> str:
        app_id = await grant_caliber(workflow_id, version, who, role)
        made["apps"].append(app_id)
        return app_id

    async def revoke(app_id: str) -> None:
        async with SessionLocal() as session:
            await session.execute(delete(AppGrant).where(AppGrant.app_id == app_id))
            await session.commit()

    await auth_env.reset()
    admin = await auth_env.turn_on_local()
    await auth_env.make_user("uma", "user")
    uma = await auth_env.login("uma")
    started = utcnow()
    try:
        yield SimpleNamespace(publish=publish, grant=grant, revoke=revoke, uma=uma, admin=admin)
    finally:
        await uma.aclose()
        await admin.aclose()
        await auth_env.reset()
        async with SessionLocal() as session:
            # 只删这一次用例里它自己发起的运行（按开始的时刻划界），别去动别的用例留下的行
            await session.execute(delete(Run).where(Run.started_by == "uma", Run.created_at >= started))
            for app_id in made["apps"]:
                await session.execute(delete(App).where(App.id == app_id))
            for wf_id in made["workflows"]:
                await session.execute(delete(WorkflowVersion).where(WorkflowVersion.workflow_id == wf_id))
                await session.execute(delete(Workflow).where(Workflow.id == wf_id))
            await session.commit()


async def test_an_authorized_user_reconciles_and_reads_it_back(monkeypatch):
    """有这一版的授权：对账照常发起（运行记在使用者名下），对过的账、那条运行照读。"""
    async with _sandbox() as box:
        wf = await box.publish()
        await box.grant(wf, 1, "uma", "use")
        row = await _answer_by(monkeypatch, "uma")
        action = await _reconcile_action(row.id)
        res = await box.uma.post(f"/api/ledger/answers/{row.id}/reconcile", json=_body(action))
        assert res.status_code == 201, res.text
        done = await S.wait(res.json()["run_id"])
        assert done.status == "succeeded", done.error
        assert done.started_by == "uma" and done.origin["source_run"] == row.id
        assert done.origin["caliber"] == f"{wf}@1"
        # 只读地看历史：账本视图里列着这一次对账，它自己那条运行也读得到
        view = (await box.uma.get(f"/api/runs/{row.id}/ledger")).json()
        assert [r["run_id"] for r in view["reconciled"]] == [done.id]
        assert (await box.uma.get(f"/api/runs/{done.id}/ledger")).status_code == 200


async def test_a_version_without_the_grant_starts_no_run(monkeypatch):
    """授权换到了新版（v2），对账要执行的是旧版（v1）：发起前 403 querycore_not_authorized，一条运行都不起；历史照读。"""
    async with _sandbox() as box:
        wf = await box.publish()
        first = await box.grant(wf, 1, "uma", "use")
        row = await _answer_by(monkeypatch, "uma")
        action = await _reconcile_action(row.id)
        before = await box.uma.post(f"/api/ledger/answers/{row.id}/reconcile", json=_body(action))
        assert before.status_code == 201, before.text
        done = await S.wait(before.json()["run_id"])

        # 授权换到新版：v1 那份撤掉，另给一份 v2 的（对账要执行的还是记在这次运行里的 v1）
        await box.revoke(first)
        await box.publish(version=2, workflow_id=wf)
        await box.grant(wf, 2, "uma", "use")

        res = await box.uma.post(f"/api/ledger/answers/{row.id}/reconcile", json=_body(action))
        assert res.status_code == 403 and res.json()["code"] == "querycore_not_authorized", res.text
        assert res.json()["problems"] == [{"code": "querycore_not_authorized", "workflow_id": wf, "version": 1,
                                           "role": ""}]
        assert [r.id for r in await _reconciled_runs(row.id)] == [done.id], "一条新的对账运行都不该起"

        # 只读地看历史不受影响：账本视图、之前对过的账、那次对账运行的账本与证据照读
        assert (await box.uma.get(f"/api/runs/{row.id}/ledger")).status_code == 200
        assert (await box.uma.get(f"/api/runs/{done.id}/ledger")).status_code == 200
        assert (await box.uma.get(f"/api/runs/{done.id}/evidence")).status_code == 200


async def test_any_unauthorized_version_of_the_batch_is_refused():
    """一次要执行好几版（召回那样新旧两版一起跑）时：缺任何一版都拒，problems 里逐条点出缺的是哪一版。"""
    from app.api.coded import CodedHTTPException
    from app.api.deps import Principal
    from app.api.ledger import ensure_caliber_use

    async with _sandbox() as box:
        wf = await box.publish()
        await box.publish(version=2, workflow_id=wf)
        await box.grant(wf, 1, "uma", "use")
        async with SessionLocal() as session:
            await ensure_caliber_use(session, "uma", [(wf, 1)], what="只执行授权过的那一版")
            with pytest.raises(CodedHTTPException) as caught:
                await ensure_caliber_use(session, "uma", [(wf, 2), (wf, 1)], what="口径召回")
            assert caught.value.status_code == 403 and caught.value.code == "querycore_not_authorized"
            assert caught.value.extra["problems"] == [{"code": "querycore_not_authorized", "workflow_id": wf,
                                                       "version": 2, "role": ""}]
            # builder / admin：不按资产授权收窄
            for who in (Principal(username="uma", role="builder", verified=True, mode="local"),
                        Principal(username="uma", role="admin", verified=True, mode="local")):
                await ensure_caliber_use(session, who, [(wf, 1), (wf, 2)], what="搭建者及以上")
            # 两版都授权了：混着也不拒
            await box.grant(wf, 2, "uma", "use")
            await ensure_caliber_use(session, "uma", [(wf, 1), (wf, 2)], what="口径召回")
    # 关登录（这一档整条判定直接放行，不看资产授权）
    async with SessionLocal() as session:
        await ensure_caliber_use(session, None, [(wf, 1), (wf, 2)], what="关登录")


async def test_the_action_is_available_only_when_the_current_user_may_run_it(monkeypatch):
    """`available` 按同一份资产授权标（caliber_role ≥ use；builder / admin / 关登录显式放行）：

    - 使用者还没授权时那一项不可用，它在**它的**账本里也不带请求要用的 workflow_id / version——发不出去；
    - 授权给它之后同一项变成可用，身份随之下发，照它发的请求真的对上；
    - 搭建者那一档不受这一层收窄，同一项照样可用、带身份。"""
    async with _sandbox() as box:
        wf = await box.publish()
        row = await _answer_by(monkeypatch, "uma")

        [before] = [a for a in await _actions(box.uma, row.id) if a["kind"] == "reconcile"]
        assert before["available"] is False
        assert "workflow_id" not in before and "version" not in before, "不可用的动作不带请求要用的身份"
        assert before["caliber"] and before["query"], "人读的那一套（口径名、这条查询）照旧留着"

        await box.grant(wf, 1, "uma", "use")
        [after] = [a for a in await _actions(box.uma, row.id) if a["kind"] == "reconcile"]
        assert after["available"] is True
        assert (after["workflow_id"], after["version"]) == (wf, 1)
        res = await box.uma.post(f"/api/ledger/answers/{row.id}/reconcile", json=_body(after))
        assert res.status_code == 201, res.text
        assert (await S.wait(res.json()["run_id"])).status == "succeeded"

        [builder] = [a for a in await _actions(box.admin, row.id) if a["kind"] == "reconcile"]
        assert builder["available"] is True and builder["workflow_id"] == wf, "搭建者不看资产授权"


def _frontend_reconcile_body() -> tuple[set[str], set[str]]:
    """前端 `LedgerReconcileIn`（frontend/src/typesLedger.ts）的（必填字段, 可选字段）：服务端下发的动作要能拼出它。

    身份（`workflow_id` / `version`）在账本投影里只随 `available` 下发，前端把它们写成可选也是对的——所以这里两个集合
    都要看。"""
    text = (Path(__file__).resolve().parents[2] / "frontend" / "src" / "typesLedger.ts").read_text(encoding="utf-8")
    block = text.split("export interface LedgerReconcileIn {", 1)[1].split("}", 1)[0]
    required: set[str] = set()
    optional: set[str] = set()
    for line in block.splitlines():
        piece = line.strip().rstrip(",")
        if not piece or ":" not in piece:
            continue
        name = piece.split(":", 1)[0].strip()
        (optional if name.endswith("?") else required).add(name.rstrip("?"))
    return required, optional


async def test_the_frontend_contract_holds_for_available_and_unavailable_actions(monkeypatch):
    """前端契约：**可用**的那一项带齐前端发起请求要的字段（这条查询 + 那一版口径的身份），**不可用**的那一项不带身份。

    两者形状同一份，差别只在 `available`——账本投影就是按它决定留不留身份（无权的那几项只是说明，发不出去）。"""
    async with _sandbox() as box:
        wf = await box.publish()
        app = await box.grant(wf, 1, "uma", "use")
        row = await _answer_by(monkeypatch, "uma")
        required, optional = _frontend_reconcile_body()
        # 请求体要点名三样：这条查询（必填）+ 那一版口径的身份（可以写成可选）
        assert "query" in required, f"前端契约变了：必填 {required}"
        assert {"workflow_id", "version"} <= (required | optional), f"前端不再带口径身份：{required | optional}"

        [ok] = [a for a in await _actions(box.uma, row.id) if a["kind"] == "reconcile"]
        assert ok["available"] is True
        assert required <= set(ok) and {"workflow_id", "version"} <= set(ok), \
            f"可用动作缺前端要的字段：{(required | {'workflow_id', 'version'}) - set(ok)}"

        await box.revoke(app)
        [no] = [a for a in await _actions(box.uma, row.id) if a["kind"] == "reconcile"]
        assert no["available"] is False
        missing = (required | {"workflow_id", "version"}) - set(no)
        assert missing == {"workflow_id", "version"}, f"不可用动作只该少掉口径身份，别的照旧：{missing}"
