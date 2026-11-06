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
