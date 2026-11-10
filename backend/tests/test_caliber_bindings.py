"""K04 试点业务映射与编辑能力：显式声明的版本化受控模板绑定，定义和取数 SQL 一次改到位。

- 合成 A/B/C/D 四单（tests/binding_samples.py，任务书 6.2）：旧定义（下单时间、不过滤退款）十月 3 单 900 元；
  经**真实 PATCH operations** 把两个共享 SQL 的指标一起改成支付时间 + 排除全额退款之后，真实试跑的独立预期 2 单 500 元
  （A 纳入、B 跨期排除、C 排除、D 保留）；
- 一次变更把定义和 SQL 放在同一个事务里改：版本只 +1，args.sql 真的变了（剩下那一段新写法），binding 的 values /
  rendered_hash / fingerprint 跟着走，没提到的键、别的节点和扩展字段一个字不动；
- 换查询窗口只改参数，不建定义版本；
- 同步不了的一律**定义照常保存、SQL 一个字不动**，照实说 needs_update / unsupported + 原因 + 影响对象 + 该做什么，
  并且在**发运行之前**拦住试跑（旧 SQL 的数不能冒充新定义）：复杂 SQL（没有 [[time_role]] 的落点）、SQL 被手工改过
  （哈希对不上）、同一条 SQL 上的两个指标说法打架、画布上手工改了定义；
- 读接口给服务端能力描述：能改哪些业务字段、允许值、现在的值、同步状态；aligned 不等于验证通过
  （semantic_unverified 和质量状态不因它变 pass）；
- 非法值、幂等重放、并发同键、没有业务字段的旧资产照旧。

真引擎跑试跑（run_manager.setup + fixtures 里的真实 SQLite 订单库），不发模型请求。
"""
from __future__ import annotations

import asyncio
import copy

import pytest
from sqlalchemy import delete, func, select

from app.db.base import SessionLocal
from app.db.models import CaliberDraft, DataSource, Run, Workflow, WorkflowVersion
from app.engine.runner import run_manager
from app.ledger import bindings
from tests import binding_samples as S
from tests.test_ledger_docdraft_api import client  # noqa: F401 - fixture 和起草用例共用

#: 这个文件自己建的工作流（连同版本、草稿）和临时数据源跑完删掉
OWN_PREFIX = "K04 "


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
        for name in S.MADE["sources"]:
            await session.execute(delete(DataSource).where(DataSource.name == name))
        S.MADE["sources"].clear()
        await session.commit()


@pytest.fixture(autouse=True)
async def use_iteration_free_source(tmp_path):
    """每个用例一个真实的 SQLite 订单库：图里的查询工具指向它，试跑真的查它。"""
    await S.fresh_source(tmp_path / "orders.db")
    yield


@pytest.fixture(autouse=True)
async def engine_up():
    """试跑走真引擎（探针图里没有报告节点，不发模型请求）。"""
    await run_manager.setup()
    yield
    await run_manager.shutdown()


def patch_url(draft_id: str) -> str:
    return f"/api/ledger/drafts/{draft_id}"


def biz_ops(*, metrics=("m1", "m2"), time_role="payment_time", refund_rule="exclude_full_refund") -> list[dict]:
    """把这几条指标的时间口径和退款规则一起改掉的字段操作。"""
    out: list[dict] = []
    for metric_id in metrics:
        if time_role is not None:
            out.append({"op": "set", "metric_id": metric_id, "path": "/time_role", "value": time_role})
        if refund_rule is not None:
            out.append({"op": "set", "metric_id": metric_id, "path": "/refund_rule", "value": refund_rule})
    return out


async def patch_biz(client, body: dict, operations: list[dict], **extra):
    payload = {"operations": operations, "expected_revision": body["revision"], **extra}
    return await client.patch(patch_url(body["id"]), json=payload)


async def trial(client, body: dict, values: dict | None = None) -> dict:
    """按入口字段真跑一次试跑（真实引擎）。"""
    res = await client.post(f"/api/ledger/drafts/{body['id']}/trial", json={"inputs": dict(values or S.OCTOBER)})
    assert res.status_code == 200, res.text
    return res.json()


def values_of(trial_body: dict) -> dict[str, float]:
    return {metric["id"]: float(metric["value"]) for metric in trial_body["metrics"] if metric.get("value") is not None}


async def graph_of(workflow_id: str, version: int | None = None) -> dict:
    async with SessionLocal() as session:
        workflow = await session.get(Workflow, workflow_id)
        if version is None or version == workflow.version:
            return copy.deepcopy(workflow.graph)
        row = (await session.execute(select(WorkflowVersion).where(
            WorkflowVersion.workflow_id == workflow_id, WorkflowVersion.version == version))).scalars().one()
        return copy.deepcopy(row.graph)


async def versions_of(workflow_id: str) -> list[int]:
    async with SessionLocal() as session:
        return list((await session.execute(select(WorkflowVersion.version).where(
            WorkflowVersion.workflow_id == workflow_id).order_by(WorkflowVersion.version))).scalars())


async def runs_of(workflow_id: str) -> int:
    async with SessionLocal() as session:
        return int(await session.scalar(select(func.count(Run.id)).where(Run.workflow_id == workflow_id)) or 0)


def fetch_config(graph: dict, node_id: str = "fetch") -> dict:
    node = next(n for n in graph["nodes"] if n["id"] == node_id)
    return node["data"]["config"]


def card_metric(graph: dict, metric_id: str, node_id: str = "card") -> dict:
    config = next(n for n in graph["nodes"] if n["id"] == node_id)["data"]["config"]
    return next(metric for metric in config["metrics"] if metric["id"] == metric_id)


# ==========================================================================
# 正向：旧定义 3，一次变更之后 2
# ==========================================================================

async def test_the_old_definition_counts_three_orders_in_october(client):
    """旧定义（按下单时间、不排除退款）：十月房子的独立预期是 3 单——真引擎试跑对得上。"""
    body = await S.draft(S.graph())
    done = await trial(client, body)
    assert done["status"] == "succeeded", done.get("error")
    assert values_of(done) == {"m1": S.EXPECTED["old"]["orders"], "m2": S.EXPECTED["old"]["amount"]}
    assert done["version"] == 1


async def test_one_patch_moves_two_shared_sql_metrics_and_the_sql_with_them(client):
    """E05 的正向样例：一次字段变更把两个共享 SQL 的指标改成支付时间 + 排除全额退款，SQL 一起改，版本只 +1。"""
    body = await S.draft(S.graph())
    before = await graph_of(body["workflow_id"])
    before_sql = fetch_config(before)["args"]["sql"]
    before_binding = fetch_config(before)[bindings.KEY]

    res = await patch_biz(client, body, [*biz_ops(),
                                         {"op": "set", "metric_id": "m1", "path": "/definition/text",
                                          "value": "订单数按支付时间统计，全额退款排除"}])
    assert res.status_code == 200, res.text
    made = res.json()
    assert made["version"] == 2 and await versions_of(body["workflow_id"]) == [1, 2]

    change = made["change"]
    assert change["applied"] is True and change["binding"]["synced"] is True
    assert change["metrics"] == ["m1", "m2"]
    assert change["binding"]["status"] == bindings.ALIGNED and change["binding"]["checked"] is True
    assert change["binding"]["synced"] is True and change["binding"]["fix"] == "none"
    assert "不等于业务验证通过" in change["binding"]["reason"]
    assert set(change["binding"]["binding_fingerprints"]) == {"fetch"}

    after = await graph_of(body["workflow_id"])
    sql = fetch_config(after)["args"]["sql"]
    assert sql == S.render(S.NEW) and sql != before_sql
    assert "o.paid_at" in sql and "o.refund_state <> 'full'" in sql and "o.ordered_at" not in sql
    # 没提到的：这条取数节点上别的键、别的节点、图里的扩展字段一个字不动
    assert fetch_config(after)["hints"] == {"keep": "me"}
    assert fetch_config(after)["tool"] == fetch_config(before)["tool"]
    assert [n["id"] for n in after["nodes"]] == [n["id"] for n in before["nodes"]]
    assert next(n for n in after["nodes"] if n["id"] == "start") == next(n for n in before["nodes"] if n["id"] == "start")
    updated = fetch_config(after)[bindings.KEY]
    assert updated["values"] == S.NEW and updated["rendered_hash"] == bindings.sql_hash(sql)
    assert updated["fingerprint"] == change["binding"]["binding_fingerprints"]["fetch"]
    assert updated["fingerprint"] != bindings.fingerprint(before_binding)
    assert updated["domain"] == "order_sales" and updated["mapping_version"] == 1
    # 定义也改了，两个指标一致
    assert card_metric(after, "m1")["time_role"] == "payment_time"
    assert card_metric(after, "m2")["refund_rule"] == "exclude_full_refund"
    assert card_metric(after, "m1")["definition"]["text"] == "订单数按支付时间统计，全额退款排除"

    # 真跑一次：独立预期 2 单 500 元
    done = await trial(client, body)
    assert done["status"] == "succeeded", done.get("error")
    assert values_of(done) == {"m1": S.EXPECTED["new"]["orders"], "m2": S.EXPECTED["new"]["amount"]}
    assert done["version"] == 2

    # 换查询窗口只是请求变化：不建定义版本（九月没有订单：计数是 0，金额是空值）
    september = await trial(client, body, S.SEPTEMBER)
    assert september["status"] == "succeeded", september.get("error")
    assert values_of(september) == {"m1": 0.0}
    assert next(m for m in september["metrics"] if m["id"] == "m2").get("value") is None
    assert await versions_of(body["workflow_id"]) == [1, 2]


async def test_the_query_snapshot_records_which_binding_produced_the_sql(client):
    """查询快照记下这条 SQL 是哪份绑定的哪一次展开（业务值 + 绑定指纹）：事后认得出「这个数是按支付时间算的」。"""
    from app.core.artifact_store import load
    from app.engine.evidence_reader import read_sealed

    body = await S.draft(S.graph())
    await patch_biz(client, body, biz_ops())
    done = await trial(client, body)
    assert done["status"] == "succeeded", done.get("error")

    sealed = await read_sealed(done["run_id"])
    artifact = next(iter(sealed.queries))
    snapshot = load(artifact)
    material = snapshot["binding"]
    assert material["domain"] == "order_sales" and material["mapping_version"] == 1
    assert material["values"] == S.NEW and material["binder"] == bindings.BINDER
    assert material["fingerprint"] == (await graph_of(body["workflow_id"]))["nodes"][1]["data"]["config"][
        bindings.KEY]["fingerprint"]
    # 快照里记的实际语句是渲染过参数的那一条（K01），绑定材料说的是它来自哪次展开（模板展开的那一条，
    # {{ input.* }} 还在里面）——两种哈希各记各的，不混
    assert "o.paid_at >=" in snapshot["sql"] and "'2026-10-01'" in snapshot["sql"]
    configured = fetch_config(await graph_of(body["workflow_id"]))["args"]["sql"]
    assert material["rendered_hash"] == bindings.sql_hash(configured)
    assert "template" not in material and "snippets" not in material


# ==========================================================================
# 反例：同步不了的定义照常保存，SQL 一个字不动，运行之前拦住
# ==========================================================================

async def test_a_complex_sql_is_kept_as_a_draft_with_a_specific_unsupported(client):
    """复杂 SQL 反例：时间条件写死在 CTE 里，受控模板没有 [[time_role]] 的落点——定义照常保存，SQL 不动，不许跑。"""
    body = await S.draft(S.complex_graph())
    before = await graph_of(body["workflow_id"])
    runs_before = await runs_of(body["workflow_id"])

    res = await patch_biz(client, body, biz_ops(metrics=("m1", "m2")))
    assert res.status_code == 200, res.text
    made = res.json()
    assert made["version"] == 2
    change = made["change"]
    assert change["binding"]["synced"] is False and change["binding"]["status"] == bindings.UNSUPPORTED
    assert change["binding"]["fix"] == "author"
    assert "没有「统计时间口径」的落点" in change["binding"]["unsupported_reason"]
    assert change["binding"]["metrics"] == ["m1", "m2"] and change["binding"]["nodes"][0]["node_id"] == "fetch"

    after = await graph_of(body["workflow_id"])
    assert fetch_config(after)["args"]["sql"] == fetch_config(before)["args"]["sql"]
    assert fetch_config(after)[bindings.KEY]["values"] == {"refund_rule": "include_all"}   # 绑定也没被改
    assert card_metric(after, "m1")["time_role"] == "payment_time"      # 定义存下来了，草稿还在

    # 发运行之前拦住：一条 SQL 都不发，也不把旧数当新定义交回
    refused = await client.post(patch_url(body["id"]) + "/trial", json={"inputs": dict(S.OCTOBER)})
    assert refused.status_code == 200, refused.text
    assert refused.json()["status"] == "failed" and refused.json()["error"]
    assert refused.json()["metrics"] == []
    assert await runs_of(body["workflow_id"]) == runs_before
    shown = (await client.get(patch_url(body["id"]))).json()
    assert shown["binding"]["status"] == bindings.UNSUPPORTED
    assert shown["trial"]["status"] in ("failed", "stale") or shown["trial"]["status"] == "needs_input"


async def test_a_hand_edited_sql_is_needs_update_and_is_never_rewritten(client):
    """SQL 被手工改过（和绑定记录对不上）：不覆盖人的修改，照实说 needs_update，也不许跑。"""
    graph = S.graph()
    fetch_config(graph)["args"]["sql"] = S.render(S.OLD) + " -- 手工加的注释"
    body = await S.draft(graph)
    runs_before = await runs_of(body["workflow_id"])

    res = await patch_biz(client, body, biz_ops())
    assert res.status_code == 200, res.text
    change = res.json()["change"]
    assert change["binding"]["synced"] is False and change["binding"]["status"] == bindings.NEEDS_UPDATE
    assert change["binding"]["fix"] == "manual"
    assert "不是这份绑定展开出来的" in change["binding"]["reason"]
    after = await graph_of(body["workflow_id"])
    assert fetch_config(after)["args"]["sql"].endswith("-- 手工加的注释")
    assert fetch_config(after)[bindings.KEY]["values"] == S.OLD     # 绑定也没被改

    refused = await client.post(patch_url(body["id"]) + "/trial", json={"inputs": dict(S.OCTOBER)})
    assert refused.json()["status"] == "failed"
    assert await runs_of(body["workflow_id"]) == runs_before


async def test_two_metrics_disagreeing_on_one_sql_are_a_conflict(client):
    """同一条 SQL 上的两个指标说法不一致：一个节点只有一份 SQL，先统一再改——SQL 不动，也不许跑。"""
    body = await S.draft(S.graph())
    before = await graph_of(body["workflow_id"])
    res = await patch_biz(client, body, [{"op": "set", "metric_id": "m1", "path": "/time_role",
                                          "value": "payment_time"}])
    assert res.status_code == 200, res.text
    change = res.json()["change"]
    assert change["binding"]["synced"] is False and change["binding"]["status"] == bindings.NEEDS_UPDATE
    assert change["binding"]["fix"] == "manual" and "说法不一致" in change["binding"]["reason"]
    assert fetch_config(await graph_of(body["workflow_id"]))["args"]["sql"] == fetch_config(before)["args"]["sql"]

    runs_before = await runs_of(body["workflow_id"])
    refused = await client.post(patch_url(body["id"]) + "/trial", json={"inputs": dict(S.OCTOBER)})
    assert refused.json()["status"] == "failed"
    assert await runs_of(body["workflow_id"]) == runs_before


async def test_a_definition_edited_on_the_canvas_reads_as_needs_update(client):
    """画布上手工改定义（绕过字段变更服务）：读接口如实说 needs_update，试跑在发运行之前拦住。"""
    body = await S.draft(S.graph())
    graph = await graph_of(body["workflow_id"])
    # 两个指标在画布上一起改成支付时间（说法仍然一致）：定义走到了实现前面，SQL 还是旧的那条
    card_metric(graph, "m1")["time_role"] = "payment_time"
    card_metric(graph, "m2")["time_role"] = "payment_time"
    saved = await client.patch(f"/api/workflows/{body['workflow_id']}",
                               json={"graph": graph, "expected_revision": body["revision"]})
    assert saved.status_code == 200 and saved.json()["version"] == 2, saved.text

    shown = (await client.get(patch_url(body["id"]))).json()
    assert shown["binding"]["status"] == bindings.NEEDS_UPDATE
    assert shown["binding"]["fix"] == "sync" and "可以按受控模板" in shown["binding"]["reason"]
    assert shown["binding"]["metric_values"]["m1"]["time_role"] == "payment_time"

    runs_before = await runs_of(body["workflow_id"])
    refused = await client.post(patch_url(body["id"]) + "/trial", json={"inputs": dict(S.OCTOBER)})
    assert refused.json()["status"] == "failed" and refused.json()["metrics"] == []
    assert "绑定里还是旧的" in refused.json()["error"]
    assert await runs_of(body["workflow_id"]) == runs_before


async def test_an_unaligned_caliber_cannot_be_composed_or_recalled(client):
    """派生那条路（组合式问数、召回、对账、评测）问的是同一份 load_pinned：对不上就派生不出来。"""
    from app.ledger import derive

    body = await S.draft(S.graph())
    # 只改一个指标 → 同一条 SQL 上的两个指标说法不一致：派生不出来
    await patch_biz(client, body, [{"op": "set", "metric_id": "m1", "path": "/time_role",
                                    "value": "payment_time"}])
    async with SessionLocal() as session:
        with pytest.raises(derive.NotDerivable) as refused:
            await derive.load_pinned(session, body["workflow_id"], 2, "card")
    assert refused.value.code == derive.BINDING_UNALIGNED
    assert "说法不一致" in refused.value.message


async def test_the_raw_graph_run_stops_before_computing_any_metric(client):
    """原图直接运行（不走派生）：口径卡在算数之前就停——旧 SQL 的结果不许冒充新定义。"""
    from tests.ledger_samples import wait as wait_run

    body = await S.draft(S.graph())
    await patch_biz(client, body, [{"op": "set", "metric_id": "m1", "path": "/time_role",
                                    "value": "payment_time"}])
    graph = await graph_of(body["workflow_id"])
    run = await run_manager.start(graph=graph, input_payload=dict(S.OCTOBER), run_class="exploratory",
                                  origin_kind="chat", origin={"conversation_id": "k04-raw"})
    row = await wait_run(run.id)
    assert row.status == "failed" and "对不上" in str(row.error)
    assert "没有计算任何指标" in str(row.error)


# ==========================================================================
# 非法值、幂等、并发
# ==========================================================================

@pytest.mark.parametrize("path,value", [("/time_role", "yesterday"), ("/refund_rule", "full_only"),
                                        ("/time_role", ""), ("/refund_rule", 3)])
async def test_an_illegal_business_value_is_refused(client, path, value):
    body = await S.draft(S.graph())
    res = await patch_biz(client, body, [{"op": "set", "metric_id": "m1", "path": path, "value": value}])
    assert res.status_code == 422, res.text
    assert res.json()["code"] in ("operation_value",) and res.json()["detail"]
    assert await versions_of(body["workflow_id"]) == [1]


async def test_the_business_change_replays_idempotently_and_refuses_concurrently(client):
    """同一个 request_id 重放不新增版本；同键并发只落一版。"""
    body = await S.draft(S.graph())
    operations = biz_ops()
    first = await patch_biz(client, body, operations, request_id="k04-0001")
    assert first.status_code == 200 and first.json()["version"] == 2, first.text
    again = await patch_biz(client, body, operations, request_id="k04-0001")
    assert again.status_code == 200 and again.json().get("replayed") is True
    assert await versions_of(body["workflow_id"]) == [1, 2]

    other = await S.draft(S.graph())
    payload = {"operations": biz_ops(), "expected_revision": other["revision"], "request_id": "k04-race"}
    url = patch_url(other["id"])
    raced = await asyncio.gather(client.patch(url, json=payload), client.patch(url, json=payload))
    assert sorted(res.status_code for res in raced) == [200, 200], (raced[0].text, raced[1].text)
    assert await versions_of(other["workflow_id"]) == [1, 2]


# ==========================================================================
# 转抄卡（caliber_from）：原图直接跑时，本地那句 SQL 也必须和钉住的定义对得上
# ==========================================================================

def test_analyzing_a_chained_card_uses_the_pinned_definitions_and_the_local_nodes():
    """转抄卡在本图里没有自己的指标：拿**钉住那一版**的定义（式子已按源→本地映射改写到本地节点）判本地取数节点。"""
    upstream = asyncio.run(S.upstream()) if False else None      # 纯函数用例，不需要库
    rename = {"fetch": "c1_fetch"}
    graph = S.chained_graph({"workflow_id": "up", "card": "card"}, version=2, local_values=S.OLD, rename=rename)
    pinned_metrics = [
        {"id": "m1", "name": "订单数", "expression": "cell(nodes.c1_fetch, 0, 'orders')",
         "time_role": "payment_time", "refund_rule": "exclude_full_refund"},
        {"id": "m2", "name": "销售额", "expression": "cell(nodes.c1_fetch, 0, 'amount')",
         "time_role": "payment_time"},
    ]
    report = bindings.analyze(graph, "card", pinned_metrics)
    assert upstream is None
    # 判的是本地那个取数节点（映射之后的名字），而不是「没声明业务字段」而放行
    assert report.status == bindings.NEEDS_UPDATE and report.blocked
    assert [node.node_id for node in report.nodes] == ["c1_fetch"]
    assert report.nodes[0].declared == {"time_role": "payment_time", "refund_rule": "exclude_full_refund"}
    assert report.nodes[0].values == S.OLD and report.metrics == ("m1", "m2")
    assert bindings.blocking(report)

    # 本地节点就是那一版的展开：aligned，放行
    aligned = S.chained_graph({"workflow_id": "up", "card": "card"}, version=2, local_values=S.NEW, rename=rename)
    assert bindings.analyze(aligned, "card", pinned_metrics).status == bindings.ALIGNED


async def test_a_chained_card_never_shows_the_old_local_numbers_as_the_pinned_definition(client):
    """复核里的反例：上游 v2 是「按支付时间」（2 单 500），消费侧本地那句取数还停在旧的「按下单时间」（3 单 900）。

    原图直接跑必须在算数之前拦住——不能把 3 单 900 元当成 v2 的定义交出来。
    """
    from app.engine.evidence_reader import metric_cards, read_sealed
    from tests.ledger_samples import wait as wait_run

    upstream = await S.upstream()
    graph = S.chained_graph(upstream, version=2, local_values=S.OLD)
    # 先证明本地那句 SQL 自己跑出来确实是旧定义的数（3 单 900 元）：它不是错的，错的是拿它当新定义
    assert S.graph(values=S.OLD)["nodes"][1]["data"]["config"]["args"]["sql"] == S.render(S.OLD)

    run = await run_manager.start(graph=graph, input_payload=dict(S.OCTOBER), run_class="exploratory",
                                  origin_kind="chat", origin={"conversation_id": "k04-chain"})
    row = await wait_run(run.id)
    assert row.status == "failed", row.error
    assert "对不上" in str(row.error) and "没有计算任何指标" in str(row.error)
    assert "绑定里还是旧的" in str(row.error)
    sealed = await read_sealed(run.id)
    assert list(metric_cards(sealed)) == []          # 一个数都没算出来，更没有 3 单 900 元


async def test_a_chained_card_with_the_pinned_sql_runs_and_gives_the_pinned_numbers(client):
    """同一张转抄卡，本地取数就是钉住那一版的展开（还带源→本地的节点改名映射）：照常跑，数就是新定义的 2 单 500 元。"""
    from app.engine.evidence_reader import metric_cards, read_sealed
    from tests.ledger_samples import wait as wait_run

    upstream = await S.upstream()
    graph = S.chained_graph(upstream, version=2, local_values=S.NEW, rename={"fetch": "c1_fetch"})
    run = await run_manager.start(graph=graph, input_payload=dict(S.OCTOBER), run_class="exploratory",
                                  origin_kind="chat", origin={"conversation_id": "k04-chain-ok"})
    row = await wait_run(run.id)
    assert row.status == "succeeded", row.error
    sealed = await read_sealed(run.id)
    computed = {metric["id"]: float(metric["value"]) for _node, _artifact, card in metric_cards(sealed)
                for metric in card.get("metrics") or []}
    assert computed == {"m1": S.EXPECTED["new"]["orders"], "m2": S.EXPECTED["new"]["amount"]}   # 2 单 500 元


async def test_a_composed_copy_of_an_aligned_caliber_is_not_blocked(client):
    """组合式问数把口径拷进另一张图时参数只渲染一次（K01 契约）：这份副本不该被判成「SQL 和绑定对不上」。

    拷出来的节点 args 已经是渲染好的字面语句，和「模板展开」不可能逐字相等——业务语义在派生那一刻按源版本核过
    （load_pinned），这里只比这一版的指标定义和副本上记的业务值。改过 args 的副本契约作废，照普通节点判。
    """
    from app.engine.nodes.metrics import rename_definition
    from app.ledger import derive

    workflow_id = await S.publish(S.graph(values=S.NEW), name=f"{OWN_PREFIX}组合")
    async with SessionLocal() as session:
        pinned = await derive.load_pinned(session, workflow_id, 1, "card")
    block = derive.caliber_block(pinned, prefix="c1_", inputs={"start": "2026-10-01", "end": "2026-11-01"})
    graph = derive.assemble("caliber_probe", [block], fields=pinned.entry_fields, defaults=pinned.defaults).graph
    definitions = [rename_definition(metric, {"fetch": "c1_fetch"}) for metric in pinned.metrics]

    report = bindings.analyze(graph, "c1_card", definitions)
    assert report.status == bindings.ALIGNED and not report.blocked
    assert [node.node_id for node in report.nodes] == ["c1_fetch"]
    assert report.nodes[0].values == S.NEW

    # 副本的 args 被手工改过（契约作废）：不再是渲染副本，按普通节点判——SQL 和绑定对不上，拦住
    broken = copy.deepcopy(graph)
    fetch = next(node for node in broken["nodes"] if node["id"] == "c1_fetch")
    fetch["data"]["config"]["args"]["sql"] = "SELECT COUNT(*) AS orders, SUM(amount) AS amount FROM orders"
    assert bindings.analyze(broken, "c1_card", definitions).status == bindings.NEEDS_UPDATE


# ==========================================================================
# 绑定自己就不自洽：模板里的 token 没有值
# ==========================================================================

#: 引号里写 token 的受控模板（复核要求覆盖裸 token 和引号内的 token 两种写法）
QUOTED_TEMPLATE = ("SELECT COUNT(*) AS orders, SUM(o.amount) AS amount FROM orders o "
                   "WHERE o.pay_state = '[[refund_rule]]' AND ([[time_role]])")
QUOTED_SNIPPETS = {"refund_rule": {"include_all": "any", "exclude_full_refund": "paid_only"},
                   "time_role": S.SNIPPETS["time_role"]}
QUOTED_VALUES = {"refund_rule": "exclude_full_refund", "time_role": "payment_time"}


@pytest.mark.parametrize("template,snippets,full_values,broken_values,label", [
    # 裸 token：模板里 [[time_role]] 有映射，当前业务值里却没有它
    (S.TEMPLATE, S.SNIPPETS, S.NEW, {"refund_rule": "exclude_full_refund"}, "统计时间口径"),
    # 引号里的 token：'[[refund_rule]]' 同样不能没值就放行
    (QUOTED_TEMPLATE, QUOTED_SNIPPETS, QUOTED_VALUES, {"time_role": "payment_time"}, "退款处理"),
])
def test_a_token_without_a_value_is_unsupported_and_is_never_written_back(template, snippets, full_values,
                                                                         broken_values, label):
    """模板/允许映射里有 token、当前业务值却缺这个字段：判 unsupported（点名是哪个字段），不写 SQL、不记材料。"""
    sql = bindings.render(template, snippets, full_values)          # 先在完整取值下展开（对照组：展开得了）
    assert "[[" not in sql
    graph = S.graph(values=S.NEW, metrics=S.card_metrics())
    config = fetch_config(graph)
    config["args"]["sql"] = sql
    config[bindings.KEY] = {"binder": bindings.BINDER, "version": 1, "domain": "order_sales", "mapping_version": 1,
                            "template": template, "snippets": copy.deepcopy(snippets), "values": dict(broken_values),
                            "rendered_hash": bindings.sql_hash(sql)}
    assert bindings.shape_problem(config[bindings.KEY])

    report = bindings.plan(graph, "card")
    assert report.status == bindings.UNSUPPORTED and report.configs == {}
    assert report.fix == bindings.FIX_AUTHOR and label in report.reason
    assert "没有它" in report.reason
    assert bindings.blocking(report)
    assert bindings.snapshot_material(config, config["args"]) is None      # 不记一份假的绑定材料
    with pytest.raises(bindings.UnresolvedToken):
        bindings.render(template, snippets, broken_values)                 # 也不许把 [[…]] 原样留着


async def test_a_broken_binding_keeps_the_definition_and_leaves_the_sql_alone(client):
    """这条不自洽的绑定经真实 PATCH：定义照常保存，SQL 一个字不动（也不把 [[…]] 写回去），运行照样被拦。"""
    graph = S.graph(values=S.OLD, metrics=S.card_metrics())
    config = fetch_config(graph)
    broken = config[bindings.KEY]
    broken["values"] = {"time_role": "order_time"}          # 模板里的 [[refund_rule]] 没值了
    body = await S.draft(graph)
    before_sql = config["args"]["sql"]

    res = await patch_biz(client, body, biz_ops())
    assert res.status_code == 200, res.text
    change = res.json()["change"]
    assert change["binding"]["synced"] is False and change["binding"]["status"] == bindings.UNSUPPORTED
    assert "退款处理" in change["binding"]["unsupported_reason"]
    after = await graph_of(body["workflow_id"])
    assert fetch_config(after)["args"]["sql"] == before_sql
    assert "[[" not in fetch_config(after)["args"]["sql"]               # 没把占位写回 SQL
    assert card_metric(after, "m1")["refund_rule"] == "exclude_full_refund"   # 定义存下来了

    runs_before = await runs_of(body["workflow_id"])
    refused = await client.post(patch_url(body["id"]) + "/trial", json={"inputs": dict(S.OCTOBER)})
    assert refused.json()["status"] == "failed" and refused.json()["metrics"] == []
    assert "退款处理" in refused.json()["error"]
    assert await runs_of(body["workflow_id"]) == runs_before


# ==========================================================================
# 声明了业务字段但定位不到取数节点：不能当成「没声明」放行
# ==========================================================================

def _unlocatable_graph(expression: str, *, extra_node: dict[str, Any] | None = None) -> dict:
    """一张卡：取数节点照常有受控绑定，但指标的式子读不到它（读 code / merge 节点、读 vars……）。"""
    graph = S.graph(values=S.NEW, metrics=[])
    card = next(node for node in graph["nodes"] if node["id"] == "card")
    card["data"]["config"]["metrics"] = [
        {"id": "m1", "name": "订单数", "expression": expression, "time_role": "payment_time"}]
    if extra_node is not None:
        graph["nodes"].insert(2, extra_node)
    return graph


@pytest.mark.parametrize("expression,extra_node,expected", [
    # 读沙箱代码节点的结果：那条 SQL 是别处来的，定位不到取数节点
    ("cell(nodes.code_1, 0, 'v')", S.node("code_1", "code", code="1"), "沙箱代码"),
    # 读合并查询节点：同样不是取数节点
    ("cell(nodes.merge_1, 0, 'v')", S.node("merge_1", "merge", sql="SELECT 1 AS v"), "合并查询"),
    # 式子读的是 vars/rows 这类拿不到节点身份的写法
    ("vars.total", None, "不是取数节点某一格"),
])
def test_a_declaration_that_cannot_be_located_is_unsupported(expression, extra_node, expected):
    """有声明、却定位不到它读的取数节点：unsupported（不是 unbound），保住 metric_values、原因和该做什么。"""
    graph = _unlocatable_graph(expression, extra_node=extra_node)
    report = bindings.analyze(graph, "card")
    assert report.status == bindings.UNSUPPORTED and report.blocked
    assert report.fix == bindings.FIX_AUTHOR
    assert report.metric_values == {"m1": {"time_role": "payment_time"}}     # 声明了什么，照实留着
    assert report.metrics == ("m1",)
    assert "「m1」" in report.reason and expected in report.reason
    assert "定位不到" in report.reason and bindings.blocking(report)

    # 同步不了，也不假装对齐：定义照旧、SQL 一个字不写
    assert bindings.plan(graph, "card").configs == {}
    capability = bindings.capability(graph, "card")
    assert capability["status"] == bindings.UNSUPPORTED and capability["metric_values"] == report.metric_values
    assert capability["unsupported_reason"] == report.reason

    # 同一张卡上照常挂在取数节点上的指标（能定位的那种）不受牵连：这次两份声明都在，整体仍是 unsupported
    both = copy.deepcopy(graph)
    next(node for node in both["nodes"] if node["id"] == "card")["data"]["config"]["metrics"].append(
        {"id": "m2", "name": "销售额", "expression": "cell(nodes.fetch, 0, 'amount')", "time_role": "payment_time"})
    mixed = bindings.analyze(both, "card")
    assert mixed.status == bindings.UNSUPPORTED and mixed.metrics == ("m1", "m2")
    assert [state.node_id for state in mixed.nodes] == ["fetch"]            # 能定位的那个照常判
    assert mixed.nodes[0].status == bindings.ALIGNED                        # 这张图的取数就是按支付时间展开的
    assert mixed.metric_values["m2"] == {"time_role": "payment_time"}


async def test_an_unlocatable_declaration_blocks_the_raw_run_and_the_change(client):
    """统一阻断：这种声明在原图直接运行和字段变更两条路上都拦住，一条 SQL 都不用发、也不落成「已同步」。"""
    from app.engine.evidence_reader import metric_cards, read_sealed
    from tests.ledger_samples import wait as wait_run

    graph = _unlocatable_graph("vars.total")
    body = await S.draft(graph)
    # 读接口：草稿页照实说 unsupported + 具体原因（不是 unbound）
    shown = (await client.get(patch_url(body["id"]))).json()
    assert shown["binding"]["status"] == bindings.UNSUPPORTED
    assert "定位不到" in shown["binding"]["unsupported_reason"]
    assert shown["binding"]["metric_values"] == {"m1": {"time_role": "payment_time"}}

    # 字段变更：定义照常保存，SQL 一个字不动，回执说清为什么没同步
    res = await patch_biz(client, body, [{"op": "set", "metric_id": "m1", "path": "/refund_rule",
                                          "value": "exclude_full_refund"}])
    assert res.status_code == 200, res.text
    change = res.json()["change"]
    assert change["binding"]["synced"] is False and change["binding"]["status"] == bindings.UNSUPPORTED
    assert "定位不到" in change["binding"]["unsupported_reason"]
    after = await graph_of(body["workflow_id"])
    assert fetch_config(after)["args"]["sql"] == S.render(S.NEW)            # 没被改写
    assert card_metric(after, "m1")["refund_rule"] == "exclude_full_refund"

    # 原图直接运行：算数之前停，一个数都没有
    run = await run_manager.start(graph=after, input_payload=dict(S.OCTOBER), run_class="exploratory",
                                  origin_kind="chat", origin={"conversation_id": "k04-locate"})
    row = await wait_run(run.id)
    assert row.status == "failed", row.error
    assert "没有计算任何指标" in str(row.error) and "定位不到" in str(row.error)
    assert list(metric_cards(await read_sealed(run.id))) == []

    # 试跑也一样：派生那一步（derive.load_pinned）就拒，一条 SQL 都不发、运行表里不多行
    runs_before = await runs_of(body["workflow_id"])
    refused = await client.post(patch_url(body["id"]) + "/trial", json={"inputs": dict(S.OCTOBER)})
    assert refused.status_code == 200, refused.text
    assert refused.json()["status"] == "failed" and refused.json()["metrics"] == []
    assert await runs_of(body["workflow_id"]) == runs_before


def test_normal_external_metrics_and_old_assets_are_not_harmed():
    """照旧不受影响的两类：没有业务声明的外部常量旧资产（unbound），以及和声明混在一起的外部常量指标。"""
    # 只有外部常量、没有任何业务声明：unbound，不判同步、不拦
    external_only = S.graph(values=S.OLD, metrics=[{
        "id": "m1", "name": "预算", "kind": "external",
        "external": {"source": "年度预算表", "as_of": "2026-01-01"}}])
    report = bindings.analyze(external_only, "card")
    assert report.status == bindings.UNBOUND and not report.blocked
    assert report.metric_values == {} and report.nodes == ()

    # 外部常量 + 一个声明了业务字段的取数指标：只判取数那个，外部常量不算「定位不到」
    mixed = S.graph(values=S.NEW)
    next(node for node in mixed["nodes"] if node["id"] == "card")["data"]["config"]["metrics"].append(
        {"id": "m9", "name": "预算", "kind": "external", "external": {"source": "年度预算表"}})
    report = bindings.analyze(mixed, "card")
    assert report.status == bindings.ALIGNED and not report.blocked
    assert report.metrics == ("m1", "m2") and report.metric_values.get("m9") is None
    assert [state.node_id for state in report.nodes] == ["fetch"]


# ==========================================================================
# 读接口：能力描述
# ==========================================================================

async def test_the_capability_description_lists_the_fields_the_values_and_the_state(client):
    """口径描述接口给：能改哪些业务字段、允许值、现在的值、同步状态；不返回模板和 SQL。"""
    workflow_id = await S.publish(S.graph(), name=f"{OWN_PREFIX}描述")
    made = (await client.get(f"/api/ledger/calibers/{workflow_id}/1/card")).json()
    block = made["binding"]
    assert [field["field"] for field in block["editable_fields"]] == ["time_role", "refund_rule"]
    time_role = block["editable_fields"][0]
    assert time_role["values"] == ["order_time", "payment_time"]
    assert time_role["value_labels"]["payment_time"] == "按支付时间" and time_role["label"] == "统计时间口径"
    assert block["current"] == S.OLD and block["status"] == bindings.ALIGNED and block["checked"] is True
    assert block["metric_values"] == {"m1": S.OLD, "m2": S.OLD}
    assert set(block["binding_fingerprints"]) == {"fetch"}
    dumped = str(made)
    assert "[[time_role]]" not in dumped and "SELECT COUNT(*)" not in dumped      # 不给模板、不给 SQL

    # 草稿页也给同一份（工作台按它决定展示哪些业务修改）
    body = await S.draft(S.graph())
    shown = (await client.get(patch_url(body["id"]))).json()
    assert shown["binding"]["current"] == S.OLD
    assert [field["field"] for field in shown["binding"]["editable_fields"]] == ["time_role", "refund_rule"]

    # 同步不了的：读接口照实给 unsupported + 原因（不返回模板，也不假装对齐）
    complex_wf = await S.publish(S.complex_graph(), name=f"{OWN_PREFIX}复杂描述")
    complex_block = (await client.get(f"/api/ledger/calibers/{complex_wf}/1/card")).json()["binding"]
    assert complex_block["status"] == bindings.UNSUPPORTED
    assert "没有「统计时间口径」的落点" in complex_block["unsupported_reason"]
    assert complex_block["fix"] == bindings.FIX_AUTHOR and complex_block["metrics"] == ["m1", "m2"]


async def test_aligned_never_means_verified(client):
    """aligned 只说明定义和实现同步：语义审阅标记和质量结论不会因为它变成「通过」。"""
    from app.engine.evidence_reader import metric_cards, read_sealed

    body = await S.draft(S.graph())
    assert (await client.get(patch_url(body["id"]))).json()["binding"]["status"] == bindings.ALIGNED
    done = await trial(client, body)
    assert done["status"] == "succeeded", done.get("error")

    # 封存的口径卡照旧标 semantic_unverified（业务描述和取数条件的一致性仍需审阅），质量结论是 unknown 不是 pass
    sealed = await read_sealed(done["run_id"])
    computed = {metric["id"]: metric for _node, _artifact, card in metric_cards(sealed)
                for metric in card.get("metrics") or []}
    assert computed["m1"]["semantic_unverified"] is True and computed["m2"]["semantic_unverified"] is True
    for metric in computed.values():
        assert metric["quality"]["status"] == "unknown"
        assert any(check["code"] == "semantic_definition" for check in metric["quality"]["checks"])


# ==========================================================================
# 旧资产：没有业务字段，照旧
# ==========================================================================

async def test_a_caliber_without_business_fields_keeps_its_old_behaviour(client):
    """没有声明时间口径 / 退款规则的卡（绝大多数旧资产）：状态 unbound、不拦、照旧跑。"""
    graph = S.graph(values=S.OLD, metrics=S.card_metrics(time_role=None, refund_rule=None), with_binding=False)
    body = await S.draft(graph)
    shown = (await client.get(patch_url(body["id"]))).json()
    assert shown["binding"]["status"] == bindings.UNBOUND
    assert shown["binding"]["metric_values"] == {} and shown["binding"]["nodes"] == []

    done = await trial(client, body)
    assert done["status"] == "succeeded", done.get("error")
    assert values_of(done) == {"m1": S.EXPECTED["old"]["orders"], "m2": S.EXPECTED["old"]["amount"]}
    # 改别的字段照旧（不因为「没绑定」被拦）
    res = await patch_biz(client, body, [{"op": "set", "metric_id": "m1", "path": "/unit", "value": "笔"}])
    assert res.status_code == 200, res.text
    assert res.json()["change"]["binding"]["synced"] is False
    assert res.json()["change"]["binding"]["status"] == bindings.UNBOUND

    # 一旦声明了业务字段，而取数节点上没有受控绑定：定义照常保存，但从此如实说 needs_update——不许再跑
    # （跑了也还是那条旧 SQL 算出来的数，那就是拿旧数冒充新定义）
    before_sql = fetch_config(await graph_of(body["workflow_id"]))["args"]["sql"]
    changed = await patch_biz(client, res.json(), biz_ops())
    assert changed.status_code == 200, changed.text
    assert changed.json()["change"]["binding"]["status"] == bindings.NEEDS_UPDATE
    assert changed.json()["change"]["binding"]["fix"] == bindings.FIX_AUTHOR
    assert fetch_config(await graph_of(body["workflow_id"]))["args"]["sql"] == before_sql
    runs_before = await runs_of(body["workflow_id"])
    refused = await client.post(patch_url(body["id"]) + "/trial", json={"inputs": dict(S.OCTOBER)})
    assert refused.json()["status"] == "failed" and refused.json()["metrics"] == []
    assert "没有受控绑定" in refused.json()["error"]
    assert await runs_of(body["workflow_id"]) == runs_before


def test_an_unbound_node_that_declares_business_fields_is_needs_update():
    """节点上没有绑定、指标却声明了业务字段：如实说 needs_update + 该做什么（不假装对齐，也不猜 SQL）。"""
    graph = S.graph(with_binding=False)
    report = bindings.plan(graph, "card")
    assert report.status == bindings.NEEDS_UPDATE and report.fix == bindings.FIX_AUTHOR
    assert report.configs == {} and "没有受控绑定" in report.reason
    assert bindings.blocking(report)
