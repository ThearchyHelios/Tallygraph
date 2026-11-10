"""K10：父 Run 的口径查询工具（`caliber_query`）与父子 Run 的证据接入。

这份文件盯住这几条：

1. **工具是真的、参数是固定的。** 登记表里有 `caliber_query`（不是 `db_query__` 前缀那类动态工具），参数只有口径身份
   （workflow / card）、要算的指标和这次入口的类型化取值；用途、主体、父 Run 身份、固定的数据版本都由父 Run 的上下文定，
   参数里没有也传不了。
2. **证据接回父 Run。** 用父 Run 的上下文执行（`ToolContext`），当场 await 收尾；派生的探针运行是**明确的子运行**
   （origin 里记 parent_run_id），父 Run 的节点把每条 query artifact 记进自己的台账（query_id / execution_id / attempt、
   来源、列与行数、截断都在）——一次交回**多条**查询（queries 数组）不会只记第一条。
3. **伪造、跨运行、别的种类的工件一律拒绝。** 每条 query artifact 必须是这次授权的父运行自己落的，或它明确派生的子运行
   落下的，且 kind=query_snapshot（capabilities.registry.snapshot_owned）。
4. **同一指标工作台与父 Run 是同一种说法。** 同一份口径、同一组指标与入口，HTTP 工作台（compose）与父 Run 拿到的发布
   清单、结果类型、参数解释一致。
5. **父 Run 的边界跟着走。** 遮罩、截断、上传/记录源版本固定、只读、预算、复算要的材料；正式父 Run 走 formal judge，
   交互（compose）拿不到正式身份；超时、失败都不是成功。

数据全是合成的（一个临时 SQLite 库、几条合成标量指标），不连真实业务库；这些指标不是任何真实业务口径。
"""
from __future__ import annotations

import asyncio
import copy
import json
import sqlite3
import uuid
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from langchain_core.messages import AIMessage, ToolMessage
from sqlalchemy import select

from app.apps import publications
from app.db.base import SessionLocal
from app.db.models import Artifact, DataSource, Run, RunEvent
from app.engine.runner import run_manager
from app.ledger import querycore
from app.main import app
from app.records import store
from app.records.tables import columns_of, path_of
from tests import ledger_samples as S
from tests.publication_samples import add_record, add_version, confirm, make_workflow, publish

#: 这一版口径卡的节点 id（源头口径卡）
CARD = "card"
#: 取数窗口：start（含）/ end（不含）
WINDOW = "AND paid_at >= '{{ input.start }}' AND paid_at < '{{ input.end }}'"
ROWS = [
    (1, "east", "paid", 100.0, 2, "2026-09-02"),
    (2, "east", "paid", 50.0, 1, "2026-09-03"),
    (3, "west", "refunded", 30.0, 3, "2026-09-04"),
    (4, "west", "paid", 20.0, 4, "2026-09-07"),
    (5, "east", "open", 12.5, 5, "2026-09-07"),
    (6, "west", "paid", 70.0, 6, "2026-10-02"),
    (7, "east", "paid", 5.0, 7, "2026-08-31"),
]
#: 窗口内的期望值（2026-09-01 ≤ paid_at < 2026-10-01，只算 paid）
WINDOW_SUM = 170.0
INPUTS = {"start": "2026-09-01", "end": "2026-10-01"}


def build_db(path) -> None:
    db = sqlite3.connect(path)
    db.executescript("CREATE TABLE orders (id INTEGER PRIMARY KEY, region TEXT, status TEXT, amount REAL, "
                     "qty INTEGER, paid_at TEXT);")
    db.executemany("INSERT INTO orders VALUES (?, ?, ?, ?, ?, ?)", ROWS)
    db.commit()
    db.close()


async def fresh_source(tmp_path) -> str:
    """每个用例一个名字不同的数据源（候选口径按数据源预筛，别的用例的口径不会混进来）。"""
    path = tmp_path / "caliber_query.db"
    build_db(path)
    S.CURRENT["source"] = f"cq_{uuid.uuid4().hex[:8]}"
    await S.set_source(path)
    return S.CURRENT["source"]


def graph(*, sales_limit: int | None = None, grouped: bool = False) -> dict[str, Any]:
    """入口 → 两条取数 → 口径卡 → 报告 → 出口（报告节点只在原图里，探针图不跑它）。

    grouped=True 时 sales 那条按大区分组、可以给 limit：用来测截断（结果被截短照实记进台账）。"""
    sales_sql = (f"SELECT region, SUM(amount) AS gmv, SUM(qty) AS units FROM orders WHERE status = 'paid' {WINDOW} "
                 "GROUP BY region" if grouped else
                 f"SELECT SUM(amount) AS gmv, SUM(qty) AS units FROM orders WHERE status = 'paid' {WINDOW}")
    sales_args: dict[str, Any] = {"sql": sales_sql}
    if sales_limit is not None:
        sales_args["limit"] = sales_limit
    return S.chain(
        S.node("start", "input", fields=[
            {"name": "start", "type": "string", "required": True, "description": "开始日期（含）"},
            {"name": "end", "type": "string", "required": True, "description": "结束日期（不含）"},
        ]),
        S.node("sales", "tool", tool=S.tool(), args=sales_args),
        S.node("shared", "tool", tool=S.tool(),
               args={"sql": f"SELECT SUM(amount) AS total, MAX(amount) AS peak FROM orders "
                            f"WHERE status = 'paid' {WINDOW}"}),
        S.node(CARD, "metrics", caliber="合成周销售", caliber_version="v1", metrics=[
            {"id": "gmv", "name": "销售额", "unit": "元", "decimals": 2, "format": "thousands",
             "expression": "cell(nodes.sales, 0, 'gmv')"},
            {"id": "units", "name": "件数", "unit": "件", "expression": "cell(nodes.sales, 0, 'units')"},
            {"id": "peak", "name": "最大单笔", "unit": "元", "decimals": 2,
             "expression": "cell(nodes.shared, 0, 'peak')"},
        ]),
        S.node("write", "report", instructions="写周销售"),
        S.node("out", "output", fields=[{"name": "报告", "value": "{{ nodes.write.text }}"}]),
    )


def twin_graph() -> dict[str, Any]:
    """两个取数节点写**同一条 SQL**、读同一张表、同一批数据（内容寻址下会落到同一个工件 id）：两条不同的逻辑查询。

    这是「同 artifact 对应不同逻辑 query」的反例：按工件 id 去重的证据索引会把它们并成一条。"""
    sql = f"SELECT SUM(amount) AS gmv FROM orders WHERE status = 'paid' {WINDOW}"
    metrics = [{"id": "left", "name": "甲", "unit": "元", "expression": "cell(nodes.one, 0, 'gmv')"},
               {"id": "right", "name": "乙", "unit": "元", "expression": "cell(nodes.two, 0, 'gmv')"}]
    return S.chain(
        S.node("start", "input", fields=[{"name": "start", "type": "string", "required": True},
                                         {"name": "end", "type": "string", "required": True}]),
        S.node("one", "tool", tool=S.tool(), args={"sql": sql}),
        S.node("two", "tool", tool=S.tool(), args={"sql": sql}),
        S.node(CARD, "metrics", caliber="合成双查", caliber_version="v1", metrics=metrics),
        S.node("write", "report", instructions="写周销售"),
        S.node("out", "output", fields=[{"name": "报告", "value": "{{ nodes.write.text }}"}]),
    )


def parent_graph(workflow_id: str, *, metric_ids: list[str] | None = None, card: str = CARD,
                 inputs: dict[str, Any] | None = None, gate: bool = False) -> dict[str, Any]:
    """父 Run 的图：入口 →（可选的人工确认）→ 调用工具（caliber_query）→ 出口。"""
    parts = [S.node("start", "input")]
    if gate:
        parts.append(S.node("gate", "human", mode="approve", title="看一眼"))
    parts.append(S.node("look", "tool", tool=querycore.CALIBER_QUERY, args={
        "workflow": workflow_id, "card": card,
        "metric_ids": metric_ids or ["gmv", "peak"],
        "inputs": inputs if inputs is not None else dict(INPUTS)}))
    parts.append(S.node("out", "output", fields=[{"name": "答", "value": "{{ nodes.look.ok }}"}]))
    return S.chain(*parts)


@pytest.fixture(autouse=True)
async def engine_up(tmp_path):
    await run_manager.setup()
    await fresh_source(tmp_path)
    yield
    await run_manager.shutdown()


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def published(client: AsyncClient, *, sales_limit: int | None = None, grouped: bool = False) -> dict:
    """建一份已发布的口径（走 K07 的真实发布入口），返回资产。"""
    asset = await make_workflow(graph=graph(sales_limit=sales_limit, grouped=grouped))
    await publish(client, asset)
    return asset


async def run_parent(asset: dict, *, inputs: dict[str, Any] | None = None, metric_ids: list[str] | None = None,
                     data_versions: dict[str, Any] | None = None, options: dict[str, Any] | None = None,
                     run_class: str = "exploratory") -> Run:
    """起一次父 Run（走调用工具节点那条路），等它收尾。"""
    run = await run_manager.start(graph=parent_graph(asset["workflow_id"], inputs=inputs, metric_ids=metric_ids),
                                  input_payload={}, run_class=run_class, data_versions=data_versions,
                                  options=options)
    return await settled(run.id)


async def run_once(asset: dict, *, metric_ids: list[str], inputs: dict[str, Any] | None = None, **kw: Any):
    """走服务入口跑一次（HTTP 之外的那条路），等 K09 的执行结果。"""
    async with SessionLocal() as session:
        return await querycore.execute(session, workflow_id=asset["workflow_id"], card_id=CARD,
                                       metric_ids=metric_ids,
                                       inputs=inputs if inputs is not None else dict(INPUTS), **kw)


async def settled(run_id: str) -> Run:
    for _ in range(600):
        async with SessionLocal() as session:
            row = await session.get(Run, run_id)
        if row.status in querycore.TERMINAL_RUN_STATUSES:
            return row
        await asyncio.sleep(0.05)
    raise AssertionError(f"运行 {run_id} 没有结束：{row.status}")


async def events_of(run_id: str, etype: str) -> list[dict[str, Any]]:
    async with SessionLocal() as session:
        rows = (await session.execute(select(RunEvent).where(RunEvent.run_id == run_id,
                                                             RunEvent.type == etype)
                                      .order_by(RunEvent.seq))).scalars().all()
    return [dict(r.data or {}) for r in rows]


async def tool_end(run_id: str) -> dict[str, Any]:
    [data] = await events_of(run_id, "tool.end")
    return data


async def tool_result(run_id: str, node_id: str = "look") -> dict[str, Any]:
    """调用工具节点的**完整**产出：tool.end 的 preview 截到 2000 个字符，读不全 JSON（K10 的结果里带几条查询）。"""
    from app.engine.evidence_reader import read_sealed

    sealed = await read_sealed(run_id)
    payload = sealed.payload_of([node_id])
    assert isinstance(payload, dict), "调用工具节点没有产出"
    return payload


async def finished_evidence(run_id: str, node_id: str = "look") -> list[dict[str, Any]]:
    async with SessionLocal() as session:
        rows = (await session.execute(select(RunEvent).where(RunEvent.run_id == run_id,
                                                             RunEvent.type == "node.finished")
                                      .order_by(RunEvent.seq))).scalars().all()
    for row in rows:
        data = dict(row.data or {})
        if row.node_id == node_id and data.get("evidence"):
            return list(data["evidence"])
    return []


# ==========================================================================
# 1、工具是真的：登记表里有它、参数固定、不是 db_query__ 前缀
# ==========================================================================


async def test_the_tool_is_registered_with_fixed_parameters():
    from app.capabilities.registry import is_platform_query
    from app.tools.registry import all_specs

    spec = all_specs()[querycore.CALIBER_QUERY]
    assert spec.category and spec.description and not spec.dangerous and not spec.writes_state
    assert spec.effect == "read", "口径查询是只读的：不写外部系统、不该要确认"
    assert set(spec.args_schema.model_fields) == {"workflow", "card", "metric_ids", "inputs"}
    # 用途、主体、版本、预算都不在参数里：由父 Run 的上下文定，模型改不了
    assert not set(spec.args_schema.model_fields) & {"purpose", "role", "actor", "version", "budget", "trace"}
    assert is_platform_query(querycore.CALIBER_QUERY), "口径查询的结果要能当查询快照作证"
    assert not querycore.CALIBER_QUERY.startswith("db_query__"), "它是真实工具，不是数据源那类动态工具"


async def test_it_needs_a_parent_run():
    from app.tools.registry import ToolContext

    with pytest.raises(querycore.QueryCoreRefused) as caught:
        await querycore.caliber_query(ToolContext(), workflow="w", card=CARD, metric_ids=["gmv"], inputs={})
    assert caught.value.code == querycore.NO_PARENT


# ==========================================================================
# 2、证据接回父 Run：子运行、多条查询、执行身份
# ==========================================================================


async def test_the_parent_run_records_every_query_of_one_call(client):
    asset = await published(client)
    parent = await run_parent(asset, metric_ids=["gmv", "peak"])
    assert parent.status == "succeeded", parent.error

    end = await tool_end(parent.id)
    assert end["tool"] == querycore.CALIBER_QUERY
    # 一次调用交回两条查询（sales、shared）：tool.end 上两条的工件 id 都在
    assert len(end["query_artifacts"]) == 2 and end["query_artifact"] == end["query_artifacts"][0]

    entries = [e for e in await finished_evidence(parent.id) if e.get("kind") == "query"]
    assert [e["tool"] for e in entries] == [querycore.CALIBER_QUERY] * 2
    assert {e["artifact"] for e in entries} == set(end["query_artifacts"])
    child_id = (await tool_result(parent.id))["run_id"]
    for entry in entries:
        assert entry["columns"] and entry["rows"] and entry["truncated"] is False
        # 源名写在和数据源查询同一个位置（证据面板、遮罩按它认源）
        assert entry["source"] == S.CURRENT["source"]
        # 出处是这一版这一张图里的那条取数，不是父 Run 的节点
        assert entry["origin"]["workflow_id"] == asset["workflow_id"] and entry["origin"]["version"] == 1
        assert entry["origin"]["node_id"] in ("sales", "shared")
        # 执行身份三件套齐（K10）：执行号记的是真的执行那次 SQL 的运行（派生的子运行），不是父 Run
        assert entry["query_id"] and entry["execution_id"] and entry["attempt"] >= 1
        assert child_id in entry["execution_id"], "执行身份带上真的执行那次，别把两次调用的执行混成一个"
        # 复算要的材料跟着条目走：这次参数输入的哈希与源模板哈希（值本身、SQL 正文都不进台账）
        assert entry["parameters"]["inputs_hash"] and entry["parameters"]["template_hash"]
    assert {e["query_id"] for e in entries} == {f"{asset['workflow_id']}:sales", f"{asset['workflow_id']}:shared"}

    # 父 Run 的封存证据里认得出这两条查询（报告目录、证据面板从这里出发）
    from app.engine.evidence_reader import read_sealed

    sealed = await read_sealed(parent.id)
    assert {e["artifact"] for e in entries} <= set(sealed.queries)
    for artifact in end["query_artifacts"]:
        assert sealed.queries[artifact]["node_id"] == "look"


async def test_the_probe_run_is_an_explicit_child_of_the_parent(client):
    asset = await published(client)
    parent = await run_parent(asset)
    end = await tool_end(parent.id)
    child_id = (await tool_result(parent.id))["run_id"]
    assert child_id, "结果里要给出子运行的 id"
    async with SessionLocal() as session:
        child = await session.get(Run, child_id)
    assert child is not None and child.id != parent.id and child.run_class == "exploratory"
    origin = child.origin or {}
    assert origin["service"] == "querycore" and origin["purpose"] == querycore.DEFAULT_PURPOSE
    assert origin["parent_run_id"] == parent.id, "子运行的来源里必须记着父运行，父子关系不能丢"
    assert origin["manifest"] and origin["selection_hash"] and origin["closure_hash"], "复算要的材料都在"
    assert child.options.get("read_only_data") is True, "派生的子运行照旧一律只读"


async def test_two_identical_calls_get_different_execution_ids(client):
    """同样的参数、同样的 SQL 调两次：两个子运行，执行身份各不相同（工件按内容寻址，可以是同一件）。"""
    from app.tools.registry import ToolContext

    asset = await published(client)
    ctx = ToolContext(run_id="parent-fixed", node_id="look")
    first = await querycore.caliber_query(ctx, workflow=asset["workflow_id"], card=CARD,
                                          metric_ids=["gmv"], inputs=dict(INPUTS))
    second = await querycore.caliber_query(ctx, workflow=asset["workflow_id"], card=CARD,
                                           metric_ids=["gmv"], inputs=dict(INPUTS))
    assert first["ok"] and second["ok"]
    assert first["run_id"] != second["run_id"], "每次调用派生一次自己的子运行"
    left, right = first["queries"][0], second["queries"][0]
    assert left["query_id"] == right["query_id"], "逻辑查询是同一条"
    assert left["execution_id"] != right["execution_id"], "同一条 SQL 执行两次，执行身份必须不同"


async def test_two_identical_executions_in_one_run_are_not_merged(client):
    """同一个父 Run 里两个节点跑同一条 SQL：台账里两条各记各的执行，不合并成一条。"""
    asset = await published(client)
    args = {"workflow": asset["workflow_id"], "card": CARD, "metric_ids": ["gmv"], "inputs": dict(INPUTS)}
    graph = S.chain(
        S.node("start", "input"),
        S.node("one", "tool", tool=querycore.CALIBER_QUERY, args=dict(args)),
        S.node("two", "tool", tool=querycore.CALIBER_QUERY, args=dict(args)),
        S.node("out", "output", fields=[{"name": "答", "value": "{{ nodes.two.ok }}"}]),
    )
    run = await run_manager.start(graph=graph, input_payload={})
    row = await settled(run.id)
    assert row.status == "succeeded", row.error
    entries = ([e for e in await finished_evidence(row.id, "one") if e.get("kind") == "query"]
               + [e for e in await finished_evidence(row.id, "two") if e.get("kind") == "query"])
    assert len(entries) == 2, "两次执行各记一条"
    assert len({e["execution_id"] for e in entries}) == 2, "同一份 SQL 跑两次，各记各的执行身份"
    assert len({e["query_id"] for e in entries}) == 1, "逻辑查询是同一条"


# ==========================================================================
# 3、伪造 / 跨运行 / 别的种类：一律不当证据
# ==========================================================================


async def test_a_forged_or_cross_run_artifact_is_refused(client):
    """名字对了，工件还得是这次父运行（或它明确派生的子运行）落下的 query_snapshot。"""
    from app.capabilities.registry import snapshot_owned
    from app.core.artifact_store import put_json

    asset = await published(client)
    parent = await run_parent(asset)
    [artifact] = (await tool_end(parent.id))["query_artifacts"][:1]
    # 真的：父 Run 明确派生的子运行落下的查询快照
    assert await snapshot_owned(artifact, parent.id, querycore.CALIBER_QUERY)
    # 别的运行落下的同一件：不认
    assert not await snapshot_owned(artifact, "another-run", querycore.CALIBER_QUERY)
    # 伪造的工件 id：不认
    assert not await snapshot_owned("f" * 64, parent.id, querycore.CALIBER_QUERY)

    # 种类不对（同一件内容、父运行自己落下的 tool_snapshot）：不认
    async with SessionLocal() as session:
        snap = (await session.execute(select(Artifact).where(Artifact.id == artifact))).first()
    wrong_kind = await put_json({"x": 1}, kind="tool_snapshot", run_id=parent.id)
    assert wrong_kind != artifact
    assert not await snapshot_owned(wrong_kind, parent.id, querycore.CALIBER_QUERY), "不是 query_snapshot 的不当作证"
    assert snap is not None, "子运行那件工件在工件表里记着"


async def test_a_forged_tool_result_does_not_become_evidence(client, monkeypatch):
    """调用工具节点交回一份拼出来的 caliber 结果（工件是别人的）：不进台账、tool.end 上也不带。"""
    from app.engine.nodes import tools as tool_node

    asset = await published(client)

    async def forged_call_tool(*_a, **_k):
        return {"tool": querycore.CALIBER_QUERY, "kind": querycore.CALIBER_QUERY, "ok": True, "run_id": "made-up",
                "queries": [{"artifact": "a" * 64, "source": {"workflow_id": asset["workflow_id"], "version": 1,
                                                              "node_id": "sales"},
                             "columns": ["gmv"], "row_count": 1, "truncated": False,
                             "query_id": "made-up:sales", "execution_id": "made-up:sales#e1", "attempt": 1}]}

    monkeypatch.setattr(tool_node, "call_tool", forged_call_tool)
    parent = await run_parent(asset)
    assert parent.status == "succeeded", parent.error
    end = await tool_end(parent.id)
    assert "query_artifact" not in end and "query_artifacts" not in end
    assert [e for e in await finished_evidence(parent.id) if e.get("kind") == "query"] == []


# ==========================================================================
# 4、同一指标工作台与父 Run 是同一种说法
# ==========================================================================


async def test_the_workbench_and_the_parent_run_agree(client):
    asset = await published(client)
    res = await client.post("/api/ledger/querycore", json={
        "workflow_id": asset["workflow_id"], "card_id": CARD, "metric_ids": ["gmv", "peak"], "inputs": INPUTS})
    assert res.status_code == 200, res.text
    bench = res.json()
    assert bench["ok"] and bench["purpose"] == publications.PURPOSE_COMPOSE

    parent = await run_parent(asset, metric_ids=["gmv", "peak"])
    end = await tool_end(parent.id)
    tool = await tool_result(parent.id)

    # 同一份发布清单、同一种结果类型、同一套参数解释
    assert tool["publication"] == bench["publication"]
    assert tool["version"] == bench["version"] and tool["explicit_version"] == bench["explicit_version"]
    assert [m["id"] for m in tool["metrics"]] == [m["id"] for m in bench["metrics"]]
    assert [m["value"] for m in tool["metrics"]] == [m["value"] for m in bench["metrics"]]
    assert tool["metrics"] == bench["metrics"]
    assert tool["parameters"]["values"] == bench["parameters"]["values"] == INPUTS
    assert tool["parameters"]["contract"] == bench["parameters"]["contract"]
    assert tool["parameters"]["adapter"] == bench["parameters"]["adapter"]
    assert tool["selection"]["closure_hash"] == bench["selection"]["closure_hash"]
    assert tool["data"]["tables"] == bench["data"]["tables"] == ["orders"]
    # 每条查询：逻辑身份、列、参数输入哈希、行数与截断一致（工件是两次运行各自的，不要求相同）
    assert [(q["query_id"], q["columns"], q["truncated"]) for q in tool["queries"]] \
        == [(q["query_id"], q["columns"], q["truncated"]) for q in bench["queries"]]
    assert [q["parameters"]["inputs_hash"] for q in tool["queries"]] \
        == [q["parameters"]["inputs_hash"] for q in bench["queries"]]


# ==========================================================================
# 5、父 Run 的边界：遮罩、截断、版本固定、只读、预算、formal
# ==========================================================================


async def test_masked_values_are_not_handed_out(client, tmp_path):
    asset = await published(client)
    await S.set_source(tmp_path / "caliber_query.db", options={"mask_columns": ["gmv"]})
    parent = await run_parent(asset, metric_ids=["gmv"])
    tool = await tool_result(parent.id)
    [metric] = [m for m in tool["metrics"] if m["id"] == "gmv"]
    assert metric.get("masked") is True and metric["value"] is None
    assert tool["ok"] is False and querycore.METRIC_MASKED in [p["code"] for p in tool["problems"]]
    assert "masked" not in json.dumps([{"id": m["id"], "v": m["value"]} for m in tool["metrics"]])


async def test_truncated_results_are_reported_as_truncated(client):
    asset = await published(client, grouped=True, sales_limit=1)
    parent = await run_parent(asset, metric_ids=["gmv"])
    [entry] = [e for e in await finished_evidence(parent.id) if e.get("kind") == "query"]
    assert entry["truncated"] is True, "结果被截短照实记进台账，不悄悄当完整结果"
    assert entry["rows"] == 1, "台账里记的是快照里真的有多少行"


async def test_the_parent_run_pins_the_source_versions_into_the_child(client):
    """父 Run 固定的上传/记录源版本照原样带进子运行：父子两段查的是同一版数据。"""
    asset = await published(client)
    pinned = {"src-1": {"snapshot": "snap-1", "name": "表格"}}
    parent = await run_parent(asset, data_versions=pinned)
    assert parent.data_versions == pinned
    child_id = (await tool_result(parent.id))["run_id"]
    async with SessionLocal() as session:
        child = await session.get(Run, child_id)
    assert child.data_versions == pinned, "子运行必须照父 Run 那一份固定，不能改用当前版本"
    assert child.options.get("read_only_data") is True


async def test_a_timeout_is_not_a_success(client):
    """父 Run 给的预算到点没跑完：交回 running + 子运行 id，ok=False，台账里一条查询都没有。"""
    asset = await published(client)
    parent = await run_parent(asset, options={querycore.BUDGET_OPTION: {"timeout_s": 0.0}})
    assert parent.status == "succeeded", parent.error
    end = await tool_end(parent.id)
    tool = await tool_result(parent.id)
    assert tool["ok"] is False and tool["status"] == "running" and tool["run_id"]
    assert [p["code"] for p in tool["problems"]] == [querycore.TIMEOUT]
    assert "query_artifact" not in end
    assert [e for e in await finished_evidence(parent.id) if e.get("kind") == "query"] == []


async def test_a_refusal_is_a_tool_failure(client):
    """口径不在（卡的名字写错）：工具当场拒，节点失败并说清原因——不交回一份空结果冒充成功。"""
    asset = await published(client)
    parent = await run_parent(asset, metric_ids=["gmv"], inputs=dict(INPUTS))
    assert parent.status == "succeeded"
    run = await run_manager.start(graph=parent_graph(asset["workflow_id"], card="nope", inputs=dict(INPUTS)),
                                  input_payload={})
    row = await settled(run.id)
    assert row.status == "failed" and querycore.CARD_MISSING in (row.error or "")


async def test_a_formal_parent_uses_the_formal_judge(client):
    """正式父 Run：走 execute_formal 那一套（发布依据不齐就拒）；交互（compose）拿不到正式身份。"""
    from app.tools.registry import ToolContext

    asset = await published(client)             # 有清单、没有验证记录与审阅
    compose = ToolContext(run_id="p1", node_id="look")
    outcome = await querycore.caliber_query(compose, workflow=asset["workflow_id"], card=CARD,
                                             metric_ids=["gmv"], inputs=dict(INPUTS))
    assert outcome["ok"] and outcome["purpose"] == publications.PURPOSE_COMPOSE

    formal = ToolContext(run_id="p2", node_id="look", run_class="formal")
    with pytest.raises(ValueError) as caught:
        await querycore.caliber_query(formal, workflow=asset["workflow_id"], card=CARD,
                                      metric_ids=["gmv"], inputs=dict(INPUTS))
    assert publications.RECORD_MISSING in str(caught.value), "正式父 Run 用的是 publications.judge 的原码"

    record_id = await add_record(asset)
    await confirm(asset, record_id)
    await publish(client, asset)
    proven = await querycore.caliber_query(ToolContext(run_id="p3", node_id="look", run_class="formal"),
                                           workflow=asset["workflow_id"], card=CARD, metric_ids=["gmv"],
                                           inputs=dict(INPUTS))
    assert proven["ok"] and proven["purpose"] == publications.PURPOSE_FORMAL
    assert proven["publication"]["identity"] == publications.PROVEN
    async with SessionLocal() as session:
        child = await session.get(Run, proven["run_id"])
    assert child.origin["purpose"] == publications.PURPOSE_FORMAL and child.origin["parent_run_id"] == "p3"


async def test_the_actor_comes_from_the_parent_run_not_the_parameters(client):
    """认证主体来自父 Run，工具参数里没有这一项，也传不了。"""
    asset = await published(client)
    run = await run_manager.start(graph=parent_graph(asset["workflow_id"]), input_payload={}, started_by="老王")
    row = await settled(run.id)
    assert row.status == "succeeded", row.error
    child_id = (await tool_result(row.id))["run_id"]
    async with SessionLocal() as session:
        child = await session.get(Run, child_id)
    assert child.started_by == "老王"


# ==========================================================================
# 6、协作成员路径：成员绑口径查询时，多查询逐条编号、逐条认工件
# ==========================================================================


def team_graph(workflow_id: str) -> dict[str, Any]:
    """协作团队：成员「查数」绑 caliber_query 与分析工具（绑了分析工具才编 Qn），另一个只是说话。"""
    agents = [
        {"name": "查数", "description": "查口径", "system": "你会查口径", "max_steps": 1,
         "tools": [querycore.CALIBER_QUERY, "analysis_comparable"]},
        {"name": "复核", "description": "复核别人算的数", "system": "你复核", "max_steps": 1, "tools": []},
    ]
    return S.chain(
        S.node("start", "input"),
        S.node("team", "supervisor", goal="算一下销售额与最大单笔", agents=agents, max_rounds=2, max_parallel=1,
               approval="never"),
        S.node("out", "output", fields=[{"name": "答", "value": "{{ nodes.team.text }}"}]),
    )


async def test_a_cooperative_member_numbers_every_query_of_one_call(client):
    """成员绑的口径查询交回两条查询：逐条编号、逐条进成员自己的分析容器，编号与执行身份记在调用记录里。

    协作节点不记证据台账时编号只给成员自己下钻用，所以编号要和这次调用的调用记录一起留下来；否则事后看不出
    某个 Qn 是哪一次执行的哪条查询。给了 ledger 时（K10 收尾）同时逐条记一条运行台账的 query 条目。"""
    from app.capabilities.registry import snapshot_owned
    from app.engine.nodes import multi
    from app.tools.analysis import AnalysisScope

    asset = await published(client)
    parent = await run_parent(asset, metric_ids=["gmv", "peak"])
    payload = await tool_result(parent.id)
    content = json.dumps(payload, ensure_ascii=False)
    scope = AnalysisScope(node_id="team")
    record: dict[str, Any] = {}
    ledger: list[dict[str, Any]] = []

    out = await multi._numbered(scope, querycore.CALIBER_QUERY, content, None, parent.id, "team",
                                call_id="call-1", member="查数", exec_no=1, record=record, ledger=ledger)
    assert out.startswith("【证据 Q1、Q2】"), "一次交回多条查询：两条都要编号（以前只认第一条）"
    assert list(scope.queries) == ["Q1", "Q2"]
    traced = record["queries"]
    assert [q["alias"] for q in traced] == ["Q1", "Q2"]
    assert record["alias"] == "Q1" and record["artifact"] == payload["queries"][0]["artifact"]
    assert [q["query_id"] for q in traced] == [q["query_id"] for q in payload["queries"]]
    assert [q["artifact"] for q in traced] == [q["artifact"] for q in payload["queries"]]
    # 执行身份照抄口径查询自己带着的那一份（K09 算好的）：真的执行那次 SQL 的子运行、那条查询的第几次
    child = payload["run_id"]
    assert all(q["execution_id"] == p["execution_id"] and child in q["execution_id"]
               for q, p in zip(traced, payload["queries"]))
    assert all(q["attempt"] == p["attempt"] >= 1 for q, p in zip(traced, payload["queries"]))
    # 两条工件都是这次父运行（明确派生的子运行）落下的查询快照
    assert all([await snapshot_owned(q["artifact"], parent.id, querycore.CALIBER_QUERY) for q in traced])
    # 运行台账的条目：与 tools/llm 记的那一种同形，另带成员归属（node_id 前缀 + member）
    assert [e["node_id"] for e in ledger] == ["team:查数", "team:查数"]
    assert [e["member"] for e in ledger] == ["查数", "查数"]
    assert [e["artifact"] for e in ledger] == [q["artifact"] for q in payload["queries"]]
    assert all(e["kind"] == "query" and e["tool"] == querycore.CALIBER_QUERY and e["exec"] == 1
               and e["call_id"] == "call-1" for e in ledger)
    assert all(e["source"] and e["columns"] and e["rows"] >= 1 and e["truncated"] is False for e in ledger)
    assert [e["query_id"] for e in ledger] == [q["query_id"] for q in payload["queries"]]
    assert [e["execution_id"] for e in ledger] == [q["execution_id"] for q in payload["queries"]]
    assert all(e["origin"]["workflow_id"] == asset["workflow_id"] for e in ledger)
    assert all(e["parameters"]["inputs_hash"] for e in ledger), "复算要的材料跟着条目走"


async def test_a_cooperative_member_refuses_forged_and_other_runs_artifacts(client):
    """名字对了，工件也得对：伪造的、别的运行落的、不是查询快照的都不进成员的分析容器、也不进运行台账。"""
    from app.engine.nodes import multi
    from app.tools.analysis import AnalysisScope

    asset = await published(client)
    parent = await run_parent(asset, metric_ids=["gmv", "peak"])
    payload = await tool_result(parent.id)
    # 一份真的、一份工件 id 换掉的
    forged = json.loads(json.dumps(payload))
    forged["queries"][0]["artifact"] = "a" * 64
    content = json.dumps(forged, ensure_ascii=False)
    scope = AnalysisScope(node_id="team")
    record: dict[str, Any] = {}
    ledger: list[dict[str, Any]] = []
    out = await multi._numbered(scope, querycore.CALIBER_QUERY, content, None, parent.id, "team",
                                call_id="call-1", member="查数", exec_no=1, record=record, ledger=ledger)
    assert out == f"【证据 Q1】\n{content}", "只给认下来的那条编号"
    assert list(scope.queries) == ["Q1"], "伪造的那条不进容器，真的那条照记"
    assert [q["artifact"] for q in record["queries"]] == [payload["queries"][1]["artifact"]]
    assert [e["artifact"] for e in ledger] == [payload["queries"][1]["artifact"]], "台账里也只记真的那条"

    # 同一份真工件，换一个运行 id：不是这次运行（也不是它派生的子运行）落的，整条都不认
    real = json.dumps(payload, ensure_ascii=False)
    other = AnalysisScope(node_id="team")
    other_record: dict[str, Any] = {}
    other_ledger: list[dict[str, Any]] = []
    out = await multi._numbered(other, querycore.CALIBER_QUERY, real, None, "another-run", "team",
                                call_id="call-1", member="查数", exec_no=1, record=other_record,
                                ledger=other_ledger)
    assert out == real and not other.queries and not other_record and not other_ledger


async def test_a_cooperative_member_keeps_the_single_query_behavior():
    """单条数据源查询照旧：一个 Qn、内容前面一行编号，执行身份按这次调用的 id 算（同 tools/registry）。"""
    from app.engine.nodes import multi
    from app.tools.analysis import AnalysisScope

    scope = AnalysisScope(node_id="team")
    record: dict[str, Any] = {}
    ledger: list[dict[str, Any]] = []
    payload = {"artifact": "b" * 64, "columns": ["gmv"], "rows": [[1.0]], "truncated": False, "source": "shop",
               "sql": "SELECT 1"}
    content = json.dumps(payload)
    out = await multi._numbered(scope, "db_query__shop", content, None, "run-1", "team",
                                call_id="call-1", member="查数", exec_no=2, record=record, ledger=ledger)
    assert out == f"【证据 Q1】\n{content}" and list(scope.queries) == ["Q1"]
    assert [q["artifact"] for q in record["queries"]] == ["b" * 64]
    assert record["queries"][0]["execution_id"] == "run-1:team#call-1"
    assert record["queries"][0]["query_id"] == "team"
    [entry] = ledger
    assert entry["node_id"] == "team:查数" and entry["member"] == "查数" and entry["exec"] == 2
    assert entry["query_id"] == "team" and entry["attempt"] == 2
    assert entry["source"] == "shop" and entry["columns"] == ["gmv"] and entry["rows"] == 1


async def test_a_plain_tool_result_leaves_no_ledger_behind():
    """不是平台执行的查询（普通工具、编造的同样的 JSON）：容器、调用记录、台账都不动。"""
    from app.engine.nodes import multi
    from app.tools.analysis import AnalysisScope

    scope = AnalysisScope(node_id="team")
    record: dict[str, Any] = {}
    ledger: list[dict[str, Any]] = []
    content = json.dumps({"artifact": "c" * 64, "columns": ["x"], "rows": [[1]]})
    out = await multi._numbered(scope, "calculator", content, None, "run-1", "team",
                                call_id="call-1", member="查数", exec_no=1, record=record, ledger=ledger)
    assert out == content and not scope.queries and not record and not ledger


async def test_the_team_member_path_records_the_queries_it_numbered(client, monkeypatch):
    """走真的一次协作运行：成员绑口径查询，编号与执行身份随团队产出留下来，成员看到的是「【证据 Q1、Q2】」。"""
    from app.providers import mock_model

    asset = await published(client)
    args = {"workflow": asset["workflow_id"], "card": CARD, "metric_ids": ["gmv", "peak"], "inputs": dict(INPUTS)}
    decisions = [
        {"assignments": [{"agent": "查数", "instruction": "算 gmv 与 peak"}], "done": False, "reason": "先查"},
        {"assignments": [], "done": True, "reason": "够了"},
    ]
    turns = {"n": 0}
    seen: list[str] = []

    def _decide(self, messages):
        seen.extend(str(m.content) for m in messages if isinstance(m, ToolMessage))
        if self.response_format:
            i = turns["n"]
            turns["n"] += 1
            return AIMessage(content=json.dumps(decisions[i] if i < len(decisions) else decisions[-1],
                                                ensure_ascii=False))
        if self.tools and not any(isinstance(m, ToolMessage) for m in messages):
            return AIMessage(content="查一下口径。",
                             tool_calls=[{"name": querycore.CALIBER_QUERY, "args": args, "id": "c1"}])
        return AIMessage(content="查完了：gmv 与 peak 都取到了。")

    monkeypatch.setattr(mock_model.MockChatModel, "_decide", _decide)
    run = await run_manager.start(graph=team_graph(asset["workflow_id"]), input_payload={})
    row = await settled(run.id)
    assert row.status == "succeeded", row.error
    assert any(text.startswith("【证据 Q1、Q2】") for text in seen), "成员拿到的结果前面要写全这一次的编号"

    from app.engine.evidence_reader import read_sealed

    team = (await read_sealed(row.id)).payload_of(["team"])
    assert isinstance(team, dict)
    [record] = team["transcript"][0]["tool_calls"]
    traced = record["queries"]
    assert [q["alias"] for q in traced] == ["Q1", "Q2"]
    assert {q["query_id"] for q in traced} == {f"{asset['workflow_id']}:sales", f"{asset['workflow_id']}:shared"}
    # 执行身份指的是真的执行那次 SQL 的子运行；父子关系在子运行的来源里
    child_id = str(traced[0]["execution_id"]).split(":", 1)[0]
    async with SessionLocal() as session:
        child = await session.get(Run, child_id)
    assert child is not None and (child.origin or {}).get("parent_run_id") == row.id
    assert {str(q["execution_id"]).split(":", 1)[0] for q in traced} == {child_id}, "两条查询在同一次执行里"
    assert [q["attempt"] for q in traced] == [1, 1]


#: 成员这一轮查的是另一个窗口：和上面调用工具节点那条的 SQL 不一样，两条的查询快照各是各的（好断言归属）
MEMBER_INPUTS = {"start": "2026-08-01", "end": "2026-12-01"}


def team_report_graph(workflow_id: str) -> dict[str, Any]:
    """入口 → 调用工具（口径查询）→ 协作团队（成员也绑口径查询）→ 报告 → 出口。

    两处口径查询各交回两条 SQL，报告目录只收调用工具节点那两条：成员的查询是**附加**在封存证据上的。"""
    agents = [
        {"name": "查数", "description": "查口径", "system": "你会查口径", "max_steps": 1,
         "tools": [querycore.CALIBER_QUERY, "analysis_comparable"]},
        {"name": "复核", "description": "复核别人算的数", "system": "你复核", "max_steps": 1, "tools": []},
    ]
    return S.chain(
        S.node("start", "input"),
        S.node("look", "tool", tool=querycore.CALIBER_QUERY, args={
            "workflow": workflow_id, "card": CARD, "metric_ids": ["gmv", "peak"], "inputs": dict(INPUTS)}),
        S.node("team", "supervisor", goal="再按全年口径核一遍", agents=agents, max_rounds=2, max_parallel=1,
               approval="never"),
        S.node("write", "report", instructions="写一段说明"),
        S.node("out", "output", fields=[{"name": "报告", "value": "{{ nodes.write.text }}"}]),
    )


async def test_member_queries_join_the_sealed_evidence_without_renumbering_the_report(client, monkeypatch):
    """成员查到的数进父 Run 的封存证据（带成员前缀、来源、执行身份的 query 条目），报告目录的全局 Q 编号一个字不变。

    报告目录按祖先收证据（report.report_catalog 的 allowed），成员条目的节点 id 是「协作节点 id:成员名」，既不像子工作流
    转发的「协作节点 id/成员名」那样被 engine/compiler 的 _own_evidence 滤掉（那会让条目根本进不了封存），也不落在报告的
    收证据范围里——成员查到的数是附加证据：封存读取（sealed.queries / query_records / entries）、导出包的来源与遮罩判断、
    运行对比读得到，报告里的 Qn 还是原来那几条、面板按报告引用的那一份也不变。"""
    from app.core import artifact_store
    from app.engine.evidence_reader import read_sealed
    from app.providers import mock_model

    asset = await published(client)
    args = {"workflow": asset["workflow_id"], "card": CARD, "metric_ids": ["gmv", "peak"],
            "inputs": dict(MEMBER_INPUTS)}
    decisions = [
        {"assignments": [{"agent": "查数", "instruction": "按全年口径核一遍"}], "done": False, "reason": "先查"},
        {"assignments": [], "done": True, "reason": "够了"},
    ]
    turns = {"n": 0}

    def _decide(self, messages):
        if self.response_format:
            i = turns["n"]
            turns["n"] += 1
            return AIMessage(content=json.dumps(decisions[i] if i < len(decisions) else decisions[-1],
                                                ensure_ascii=False))
        if self.tools and not any(isinstance(m, ToolMessage) for m in messages):
            return AIMessage(content="查一下口径。",
                             tool_calls=[{"name": querycore.CALIBER_QUERY, "args": args, "id": "c1"}])
        return AIMessage(content="按全年口径核过一遍，与周口径口径一致。")

    monkeypatch.setattr(mock_model.MockChatModel, "_decide", _decide)
    run = await run_manager.start(graph=team_report_graph(asset["workflow_id"]), input_payload={})
    row = await settled(run.id)
    assert row.status == "succeeded", row.error

    sealed = await read_sealed(row.id)
    # 1) 调用工具节点那两条（tools.py 记的）与成员那两条（这次新加的）各是各的
    look = [e for e in await finished_evidence(row.id, "look") if e.get("kind") == "query"]
    member = [e for e in await finished_evidence(row.id, "team") if e.get("kind") == "query"]
    assert [e["member"] for e in member] == ["查数", "查数"], "一次调用两条查询，各记一条（不重复）"
    assert {e["node_id"] for e in member} == {"team:查数"}, "条目带成员归属"
    assert all(e["exec"] == 1 and e["call_id"] == "c1" and e["tool"] == querycore.CALIBER_QUERY for e in member)
    assert all(e["source"] == S.CURRENT["source"] and e["columns"] and e["rows"] >= 1
               and e["truncated"] is False for e in member)
    assert all(e["query_id"] and e["execution_id"] and e["attempt"] == 1 for e in member)
    assert all(e["origin"]["workflow_id"] == asset["workflow_id"] and e["parameters"]["inputs_hash"]
               for e in member), "复算要的出处与材料跟着条目走"
    assert not ({e["artifact"] for e in member} & {e["artifact"] for e in look}), "两处查的是不同窗口，工件各是各的"

    # 2) 封存证据（证据面板、导出包从这里认）：成员那两条在 sealed.queries / query_records / entries 里
    member_artifacts = {e["artifact"] for e in member}
    assert member_artifacts <= set(sealed.queries)
    assert all(sealed.queries[a]["tool"] == querycore.CALIBER_QUERY for a in member_artifacts)
    records = sealed.query_records_of("team")
    assert {r["execution_id"] for r in records} == {e["execution_id"] for e in member}
    assert [e.get("member") for e in sealed.entries if e.get("artifact") in member_artifacts] == ["查数", "查数"]

    # 3) 报告目录的全局 Q 编号一个字不变：只有调用工具节点那两条，成员的查数不在目录里
    write = sealed.payload_of(["write"])
    doc = artifact_store.load(str(write["doc_artifact"]))
    aliases = {a: e.get("artifact") for a, e in (doc.get("catalog") or {}).items() if e.get("kind") == "query"}
    assert set(aliases) == {"Q1", "Q2"}, "成员的查询不重编报告里的编号"
    assert set(aliases.values()) == {e["artifact"] for e in look}
    assert not (member_artifacts & set(aliases.values()))

    # 4) 导出包（可验证报告）也把成员查到的快照算进这次运行的查询：以前成员那条路连工具快照都不落，只能看事件里的预览，
    #    长一点的返回被截断就认不出来（打包时按「认不出」宁可多遮），来源也就认不出
    from app.bundle.build import _query_artifacts, _query_sources

    events = list(sealed.events)
    assert member_artifacts <= set(_query_artifacts(events))
    assert all(_query_sources(events).get(a) == S.CURRENT["source"] for a in member_artifacts)
    page = await client.get(f"/api/runs/{row.id}/bundle.html")
    assert page.status_code == 200, page.text


async def test_a_member_without_queries_leaves_the_team_output_unchanged(client, monkeypatch):
    """普通成员（没查库）的产出与以前一字不差：团队产出不带 evidence 键，transcript 里也不多东西。"""
    from app.providers import mock_model

    asset = await published(client)
    agents = [{"name": "撰稿", "description": "写一段话", "system": "你撰稿", "max_steps": 1, "tools": []}]
    graph = S.chain(
        S.node("start", "input"),
        S.node("team", "supervisor", goal="写一句", agents=agents, max_rounds=1, max_parallel=1, approval="never"),
        S.node("out", "output", fields=[{"name": "答", "value": "{{ nodes.team.text }}"}]),
    )

    turns = {"n": 0}

    def _decide(self, messages):
        if self.response_format:
            turns["n"] += 1
            decided = ({"assignments": [{"agent": "撰稿", "instruction": "写一句"}], "done": False, "reason": "派活"}
                       if turns["n"] == 1 else
                       {"assignments": [], "done": True, "reason": "够了"})
            return AIMessage(content=json.dumps(decided, ensure_ascii=False))
        return AIMessage(content="写好了。")

    monkeypatch.setattr(mock_model.MockChatModel, "_decide", _decide)
    run = await run_manager.start(graph=graph, input_payload={})
    row = await settled(run.id)
    assert row.status == "succeeded", row.error
    assert await finished_evidence(row.id, "team") == [], "成员没查库，一条台账条目都不记"
    from app.engine.evidence_reader import read_sealed

    team = (await read_sealed(row.id)).payload_of(["team"])
    [entry] = team["transcript"]
    assert set(entry) == {"round", "agent", "instruction", "text", "tool_calls", "duration_ms"}, entry
    assert "queries" not in team  # 团队产出里只有 transcript 那一条，没有别的证据键
    assert asset["workflow_id"]


async def test_a_member_without_an_analysis_tool_still_gets_its_queries_into_the_evidence(client, monkeypatch):
    """成员只绑口径查询（没绑分析工具）：交给成员的内容一个字不改（没有容器可放编号），查到的数照样进封存证据。"""
    from app.engine.evidence_reader import read_sealed
    from app.providers import mock_model

    asset = await published(client)
    args = {"workflow": asset["workflow_id"], "card": CARD, "metric_ids": ["gmv", "peak"],
            "inputs": dict(MEMBER_INPUTS)}
    agents = [{"name": "查数", "description": "查口径", "system": "你会查口径", "max_steps": 1,
               "tools": [querycore.CALIBER_QUERY]},
              {"name": "复核", "description": "复核别人算的数", "system": "你复核", "max_steps": 1, "tools": []}]
    graph = S.chain(
        S.node("start", "input"),
        S.node("team", "supervisor", goal="算一下销售额", agents=agents, max_rounds=2, max_parallel=1,
               approval="never"),
        S.node("out", "output", fields=[{"name": "答", "value": "{{ nodes.team.text }}"}]),
    )
    decisions = [
        {"assignments": [{"agent": "查数", "instruction": "算 gmv 与 peak"}], "done": False, "reason": "先查"},
        {"assignments": [], "done": True, "reason": "够了"},
    ]
    turns = {"n": 0}
    seen: list[str] = []

    def _decide(self, messages):
        seen.extend(str(m.content) for m in messages if isinstance(m, ToolMessage))
        if self.response_format:
            i = turns["n"]
            turns["n"] += 1
            return AIMessage(content=json.dumps(decisions[i] if i < len(decisions) else decisions[-1],
                                                ensure_ascii=False))
        if self.tools and not any(isinstance(m, ToolMessage) for m in messages):
            return AIMessage(content="查一下口径。",
                             tool_calls=[{"name": querycore.CALIBER_QUERY, "args": args, "id": "c1"}])
        return AIMessage(content="查完了。")

    monkeypatch.setattr(mock_model.MockChatModel, "_decide", _decide)
    run = await run_manager.start(graph=graph, input_payload={})
    row = await settled(run.id)
    assert row.status == "succeeded", row.error
    assert seen and all(not t.startswith("【证据") for t in seen), "没绑分析工具：交回成员的内容一个字不改"

    entries = [e for e in await finished_evidence(row.id, "team") if e.get("kind") == "query"]
    assert len(entries) == 2 and {e["node_id"] for e in entries} == {"team:查数"}
    assert all(e["member"] == "查数" and e["query_id"] and e["execution_id"] and e["attempt"] == 1
               for e in entries)
    sealed = await read_sealed(row.id)
    assert {e["artifact"] for e in entries} <= set(sealed.queries)
    [record] = (sealed.payload_of(["team"]) or {}).get("transcript", [{}])[0].get("tool_calls") or [{}]
    assert [q["artifact"] for q in record["queries"]] == [e["artifact"] for e in entries]
    assert all("alias" not in q for q in record["queries"]), "没有容器就没有本节点内的编号"


# ==========================================================================
# 6、复核出来的三条：授权范围继承、逐次执行身份、发起时预解析口径的源
# ==========================================================================

#: 记录表的列（和 tests/test_records.py 同一个形状的假数据）
RECORD_COLUMNS = [{"name": "门店", "label": "门店", "type": "string", "required": True},
                  {"name": "数量", "label": "报损数量", "type": "integer", "required": True}]
#: 记录表的行级范围认的一列：查全部、只看自己的、一行都不给
RECORD_COUNT = "SELECT count(*) AS n FROM records"


def records_graph(source_name: str) -> dict[str, Any]:
    """父 Run 的图：查这张记录表（固定的数据版本里带着它的高水位与行级范围）。"""
    return S.chain(
        S.node("start", "input"),
        S.node("q", "tool", tool=f"db_query__{source_name}", args={"sql": RECORD_COUNT}),
        S.node("out", "output", fields=[{"name": "n", "value": "{{ nodes.q }}"}]),
    )


async def records_source(name: str = "门店报损", *, grants: dict[str, str] | None = None) -> tuple[str, str]:
    """建一张记录表：所属应用 + 逐人的授权。返回 (源 id, 源名)。grants 缺省是给 uma 的一档 use。"""
    from app.db.models import DataSource
    from app.db.models_apps import App, AppGrant
    from app.records.tables import ensure_table

    async with SessionLocal() as session:
        app = App(name=f"{name}-{uuid.uuid4().hex[:6]}", created_by="建立人")
        session.add(app)
        await session.flush()
        for who, role in ({"uma": "use"} if grants is None else grants).items():
            session.add(AppGrant(app_id=app.id, principal_kind="user", principal=who, role=role))
        table = await ensure_table(session, app, RECORD_COLUMNS)
        await session.commit()
        source = await session.get(DataSource, table.source_id)
    # 一行 uma 提交的记录：行级范围是 all / mine 时看得见这一行，none 看不见
    store.append(path_of(table), columns_of(table), {"门店": "一店", "数量": 1}, version=table.version,
                 run_id=None, actor="uma", action_key=uuid.uuid4().hex)
    return source.id, source.name


async def grant_caliber(workflow_id: str, version: int, who: str, role: str) -> str:
    """给这个人在**这一版口径**上的资产授权：一份应用（当前发布版精确固定着这一版）+ 一条 AppGrant。返回应用 id。

    普通使用者按口径取数靠的就是它（apps/access.usable_calibers / caliber_role，K14）：没有这一份，QueryCore 在起 Run
    之前就拒（querycore_not_authorized），工具那一层也拿不到数——记录表的行级授权（records_source 的 grants）不管口径。"""
    from app.db.models_apps import App, AppGrant, AppVersion

    async with SessionLocal() as session:
        app = App(name=f"口径授权-{uuid.uuid4().hex[:6]}", status="active", published_version=1)
        session.add(app)
        await session.flush()
        session.add(AppVersion(app_id=app.id, version=1, spec={}, spec_hash="grant",
                               pins=[{"alias": "cal", "workflow_id": workflow_id, "version": int(version)}]))
        session.add(AppGrant(app_id=app.id, principal_kind="user", principal=who, role=role))
        await session.commit()
        return app.id


async def wait_for(run_id: str, statuses: tuple[str, ...], timeout: float = 30.0) -> Run:
    """等运行到这几个状态之一（settled 把 interrupted 也当终态，审批那一段要另等）。"""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        async with SessionLocal() as session:
            row = await session.get(Run, run_id)
        if row is not None and row.status in statuses:
            return row
        await asyncio.sleep(0.05)
    raise AssertionError(f"运行 {run_id} 没有进入 {statuses}")


async def test_inherit_scope_keeps_the_parents_authorized_rows():
    """继承父 Run 的 data_versions：记录表的行级范围原样带着（all/mine/none 都不动），缺 rows 的按最窄的算。"""
    from app.records.views import inherit_scope

    versions = {
        "a": {"name": "甲", "origin": "records", "table": "t", "version": 1, "seq": 3, "rows": "all"},
        "b": {"name": "乙", "origin": "records", "table": "t", "version": 1, "seq": 4, "rows": "mine",
              "viewer": "uma"},
        "c": {"name": "丙", "origin": "records", "table": "t", "version": 1, "seq": 5, "rows": "none"},
        "d": {"name": "丁", "origin": "records", "table": "t", "version": 1, "seq": 6},     # 老运行：没有范围
        "e": {"snapshot": "s1", "name": "上传表格"},                                          # 不是记录表
    }
    inherit_scope(versions)
    assert versions["a"]["rows"] == "all"
    assert versions["b"]["rows"] == "mine" and versions["b"]["viewer"] == "uma"
    assert versions["c"]["rows"] == "none"
    assert versions["d"]["rows"] == "none", "没有范围的老项按最窄的算，绝不当成 all"
    assert versions["e"] == {"snapshot": "s1", "name": "上传表格"}, "上传源不碰"


async def test_a_derived_child_inherits_the_parents_authorized_scope(client):
    """正式父 Run 的行级范围是 all；派生的子运行是 exploratory，也不能按发起人重算成 mine。"""
    from app.engine.runner import _data_versions  # noqa: F401
    from tests.fakes import auth_env

    await auth_env.turn_on_local()
    try:
        await auth_env.make_user("uma", "user")
        source_id, name = await records_source()
        graph = records_graph(name)
        # 父 Run：正式运行 → 看全部
        parent = await run_manager.start(graph=graph, input_payload={}, run_class="formal", started_by="uma")
        parent = await settled(parent.id)
        assert parent.status == "succeeded", parent.error
        assert parent.data_versions[source_id]["rows"] == "all"
        # 反例：同一个起点自己发起（不继承父 Run）→ 按发起人的 use 档收成 mine。没有这一条，上面那句断言说明不了什么
        own = await settled((await run_manager.start(graph=graph, input_payload={}, run_class="exploratory",
                                                     started_by="uma")).id)
        assert own.data_versions[source_id]["rows"] == "mine"
        assert own.data_versions[source_id]["viewer"] == "uma"
        # 反例（同一条规则直接跑一遍）：拿父 Run 那一份按子运行的类别与发起人重算，all 会收成 mine
        from app.records.views import scope_entries

        recomputed = copy.deepcopy(parent.data_versions)
        async with SessionLocal() as session:
            await scope_entries(session, recomputed, run_class="exploratory", started_by="uma")
        assert recomputed[source_id]["rows"] == "mine"
        # 派生：照父 Run 那一份原样，不收窄、也不添 viewer
        child = await settled((await run_manager.start(graph=graph, input_payload={}, run_class="exploratory",
                                                       started_by="uma", data_versions=parent.data_versions)).id)
        assert child.status == "succeeded", child.error
        assert child.data_versions[source_id]["rows"] == "all", "父已授权 all，子运行不能收成 mine"
        assert "viewer" not in child.data_versions[source_id]
        assert child.data_versions[source_id]["seq"] == parent.data_versions[source_id]["seq"]
    finally:
        await auth_env.reset()


async def test_a_compose_parent_keeps_its_scope_after_the_user_is_disabled(client):
    """发起之后人停用了：同一次运行（派生出来的那一段）仍按发起时定下的那一档看，不改判成 none。"""
    from app.db.models import User
    from tests.fakes import auth_env

    await auth_env.turn_on_local()
    try:
        await auth_env.make_user("uma", "user")
        source_id, name = await records_source()
        graph = records_graph(name)
        parent = await settled((await run_manager.start(graph=graph, input_payload={}, started_by="uma")).id)
        assert parent.data_versions[source_id]["rows"] == "mine"
        async with SessionLocal() as session:
            user = (await session.execute(select(User).where(User.username == "uma"))).scalar_one()
            user.disabled = True
            await session.commit()
        # 现在自己发起一次：人不在（停用）→ 一行都不给
        fresh = await settled((await run_manager.start(graph=graph, input_payload={}, started_by="uma")).id)
        assert fresh.data_versions[source_id]["rows"] == "none"
        # 派生：照父 Run 那一份（mine + 看的人），不因为人后来停用而改判
        child = await settled((await run_manager.start(graph=graph, input_payload={}, run_class="exploratory",
                                                       started_by="uma", data_versions=parent.data_versions)).id)
        assert child.data_versions[source_id]["rows"] == "mine"
        assert child.data_versions[source_id]["viewer"] == "uma"
    finally:
        await auth_env.reset()


# ---- 2) 同一份工件对应两条不同的逻辑查询 ----------------------------------


def same_duration(monkeypatch) -> None:
    """把每条查询的耗时记成 0：同一条 SQL、同一批数据、同一份参数的两条查询就得到**同一个**工件（内容寻址）。

    耗时是快照内容的一部分（每条查询各记各的、几毫秒上下的抖动）：这一条测的是「同一份工件对应两条不同的逻辑查询」
    时证据索引的取法，所以先把那个抖动按平，让两份内容真的逐字相同。"""
    import dataclasses

    from app.tools import datasource

    real = datasource.run_query

    async def zeroed(source: Any, sql: str, **kw: Any) -> Any:
        return dataclasses.replace(await real(source, sql, **kw), elapsed_ms=0)

    monkeypatch.setattr(datasource, "run_query", zeroed)


async def test_two_logical_queries_sharing_one_artifact_are_both_reported(client, monkeypatch):
    """两个取数节点写同一条 SQL、读同一批数据：内容寻址下是**同一个**工件，但它们是两条不同的逻辑查询。

    按工件 id 去重的证据索引会把它们并成一条，早先那条就找不到自己那一次——所以逐次记下的执行身份才是取法。"""
    same_duration(monkeypatch)
    asset = await make_workflow(graph=twin_graph())
    await publish(client, asset)
    outcome = await run_once(asset, metric_ids=["left", "right"], inputs=dict(INPUTS))
    assert outcome.ok, [p["code"] for p in outcome.problems]
    data = outcome.as_dict()
    assert [q["query_id"] for q in data["queries"]] == [f"{asset['workflow_id']}:one",
                                                       f"{asset['workflow_id']}:two"]
    assert all(q["found"] for q in data["queries"])
    assert len({q["artifact"] for q in data["queries"]}) == 1, "同一条 SQL 同一批数据 → 同一个工件"
    assert len({q["execution_id"] for q in data["queries"]}) == 2, "两条逻辑查询各有各的执行身份"
    assert all(q["row_count"] == 1 and q["columns"] for q in data["queries"])
    assert {m["id"]: m["value"] for m in outcome.metrics} == {"left": WINDOW_SUM, "right": WINDOW_SUM}


async def test_a_later_execution_of_the_same_sql_takes_its_own_record(client):
    """同一个节点执行两次（同一条 SQL、同一份数据、同一个工件）：引用取**最后一次**那次执行的身份。"""
    from app.engine.evidence_reader import read_sealed

    asset = await published(client)
    outcome = await run_once(asset, metric_ids=["gmv"], inputs=dict(INPUTS))
    assert outcome.ok
    sealed = await read_sealed(outcome.run_id)
    records = sealed.query_records_of("sales")
    assert len(records) == 1 and records[-1]["artifact"] == outcome.queries[0]["artifact"]
    # 逐次记录与按工件去重的那一份指向同一次执行（认法不同、结论一致）
    assert sealed.queries[records[-1]["artifact"]]["exec"] == records[-1]["exec"]
    assert querycore._query_record_of(sealed, "sales")["execution_id"] == records[-1]["execution_id"]
    # 没有这个节点的逐次记录时（老运行、只回了 tool.end）退回按工件去重的那一份
    assert querycore._query_record_of(sealed, "shared") is None


# ---- 3) 父 Run 发起时预解析口径那一版读的源 -------------------------------


def book(sheets: dict[str, list[list]]) -> bytes:
    import io

    from openpyxl import Workbook

    wb = Workbook()
    wb.remove(wb.active)
    for title, rows in sheets.items():
        ws = wb.create_sheet(title)
        for row in rows:
            ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


#: 上传表格第一版：数量合计 3；第二版（重传）：合计 100，另多一张表
UP_V1 = {"明细": [["区域", "数量"], ["分区甲", 1], ["分区乙", 2]]}
UP_V2 = {"明细": [["区域", "数量"], ["分区甲", 60], ["分区乙", 40]], "新增": [["时段", "客流"], ["上午", 7]]}


async def upload_source(sheets: dict[str, list[list]], *, name: str | None = None) -> tuple[str, str, str]:
    """发布一版上传表格，返回 (源 id, 快照 id, 源名)。给了 name 就是**往同一个源上重传**（新的一版）。"""
    from app.data import table_versions

    name = name or f"up_{uuid.uuid4().hex[:8]}"
    async with SessionLocal() as session:
        row = (await session.execute(select(DataSource).where(DataSource.name == name))).scalar_one_or_none()
        if row is None:
            row = DataSource(name=name, kind="sqlite", readonly=True, origin="upload", enabled=True)
            session.add(row)
        result = await table_versions.publish_upload(session, row, book(sheets), "客流.xlsx",
                                                     header_row=1, mixed="reject", raw_mode=False)
        return row.id, result.snapshot_id, name


def upload_caliber_graph(source_name: str) -> dict[str, Any]:
    """口径的取数节点查这张上传表格（国产表名照原样写在 SQL 里）。"""
    return S.chain(
        S.node("start", "input"),
        S.node("fetch", "tool", tool=f"db_query__{source_name}",
               args={"sql": 'SELECT SUM("数量") AS total FROM "明细"'}),
        S.node(CARD, "metrics", caliber="上传合计", caliber_version="v1",
               metrics=[{"id": "total", "name": "合计", "unit": "件",
                         "expression": "cell(nodes.fetch, 0, 'total')"}]),
        S.node("write", "report", instructions="写周销售"),
        S.node("out", "output", fields=[{"name": "报告", "value": "{{ nodes.write.text }}"}]),
    )


async def test_the_parent_pins_the_sources_its_caliber_reads(client):
    """绑了 caliber_query 的节点读的**不在本图里**：口径那一版图里的源也要在父 Run 发起时固定下来。"""
    from app.engine.schema import GraphSpec
    from app.engine.runner import _data_versions

    other_id, _, _ = await upload_source(UP_V1)                    # 别的源：不许被顺手固定
    source_id, snap1, name = await upload_source(UP_V1)
    asset = await make_workflow(graph=upload_caliber_graph(name))
    await publish(client, asset)
    graph = parent_graph(asset["workflow_id"], metric_ids=["total"], inputs={})
    async with SessionLocal() as session:
        pins = await _data_versions(session, GraphSpec.model_validate(graph))
    assert pins[source_id]["snapshot"] == snap1, "口径那一版的取数源要在父 Run 发起时就固定"
    assert pins[source_id]["name"] == name
    assert other_id not in pins, "没用到的源照旧不固定"
    # 认不到工作流（名字写错了）：这一次调用会被 QueryCore 拒，一条数据都不读——不必把全部源都固定上
    broken = parent_graph("nope-not-a-workflow", metric_ids=["total"], inputs={})
    async with SessionLocal() as session:
        assert await _data_versions(session, GraphSpec.model_validate(broken)) == {}


async def test_an_agent_or_templated_caliber_reference_pins_every_source(client):
    """口径是 Agent 挑的、或者写的是模板：发起时说不准读哪个源，把启用的上传源全部固定上。"""
    from app.engine.schema import GraphSpec
    from app.engine.runner import _data_versions

    source_id, snap1, _ = await upload_source(UP_V1)
    agent_graph = S.chain(
        S.node("start", "input"),
        S.node("bot", "agent", prompt="算一下", tools=[querycore.CALIBER_QUERY], approval="never"),
        S.node("out", "output", fields=[{"name": "答", "value": "{{ nodes.bot.text }}"}]),
    )
    async with SessionLocal() as session:
        pins = await _data_versions(session, GraphSpec.model_validate(agent_graph))
    assert pins.get(source_id, {}).get("snapshot") == snap1
    templated = S.chain(
        S.node("start", "input"),
        S.node("look", "tool", tool=querycore.CALIBER_QUERY, args={
            "workflow": "{{ vars.w }}", "card": CARD, "metric_ids": ["total"], "inputs": {}}),
        S.node("out", "output", fields=[{"name": "答", "value": "{{ nodes.look.ok }}"}]),
    )
    async with SessionLocal() as session:
        pins = await _data_versions(session, GraphSpec.model_validate(templated))
    assert pins.get(source_id, {}).get("snapshot") == snap1


async def test_a_reupload_after_launch_still_reads_the_pinned_version(client):
    """父 Run 发起之后重传：这次运行里口径读的还是发起时那一版（run.started 里记的就是它）。"""
    source_id, s1, name = await upload_source(UP_V1)
    asset = await make_workflow(graph=upload_caliber_graph(name))
    await publish(client, asset)
    graph = parent_graph(asset["workflow_id"], metric_ids=["total"], inputs={}, gate=True)
    run = await run_manager.start(graph=graph, input_payload={})
    assert (await wait_for(run.id, ("interrupted",))).status == "interrupted"

    _, s2, _ = await upload_source(UP_V2, name=name)      # 停在确认上时重传：当前版本换成合计 100 的那一版
    assert s2 != s1
    await run_manager.resume(run.id, {"approved": True})
    done = await settled(run.id)
    assert done.status == "succeeded", done.error

    assert done.data_versions[source_id]["snapshot"] == s1
    started = await events_of(run.id, "run.started")
    assert all(e["data_versions"][source_id]["snapshot"] == s1 for e in started), "run.started 里可见实际固定的版本"
    tool = await tool_result(done.id)
    assert tool["ok"], tool.get("problems")
    assert tool["metrics"][0]["value"] == 3, "重传之后这次运行读的还是发起时那一版"
    assert tool["data"]["data_versions"] == {name: s1}


async def test_two_calls_in_one_parent_run_read_the_same_version(client):
    """父 Run 的两次调用（中间重传过）读的还是同一版：不是各自在子运行里补固定当前版本。"""
    source_id, s1, name = await upload_source(UP_V1)
    asset = await make_workflow(graph=upload_caliber_graph(name))
    await publish(client, asset)
    args = {"workflow": asset["workflow_id"], "card": CARD, "metric_ids": ["total"], "inputs": {}}
    graph = S.chain(
        S.node("start", "input"),
        S.node("one", "tool", tool=querycore.CALIBER_QUERY, args=dict(args)),
        S.node("gate", "human", mode="approve", title="看一眼"),
        S.node("two", "tool", tool=querycore.CALIBER_QUERY, args=dict(args)),
        S.node("out", "output", fields=[{"name": "答", "value": "{{ nodes.two.ok }}"}]),
    )
    run = await run_manager.start(graph=graph, input_payload={})
    assert (await wait_for(run.id, ("interrupted",))).status == "interrupted"

    _, s2, _ = await upload_source(UP_V2, name=name)      # 第一次调用之后重传（同一个源的新一版）
    assert s2 != s1
    await run_manager.resume(run.id, {"approved": True})
    done = await settled(run.id)
    assert done.status == "succeeded", done.error

    first, second = await tool_result(done.id, "one"), await tool_result(done.id, "two")
    assert first["ok"] and second["ok"], (first.get("problems"), second.get("problems"))
    assert first["metrics"][0]["value"] == 3 and second["metrics"][0]["value"] == 3, \
        "同一次运行里的两次调用要读同一版数据"
    assert done.data_versions[source_id]["snapshot"] == s1
    for result in (first, second):
        async with SessionLocal() as session:
            child = await session.get(Run, result["run_id"])
        assert child.data_versions[source_id]["snapshot"] == s1, "子运行照父 Run 固定的那一版，不各自补固定"


# ==========================================================================
# 7、晚解析的源固定在**父 Run** 上（发布得晚 / 推荐版变了 / 动态引用）
# ==========================================================================


def records_caliber_graph(source_name: str) -> dict[str, Any]:
    """口径的取数节点查这张记录表（读哪些行由这次运行定下的行级范围决定）。"""
    return S.chain(
        S.node("start", "input"),
        S.node("fetch", "tool", tool=f"db_query__{source_name}", args={"sql": RECORD_COUNT}),
        S.node(CARD, "metrics", caliber="记录合计", caliber_version="v1",
               metrics=[{"id": "n", "name": "条数", "unit": "条",
                         "expression": "cell(nodes.fetch, 0, 'n')"}]),
        S.node("write", "report", instructions="写周销售"),
        S.node("out", "output", fields=[{"name": "报告", "value": "{{ nodes.write.text }}"}]),
    )


def gated_parent_graph(workflow_id: str, *, metric_ids: list[str], card: str = CARD,
                       calls: tuple[str, ...] = ("look",)) -> dict[str, Any]:
    """父 Run 的图：入口 → 人工确认 →（一到两处）调用工具（caliber_query）→ 出口。

    审批挡在调用之前：测试要在「发起」与「第一次调用」之间做事（发布口径、重传、换推荐版）。"""
    parts = [S.node("start", "input"), S.node("gate", "human", mode="approve", title="看一眼")]
    for name in calls:
        parts.append(S.node(name, "tool", tool=querycore.CALIBER_QUERY, args={
            "workflow": workflow_id, "card": card, "metric_ids": metric_ids, "inputs": {}}))
    parts.append(S.node("out", "output", fields=[{"name": "答", "value": "运行结束"}]))
    return S.chain(*parts)


async def pin_events(run_id: str) -> list[dict[str, Any]]:
    """这次运行里补固定数据版本的那些事件（tools/datasource.LATE_PIN_CODE）。"""
    from app.tools.datasource import LATE_PIN_CODE

    return [e for e in await events_of(run_id, "log") if e.get("code") == LATE_PIN_CODE]


@pytest.mark.parametrize("run_class, grant, rows, viewer", [
    ("formal", "use", "all", None),          # 正式父 Run：看全部
    ("exploratory", "use", "mine", "uma"),   # 交互：use 档只看自己的
    ("exploratory", "observe", "all", None),  # 交互：observe 档看全部
    ("exploratory", None, "none", None),     # 交互：没有口径授权，这一次调用在起 Run 之前就被拒
])
async def test_a_caliber_published_after_launch_is_pinned_on_the_parent(client, run_class, grant, rows, viewer):
    """新发布：口径在父 Run 发起时还没有推荐版（发起时算不到），第一次调用时按**父 Run 的**类别与授权补固定。

    固定记在父 Run 上、追加一条补固定事件（run.started 已经落库），子运行照父 Run 那一份继承——不能落到子运行里
    按 exploratory 重算（那会把正式父 Run 的 all 收成 mine/none）。

    exploratory 这一档（普通使用者按口径取数）另有一道 K14 的资产授权：发布之后另给 uma 一份与这里那一档相同的授权
    （grant_caliber）；**没有**口径授权的那一档调用在起 Run 之前就被拒，一行都不读、也不补固定。"""
    from app.engine.runner import _data_versions
    from app.engine.schema import GraphSpec
    from tests.fakes import auth_env

    admin = await auth_env.turn_on_local()
    try:
        await auth_env.make_user("uma", "user")
        source_id, name = await records_source(grants={} if grant is None else {"uma": grant})
        asset = await make_workflow(graph=records_caliber_graph(name))     # 建了，但**还没发布**
        graph = gated_parent_graph(asset["workflow_id"], metric_ids=["n"])
        async with SessionLocal() as session:
            assert await _data_versions(session, GraphSpec.model_validate(graph)) == {}, "还没发布：发起时算不到"
        run = await run_manager.start(graph=graph, input_payload={}, run_class=run_class, started_by="uma")
        assert (await wait_for(run.id, ("interrupted",))).status == "interrupted"

        if run_class == "formal":                        # 正式父 Run 走 formal judge：依据要齐
            await confirm(asset, await add_record(asset, metric_ids=("n",)))
        await publish(admin, asset)                      # 停在确认上时发布：推荐版出现了
        if grant is not None:
            await grant_caliber(asset["workflow_id"], 1, "uma", grant)
        await run_manager.resume(run.id, {"approved": True})
        done = await settled(run.id)
        if grant is None:
            assert done.status == "failed" and querycore.NOT_AUTHORIZED in str(done.error or ""), done.error
            assert source_id not in (done.data_versions or {}), "被拒的这一次没有固定任何数据版本"
            assert await pin_events(done.id) == [], "被拒的这一次连一条补固定事件都不该有"
            return
        assert done.status == "succeeded", done.error

        entry = done.data_versions[source_id]
        assert entry["rows"] == rows, "行级范围取父 Run 已授权的那一档，不按子运行的类别重算"
        assert entry.get("viewer") == viewer
        assert entry["late"] is True and entry["caliber"] == f"{asset['workflow_id']}@1:{CARD}"
        events = await pin_events(done.id)
        assert len(events) == 1 and events[0]["source"] == name and events[0]["rows"] == rows
        assert events[0]["caliber"] == f"{asset['workflow_id']}@1:{CARD}" and events[0]["late"] is True
        # 子运行照父 Run 那一份继承：不是它自己按 exploratory 另算一份
        tool = await tool_result(done.id)
        assert tool["ok"], tool.get("problems")
        async with SessionLocal() as session:
            child = await session.get(Run, tool["run_id"])
        assert child.data_versions[source_id]["rows"] == rows
        assert child.data_versions[source_id]["caliber"] == f"{asset['workflow_id']}@1:{CARD}"
        assert tool["metrics"][0]["value"] == (1 if rows != "none" else 0)
    finally:
        await admin.aclose()
        await auth_env.reset()


async def test_later_calls_reuse_the_first_pin(client):
    """同父 Run 的两次调用：第一次把源固定在父 Run 上，第二次照旧复用那一版，不重新固定、不再补一条事件。"""
    from app.engine.runner import _data_versions
    from app.engine.schema import GraphSpec
    from tests.fakes import auth_env

    admin = await auth_env.turn_on_local()
    try:
        await auth_env.make_user("uma", "user")
        source_id, _ = await records_source()
        asset = await make_workflow(graph=records_caliber_graph(await _records_name(source_id)))
        graph = gated_parent_graph(asset["workflow_id"], metric_ids=["n"], calls=("one", "two"))
        async with SessionLocal() as session:
            assert await _data_versions(session, GraphSpec.model_validate(graph)) == {}
        run = await run_manager.start(graph=graph, input_payload={}, run_class="formal", started_by="uma")
        assert (await wait_for(run.id, ("interrupted",))).status == "interrupted"
        await confirm(asset, await add_record(asset, metric_ids=("n",)))    # 正式父 Run：依据要齐
        await publish(admin, asset)
        # 正式运行也要这一版口径的资产授权（K14）：发布之后按同一档补上
        await grant_caliber(asset["workflow_id"], 1, "uma", "use")
        await run_manager.resume(run.id, {"approved": True})
        done = await settled(run.id)
        assert done.status == "succeeded", done.error

        entry = done.data_versions[source_id]
        assert entry["rows"] == "all" and entry["late"] is True
        assert len(await pin_events(done.id)) == 1, "两次调用只补固定一次：后面那次复用第一版"
        first, second = await tool_result(done.id, "one"), await tool_result(done.id, "two")
        assert first["ok"] and second["ok"], (first.get("problems"), second.get("problems"))
        assert first["data"]["data_versions"] == second["data"]["data_versions"]
        for tool in (first, second):
            async with SessionLocal() as session:
                child = await session.get(Run, tool["run_id"])
            assert child.data_versions[source_id]["seq"] == entry["seq"]
            assert child.data_versions[source_id]["rows"] == "all"
    finally:
        await admin.aclose()
        await auth_env.reset()


async def _records_name(source_id: str) -> str:
    async with SessionLocal() as session:
        return str((await session.get(DataSource, source_id)).name)


async def test_a_new_recommended_version_with_a_moved_source_is_refused(client):
    """审批期间推荐版换了一版、源也重传了：这一次要执行的口径引用的是另一版——明确说冲突，不静默跨版。"""
    source_id, s1, name = await upload_source(UP_V1)
    asset = await make_workflow(graph=upload_caliber_graph(name))
    await publish(client, asset)                                  # v1 是推荐版
    graph = gated_parent_graph(asset["workflow_id"], metric_ids=["total"])
    run = await run_manager.start(graph=graph, input_payload={})
    row = await wait_for(run.id, ("interrupted",))
    assert row.status == "interrupted"
    assert row.data_versions[source_id]["snapshot"] == s1         # 发起时固定在这一版
    assert row.data_versions[source_id]["caliber"] == f"{asset['workflow_id']}@1:{CARD}"

    _, s2, _ = await upload_source(UP_V2, name=name)              # 重传：当前版本换成合计 100 的那一版
    changed = upload_caliber_graph(name)
    next(metrics for metrics in changed["nodes"] if metrics["id"] == CARD)["data"]["config"]["metrics"][0]["label"] = "合计2"
    asset = await add_version(asset, changed, version=2)
    await publish(client, asset, version=2)                       # 推荐版换成 v2（引用的是重传后的那一版）
    await run_manager.resume(run.id, {"approved": True})
    done = await settled(run.id)

    assert done.status == "failed", done.error
    error = done.error or ""
    assert querycore.DATA_VERSION_CONFLICT in error, error
    assert name in error and s1[:12] in error and s2[:12] in error, error
    async with SessionLocal() as session:
        children = (await session.execute(select(Run).where(Run.origin.is_not(None)))).scalars().all()
    assert [c for c in children if (c.origin or {}).get("parent_run_id") == done.id] == [], "冲突就不发起子运行"


async def test_two_calls_after_a_late_pin_read_the_same_version(client):
    """新发布的口径：第一次调用把源固定在父 Run 上，两次调用之间重传，第二次读的还是那一版（同一次运行不跨版）。"""
    source_id, s1, name = await upload_source(UP_V1)
    asset = await make_workflow(graph=upload_caliber_graph(name))         # 还没发布
    args = {"workflow": asset["workflow_id"], "card": CARD, "metric_ids": ["total"], "inputs": {}}
    graph = S.chain(
        S.node("start", "input"),
        S.node("gate1", "human", mode="approve", title="看一眼"),
        S.node("one", "tool", tool=querycore.CALIBER_QUERY, args=dict(args)),
        S.node("gate2", "human", mode="approve", title="再确认"),
        S.node("two", "tool", tool=querycore.CALIBER_QUERY, args=dict(args)),
        S.node("out", "output", fields=[{"name": "答", "value": "结束"}]),
    )
    run = await run_manager.start(graph=graph, input_payload={})
    assert (await wait_for(run.id, ("interrupted",))).status == "interrupted"

    await publish(client, asset)                                          # 新发布：发起时算不到
    await run_manager.resume(run.id, {"approved": True})
    assert (await wait_for(run.id, ("interrupted",))).status == "interrupted"

    _, s2, _ = await upload_source(UP_V2, name=name)                      # 两次调用之间重传
    assert s2 != s1
    await run_manager.resume(run.id, {"approved": True})
    done = await settled(run.id)
    assert done.status == "succeeded", done.error

    assert done.data_versions[source_id]["snapshot"] == s1
    assert len(await pin_events(done.id)) == 1, "第一次调用补固定一次，第二次复用"
    first, second = await tool_result(done.id, "one"), await tool_result(done.id, "two")
    assert first["ok"] and second["ok"], (first.get("problems"), second.get("problems"))
    assert [first["metrics"][0]["value"], second["metrics"][0]["value"]] == [3, 3], \
        "同一次运行里的两次调用读同一版数据"
