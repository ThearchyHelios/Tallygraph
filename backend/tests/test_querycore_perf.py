"""K13：固定执行的测量与受控性能基线（app/ledger/querycore.py 的 measurement、engine/runner 的 usage.queue_ms）。

这份文件盯住两件事：

1. **测量本身是真的**（默认每次都跑）：一次固定执行交回的 measurement 把阶段耗时（发布/定位、闭包/计划、参数、排队、
   数据库、证据、总耗时）、计数（计划与**真发出去**的 SQL 条数、内部模型调用数、交回的查询条数与截断条数、问题/错误/
   超时）连同 trace / run / query 身份一起记下来。各阶段加得起来；量不到的字段是 None，不填 0 冒充；SQL 条数对着真的
   执行次数；LLM 恒为 0；超时回 running 且**不冒充成功**；排队（usage.queue_ms）绝不拿等人审批的 wait_ms 顶上。
2. **基线是 opt-in 的**（`AGENTLAB_QUERYCORE_PERF=1` 才跑）：同一份代码、同一份数据快照、同一组参数、同一台机器、
   同一份预算，冷启动（先清掉进程内缓存的那一次）与热执行分开，各档至少 100 次，原始样本 + P50/P95 + 错误率；跑并发 1
   与并发 4 两档（名额上限 1 与 2，4 那一档一半在排队，用来验证 queue_ms）。**失败样本不剔除**：都留在样本里、算进错误率。

数据全是合成的（一个临时 SQLite 库、7 条合成标量指标），不连真实业务库；样本只写临时目录。

**没有生产负载，这份基线推不出生产的 P50/P95**——它只说明这套代码在这台机器、这份数据、这组参数、这份预算下的样子。
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import math
import os
import platform
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from app.db.base import SessionLocal
from app.db.models import Run
from app.engine.runner import run_manager
from app.ledger import querycore
from app.main import app
from tests import ledger_samples as S
from tests.fixtures.sources import drop_source
from tests.publication_samples import make_workflow, publish

#: 口径卡的节点 id
CARD = "card"
#: 取数窗口：start（含）/ end（不含）
WINDOW = "AND paid_at >= '{{ input.start }}' AND paid_at < '{{ input.end }}'"
#: 这次的入口取值（每个样本都一样：同一组参数）
INPUTS = {"start": "2026-09-01", "end": "2026-09-08"}
#: 窗口内的期望值（合成店铺库：三单、两个大区、两家门店）
WINDOW_GMV = 170.5
WINDOW_ORDERS = 3
#: 合成库上的 7 条标量指标（落在 3 条取数 SQL 上）
METRIC_IDS = ["gmv", "orders", "peak", "buyers", "gmv_cents", "region_gmv", "store_count"]
#: 闭包要执行几条查询：三条取数各一条
QUERIES = 3


def metrics() -> list[dict[str, Any]]:
    """7 条合成标量指标：sales 那条 SQL 出 4 个（gmv_cents 与之共用），另两条取数各出 1 个。"""
    return [
        {"id": "gmv", "name": "销售额", "unit": "元", "decimals": 2, "expression": "cell(nodes.sales, 0, 'gmv')"},
        {"id": "orders", "name": "订单数", "unit": "单", "expression": "cell(nodes.sales, 0, 'orders')"},
        {"id": "peak", "name": "最大单笔", "unit": "元", "decimals": 2, "expression": "cell(nodes.sales, 0, 'peak')"},
        {"id": "buyers", "name": "买家数", "unit": "人", "expression": "cell(nodes.sales, 0, 'buyers')"},
        {"id": "gmv_cents", "name": "销售额（分）", "unit": "分", "decimals": 0,
         "expression": "cell(nodes.sales, 0, 'gmv') * 100"},
        {"id": "region_gmv", "name": "大区销售额", "unit": "元", "decimals": 2,
         "expression": "cell(nodes.region, 0, 'region_gmv')"},
        {"id": "store_count", "name": "门店数", "unit": "家", "expression": "cell(nodes.stores, 0, 'stores')"},
    ]


def perf_graph() -> dict[str, Any]:
    """入口 → 三条取数 → 口径卡（7 条指标）→ 出口。取数节点自己的 SQL 不点入口参数以外的任何东西。"""
    return S.chain(
        S.node("start", "input", fields=[{"name": "start", "type": "string", "required": True},
                                         {"name": "end", "type": "string", "required": True}]),
        S.node("sales", "tool", tool=S.tool(),
               args={"sql": "SELECT SUM(amount) AS gmv, COUNT(*) AS orders, MAX(amount) AS peak, "
                            f"COUNT(DISTINCT phone) AS buyers FROM orders WHERE status = 'paid' {WINDOW}"}),
        S.node("region", "tool", tool=S.tool(),
               args={"sql": "SELECT region, SUM(amount) AS region_gmv FROM orders "
                            f"WHERE status = 'paid' {WINDOW} GROUP BY region ORDER BY region"}),
        S.node("stores", "tool", tool=S.tool(), args={"sql": "SELECT COUNT(*) AS stores FROM stores"}),
        S.node(CARD, "metrics", caliber="周销售", caliber_version="v1", metrics=metrics()),
        S.node("out", "output", fields=[{"name": "报告", "value": "{{ nodes.sales }}"}]))


def clipping_graph() -> dict[str, Any]:
    """一条取数按大区分组、只留一行：结果被截短（truncated），测量里要数得出来。"""
    return S.chain(
        S.node("start", "input", fields=[{"name": "start", "type": "string", "required": True},
                                         {"name": "end", "type": "string", "required": True}]),
        S.node("sales", "tool", tool=S.tool(),
               args={"sql": "SELECT region, SUM(amount) AS gmv FROM orders "
                            f"WHERE status = 'paid' {WINDOW} GROUP BY region ORDER BY region", "limit": 1}),
        S.node(CARD, "metrics", caliber="周销售", caliber_version="v1", metrics=[
            {"id": "gmv", "name": "销售额", "unit": "元", "decimals": 2, "expression": "cell(nodes.sales, 0, 'gmv')"}]),
        S.node("out", "output", fields=[{"name": "报告", "value": "{{ nodes.sales }}"}]))


async def make_asset(tmp_path, client, build) -> dict[str, Any]:
    """建一份已发布的合成口径：临时 SQLite 库（合成店铺库）+ 已发布版本 + 发布清单。数据只写临时目录。

    build() 在数据源定下来**之后**才调用：图里的工具名带着这一次的源名。"""
    path = tmp_path / f"perf_{uuid.uuid4().hex[:6]}.db"
    S.build_shop(path)
    S.CURRENT["source"] = f"perf_{uuid.uuid4().hex[:8]}"
    source_id = await S.set_source(path)
    made = await make_workflow(graph=build())
    await publish(client, made)
    return {**made, "source_id": source_id}


@pytest.fixture(autouse=True)
async def engine_up():
    await run_manager.setup()
    yield
    await run_manager.shutdown()


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.fixture
async def asset(tmp_path, client):
    """这份文件大部分用例用的那份口径（7 条指标、3 条取数）。"""
    made = await make_asset(tmp_path, client, perf_graph)
    yield made
    await drop_source(made["source_id"])


async def execute_once(asset: dict, *, trace: str | None = None, timeout_s: float | None = None,
                       metric_ids: list[str] | None = None) -> Any:
    """走服务入口跑一次（同一组参数、同一份数据快照），等结果。"""
    async with SessionLocal() as session:
        return await querycore.execute(session, workflow_id=asset["workflow_id"], card_id=CARD,
                                      metric_ids=list(METRIC_IDS if metric_ids is None else metric_ids),
                                      inputs=dict(INPUTS), trace=trace, timeout_s=timeout_s)


async def run_row(run_id: str) -> Run | None:
    async with SessionLocal() as session:
        return await session.get(Run, run_id)


def no_model(monkeypatch) -> None:
    """内部 LLM = 0：任何模型调用都让这次执行炸掉。"""
    from app.providers import mock_model

    def boom(self, messages):
        raise AssertionError("QueryCore 不该调用模型")

    monkeypatch.setattr(mock_model.MockChatModel, "_decide", boom)


def count_sql_calls(monkeypatch) -> dict[str, int]:
    """独立地数这次真正发出去几条 SQL：包住调用工具节点的执行器，只数这次那个数据源工具被调了几次。"""
    from app.engine import compiler
    from app.engine.schema import NodeType

    original = compiler.RUNNERS[NodeType.TOOL]
    seen = {"calls": 0}

    async def runner(state, ctx):
        if str(getattr(ctx, "config", {}).get("tool") or "") == S.tool():
            seen["calls"] += 1
        return await original(state, ctx)

    monkeypatch.setitem(compiler.RUNNERS, NodeType.TOOL, runner)
    return seen


def slow_tools(monkeypatch, seconds: float) -> None:
    """把取数拖慢：让「还在跑」那一段足够长，超时用例不靠机器快不快碰运气。"""
    from app.engine import compiler
    from app.engine.schema import NodeType

    original = compiler.RUNNERS[NodeType.TOOL]

    async def runner(state, ctx):
        if str(getattr(ctx, "config", {}).get("tool") or "") == S.tool():
            await asyncio.sleep(seconds)
        return await original(state, ctx)

    monkeypatch.setitem(compiler.RUNNERS, NodeType.TOOL, runner)


def limit_slots(monkeypatch, limit: int) -> None:
    """把并发名额的全站上限定成 limit：名额本身走真实那一套（app/cluster/slots），只把「现在生效的设置」这一个入口
    换掉。**不往库里写设置**——写下去这一条用例跑完也不恢复，后面的用例会跟着排成单名额，而父运行调用口径查询时
    父子两个运行同时要名额，单名额就等成死锁。"""
    from app.cluster import slots

    value = slots.values({"max_total": limit,
                          "classes": {k: {"limit": limit, "priority": 40 - i} for i, k in enumerate(slots.CLASSES)}})

    async def fixed(*, fresh: bool = False):  # noqa: ARG001 - 和真 policy 同一个签名
        return value

    monkeypatch.setattr(slots, "policy", fixed)


# ==========================================================================
# 1、测量本身：阶段加得起来、字段不虚构、身份留得住
# ==========================================================================


def test_an_old_run_never_gets_a_queue_time_invented_for_it():
    """加 queue_ms 这一项之前跑完的运行记录里没有它：读的一方要按「没量过」处理（None），绝不拿 wait_ms 冒充排队。"""
    class Old:
        usage = {"duration_ms": 12, "wall_ms": 12, "active_ms": 12, "wait_ms": 900}

    class Fresh:
        usage = {"duration_ms": 12, "wall_ms": 12, "active_ms": 12, "wait_ms": 0, "queue_ms": 7}

    class Bogus:
        usage = {"queue_ms": "7"}

    assert querycore._queue_of(Old()) is None, "老运行没有这一项：不许拿那 900ms 的 wait_ms 顶上"
    assert querycore._queue_of(Fresh()) == 7
    assert querycore._queue_of(Bogus()) is None, "存坏了的按没量到算"
    assert querycore._queue_of(None) is None


async def test_the_measurement_locates_every_stage_and_adds_up(asset):
    """一次跑得成、数也对的执行：阶段加得起来，计数与身份都对得上，排队按运行记录上那一项来。"""
    outcome = await execute_once(asset, trace="perf-locate")
    assert outcome.ok, outcome.problems
    measured = outcome.as_dict()["measurement"]
    phases, counts, ids = measured["phases"], measured["counts"], measured["ids"]

    assert set(phases) == set(querycore.PHASES)
    for name, value in phases.items():
        assert isinstance(value, int) and value >= 0, (name, value)
    # 跑的还是那份合成数据、那组参数：窗口里的期望值对得上（不是「跑通了但算的是别的东西」）
    values = {m["id"]: m["value"] for m in outcome.metrics}
    assert values["gmv"] == WINDOW_GMV and values["orders"] == WINDOW_ORDERS
    # 首尾相接的五段加起来就是总耗时，中间只差测量自身那几十微秒
    disjoint = sum(phases[name] for name in ("locate_ms", "closure_ms", "params_ms", "run_ms", "evidence_ms"))
    assert 0 <= phases["total_ms"] - disjoint <= 60, phases
    # queue / database 是 run 里面的两段：各出自己的出处，不重复计时
    assert 0 <= phases["queue_ms"] <= phases["run_ms"], phases
    assert 0 <= phases["database_ms"] <= phases["run_ms"], phases
    assert phases["database_ms"] > 0, "真查了三条 SQL，取数那一段不该是 0"

    # 排队那一项就是运行记录上的 queue_ms（不是等人审批的 wait_ms）
    row = await run_row(outcome.run_id)
    assert row is not None and row.status == "succeeded"
    assert phases["queue_ms"] == row.usage["queue_ms"]
    assert row.usage["wait_ms"] == 0, "这次没人审批"

    # 计数：计划 3 条、真发出去 3 条、模型 0 次、交回 3 条、一条都没截断、没有错也没有超时
    assert counts == {"planned_queries": QUERIES, "executed_sql": QUERIES, "llm_calls": 0,
                      "returned_queries": QUERIES, "truncated_queries": 0,
                      "problems": 0, "errors": 0, "timeouts": 0}

    # 身份：run / trace / query 都对得上，按其中任何一个都能找回来
    assert ids["run_id"] == outcome.run_id
    assert ids["trace"] == "perf-locate"
    assert ids["workflow_id"] == asset["workflow_id"] and ids["card_id"] == CARD and ids["version"] == 1
    assert len(ids["query_ids"]) == QUERIES and len(ids["execution_ids"]) == QUERIES
    assert ids["execution_ids"] == [q["execution_id"] for q in outcome.as_dict()["queries"]]
    assert set(outcome.as_dict()["data"]["sources"]) == {S.CURRENT["source"]}


async def test_the_http_entry_hands_the_measurement_out(asset, client):
    """接口这一层也交回测量：调用方拿这一份就能按 run_id / trace / query_ids 定位回去。"""
    res = await client.post("/api/ledger/querycore", json={
        "workflow_id": asset["workflow_id"], "card_id": CARD, "metric_ids": list(METRIC_IDS),
        "inputs": dict(INPUTS), "trace": "perf-http"})
    assert res.status_code == 200, res.text
    body = res.json()
    measured = body["measurement"]
    assert measured["ids"]["run_id"] == body["run_id"] and measured["ids"]["trace"] == "perf-http"
    assert measured["counts"]["executed_sql"] == QUERIES and measured["counts"]["llm_calls"] == 0
    assert measured["phases"]["total_ms"] >= measured["phases"]["run_ms"]
    assert len(measured["ids"]["query_ids"]) == QUERIES


async def test_the_sql_count_is_the_number_of_statements_actually_sent(asset, monkeypatch):
    """SQL 条数对着真的执行次数（独立数一遍），内部模型调用恒为 0。"""
    no_model(monkeypatch)
    seen = count_sql_calls(monkeypatch)
    outcome = await execute_once(asset)
    assert outcome.ok, outcome.problems
    measured = outcome.as_dict()["measurement"]
    assert seen["calls"] == QUERIES, "三次取数各发一条 SQL"
    assert measured["counts"]["executed_sql"] == seen["calls"] == QUERIES
    assert measured["counts"]["planned_queries"] == QUERIES
    assert measured["counts"]["llm_calls"] == 0
    # 取数那一段也确实量到了：三条 SQL 各自都记了执行时长
    assert measured["phases"]["database_ms"] > 0


async def test_a_timeout_stays_running_and_never_fills_in_what_it_could_not_measure(asset, monkeypatch):
    """超时：状态是 running、ok=False，**没量到的字段是 None**——不拿计划条数或 0 冒充已执行。"""
    slow_tools(monkeypatch, 0.4)
    started = time.perf_counter()
    outcome = await execute_once(asset, timeout_s=0.0)
    assert outcome.status == "running" and outcome.ok is False
    assert [p["code"] for p in outcome.problems] == [querycore.TIMEOUT]
    assert outcome.run_id, "没收尾也要把 run_id 交回，让人能去看"
    assert time.perf_counter() - started < 0.4, "到点就返回，不等它跑完"

    measured = outcome.as_dict()["measurement"]
    phases, counts, ids = measured["phases"], measured["counts"], measured["ids"]
    # 量得到的照记（这次是刚刚才发起的），量不到的照实空着
    assert isinstance(phases["total_ms"], int) and isinstance(phases["run_ms"], int)
    assert phases["database_ms"] is None, "没跑完没有执行证据：数据库那一截是 None，不是 0"
    assert phases["queue_ms"] is None, "还没收尾，运行记录上还没有排队这一项"
    assert counts["timeouts"] == 1 and counts["errors"] == 0
    assert counts["planned_queries"] == QUERIES
    assert counts["executed_sql"] is None and counts["llm_calls"] is None
    assert counts["returned_queries"] is None and counts["truncated_queries"] is None
    assert ids["run_id"] == outcome.run_id and len(ids["query_ids"]) == QUERIES
    row = await run_row(outcome.run_id)
    assert row is not None and row.status == "running", "超时那一刻运行还在跑"


async def test_the_truncated_count_matches_the_queries_that_were_clipped(tmp_path, client):
    """交回几条、被截几条，数得出来：结果被截短时台账标它、测量里也数到它。"""
    made = await make_asset(tmp_path, client, clipping_graph)
    try:
        outcome = await execute_once(made, metric_ids=["gmv"])
    finally:
        await drop_source(made["source_id"])
    assert outcome.ok, outcome.problems
    [query] = outcome.as_dict()["queries"]
    assert query["truncated"] is True and query["row_count"] == 1
    counts = outcome.as_dict()["measurement"]["counts"]
    assert counts["planned_queries"] == counts["executed_sql"] == 1
    assert counts["returned_queries"] == 1 and counts["truncated_queries"] == 1


async def test_queue_never_takes_the_place_of_waiting_on_a_person(asset, monkeypatch):
    """排队（等并发名额）与等人审批是两回事：谁也不冒充谁。

    一：没人和它抢名额时，queue_ms 只有「要名额到拿到名额」那一下的开销，而 wait_ms 是等人那 0.5 秒。
    二：名额确实被别人占着时，queue_ms 记的就是等名额的那一段，wait_ms 仍然是 0。
    """
    # 一、等人：图停在审批上 0.5 秒
    gate_graph = S.chain(
        S.node("start", "input"),
        S.node("gate", "human", mode="approve", title="看一眼"),
        S.node("out", "output", fields=[{"name": "r", "value": "{{ nodes.gate.approved }}"}]),
    )
    run = await run_manager.start(graph=gate_graph, input_payload={})
    for _ in range(200):
        if (await run_row(run.id)).status == "interrupted":
            break
        await asyncio.sleep(0.02)
    await asyncio.sleep(0.5)
    await run_manager.resume(run.id, {"approved": True})
    for _ in range(400):
        row = await run_row(run.id)
        if row.status in querycore.TERMINAL_RUN_STATUSES:
            break
        await asyncio.sleep(0.02)
    assert row.status == "succeeded", row.error
    assert row.usage["wait_ms"] >= 400, row.usage
    assert row.usage["queue_ms"] < 200, "排队不能把等人那 0.5 秒算进去"

    # 二、等名额：唯一的名额被别人占着，0.3 秒后才放
    limit_slots(monkeypatch, 1)
    from app.cluster import slots

    hog = slots.hold("perf-hog", "chat")
    await hog.__aenter__()

    async def release() -> None:
        await asyncio.sleep(0.3)
        await hog.__aexit__(None, None, None)

    releaser = asyncio.create_task(release())
    try:
        outcome = await execute_once(asset, trace="perf-queue")
    finally:
        # 用例中途出错也要把名额放掉：占着唯一的名额会拖住后面每一个用例
        if not releaser.done():
            releaser.cancel()
            with contextlib.suppress(Exception):
                await hog.__aexit__(None, None, None)

    assert outcome.ok, outcome.problems
    phases = outcome.as_dict()["measurement"]["phases"]
    assert phases["queue_ms"] >= 250, phases
    row = await run_row(outcome.run_id)
    assert row.usage["queue_ms"] == phases["queue_ms"] and row.usage["wait_ms"] == 0


# ==========================================================================
# 2、受控基线（opt-in：AGENTLAB_QUERYCORE_PERF=1 才跑）
# ==========================================================================

#: 打开基线的环境变量：默认不跑（一次基线是几百次真跑，不该混进日常回归）
PERF_ENV = "AGENTLAB_QUERYCORE_PERF"
#: 每一档跑多少次热执行（至少 100；显式调小只用于冒烟）
PERF_N_ENV = "AGENTLAB_QUERYCORE_PERF_N"
PERF_WARM = int(os.environ.get(PERF_N_ENV, "100"))
#: 冷启动样本各跑几次
PERF_COLD = int(os.environ.get("AGENTLAB_QUERYCORE_PERF_COLD", "3"))
#: 样本写到哪儿：默认一个临时目录（也可以指定 AGENTLAB_QUERYCORE_PERF_OUT）
PERF_OUT_ENV = "AGENTLAB_QUERYCORE_PERF_OUT"
#: 两个并发档：客户端同时发几个 execute。名额上限取 max(1, L // 2)：4 那一档有一半在排队，queue_ms 才有东西可验
PERF_LEVELS = (1, 4)
#: 报告里逐个看的耗时
REPORTED = ("total_ms", "run_ms", "database_ms", "queue_ms", "locate_ms", "closure_ms", "params_ms", "evidence_ms")


def percentile(values: list[int], pct: float) -> int | None:
    """最近的排位（nearest-rank）：样本少也不插值编一个中间值出来。"""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(0, min(len(ordered) - 1, math.ceil(pct / 100 * len(ordered)) - 1))
    return ordered[rank]


async def cold_start(source_id: str) -> None:
    """按「进程刚起来、还没连过这个源」的条件跑：清掉进程内的数据源连接池与现查缓存。

    真正的进程级冷启动要另起进程，这一版不做——报告里就按「清了进程内缓存」说清。"""
    from app.data import coverage
    from app.data.engine import engines

    await engines.invalidate(source_id)
    for key in [k for k in coverage._LIVE if k and k[0] == source_id]:
        coverage._LIVE.pop(key, None)


def sample_of(suite: str, level: int, phase: str, index: int, limit: int, wall_ms: int, outcome: Any,
              failure: str | None) -> dict[str, Any]:
    """一个原始样本：这次跑的哪一档、第几次、花了多久、交回什么、量到什么（量不到的照实 null）。"""
    measured = outcome.as_dict().get("measurement") if outcome is not None else None
    return {"suite": suite, "level": level, "phase": phase, "index": index, "slots_limit": limit,
            "wall_ms": wall_ms, "ok": bool(outcome is not None and outcome.ok),
            "status": None if outcome is None else outcome.status,
            "run_id": None if outcome is None else outcome.run_id,
            **({} if measured is None else {"phases": measured["phases"], "counts": measured["counts"],
                                            "ids": measured["ids"]}),
            "failure": failure}


async def run_sample(asset: dict, *, suite: str, level: int, phase: str, index: int, limit: int,
                     cold: bool = False) -> dict[str, Any]:
    """跑一个样本。**失败也留下**：异常记成样本的 failure，不剔除。"""
    if cold:
        await cold_start(asset["source_id"])
    began = time.perf_counter()
    outcome: Any = None
    failure: str | None = None
    try:
        outcome = await execute_once(asset, trace=f"{suite}:{index}")
    except Exception as e:  # noqa: BLE001 - 失败样本也要留下来，不能悄悄丢掉
        failure = f"{type(e).__name__}: {e}"
    return sample_of(suite, level, phase, index, limit, int((time.perf_counter() - began) * 1000), outcome, failure)


def group_key(sample: dict[str, Any]) -> tuple[str, int, str]:
    return str(sample["suite"]), int(sample["level"]), str(sample["phase"])


def summarize(samples: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按档汇总：错误率算在**全部**样本上（失败不剔除），每个耗时的分位数只用真的量到它的那些样本。"""
    groups: dict[tuple[str, int, str], list[dict[str, Any]]] = {}
    for sample in samples:
        groups.setdefault(group_key(sample), []).append(sample)
    out: list[dict[str, Any]] = []
    for (suite, level, phase), group in sorted(groups.items()):
        failed = [s for s in group if not s["ok"]]
        row: dict[str, Any] = {"suite": suite, "level": level, "phase": phase, "n": len(group),
                               "slots_limit": group[0]["slots_limit"], "failed": len(failed),
                               "error_rate": round(len(failed) / len(group), 4),
                               "failure_kinds": sorted({str(s["failure"]) for s in failed if s["failure"]})
                               or sorted({str(s.get("status")) for s in failed}),
                               "metrics": {}}
        for name in REPORTED:
            values = [int(s["phases"][name]) for s in group
                      if isinstance(s.get("phases"), dict) and isinstance(s["phases"].get(name), int)]
            row["metrics"][name] = {"n": len(values), "p50": percentile(values, 50), "p95": percentile(values, 95),
                                    "max": max(values) if values else None}
        wall = [int(s["wall_ms"]) for s in group]
        row["metrics"]["wall_ms"] = {"n": len(wall), "p50": percentile(wall, 50), "p95": percentile(wall, 95),
                                     "max": max(wall)}
        executed = [s["counts"]["executed_sql"] for s in group
                    if isinstance(s.get("counts"), dict) and isinstance(s["counts"].get("executed_sql"), int)]
        row["counts"] = {"executed_sql_p50": percentile(executed, 50), "executed_sql_n": len(executed),
                         "llm_calls": sorted({s["counts"]["llm_calls"] for s in group
                                              if isinstance(s.get("counts"), dict)}),
                         "planned_queries": sorted({s["counts"]["planned_queries"] for s in group
                                                    if isinstance(s.get("counts"), dict)})}
        out.append(row)
    return out


def render(summary: list[dict[str, Any]], header: str) -> str:
    lines = [header, ""]
    for row in summary:
        lines.append(f"{row['suite']:>12} 并发{row['level']} 名额{row['slots_limit']} {row['phase']}: "
                     f"n={row['n']} 失败={row['failed']}（{row['error_rate']:.1%}）"
                     + (f" 失败类型={row['failure_kinds']}" if row["failure_kinds"] else ""))
        for name in REPORTED:
            stat = row["metrics"][name]
            lines.append(f"    {name:<14} n={stat['n']:<4} P50={stat['p50']}ms P95={stat['p95']}ms max={stat['max']}ms")
        counts = row["counts"]
        lines.append(f"    计数            executed_sql P50={counts['executed_sql_p50']}"
                     f" (n={counts['executed_sql_n']}) llm_calls={counts['llm_calls']}"
                     f" planned_queries={counts['planned_queries']}")
    return "\n".join(lines)


async def run_baseline(asset: dict, out_dir: Path, monkeypatch) -> dict[str, Any]:
    """跑完整一套：冷启动 + 并发 1 + 并发 4，原始样本落盘，返回汇总。"""
    samples: list[dict[str, Any]] = []
    for index in range(PERF_COLD):
        limit_slots(monkeypatch, 1)
        samples.append(await run_sample(asset, suite="cold", level=1, phase="cold", index=index, limit=1, cold=True))
    for level in PERF_LEVELS:
        limit = max(1, level // 2)
        limit_slots(monkeypatch, limit)
        index = 0
        while index < PERF_WARM:
            batch = min(level, PERF_WARM - index)
            results = await asyncio.gather(*[
                run_sample(asset, suite=f"concurrency-{level}", level=level, phase="warm", index=index + offset,
                           limit=limit)
                for offset in range(batch)])
            samples.extend(results)
            index += batch
    (out_dir / "samples.jsonl").write_text(
        "\n".join(json.dumps(s, ensure_ascii=False) for s in samples) + "\n", encoding="utf-8")
    summary = summarize(samples)
    code = {}
    for name in ("app/ledger/querycore.py", "app/engine/runner.py"):
        code[name] = hashlib.sha256((Path(__file__).resolve().parents[1] / name).read_bytes()).hexdigest()[:12]
    header = (f"== QueryCore K13 基线（合成数据） ==\n"
              f"样本目录：{out_dir}（samples.jsonl / summary.json）\n"
              f"代码快照：{code}\n"
              f"机器：{platform.platform()} / python {platform.python_version()} / {os.cpu_count()} 核\n"
              f"数据：{S.CURRENT['source']}（临时 SQLite，合成店铺库）；参数：{INPUTS}；指标：{METRIC_IDS}\n"
              f"预算：timeout_s={querycore.DEFAULT_TIMEOUT_S} 秒、max_queries={querycore.DEFAULT_MAX_QUERIES}\n"
              f"冷启动 = 每次先清掉进程内数据源连接池与现查缓存的那一次（不重启进程）：代表「连接/现查缓存还没热」；\n"
              f"           进程级的一次性开销（模块按需导入、编译）只落在整套基线的**第一次**执行上，是冷启动那一档的 max\n"
              f"热执行 = 连着跑（前面那些一次性开销已经付过）\n"
              f"每档热执行 {PERF_WARM} 次（要求 ≥100）、冷启动 {PERF_COLD} 次；失败样本不剔除\n"
              f"**没有生产负载：这一份推不出生产的 P50/P95**")
    report = render(summary, header)
    (out_dir / "summary.json").write_text(json.dumps({"header": header, "code_hashes": code, "params": INPUTS,
                                                     "metric_ids": METRIC_IDS, "levels": list(PERF_LEVELS),
                                                     "warm": PERF_WARM, "cold": PERF_COLD,
                                                     "summary": summary}, ensure_ascii=False, indent=2),
                                          encoding="utf-8")
    print("\n" + report)
    return {"samples": samples, "summary": summary, "report": report, "dir": str(out_dir)}


@pytest.mark.skipif(not os.environ.get(PERF_ENV), reason=f"受控基线要 {PERF_ENV}=1 才跑（默认不跑几百次真跑）")
async def test_the_controlled_baseline(asset, monkeypatch):
    """受控基线：冷启动/热执行分开、并发 1 与 4 两档、原始样本 + P50/P95 + 错误率。失败样本不剔除。"""
    out_dir = Path(os.environ.get(PERF_OUT_ENV) or tempfile.mkdtemp(prefix="agentlab-querycore-perf-"))
    out_dir.mkdir(parents=True, exist_ok=True)
    baseline = await run_baseline(asset, out_dir, monkeypatch)

    got = {(row["suite"], row["phase"]) for row in baseline["summary"]}
    assert got == {("cold", "cold"), ("concurrency-1", "warm"), ("concurrency-4", "warm")}, got
    hot = [row for row in baseline["summary"] if row["phase"] == "warm"]
    assert hot, "至少要有热执行那一档"
    for row in hot:
        assert row["n"] == PERF_WARM, f"{row['suite']} 跑了 {row['n']} 次，应当跑满 {PERF_WARM} 次"
        # 默认就是「至少 100 次」；显式把次数调小（冒烟）就按调小的算，报告里写着实际次数
        assert row["n"] >= 100 or PERF_N_ENV in os.environ, f"基线要求每档至少 100 次，实际 {row['n']}"
        assert row["metrics"]["total_ms"]["p50"] is not None
        assert row["metrics"]["total_ms"]["p95"] is not None
        # 每次都要把这次真的执行了几条 SQL 记下来（跑得成的那些）
        assert row["counts"]["executed_sql_n"] > 0
    # 并发 4 那一档要能看到排队：一半样本在等名额
    queued = [s for s in baseline["samples"]
              if s["suite"] == "concurrency-4" and isinstance(s.get("phases"), dict)
              and isinstance(s["phases"].get("queue_ms"), int) and s["phases"]["queue_ms"] > 0]
    assert queued, "并发 4（名额 2）那一档必须有样本真的排了队，queue_ms 才谈得上被验证"
    assert all(s["counts"]["llm_calls"] == 0 for s in baseline["samples"]
               if isinstance(s.get("counts"), dict) and s["counts"].get("llm_calls") is not None)
    assert (out_dir / "samples.jsonl").exists() and (out_dir / "summary.json").exists()
