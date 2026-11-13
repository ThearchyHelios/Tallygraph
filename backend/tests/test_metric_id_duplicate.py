"""指标 ID 收口：同一张口径卡上的指标 ID 要唯一（K08 的重复身份）。

下游 `nodes.<卡>.metrics[i]`、出具契约、比例契约、报告引用都按指标 ID 与位置取数：同一张卡上两个指标同名就分不清是哪一条。
所以**画布 / 发布前检查**（engine/schema）和**运行时**（engine/nodes/metrics）都要拦住，不能取第一条静默执行——那会把另一条的
数顶替过来。闭合解析（ledger/closure.selection_index）遇到同名只留第一次，运行时不能顺着它把第一条算出来，必须在过滤前
就响亮失败（两边都不静默，不冲突）。

空 ID（没写声明的）不算重复：缺 ID 另有报法（画布 metrics.id_missing、运行时「指标定义不完整」）。旧正常图（ID 唯一）
行为一字不变。数据是虚构店铺库。
"""
from __future__ import annotations

import pytest

from app.engine.nodes.metrics import SelectionRefused, duplicate_metric_ids, require_selection
from app.engine.runner import run_manager
from app.engine.schema import GraphSpec, validate_graph
from tests import ledger_samples as S

INPUTS = {"start": "2026-09-01", "end": "2026-09-08"}


@pytest.fixture(autouse=True)
async def engine_up(tmp_path):
    await run_manager.setup()
    await S.fresh_source(tmp_path / "shop.db")
    yield
    await run_manager.shutdown()


# ==========================================================================
# 纯函数：重复的认定
# ==========================================================================


def test_duplicate_metric_ids_ignores_blank_and_reports_each_id_once():
    defs = [{"id": "a"}, {"id": "b"}, {"id": "a"}, {"id": " a "}, {"id": ""}, {"x": 1}, {"id": "b"}, {"id": "b"}]
    assert duplicate_metric_ids(defs) == ["a", "b"]
    # 空 ID / 不是字典 / 没有 id 的都不算重复（另有报法）
    assert duplicate_metric_ids([{"id": ""}, {"id": "  "}, {"x": 1}, {}, "nope"]) == []
    assert duplicate_metric_ids([]) == []


def test_require_selection_refuses_duplicates_instead_of_taking_the_first():
    """闭合解析对同名只留第一次；运行时不能顺着它算第一条——要在过滤前就响亮失败。"""
    defs = [{"id": "gmv", "expression": "cell(nodes.fetch, 0, 'gmv')"},
            {"id": "gmv", "expression": "cell(nodes.fetch, 0, 'other')"}]
    with pytest.raises(SelectionRefused, match="重复"):
        require_selection(defs, ["gmv"])


def test_closure_and_runtime_share_the_duplicate_verdict():
    """闭包把重复记成未决、运行时响亮失败：判定只有一处（ledger.closure.duplicate_metric_ids），不冲突。"""
    from app.ledger import closure

    graph = S.chain(S.node("start", "input"),
                    S.node("fetch", "tool", tool=S.tool(), args={"sql": S.TEMPLATE}),
                    S.node("card", "metrics", metrics=[{"id": "gmv", "expression": "cell(nodes.fetch, 0, 'gmv')"},
                                                       {"id": "gmv", "expression": "cell(nodes.fetch, 0, 'other')"}]))
    metrics = graph["nodes"][2]["data"]["config"]["metrics"]
    assert closure.DUPLICATE_METRIC_ID in closure.CODES
    assert closure.duplicate_metric_ids(metrics) == duplicate_metric_ids(metrics) == ["gmv"]
    # 闭包：这张卡本身未决；运行时：过滤前就拒（绝不取第一条）
    assert closure.DUPLICATE_METRIC_ID in [p["code"] for p in closure.build(graph, "wf-dup", 1, "card").unresolved]
    with pytest.raises(SelectionRefused, match="重复"):
        require_selection(metrics, ["gmv"])


# ==========================================================================
# 画布 / 发布前检查：schema 报错
# ==========================================================================


def duplicate_card(*, with_caliber_from: bool = False) -> dict:
    metrics = [{"id": "gmv", "name": "销售额", "unit": "元", "expression": "cell(nodes.fetch, 0, 'gmv')"},
               {"id": "gmv", "name": "销售额（重复）", "unit": "元",
                "expression": "cell(nodes.fetch, 0, 'gmv')"}]
    card_config: dict = {"metrics": metrics}
    if with_caliber_from:
        card_config["caliber_from"] = {"workflow_id": "wf", "workflow_version": 1, "node_id": "card"}
    return S.chain(S.node("start", "input", fields=[{"name": "start", "required": True}, {"name": "end", "required": True}]),
                   S.node("fetch", "tool", tool=S.tool(), args={"sql": S.TEMPLATE}),
                   S.node("card", "metrics", caliber="重复口径", caliber_version="v1", **card_config),
                   S.node("out", "output", fields=[{"name": "r", "value": "{{ nodes.card.text }}"}]))


def issues(graph: dict) -> list:
    return list(validate_graph(GraphSpec.model_validate(graph)).issues)


def test_schema_rejects_a_duplicate_metric_id():
    found = [i for i in issues(duplicate_card()) if i.code == "metrics.id_duplicate"]
    assert found and [i.level for i in found] == ["error"]
    assert found[0].node_id == "card" and "gmv" in found[0].message
    assert any("唯一" in i.message for i in found)


def test_schema_flags_duplicates_even_on_a_caliber_from_card():
    """转抄卡本地写的 metrics 不生效，但同一张卡上的 ID 照样要唯一（现状是给一条「不会生效」的警告）。"""
    found = [i for i in issues(duplicate_card(with_caliber_from=True)) if i.code == "metrics.id_duplicate"]
    assert found


def test_schema_leaves_unique_ids_and_blank_ids_alone():
    unique = S.chain(S.node("start", "input"), S.node("fetch", "tool", tool=S.tool(), args={"sql": S.TEMPLATE}),
                     S.node("card", "metrics", metrics=[
                         {"id": "a", "name": "A", "expression": "cell(nodes.fetch, 0, 'gmv')"},
                         {"id": "b", "name": "B", "expression": "cell(nodes.fetch, 0, 'gmv')"}]))
    assert not [i for i in issues(unique) if i.code == "metrics.id_duplicate"]
    # 两条都没有 ID：是「缺少 ID」，不是「重复」
    blank = S.chain(S.node("start", "input"), S.node("fetch", "tool", tool=S.tool(), args={"sql": S.TEMPLATE}),
                    S.node("card", "metrics", metrics=[
                        {"id": "", "name": "A", "expression": "cell(nodes.fetch, 0, 'gmv')"},
                        {"name": "B", "expression": "cell(nodes.fetch, 0, 'gmv')"}]))
    codes = [i.code for i in issues(blank)]
    assert "metrics.id_missing" in codes and "metrics.id_duplicate" not in codes


# ==========================================================================
# 运行时：源卡读取（本地卡 / 转抄卡）也响亮失败
# ==========================================================================


async def run(graph: dict):
    started = await run_manager.start(graph=graph, input_payload=dict(INPUTS))
    return await S.wait(started.id)


async def upstream_with_duplicates() -> str:
    graph = S.chain(
        S.node("start", "input", fields=[{"name": "start", "required": True}, {"name": "end", "required": True}]),
        S.node("fetch", "tool", tool=S.tool(), args={"sql": S.TEMPLATE}),
        S.node("card", "metrics", caliber="重复口径", caliber_version="v1", metrics=[
            {"id": "gmv", "name": "销售额", "unit": "元", "expression": "cell(nodes.fetch, 0, 'gmv')"},
            {"id": "gmv", "name": "销售额（重复）", "unit": "元", "expression": "cell(nodes.fetch, 0, 'gmv')"}]))
    return await S.publish(graph=graph, name="K08 重复上游")


def consumer(workflow_id: str, *, selection: list[str] | None = None) -> dict:
    ref: dict = {"workflow_id": workflow_id, "workflow_version": 1, "node_id": "card"}
    if selection is not None:
        ref["metrics"] = list(selection)
    return S.chain(
        S.node("start", "input", fields=[{"name": "start", "required": True}, {"name": "end", "required": True}]),
        S.node("fetch", "tool", tool=S.tool(), args={"sql": S.TEMPLATE}),
        S.node("card", "metrics", caliber_from=ref),
        S.node("out", "output", fields=[{"name": "r", "value": "{{ nodes.card.text }}"}]))


async def test_runtime_refuses_duplicate_ids_on_a_pinned_card():
    """钉住的那一版卡上 ID 重复：整卡读取也要响亮失败，不能取第一条静默执行。"""
    workflow_id = await upstream_with_duplicates()
    row = await run(consumer(workflow_id))
    assert row.status == "failed" and "重复" in str(row.error)
    assert "gmv" in str(row.error)


async def test_runtime_refuses_a_selection_over_a_duplicate_card():
    """带选择点名那个重复的 ID：闭合解析只留第一次，运行时不能顺着它算第一条——在过滤前就拦住。"""
    workflow_id = await upstream_with_duplicates()
    row = await run(consumer(workflow_id, selection=["gmv"]))
    assert row.status == "failed" and "重复" in str(row.error)
    assert "gmv" in str(row.error)


async def test_runtime_leaves_a_unique_card_alone():
    row = await run(consumer(await S.publish(name="K08 唯一上游")))
    assert row.status == "succeeded", row.error


async def test_publish_precheck_blocks_a_duplicate_local_card():
    """本地重复 ID 的卡过不了画布 / 发布前检查：run_manager.start 直接拒，不会跑到运行时。"""
    with pytest.raises(ValueError, match="重复"):
        await run_manager.start(graph=duplicate_card(), input_payload=dict(INPUTS))
