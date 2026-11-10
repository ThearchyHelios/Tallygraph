"""口径可调用描述（app/ledger/descriptor.py、GET /api/ledger/calibers）：目录页、应用表单、外部集成用的机器可读契约。

- 只列每个工作流**推荐发布版**（Workflow.published_version，K07：默认调用读推荐版，不机械取最高已发布版）里的源头
  口径卡：入口字段成 JSON Schema、指标照配置、依赖只报数据源和表名；
- 能不能组合按 look.calibers.metric_options 的**每个指标**那份判定（K08 S4）：B 独有那条查询坏了、绑定对不上只让 B
  不可以选；另给这一版两份闭包摘要（closure 与 selectable_metrics）和 computes_unselected 的共享声明，摘要里没有 SQL；
- 出具只描述版本策略：受管级别、图里没有人工节点或写外部系统的节点才自动出具，原因照写；
- 转抄别处口径卡的卡不列；响应里没有 SQL、没有参数值。
数据是虚构店铺库。
"""
from __future__ import annotations

import json

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
from tests import ledger_samples as S


@pytest.fixture(autouse=True)
async def shop(tmp_path):
    await S.fresh_source(tmp_path / "shop.db")
    yield


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def descriptors(client: AsyncClient, wf: str) -> list[dict]:
    body = (await client.get("/api/ledger/calibers", params={"workflow_id": wf})).json()
    assert set(body) == {"calibers"}, body
    return body["calibers"]


def with_human() -> dict:
    graph = S.caliber_graph()
    graph["nodes"].append(S.node("approve", "human", assignee="运营"))
    return graph


def with_copy() -> dict:
    graph = S.caliber_graph()
    graph["nodes"].append(S.node(
        "copy", "metrics", caliber="抄来的周销售", caliber_version="v1",
        caliber_from={"workflow_id": "elsewhere", "workflow_version": 1, "node_id": "card"},
        metrics=[{"id": "gmv", "name": "销售额", "expression": "cell(nodes.fetch, 0, 'gmv')"}]))
    return graph


async def test_a_source_caliber_is_described_as_a_callable_contract(client):
    wf = await S.publish()
    [made] = await descriptors(client, wf)
    assert (made["workflow_id"], made["workflow_name"], made["version"]) == (wf, "周销售", 1)
    assert made["level"] == "governed" and made["graph_hash"]
    assert made["card"] == {"node_id": "card", "title": "card", "caliber": "周销售", "caliber_version": "v1"}
    assert made["metrics"] == [{"id": "gmv", "name": "销售额", "unit": "元", "decimals": 2, "format": "thousands"}]
    schema = made["entry_schema"]
    assert schema["format"] == "agentlab.form/1" and schema["type"] == "object"
    assert schema["required"] == ["start", "end"]
    assert schema["properties"]["store"]["default"] == "all"
    assert made["composable"] is True and made["compose_reason"] is None
    assert made["dependencies"]["sources"] == [S.CURRENT["source"]]
    assert made["dependencies"]["tables"] == ["orders"]
    assert made["dependencies"]["catalog_versions"] is None and made["dependencies"]["schema_versions"] is None
    issuance = made["issuance"]
    assert issuance["level"] == "governed" and issuance["auto_issue"] is True
    assert issuance["requires_independent_proof"] is True
    assert issuance["side_effects"] is None and issuance["reasons"] == []

    # 单条接口交同一份描述；转抄卡、没发布过的版本、不存在的节点都是 404
    single = (await client.get(f"/api/ledger/calibers/{wf}/1/card")).json()
    assert single == made
    for path in (f"/api/ledger/calibers/{wf}/1/nope", f"/api/ledger/calibers/{wf}/9/card",
                 "/api/ledger/calibers/nope/1/card"):
        assert (await client.get(path)).status_code == 404, path

    # 契约里没有取数 SQL、没有参数值
    text = json.dumps((await client.get("/api/ledger/calibers", params={"workflow_id": wf})).json())
    assert "SELECT" not in text and '"sql"' not in text
    assert "SUM(" not in text and "paid" not in text


async def test_a_human_node_turns_auto_issue_off_with_a_reason(client):
    wf = await S.publish(name="审批周销售", graph=with_human(), level="governed")
    [made] = await descriptors(client, wf)
    assert made["level"] == "governed"
    assert made["issuance"]["auto_issue"] is False
    assert "approve" in made["issuance"]["side_effects"]
    assert any("人工节点" in r for r in made["issuance"]["reasons"])
    assert made["issuance"]["requires_independent_proof"] is True


async def test_a_published_level_version_only_supports_one_click_issuance(client):
    wf = await S.publish(name="已发布周销售", graph=S.caliber_graph(), level="published")
    [made] = await descriptors(client, wf)
    assert made["level"] == "published" and made["issuance"]["auto_issue"] is False
    assert any("已发布级别" in r and "一键出具" in r for r in made["issuance"]["reasons"])


async def test_a_card_that_copies_another_caliber_is_not_listed(client):
    wf = await S.publish(graph=with_copy())
    listed = await descriptors(client, wf)
    assert [d["card"]["node_id"] for d in listed] == ["card"]
    assert (await client.get(f"/api/ledger/calibers/{wf}/1/copy")).status_code == 404


async def test_metric_definition_and_params_are_described(client):
    graph = S.caliber_graph()
    metric = graph["nodes"][2]["data"]["config"]["metrics"][0]
    metric["definition"] = {"text": "已支付订单的销售额", "scope": "全部门店", "owner": "运营"}
    metric["params"] = [{"name": "store", "type": "string", "default": "all"}]
    wf = await S.publish(graph=graph)
    [made] = await descriptors(client, wf)
    [m] = made["metrics"]
    assert m["definition"]["text"] == "已支付订单的销售额" and m["definition"]["owner"] == "运营"
    assert m["params"] == [{"name": "store", "type": "string", "default": "all"}]


async def test_an_external_only_card_is_not_promised_automatic_issuance(client):
    graph = S.chain(
        S.node("start", "input"),
        S.node("card", "metrics", caliber="年度目标", caliber_version="v1", metrics=[{
            "id": "target", "name": "年度目标", "unit": "元", "kind": "external",
            "external": {"value": 100, "source": "预算", "as_of": "2026-01-01"}}]),
        S.node("out", "output", fields=[{"name": "v", "value": "{{ nodes.card.metrics[0].value }}"}]))
    wf = await S.publish(name="只有外部常量", graph=graph)
    [made] = await descriptors(client, wf)
    assert made["composable"] is True
    assert made["issuance"]["auto_issue"] is False
    assert any("没有取数节点" in r for r in made["issuance"]["reasons"])


async def test_metric_synonyms_reach_the_descriptor(client):
    """文档带来的同义词要出现在机器可读描述里：按名字找口径时，「月活」和指标名一样是入口。"""
    graph = S.caliber_graph()
    for node in graph["nodes"]:
        if node["type"] == "metrics":
            node["data"]["config"]["metrics"][0]["synonyms"] = ["月活", "MAU"]
    wf = await S.publish(graph=graph)
    [made] = await descriptors(client, wf)
    [metric] = made["metrics"]
    assert metric["synonyms"] == ["月活", "MAU"]


async def test_metric_constraints_and_semantic_identity_reach_the_descriptor(client):
    graph = S.caliber_graph()
    definition = {"shape": "scalar", "minimum": 0, "unit": "元"}
    for node in graph["nodes"]:
        if node["type"] == "metrics":
            node["data"]["config"]["metrics"][0].update(contract=definition, grain="汇总", time_field="支付时间")
    [made] = await descriptors(client, await S.publish(graph=graph))
    [metric] = made["metrics"]
    assert metric["contract"] == definition and metric["grain"] == "汇总"
    assert metric["time_field"] == "支付时间" and made["semantic_hash"]


# --------------------------------------------------------------------------
# 按指标判可组合性（K08 S4）：闭包摘要、每个指标的可用性、共享声明
# --------------------------------------------------------------------------

SQL_A = ("SELECT SUM(amount) AS gmv FROM orders WHERE status = 'paid' "
         "AND paid_at >= '{{ input.start }}' AND paid_at < '{{ input.end }}'")
SQL_B = SQL_A + " AND region = '{{ input.buyer }}'"


def two_metric_graph(a_sql: str = SQL_A, b_sql: str = SQL_B) -> dict:
    """一张卡两个指标、两条各自独立的查询；buyer 只有 B 那条用。"""
    return S.chain(
        S.node("start", "input", fields=[{"name": "start", "required": True}, {"name": "end", "required": True},
                                         {"name": "buyer", "required": True}]),
        S.node("fetch_a", "tool", tool=S.tool(), args={"sql": a_sql}),
        S.node("fetch_b", "tool", tool=S.tool(), args={"sql": b_sql}),
        S.node("card", "metrics", caliber="双指标", caliber_version="v1",
               metrics=[{"id": "gmv", "name": "销售额", "expression": "cell(nodes.fetch_a, 0, 'gmv')"},
                        {"id": "n", "name": "单量", "expression": "cell(nodes.fetch_b, 0, 'n')"}]),
        S.node("out", "output", fields=[{"name": "r", "value": "{{ nodes.card.text }}"}]))


def shared_sql_graph() -> dict:
    """两个指标读同一条取数（共享 SQL）：选一个时那条还在算另一个。"""
    graph = S.caliber_graph()
    next(n for n in graph["nodes"] if n["type"] == "metrics")["data"]["config"]["metrics"].append(
        {"id": "n", "name": "单量", "expression": "cell(nodes.fetch, 0, 'gmv')"})
    return graph


def leaks_sql(text: str) -> list[str]:
    """描述里不该出现的 SQL 痕迹：语句/列名/参数模板/参数值。

    表名是**照实列出来的**（``dependencies.tables`` 的契约就是报数据源和表名），所以不含在禁列里；禁的是 SQL 正文的
    片段与渲染模板。"""
    return [n for n in ("SUM(", "paid_at", "{{", '"sql"') if n in text]


async def test_the_descriptor_carries_the_default_and_per_metric_closures(client):
    """描述里给这一版两份闭包摘要：closure（不点名、整卡那份）与 selectable_metrics（每个指标各自的那份）。"""
    wf = await S.publish(graph=two_metric_graph())
    [made] = await descriptors(client, wf)
    default = made["closure"]
    assert default["whole_card"] is True and default["query_ids"] == [f"{wf}:fetch_a", f"{wf}:fetch_b"]
    assert default["required_entry_fields"] == ["start", "end", "buyer"]
    assert default["metric_ids"] == ["gmv", "n"] and default["resolver_version"]
    assert [m["metric_id"] for m in made["selectable_metrics"]] == ["gmv", "n"]
    assert made["unavailable_metrics"] == [] and made["computes_unselected"] == []
    by_metric = {m["metric_id"]: m for m in made["selectable_metrics"]}
    assert by_metric["gmv"]["query_ids"] == [f"{wf}:fetch_a"]
    assert by_metric["gmv"]["required_entry_fields"] == ["start", "end"]
    assert by_metric["gmv"]["queries"][0]["sql_hash"] and "SELECT" not in json.dumps(made)
    assert made["composable"] is True and made["compose_reason"] is None
    assert leaks_sql(json.dumps(made, ensure_ascii=False)) == []


async def test_a_broken_metric_does_not_close_the_whole_card(client):
    """B 那条查询坏了：B 进 unavailable（带原因），A 照样可选、整卡照样可组合；不点名那一份照实说算不出来。"""
    wf = await S.publish(graph=two_metric_graph(b_sql=SQL_B + " AND note = '{{ input.nope }}'"))
    [made] = await descriptors(client, wf)
    assert made["composable"] is True
    assert [m["metric_id"] for m in made["selectable_metrics"]] == ["gmv"]
    [bad] = made["unavailable_metrics"]
    assert bad["metric_id"] == "n" and "undeclared_parameter" in bad["reason"]
    assert made["closure"] is None and "undeclared_parameter" in made["compose_reason"]
    assert leaks_sql(json.dumps(made, ensure_ascii=False)) == []


async def test_a_card_whose_metrics_all_fail_is_not_composable(client):
    """两个指标都用不了：不能组合，原因照实给（描述里每个指标各写各的）。"""
    wf = await S.publish(graph=two_metric_graph(a_sql=SQL_A + " AND note = '{{ input.nope }}'",
                                                b_sql=SQL_B + " AND note = '{{ input.nope }}'"))
    [made] = await descriptors(client, wf)
    assert made["composable"] is False
    assert [m["metric_id"] for m in made["unavailable_metrics"]] == ["gmv", "n"]
    assert made["closure"] is None and "undeclared_parameter" in made["compose_reason"]
    assert leaks_sql(json.dumps(made, ensure_ascii=False)) == []


async def test_a_shared_query_is_declared_as_still_computing_the_other_metric(client):
    """A 和 B 共用一条 SQL：按指标那一份摘要声明它还在算没选中的那个指标（computes_unselected，顶层也按选择给一份）。"""
    wf = await S.publish(graph=shared_sql_graph())
    [made] = await descriptors(client, wf)
    by_metric = {m["metric_id"]: m for m in made["selectable_metrics"]}
    [query] = by_metric["gmv"]["queries"]
    assert query["query_id"] == f"{wf}:fetch" and query["node_id"] == "fetch" and query["tool"] == S.tool()
    assert query["metric_ids"] == ["gmv"] and query["computes_unselected"] == ["n"]
    assert query["required_entry_fields"] == ["start", "end"] and query["sql_hash"]
    # 顶层按「选谁、还在算谁」两组各一条（选 gmv 时还在算 n，反过来也一样）
    assert made["computes_unselected"] == [
        {"query_id": f"{wf}:fetch", "node_id": "fetch", "metric_ids": ["gmv"], "computes_unselected": ["n"]},
        {"query_id": f"{wf}:fetch", "node_id": "fetch", "metric_ids": ["n"], "computes_unselected": ["gmv"]},
    ]
    assert leaks_sql(json.dumps(made, ensure_ascii=False)) == []


async def test_the_descriptor_follows_the_working_versions_recommendation(client):
    """K07：列出的是**推荐发布版**（Workflow.published_version），不是机械取最高已发布版。"""
    from app.db.base import SessionLocal
    from app.db.models import Workflow

    wf = await S.publish(name="推荐版周销售")                                   # v1
    changed = S.caliber_graph()
    next(n for n in changed["nodes"] if n["type"] == "metrics")["data"]["config"]["metrics"][0]["name"] = "销售额（改）"
    await S.publish(name="推荐版周销售", version=2, workflow_id=wf, graph=changed)
    async with SessionLocal() as session:
        row = await session.get(Workflow, wf)
        row.published_version = 1                                               # 把推荐版推回 v1
        await session.commit()
    [made] = await descriptors(client, wf)
    assert made["version"] == 1 and made["metrics"][0]["name"] == "销售额"
    assert made["closure"]["query_ids"] == [f"{wf}:fetch"]
