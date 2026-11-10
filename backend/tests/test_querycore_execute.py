"""K09 的进程内 QueryCore 固定执行与最小 API（app/ledger/querycore.py、POST /api/ledger/querycore）。

这份文件盯住这几条：

1. **版本只解析一次并固定。** 不点名读推荐版（Workflow.published_version 指的那一版）并固定到它；点名始终固定到点名的那一版。
2. **只按固定发布执行。** 必须读并核验 K07 的发布清单（读得出来、内容哈希对得上、就是这一版这一张图）；编译器/绑定/
   参数/解析器约定不兼容、清单错图/缺卡一律明确拒，**绝不按新编译器现场重解释旧发布**；老资产（没有清单）明确拒，
   旧 trial 路径一个字不动。
3. **用 K08 闭包选指标、K01 参数语义。** 选 A 时 B 独有的取数节点与参数不进这次范围；A、B 共用一条 SQL 时那条 SQL 保留并由
   computes_unselected 声明它还在算谁；缺参数/未声明参数在发 SQL 之前就拒（一条 SQL 都不发）。
4. **内部 LLM = 0、作者侧 SQL 编译 = 0、零运行期重绑定。** 探针图里没有模型节点；执行过的语句与这次计划渲染出来的一致、
   参数输入哈希一致。
5. **类型化交回。** 大整数不转 float（精确整数原样）；Decimal 过投影时换成精确文本并标成 decimal（接口把 Decimal 编码成
   float 会丢精度）；带时区的取值原样进语句、原样回交（不 shift、不重解释）。
6. **空数组与部分失败不冒充成功。** 没跑完（超时）回 running + run_id；没有值的指标、被遮罩的指标、没有快照的查询一律
   ok=False。
7. **身份与用途由宿主定。** HTTP 入口固定 compose/探索，客户端传 purpose / role / actor 都不看；formal 只在宿主入口
   （execute_formal），复用同一个 publications.judge，发布依据不齐就拒且**不换路**（不降级成探索、不建运行）。

数据全是合成的（一个临时 SQLite 库、7 条合成标量指标），不连真实业务库；这些指标不是任何真实业务口径。
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import uuid
from decimal import Decimal
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.apps import publications
from app.core.artifact_store import graph_hash
from app.db.base import SessionLocal
from app.db.models import DataSource, Run, Workflow, WorkflowVersion
from app.engine.runner import run_manager
from app.ledger import querycore
from app.main import app
from tests import ledger_samples as S
from tests.publication_samples import (add_record, add_version, confirm, make_workflow, publish, report_model)

#: 这一版口径卡的节点 id（源头口径卡）
CARD = "card"
#: 取数窗口：start（含）/ end（不含）
WINDOW = "AND paid_at >= '{{ input.start }}' AND paid_at < '{{ input.end }}'"
#: 合成库的行（status / region / amount / qty / paid_at）
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
WINDOW_UNITS = 7
EAST_SUM = 150.0
PEAK = 100.0


def build_db(path) -> None:
    db = sqlite3.connect(path)
    db.executescript("CREATE TABLE orders (id INTEGER PRIMARY KEY, region TEXT, status TEXT, amount REAL, "
                     "qty INTEGER, paid_at TEXT);")
    db.executemany("INSERT INTO orders VALUES (?, ?, ?, ?, ?, ?)", ROWS)
    db.commit()
    db.close()


async def fresh_source(tmp_path) -> str:
    """每个用例一个名字不同的数据源（候选口径按数据源预筛，别的用例的口径不会混进来）。"""
    path = tmp_path / "querycore.db"
    build_db(path)
    S.CURRENT["source"] = f"qc_{uuid.uuid4().hex[:8]}"
    await S.set_source(path)
    return S.CURRENT["source"]


def metrics() -> list[dict[str, Any]]:
    """7 条合成标量指标：三条共用 sales 那条 SQL，一条只读 region 那条（带 region 参数），两条共用 shared 那条。"""
    return [
        {"id": "gmv", "name": "销售额", "unit": "元", "decimals": 2, "format": "thousands",
         "expression": "cell(nodes.sales, 0, 'gmv')"},
        {"id": "units", "name": "件数", "unit": "件",
         "expression": "cell(nodes.sales, 0, 'units')"},
        {"id": "label", "name": "合成标签", "unit": "",
         "expression": "cell(nodes.sales, 0, 'label')"},
        {"id": "units_exact", "name": "件数放大", "unit": "件",
         "expression": "cell(nodes.sales, 0, 'units') * 1000000000000000001"},
        {"id": "region_gmv", "name": "大区销售额", "unit": "元", "decimals": 2,
         "expression": "cell(nodes.region, 0, 'gmv')"},
        {"id": "total", "name": "订单总额", "unit": "元", "decimals": 2,
         "expression": "cell(nodes.shared, 0, 'total')"},
        {"id": "peak", "name": "最大单笔", "unit": "元", "decimals": 2,
         "expression": "cell(nodes.shared, 0, 'peak')"},
    ]


#: 除了 region_gmv 之外的全部指标（选它就不用 region 那个参数）
WITHOUT_REGION = [m["id"] for m in metrics() if m["id"] != "region_gmv"]
ALL_METRICS = [m["id"] for m in metrics()]


def graph(*, sales_sql: str | None = None, on_missing: str | None = None, table_metric: bool = False) -> dict[str, Any]:
    """入口 → 三条取数 → 口径卡 → 报告 → 出口（报告节点只在原图里，探针图不跑它）。

    on_missing="null" 时口径卡声明「缺输入按空值算」（engine/nodes/metrics 的 on_missing）：这时跑完但某个指标没有值，
    正是「跑成功不等于算成了」那一种。"""
    card_extra = {"on_missing": on_missing} if on_missing else {}
    made = metrics()
    if table_metric:
        # 声明成表的那一条：这一版只交标量，执行前就拒（不悄悄按第一行给个值）
        next(m for m in made if m["id"] == "total")["contract"] = {"shape": "table"}
    return S.chain(
        S.node("start", "input", fields=[
            {"name": "start", "type": "string", "required": True, "description": "开始日期（含）"},
            {"name": "end", "type": "string", "required": True, "description": "结束日期（不含）"},
            {"name": "region", "type": "string", "required": False, "description": "大区（只有大区口径用）"},
        ]),
        S.node("sales", "tool", tool=S.tool(),
               args={"sql": sales_sql or f"SELECT SUM(amount) AS gmv, SUM(qty) AS units, '合成标签' AS label "
                                         f"FROM orders WHERE status = 'paid' {WINDOW}"}),
        S.node("region", "tool", tool=S.tool(),
               args={"sql": "SELECT SUM(amount) AS gmv FROM orders WHERE region = '{{ input.region }}' "
                            f"AND status = 'paid' {WINDOW}"}),
        S.node("shared", "tool", tool=S.tool(),
               args={"sql": f"SELECT SUM(amount) AS total, MAX(amount) AS peak FROM orders "
                            f"WHERE status = 'paid' {WINDOW}"}),
        S.node(CARD, "metrics", caliber="合成周销售", caliber_version="v1", metrics=made, **card_extra),
        S.node("write", "report", instructions="写周销售"),
        S.node("out", "output", fields=[{"name": "报告", "value": "{{ nodes.write.text }}"}]))


def other_graph() -> dict[str, Any]:
    """换个窗口就换了取数（等价于「图上改了东西」），让 v2 有自己的版本行与清单。"""
    return graph(sales_sql=f"SELECT SUM(amount) AS gmv, SUM(qty) AS units, '合成标签' AS label "
                           f"FROM orders WHERE status IN ('paid', 'open') {WINDOW}")


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


async def run_once(asset: dict, *, metric_ids: list[str], inputs: dict[str, Any] | None = None, **kw):
    """走服务入口跑一次（HTTP 之外的那条路），等结果。asset 里带 version 就是点名那一版。"""
    calls = dict(kw)
    if "version" in asset:
        calls.setdefault("version", asset["version"])
    async with SessionLocal() as session:
        return await querycore.execute(
            session, workflow_id=asset["workflow_id"], card_id=CARD, metric_ids=metric_ids,
            inputs=inputs if inputs is not None else {"start": "2026-09-01", "end": "2026-10-01"}, **calls)


def values_of(outcome) -> dict[str, Any]:
    return {m["id"]: m["value"] for m in outcome.as_dict()["metrics"]}


async def settled(run_id: str) -> Run:
    """等这次运行收尾（测试收尾不留挂着的任务）。"""
    for _ in range(400):
        async with SessionLocal() as session:
            row = await session.get(Run, run_id)
        if row.status in querycore.TERMINAL_RUN_STATUSES:
            return row
        await asyncio.sleep(0.05)
    raise AssertionError(f"运行 {run_id} 没有结束：{row.status}")


def no_model(monkeypatch) -> None:
    """内部 LLM = 0：任何模型调用都让这次执行炸掉。"""
    from app.providers import mock_model

    def boom(self, messages):
        raise AssertionError("QueryCore 不该调用模型")

    monkeypatch.setattr(mock_model.MockChatModel, "_decide", boom)


# ==========================================================================
# 1、显式版本 / 推荐版：解析一次、当场固定
# ==========================================================================


async def test_explicit_and_recommended_versions_are_pinned_once(client):
    asset = await make_workflow(graph=graph())
    await publish(client, asset)
    asset = await add_version(asset, other_graph(), version=2)
    await publish(client, asset, version=2)
    await publish(client, asset, version=1)                     # 旧版重新推上去成推荐版

    follow = await run_once({"workflow_id": asset["workflow_id"]}, metric_ids=["gmv"],
                            inputs={"start": "2026-09-01", "end": "2026-10-01"})
    assert follow.ok and follow.version == 1 and follow.explicit_version is False
    assert follow.publication["version"] == 1 and follow.publication["manifest"]
    # 推荐版那一版是 status='paid'：v2 把 open 也算进去，值会不一样（如果解析错了会看出来）
    assert follow.metrics[0]["value"] == WINDOW_SUM

    named = await run_once({"workflow_id": asset["workflow_id"], "version": 2}, metric_ids=["gmv"],
                           inputs={"start": "2026-09-01", "end": "2026-10-01"})
    assert named.ok and named.version == 2 and named.explicit_version is True
    assert named.metrics[0]["value"] == WINDOW_SUM + 12.5


# ==========================================================================
# 2、K08 闭包：选 A 时 B 独有的取数和参数为 0；共享 SQL 保留并声明
# ==========================================================================


async def test_the_unselected_metrics_query_and_parameters_stay_out(client):
    asset = await make_workflow(graph=graph())
    await publish(client, asset)
    outcome = await run_once(asset, metric_ids=WITHOUT_REGION,
                             inputs={"start": "2026-09-01", "end": "2026-10-01", "region": "east"})
    assert outcome.ok
    data = outcome.as_dict()
    # region 那条查询不进这次范围
    assert [q["node_id"] for q in data["queries"]] == ["sales", "shared"]
    assert "region" not in json.dumps(data["queries"])
    # region 这个参数这次不用：不带进执行（照实记在 ignored 里），也不在这次的参数契约里
    assert data["parameters"]["values"] == {"start": "2026-09-01", "end": "2026-10-01"}
    assert data["parameters"]["ignored"] == ["region"]
    assert [c["name"] for c in data["parameters"]["contract"]] == ["start", "end"]
    assert "region" not in json.dumps([q["parameters"] for q in data["parameters"]["queries"]])
    assert set(data["selection"]["metric_ids"]) == set(WITHOUT_REGION)
    assert "region_gmv" not in json.dumps(data["queries"])
    for query in data["queries"]:
        assert query["found"] and query["executed_hash"] and "executed_sql" not in query


async def test_a_shared_sql_stays_and_declares_who_else_it_computes(client):
    asset = await make_workflow(graph=graph())
    await publish(client, asset)
    outcome = await run_once(asset, metric_ids=["total"])
    assert outcome.ok and outcome.metrics[0]["value"] == WINDOW_SUM
    [query] = outcome.as_dict()["queries"]
    assert query["query_id"].endswith(":shared") and query["metric_ids"] == ["total"]
    assert query["computes_unselected"] == ["peak"], "共享的那条 SQL 还在算未选中的指标，要如实声明"
    # 共享 SQL 里的另一个指标不交回
    assert "peak" not in [m["id"] for m in outcome.as_dict()["metrics"]]


async def test_missing_and_unknown_parameters_are_refused_before_any_sql(client):
    asset = await make_workflow(graph=graph())
    await publish(client, asset)
    async with SessionLocal() as session:
        with pytest.raises(querycore.QueryCoreRefused) as caught:
            await querycore.execute(session, workflow_id=asset["workflow_id"], card_id=CARD,
                                   metric_ids=["region_gmv"], inputs={"start": "2026-09-01", "end": "2026-10-01"})
    assert caught.value.code == "missing_parameter" and caught.value.status_code == 422
    assert caught.value.problems and caught.value.problems[0]["code"] == "missing_parameter"
    async with SessionLocal() as session:
        with pytest.raises(querycore.QueryCoreRefused) as unknown:
            await querycore.execute(session, workflow_id=asset["workflow_id"], card_id=CARD,
                                   metric_ids=["gmv"], inputs={"start": "2026-09-01", "end": "2026-10-01",
                                                               "store": "s1"})
    assert unknown.value.code == querycore.UNKNOWN_INPUT and unknown.value.status_code == 422
    # 两条都在发起运行之前拒：一条 SQL 都没发，也没有留下运行
    async with SessionLocal() as session:
        assert (await session.execute(select(Run).where(Run.workflow_id == asset["workflow_id"]))).scalars().all() == []


# ==========================================================================
# 3、零模型、零作者侧编译、零运行期重绑定
# ==========================================================================


async def test_no_model_and_no_author_side_compilation(client, monkeypatch):
    no_model(monkeypatch)

    def no_compile(*a, **kw):
        raise AssertionError("QueryCore 不做作者侧 SQL 编译")

    monkeypatch.setattr("app.data.rule_compile.compile_rule", no_compile)
    asset = await make_workflow(graph=graph())
    await publish(client, asset)
    outcome = await run_once(asset, metric_ids=WITHOUT_REGION,
                             inputs={"start": "2026-09-01", "end": "2026-10-01"})
    assert outcome.ok
    async with SessionLocal() as session:
        row = await session.get(Run, outcome.run_id)
    kinds = {n["type"] for n in row.graph["nodes"]}
    assert kinds <= {"input", "tool", "metrics", "output"}, f"探针图里不该有模型节点：{kinds}"
    assert row.origin["service"] == "querycore" and row.origin["purpose"] == querycore.DEFAULT_PURPOSE
    assert row.options.get("read_only_data") is True, "这次运行的数据源查询一律只读（READ_ONLY 边界）"


async def test_the_executed_sql_is_exactly_the_planned_one(client):
    """零运行期重绑定：执行过的语句与这次计划渲染出来的哈希一致、参数输入哈希一致；SQL 正文不外发。"""
    asset = await make_workflow(graph=graph())
    await publish(client, asset)
    outcome = await run_once(asset, metric_ids=WITHOUT_REGION,
                             inputs={"start": "2026-09-01", "end": "2026-10-01"})
    assert outcome.ok
    data = outcome.as_dict()
    assert all(q["rebound"] is None for q in data["queries"])
    planned = {p["node_id"]: p for p in data["parameters"]["queries"]}
    for query in data["queries"]:
        plan = planned[query["node_id"]]
        assert "executed_sql" not in query, "返回里不给 SQL 正文：要读正文走证据接口"
        assert query["executed_hash"] == query["planned_statement_hash"] == plan["statement_hash"]
        assert query["parameters"]["inputs_hash"] == plan["inputs_hash"] == query["planned_inputs_hash"]
        assert plan["template_hash"] != plan["statement_hash"], "源模板哈希与执行语句哈希不是一回事"
    # 这一整段（除快照自带的检查项）里没有 SQL 正文
    shown = json.dumps([{k: v for k, v in q.items() if k not in ("checks", "no_data")} for q in data["queries"]],
                       ensure_ascii=False).lower()
    assert "from orders" not in shown and "paid_at" not in shown and "{{" not in shown


# ==========================================================================
# 4、类型与时区：decimal / 大整数不转 float，带时区的取值原样进语句
# ==========================================================================


def test_typed_values_never_coerce_decimal_or_big_integers():
    assert querycore.typed(Decimal("123.456")) == "decimal"
    assert querycore.typed(2 ** 80) == "integer"
    assert querycore.typed("123.456") == "decimal"
    assert querycore.typed("000012") == "string"
    assert querycore.typed(True) == "boolean"
    assert querycore.typed(None) == "null"
    assert querycore.typed(1.5) == "number"


def test_the_projection_keeps_decimal_and_big_integers_exact():
    """Decimal 与超长整数过投影时一个位数都不丢：Decimal 交回精确文本并标成 decimal（接口把 Decimal 编码成 float 会
    丢精度），大整数原样是整数。整份投影还要能直接 json.dumps（不会有 Decimal 漏过去）。"""
    base = {"id": "d", "name": "十进制", "unit": "元", "status": "ok", "rendered": "123.456", "decimals": 3}
    decimal = querycore._typed_metric({**base, "value": Decimal("123.456")}, None)
    assert decimal["value"] == "123.456" and decimal["value_type"] == "decimal"
    assert not isinstance(decimal["value"], float)
    big = querycore._typed_metric({**base, "value": 10 ** 25}, None)
    assert big["value"] == 10 ** 25 and big["value_type"] == "integer" and isinstance(big["value"], int)
    assert json.dumps([decimal, big], ensure_ascii=False)
    assert querycore.exact(Decimal("0.10")) == "0.10" and querycore.exact(2 ** 70) == 2 ** 70


async def test_values_keep_their_exact_type_and_timezone(client):
    asset = await make_workflow(graph=graph())
    await publish(client, asset)
    tz_end = "2026-10-01T00:00:00+08:00"
    outcome = await run_once(asset, metric_ids=WITHOUT_REGION, inputs={"start": "2026-09-01", "end": tz_end})
    assert outcome.ok
    data = outcome.as_dict()
    by_id = {m["id"]: m for m in data["metrics"]}
    # 大整数：精确的整数，不是 float（不无条件转 float）
    assert by_id["units_exact"]["value"] == WINDOW_UNITS * 1000000000000000001
    assert by_id["units_exact"]["value_type"] == "integer"
    assert isinstance(by_id["units_exact"]["value"], int)
    # 整数与小数各自标成自己的类型；非数字文本照旧是文本
    assert by_id["units"]["value_type"] == "integer" and isinstance(by_id["units"]["value"], int)
    assert by_id["gmv"]["value_type"] == "number" and by_id["label"]["value"] == "合成标签"
    assert by_id["label"]["value_type"] == "string"
    # 带时区的取值原样进语句、原样回交（不 shift、不重解释）：执行过的语句与计划一致（见上面那条），
    # 参数那一份里记的就是原样的字符串
    assert data["parameters"]["values"]["end"] == tz_end
    assert [p["parameters"] for p in data["parameters"]["queries"]][0][0]["name"] == "start"
    assert data["data"]["window"]["end"] == tz_end and data["data"]["as_of"]


# ==========================================================================
# 5、伪造版本 / 过期清单 / 不兼容编译器 / 老资产
# ==========================================================================


async def test_a_forged_version_is_refused(client):
    asset = await make_workflow(graph=graph())
    await publish(client, asset)
    async with SessionLocal() as session:
        with pytest.raises(querycore.QueryCoreRefused) as caught:
            await querycore.execute(session, workflow_id=asset["workflow_id"], card_id=CARD, metric_ids=["gmv"],
                                    version=99, inputs={"start": "2026-09-01", "end": "2026-10-01"})
    assert caught.value.code == querycore.VERSION_MISSING and caught.value.status_code == 404
    async with SessionLocal() as session:
        assert (await session.execute(select(Run).where(Run.workflow_id == asset["workflow_id"]))).scalars().all() == []


async def test_a_card_outside_the_version_and_an_empty_selection_are_refused(client):
    asset = await make_workflow(graph=graph())
    await publish(client, asset)
    async with SessionLocal() as session:
        with pytest.raises(querycore.QueryCoreRefused) as missing:
            await querycore.execute(session, workflow_id=asset["workflow_id"], card_id="nope", metric_ids=["gmv"],
                                    inputs={"start": "2026-09-01", "end": "2026-10-01"})
        with pytest.raises(querycore.QueryCoreRefused) as empty:
            await querycore.execute(session, workflow_id=asset["workflow_id"], card_id=CARD, metric_ids=[],
                                    inputs={"start": "2026-09-01", "end": "2026-10-01"})
    assert missing.value.code == querycore.CARD_MISSING and missing.value.status_code == 409
    assert empty.value.code == querycore.SELECTION_REFUSED and empty.value.status_code == 422
    async with SessionLocal() as session:
        assert (await session.execute(select(Run).where(Run.workflow_id == asset["workflow_id"]))).scalars().all() == []


async def test_a_table_shaped_metric_is_refused_in_this_version(client):
    """首版只交标量：声明成表的指标在发 SQL 之前就拒，不悄悄按第一行给个值。"""
    asset = await make_workflow(graph=graph(table_metric=True))
    await publish(client, asset)
    async with SessionLocal() as session:
        with pytest.raises(querycore.QueryCoreRefused) as caught:
            await querycore.execute(session, workflow_id=asset["workflow_id"], card_id=CARD, metric_ids=["total"],
                                    inputs={"start": "2026-09-01", "end": "2026-10-01"})
    assert caught.value.code == querycore.OUTPUT_UNSUPPORTED and caught.value.status_code == 422
    async with SessionLocal() as session:
        assert (await session.execute(select(Run).where(Run.workflow_id == asset["workflow_id"]))).scalars().all() == []


async def test_a_manifest_for_another_graph_is_refused(client):
    asset = await make_workflow(graph=graph())
    await publish(client, asset)
    async with SessionLocal() as session:
        row = (await session.execute(select(WorkflowVersion).where(
            WorkflowVersion.workflow_id == asset["workflow_id"], WorkflowVersion.version == 1))).scalar_one()
        row.graph = other_graph()                                    # 同一版号、换了图
        await session.commit()
    async with SessionLocal() as session:
        with pytest.raises(querycore.QueryCoreRefused) as caught:
            await querycore.execute(session, workflow_id=asset["workflow_id"], card_id=CARD, metric_ids=["gmv"],
                                    inputs={"start": "2026-09-01", "end": "2026-10-01"})
    assert caught.value.code == publications.BASIS_MISMATCH
    assert caught.value.problems and "图指纹" in caught.value.problems[0]["message"]


async def test_an_incompatible_compiler_is_refused_without_reinterpreting(client, monkeypatch):
    asset = await make_workflow(graph=graph())
    await publish(client, asset)
    real = publications.compiler_versions
    monkeypatch.setattr(publications, "compiler_versions",
                        lambda: {**real(), "binding": {"binder": "controlled_template", "version": 99}})
    async with SessionLocal() as session:
        with pytest.raises(querycore.QueryCoreRefused) as caught:
            await querycore.execute(session, workflow_id=asset["workflow_id"], card_id=CARD, metric_ids=["gmv"],
                                    inputs={"start": "2026-09-01", "end": "2026-10-01"})
    assert caught.value.code == querycore.INCOMPATIBLE
    assert caught.value.problems and caught.value.problems[0]["key"] == "binding"
    assert "不按新规则重解释旧发布" in caught.value.message


def test_the_compiler_seam_rejects_an_old_manifest_without_a_resolver():
    """加 resolver 这一项之前物化的清单（缺 resolver）：认不出它按哪一版闭包解析，明确拒。"""
    content = {"format": publications.FORMAT, "compiler": {k: v for k, v in publications.compiler_versions().items()
                                                           if k != "resolver"}}
    problems = publications.compiler_problems(content)
    assert [p["key"] for p in problems] == ["resolver"] and problems[0]["code"] == publications.COMPILER_MISMATCH
    # 现在这一套自己写出来的清单一项都不差；没有 compiler 这一段的清单另给一个码
    assert publications.compiler_problems({"compiler": publications.compiler_versions()}) == []
    assert publications.compiler_problems({"compiler": None})[0]["code"] == publications.COMPILER_MISSING
    # 多出一项现在认不出的约定一样算不兼容（不静默放行）
    extra = {**publications.compiler_versions(), "future_thing": 1}
    assert [p["key"] for p in publications.compiler_problems({"compiler": extra})] == ["future_thing"]


async def test_a_legacy_asset_without_a_manifest_is_refused(client):
    """老资产（这一版没有发布事件）：QueryCore 明确拒；旧 trial 路径一个字不动。"""
    workflow_id = await S.publish(graph=graph())                    # 直接写库（老写法，不经过发布入口）
    async with SessionLocal() as session:
        with pytest.raises(querycore.QueryCoreRefused) as caught:
            await querycore.execute(session, workflow_id=workflow_id, card_id=CARD, metric_ids=["gmv"],
                                    inputs={"start": "2026-09-01", "end": "2026-10-01"})
    assert caught.value.code == querycore.MANIFEST_REQUIRED and caught.value.status_code == 409
    # 老试跑照旧能跑（试跑只认这一版发布过）
    res = await client.post(f"/api/ledger/calibers/{workflow_id}/1/{CARD}/trial",
                            json={"inputs": {"start": "2026-09-01", "end": "2026-10-01", "region": "east"}})
    assert res.status_code == 200, res.text
    assert res.json()["status"] in ("succeeded", "running", "failed")


# ==========================================================================
# 6、formal：只在宿主入口、复用同一个 judge、发布依据不齐不换路
# ==========================================================================


async def test_the_formal_host_entry_reuses_the_judge_and_never_switches_path(client):
    asset = await make_workflow(graph=graph())
    await publish(client, asset)                                    # 有清单、没有验证记录与审阅
    async with SessionLocal() as session:
        with pytest.raises(querycore.QueryCoreRefused) as caught:
            await querycore.execute_formal(session, workflow_id=asset["workflow_id"], card_id=CARD, metric_ids=["gmv"],
                                           inputs={"start": "2026-09-01", "end": "2026-10-01"}, actor="宿主")
    assert caught.value.code == publications.RECORD_MISSING, "formal 用的是 publications.judge 的原码"
    assert caught.value.status_code == 409
    # 不换路：不降级成探索执行，也不留下任何运行
    async with SessionLocal() as session:
        assert (await session.execute(select(Run).where(Run.workflow_id == asset["workflow_id"]))).scalars().all() == []

    record_id = await add_record(asset)
    await confirm(asset, record_id)
    await publish(client, asset)                                    # 依据齐了之后重新发布
    async with SessionLocal() as session:
        outcome = await querycore.execute_formal(session, workflow_id=asset["workflow_id"], card_id=CARD,
                                                 metric_ids=["gmv"],
                                                 inputs={"start": "2026-09-01", "end": "2026-10-01"}, actor="宿主")
    assert outcome.ok and outcome.purpose == publications.PURPOSE_FORMAL
    assert outcome.publication["identity"] == publications.PROVEN
    assert outcome.publication["requires_proof"] is True
    async with SessionLocal() as session:
        run = await session.get(Run, outcome.run_id)
    assert run.origin["purpose"] == publications.PURPOSE_FORMAL
    assert run.origin["identity"] == publications.PROVEN and run.started_by == "宿主"


async def test_the_interactive_entry_is_exploratory_and_cannot_be_escalated(client):
    """默认交互入口是 compose/探索：客户端传 purpose / role / actor 一律不看，也拿不到正式身份。"""
    asset = await make_workflow(graph=graph())
    await publish(client, asset)                                    # 有清单、没有验证记录（formal 会拒）
    res = await client.post("/api/ledger/querycore", json={
        "workflow_id": asset["workflow_id"], "card_id": CARD, "metric_ids": ["gmv"],
        "inputs": {"start": "2026-09-01", "end": "2026-10-01"},
        "purpose": "formal", "role": "admin", "actor": "冒充的人", "version": 1})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["ok"] and body["purpose"] == publications.PURPOSE_COMPOSE
    async with SessionLocal() as session:
        run = await session.get(Run, body["run_id"])
    assert run.origin["purpose"] == publications.PURPOSE_COMPOSE, "用途由宿主常量定，客户端改不了"
    assert run.origin["identity"] == publications.UNPROVEN and run.run_class == "exploratory"
    assert run.started_by is None, "认证主体来自服务端，请求体里的 actor 不看"


async def test_a_role_refusal_does_not_switch_path(client):
    """权限失败不换路：这个入口只给搭建者及以上（路由级依赖）。使用者被拒时不降级、不换别的路、也不留下运行。"""
    from tests.fakes import auth_env

    asset = await make_workflow(graph=graph())
    await publish(client, asset)
    try:
        await auth_env.turn_on_local()
        await auth_env.make_user("uma", "user")
        uma = await auth_env.login("uma")
        res = await uma.post("/api/ledger/querycore", json={"workflow_id": asset["workflow_id"], "card_id": CARD,
                                                            "metric_ids": ["gmv"],
                                                            "inputs": {"start": "2026-09-01", "end": "2026-10-01"}})
        assert res.status_code == 403 and res.json()["code"] == "role_required"
    finally:
        await auth_env.reset()
    async with SessionLocal() as session:
        assert (await session.execute(select(Run).where(Run.workflow_id == asset["workflow_id"]))).scalars().all() == []


# ==========================================================================
# 7、超时：回 running + run_id，绝不冒充成功
# ==========================================================================


async def test_a_timeout_returns_running_and_never_success(client):
    asset = await make_workflow(graph=graph())
    await publish(client, asset)
    outcome = await run_once(asset, metric_ids=["gmv"], timeout_s=0.0,
                             inputs={"start": "2026-09-01", "end": "2026-10-01"})
    assert outcome.status == "running" and outcome.ok is False and outcome.run_id
    assert [p["code"] for p in outcome.problems] == [querycore.TIMEOUT]
    assert outcome.metrics == () and outcome.queries == ()
    assert outcome.quality["execution"] == "running" and outcome.quality["status"] != "pass"
    # 运行照旧跑完（超时只影响这次接口怎么回，不影响运行本身）
    assert (await settled(outcome.run_id)).status == "succeeded"
    # HTTP 那条路同样：回 running + run_id，绝不成功
    res = await client.post("/api/ledger/querycore", json={
        "workflow_id": asset["workflow_id"], "card_id": CARD, "metric_ids": ["gmv"],
        "inputs": {"start": "2026-09-01", "end": "2026-10-01"}, "budget": {"timeout_s": 0.0}})
    assert res.status_code == 200 and res.json()["status"] == "running" and res.json()["ok"] is False
    assert res.json()["problems"][0]["code"] == querycore.TIMEOUT


# ==========================================================================
# 8、空结果 / 只有一部分指标有值：都不是成功
# ==========================================================================


async def test_an_empty_window_is_not_a_success(client):
    """窗口里没有数据（SUM 全空）：这次执行明确失败，ok=False，一件指标都不交回——空结果不冒充成功。"""
    asset = await make_workflow(graph=graph())
    await publish(client, asset)
    outcome = await run_once(asset, metric_ids=WITHOUT_REGION,
                             inputs={"start": "2030-01-01", "end": "2030-02-01"})
    assert outcome.status == "failed" and outcome.ok is False
    assert [p["code"] for p in outcome.problems] == [querycore.EXECUTION]
    assert outcome.metrics == () and outcome.queries == ()
    assert outcome.quality["status"] == "fail" and outcome.quality["execution"] == "failed"


async def test_a_metric_without_a_value_is_not_a_success(client):
    """声明了「缺输入按空值算」的口径：跑完了，但指标没有值——跑成功不等于算成了，照实 ok=False。"""
    asset = await make_workflow(graph=graph(on_missing="null"))
    await publish(client, asset)
    outcome = await run_once(asset, metric_ids=WITHOUT_REGION,
                             inputs={"start": "2030-01-01", "end": "2030-02-01"})
    assert outcome.status == "succeeded" and outcome.ok is False
    codes = [p["code"] for p in outcome.problems]
    # 6 条里 5 条没有值（聚合全空）；label 那一格是 SELECT 里的字面量，照常有一行，所以它有值
    no_value = [m["id"] for m in outcome.metrics if m["value"] is None]
    assert codes.count(querycore.METRIC_NO_VALUE) == len(no_value) == len(WITHOUT_REGION) - 1
    assert {m["id"]: m["value"] for m in outcome.metrics}["label"] == "合成标签"
    assert outcome.quality["status"] == "fail"


async def test_a_budget_smaller_than_the_plan_is_refused(client):
    asset = await make_workflow(graph=graph())
    await publish(client, asset)
    async with SessionLocal() as session:
        with pytest.raises(querycore.QueryCoreRefused) as caught:
            await querycore.execute(session, workflow_id=asset["workflow_id"], card_id=CARD,
                                    metric_ids=WITHOUT_REGION, budget={"max_queries": 1},
                                    inputs={"start": "2026-09-01", "end": "2026-10-01"})
    assert caught.value.code == querycore.BUDGET_REFUSED and caught.value.status_code == 429
    async with SessionLocal() as session:
        assert (await session.execute(select(Run).where(Run.workflow_id == asset["workflow_id"]))).scalars().all() == []


async def test_a_masked_metric_is_not_handed_out_as_a_value(client, tmp_path):
    """数据源设了遮罩列：被遮的指标不给值、ok=False——不把遮罩值当结果交出（同试跑与证据接口的分寸）。"""
    asset = await make_workflow(graph=graph())
    await publish(client, asset)
    await S.set_source(tmp_path / "querycore.db", options={"mask_columns": ["gmv"]})
    outcome = await run_once(asset, metric_ids=WITHOUT_REGION,
                             inputs={"start": "2026-09-01", "end": "2026-10-01"})
    by_id = {m["id"]: m for m in outcome.as_dict()["metrics"]}
    assert by_id["gmv"].get("masked") is True and by_id["gmv"]["value"] is None
    assert outcome.ok is False and querycore.METRIC_MASKED in [p["code"] for p in outcome.problems]


# ==========================================================================
# 9、证据关联：每条查询都指得到工件、来源与 attempt；指标出自这次运行的封存证据
# ==========================================================================


async def test_every_query_points_at_its_artifact_and_source(client):
    asset = await make_workflow(graph=graph())
    await publish(client, asset)
    outcome = await run_once(asset, metric_ids=WITHOUT_REGION,
                             inputs={"start": "2026-09-01", "end": "2026-10-01"})
    assert outcome.ok
    from app.core.artifact_store import load

    for query in outcome.as_dict()["queries"]:
        assert query["found"] is True and query["artifact"]
        snap = load(query["artifact"])
        assert snap["sql"] and query["executed_hash"] and "executed_sql" not in query
        assert query["data_source"] == S.CURRENT["source"]
        assert query["attempt"] is not None and query["attempt"] >= 1
        assert query["columns"] and query["row_count"] == 1 and query["truncated"] is False
        assert query["source"]["workflow_id"] == asset["workflow_id"] and query["source"]["version"] == 1
        assert query["source"]["node_id"] in ("sales", "shared")
        assert query["tables"] == ["orders"]
    assert {m["id"] for m in outcome.metrics} == set(WITHOUT_REGION)
    assert outcome.data["sources"] == [S.CURRENT["source"]] and outcome.data["tables"] == ["orders"]
    assert outcome.data["read"] is True and outcome.quality["status"] in ("pass", "unknown")


async def test_all_seven_synthetic_scalars_come_back_typed(client):
    """一次全覆盖：7 条合成标量指标都回来（金额/件数/文本/放大/大区/总额/最大单笔），不是真实业务指标。"""
    asset = await make_workflow(graph=graph())
    await publish(client, asset)
    outcome = await run_once(asset, metric_ids=ALL_METRICS,
                             inputs={"start": "2026-09-01", "end": "2026-10-01", "region": "east"})
    assert outcome.ok
    got = values_of(outcome)
    assert got["gmv"] == WINDOW_SUM and got["units"] == WINDOW_UNITS
    assert got["region_gmv"] == EAST_SUM and got["total"] == WINDOW_SUM and got["peak"] == PEAK
    assert got["label"] == "合成标签"
    assert got["units_exact"] == WINDOW_UNITS * 1000000000000000001
    assert len(outcome.metrics) == 7
    # 大区和总额两条不同的查询各留下自己的工件与来源
    assert {q["node_id"] for q in outcome.as_dict()["queries"]} == {"sales", "region", "shared"}
    assert len({q["artifact"] for q in outcome.as_dict()["queries"]}) == 3
