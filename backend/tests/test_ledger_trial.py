"""口径试跑（POST /api/ledger/calibers/{wf}/{version}/{node}/trial）：按固定版本派生只读图，跑一次探索运行。

试跑不是正式出具：来源记 trial；参数按入口 Schema 校验，缺的补缺省值；返回指标值和运行 id。
"""
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from app.db.base import SessionLocal
from app.db.models import Run
from app.engine.runner import run_manager
from app.ledger import match as ledger_match
from app.main import app
from tests import ledger_samples as S


@pytest.fixture(autouse=True)
async def engine_up(tmp_path):
    await run_manager.setup()
    await S.fresh_source(tmp_path / "shop.db")
    yield
    await ledger_match.drain()
    await run_manager.shutdown()


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def test_trial_runs_the_pinned_caliber_and_returns_its_metrics(client):
    wf = await S.publish()
    res = await client.post(f"/api/ledger/calibers/{wf}/1/card/trial",
                            json={"inputs": {"start": "2026-09-01", "end": "2026-09-08"}})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == "succeeded" and body["run_id"]
    assert [m["id"] for m in body["metrics"]] == ["gmv"]
    assert float(body["metrics"][0]["value"]) == 170.5
    assert body["metrics"][0]["unit"] == "元"
    # 试跑结果集：取数节点交回的查询快照，给列和行、不给 SQL
    assert [query["node_id"] for query in body["queries"]] == ["fetch"]
    query = body["queries"][0]
    assert set(query) == {"node_id", "tool", "source", "columns", "rows", "total_rows",
                          "truncated", "masked", "source_truncated", "preview_truncated"}
    assert query["tool"] == S.tool() and query["source"] == S.CURRENT["source"]
    assert query["columns"] == ["gmv"] and query["rows"] == [[170.5]]
    assert query["total_rows"] == 1 and query["truncated"] is False and query["masked"] == []
    assert "sql" not in query
    async with SessionLocal() as session:
        run = await session.get(Run, body["run_id"])
    assert run is not None and run.origin_kind == "caliber_trial" and run.run_class == "exploratory"
    assert run.origin["kind"] == "caliber_trial" and run.origin["workflow_id"] == wf
    # 试运行不是问数回答：不参与账本比对、不计入使用统计/召回
    assert not ledger_match.wanted(run.run_class, run.origin_kind)


async def test_trial_refuses_missing_required_inputs_and_unknown_fields(client):
    wf = await S.publish()
    missing = await client.post(f"/api/ledger/calibers/{wf}/1/card/trial", json={"inputs": {}})
    assert missing.status_code == 422 and "start" in missing.text
    unknown = await client.post(f"/api/ledger/calibers/{wf}/1/card/trial",
                                json={"inputs": {"start": "2026-09-01", "end": "2026-09-08", "nope": 1}})
    assert unknown.status_code == 422 and "nope" in unknown.text


@pytest.fixture
def executed(monkeypatch) -> list[str]:
    """这一次测试里真正发给数据库的语句（数据源适配器内联渲染之后的那一条），按先后。"""
    from app.tools import datasource

    seen: list[str] = []
    real = datasource.run_query

    async def spy(source, sql, **kw):
        seen.append(sql)
        return await real(source, sql, **kw)

    monkeypatch.setattr(datasource, "run_query", spy)
    return seen


async def trial_runs() -> list[str]:
    """这次运行记录里由试跑发起的那些（别的来源不算），按运行 id。"""
    from sqlalchemy import select

    async with SessionLocal() as session:
        runs = (await session.execute(select(Run))).scalars().all()
    return [r.id for r in runs if (r.origin or {}).get("kind") == "caliber_trial"]


async def test_trial_refuses_an_incomplete_parameter_before_any_sql(client, executed):
    """可选、没有缺省值的参数这次没给：试跑在**发起运行之前**回结构化 422，一条 SQL 都不发（K01）。"""
    sql = ("SELECT SUM(amount) AS gmv FROM orders WHERE status = 'paid' AND region = '{{ input.store }}' "
           "AND paid_at >= '{{ input.start }}' AND paid_at < '{{ input.end }}'")
    graph = S.caliber_graph(sql=sql)
    graph["nodes"][0]["data"]["config"]["fields"] = [{"name": "start", "required": True},
                                                     {"name": "end", "required": True},
                                                     {"name": "store"}]
    wf = await S.publish(graph=graph)
    before = await trial_runs()
    res = await client.post(f"/api/ledger/calibers/{wf}/1/card/trial",
                            json={"inputs": {"start": "2026-09-01", "end": "2026-09-08"}})
    assert res.status_code == 422, res.text
    body = res.json()
    assert body["code"] == "missing_parameter" and "store" in body["detail"]
    assert "不能当空串用" in body["detail"]
    [problem] = body["problems"]
    assert (problem["code"], problem["name"], problem["node_id"]) == ("missing_parameter", "store", "fetch")
    assert executed == [] and await trial_runs() == before


async def test_trial_reports_an_undeclared_parameter_as_a_structured_422(client, executed):
    """取数模板引用了入口没声明的字段：结构化 422（undeclared_parameter），连逐条由于 problems 一起交回，不发 SQL、不起运行。"""
    wf = await S.publish(graph=S.caliber_graph(
        sql="SELECT SUM(amount) AS gmv FROM orders WHERE region = '{{ input.nope }}'"))
    before = await trial_runs()
    res = await client.post(f"/api/ledger/calibers/{wf}/1/card/trial",
                            json={"inputs": {"start": "2026-09-01", "end": "2026-09-08"}})
    assert res.status_code == 422, res.text
    body = res.json()
    assert body["code"] == "undeclared_parameter" and "nope" in body["detail"]
    [problem] = body["problems"]
    assert (problem["code"], problem["name"], problem["node_id"]) == ("undeclared_parameter", "nope", "fetch")
    assert executed == [] and await trial_runs() == before


async def test_trial_treats_an_explicit_empty_string_as_missing_not_empty(client, executed):
    """显式传空串、字段又没有缺省值：按缺参数拒（结构化 422），不拿空串去发 SQL。

    空串不是值，是省略——这条和组合式问数同一套（ledger/params.provided）；声明了 `default=""` 的字段照声明走（见下一条）。"""
    sql = ("SELECT SUM(amount) AS gmv FROM orders WHERE status = 'paid' AND region = '{{ input.store }}' "
           "AND paid_at >= '{{ input.start }}' AND paid_at < '{{ input.end }}'")
    graph = S.caliber_graph(sql=sql)
    graph["nodes"][0]["data"]["config"]["fields"] = [{"name": "start", "required": True},
                                                     {"name": "end", "required": True},
                                                     {"name": "store"}]
    wf = await S.publish(graph=graph)
    before = await trial_runs()
    res = await client.post(f"/api/ledger/calibers/{wf}/1/card/trial",
                            json={"inputs": {"start": "2026-09-01", "end": "2026-09-08", "store": ""}})
    assert res.status_code == 422, res.text
    body = res.json()
    assert body["code"] == "missing_parameter" and "store" in body["detail"]
    assert executed == [] and await trial_runs() == before


async def test_trial_keeps_the_authors_declared_empty_default(client, executed):
    """作者声明了 `default=""` 的可选参数（模板里写成「空值不过滤」）：照声明当空串跑，不算缺参数。"""
    sql = ("SELECT SUM(amount) AS gmv FROM orders WHERE status = 'paid' "
           "AND ('{{ input.store }}' = '' OR region = '{{ input.store }}') "
           "AND paid_at >= '{{ input.start }}' AND paid_at < '{{ input.end }}'")
    graph = S.caliber_graph(sql=sql)
    graph["nodes"][0]["data"]["config"]["fields"] = [{"name": "start", "required": True},
                                                     {"name": "end", "required": True},
                                                     {"name": "store", "default": ""}]
    wf = await S.publish(graph=graph)
    res = await client.post(f"/api/ledger/calibers/{wf}/1/card/trial",
                            json={"inputs": {"start": "2026-09-01", "end": "2026-09-08"}})
    assert res.status_code == 200, res.text
    assert res.json()["status"] == "succeeded", res.json()
    # 参数渲染进去了（不是原样带着 {{ }} 发出去）：声明成空串的那一处就是 ''，日期照常
    [sql] = executed
    assert "{{" not in sql and "('' = '' OR region = '')" in sql and "'2026-09-01'" in sql


async def test_trial_queries_mask_the_configured_columns_and_never_return_sql(client):
    """试跑结果集把数据源设了遮罩的列换成「已遮罩」（改了名字的结果列也认）；响应里不出现 SQL，SQL 里可能写着遮罩值。"""
    from sqlalchemy import select

    from app.db.models import DataSource

    async with SessionLocal() as session:
        source = (await session.execute(select(DataSource).where(
            DataSource.name == S.CURRENT["source"]))).scalar_one()
        source.options = {"mask_columns": ["phone"]}
        await session.commit()
    wf = await S.publish(graph=S.caliber_graph(sql="SELECT phone AS tel FROM orders ORDER BY id LIMIT 3",
                                               expression="cell(nodes.fetch, 0, 'tel')"))
    res = await client.post(f"/api/ledger/calibers/{wf}/1/card/trial",
                            json={"inputs": {"start": "2026-09-01", "end": "2026-09-08"}})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == "succeeded", body
    query = body["queries"][0]
    assert query["columns"] == ["tel"] and query["masked"] == ["tel"]
    assert query["rows"] == [["已遮罩"]] * 3
    assert "value" not in body["metrics"][0]
    assert body["metrics"][0]["rendered"] == "已遮罩"
    assert body["metrics"][0]["masked"] is True
    assert body["validation"]["status"] == "unknown"
    assert body.get("output") is None
    assert query["total_rows"] == 3 and query["truncated"] is False
    assert "sql" not in query
