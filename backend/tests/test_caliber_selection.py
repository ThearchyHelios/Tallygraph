"""K08 S5/S6：口径卡的指标选择（caliber_from.metrics）在运行时、画布校验、对账与召回里的一致解释。

拍板要点（任务书 K08）：

- caliber_from.metrics 是**可选选择**，缺省整卡；给了就只算选中的那些（按源卡顺序归一）——未知、空的**响亮失败**，
  绝不退回整卡（退回去算的是别的指标）；
- 绑定判定只看**过滤后的真实定义**：B 独有（或和 A 共享一条 SQL 上的说法）的问题不牵连 A；
- ``nodes.<卡>.metrics[i]`` 的 i 落在**这次真正算出来的那份数组**（有效数组）上，不是全量定义那份：运行时算出的指标集、
  对账卡、召回卡建位置引用时都按同一份（ledger.closure.selection_index）；
- 老图（没写选择）行为一字不变。

引擎用例用 tests/binding_samples 的合成订单夹具（旧定义 3 单 900 元 / 新定义 2 单 500 元），真跑探索运行，不发模型请求。
"""
from __future__ import annotations

import pytest
from sqlalchemy import delete, select as sa_select

from app.core import artifact_store
from app.db.base import SessionLocal
from app.db.models import CaliberDraft, DataSource, RunEvent, Workflow, WorkflowVersion
from app.engine.runner import run_manager
from app.engine.schema import GraphSpec, validate_graph
from tests import binding_samples as S
from tests.ledger_samples import wait as wait_run

#: 这个文件自己建的工作流（连同版本、草稿）和临时数据源跑完删掉
OWN_PREFIX = "K08 "


# ==========================================================================
# 夹具
# ==========================================================================


@pytest.fixture(autouse=True)
async def cleanup_own_rows():
    yield
    async with SessionLocal() as session:
        ids = list((await session.execute(
            sa_select(Workflow.id).where(Workflow.name.like(f"{OWN_PREFIX}%")))).scalars())
        if ids:
            for model in (WorkflowVersion, CaliberDraft):
                await session.execute(delete(model).where(model.workflow_id.in_(ids)))
            await session.execute(delete(Workflow).where(Workflow.id.in_(ids)))
        for name in S.MADE["sources"]:
            await session.execute(delete(DataSource).where(DataSource.name == name))
        S.MADE["sources"].clear()
        await session.commit()


@pytest.fixture(autouse=True)
async def order_db(tmp_path):
    await S.fresh_source(tmp_path / "orders.db")
    yield


@pytest.fixture(autouse=True)
async def engine_up():
    await run_manager.setup()
    yield
    await run_manager.shutdown()


async def run(graph: dict) -> object:
    started = await run_manager.start(graph=graph, input_payload=dict(S.OCTOBER))
    return await wait_run(started.id)


async def card_of(run_id: str, node_id: str = "card") -> dict:
    async with SessionLocal() as session:
        event = (await session.execute(sa_select(RunEvent).where(
            RunEvent.run_id == run_id, RunEvent.type == "node.finished",
            RunEvent.node_id == node_id))).scalars().one()
    return artifact_store.load(event.data["artifact"])


def card_config(graph: dict, node_id: str = "card") -> dict:
    return next(n for n in graph["nodes"] if n["id"] == node_id)["data"]["config"]


def with_selection(graph: dict, metrics, *, node_id: str = "card") -> dict:
    card_config(graph, node_id).setdefault("caliber_from", {})["metrics"] = metrics
    return graph


async def upstream_v1(name: str = "K08 上游") -> dict:
    return await S.upstream(name=f"{OWN_PREFIX}{name}")


# ==========================================================================
# 选择的核心：唯一一份解释（和闭包同一处）
# ==========================================================================


def test_effective_metrics_and_index_share_one_interpretation():
    from app.engine.nodes import metrics
    from app.ledger import closure

    defs = [{"id": "a"}, {"id": "b"}, {"id": "c"}]
    assert [m["id"] for m in metrics.effective_metrics(defs, ["c", "a"])] == ["a", "c"]
    assert metrics.selection_index(defs, ["c", "a"]) == [0, 2]
    # 有效数组和下标是同一份解释的两种写法
    assert [defs[i]["id"] for i in metrics.selection_index(defs, ["c", "a"])] == \
        [m["id"] for m in metrics.effective_metrics(defs, ["c", "a"])]
    # 位置与取舍只由 closure.selection_index 算，运行时不再各写一遍
    assert metrics.selection_index(defs, ["c", "a"]) == list(closure.selection_index(defs, ["c", "a"]).positions)
    # 缺省整卡：顺序与条目一个不动
    assert metrics.effective_metrics(defs, None) == defs
    assert metrics.selection_index(defs, None) == [0, 1, 2]


def test_require_selection_is_loud_and_never_falls_back_to_the_whole_card():
    from app.engine.nodes.metrics import SelectionRefused, require_selection

    defs = [{"id": "a"}, {"id": "b"}]
    with pytest.raises(SelectionRefused, match="没有指标"):
        require_selection(defs, ["没有这个"])
    with pytest.raises(SelectionRefused, match="一个指标也没选"):
        require_selection(defs, [])
    for bad in ("a", [1], ["a", ""]):
        with pytest.raises(SelectionRefused):
            require_selection(defs, bad)


# ==========================================================================
# 运行时：只算选中的那几个，未知 / 空的选择响亮失败
# ==========================================================================


async def test_runtime_computes_only_the_selected_metrics():
    up = await upstream_v1()
    row = await run(with_selection(S.chained_graph(up, version=1), ["m2"]))
    assert row.status == "succeeded", row.error
    card = await card_of(row.id)
    assert [m["id"] for m in card["metrics"]] == ["m2"] and card["metrics"][0]["value"] == 900.0
    # 来源照记选择（按源卡顺序），证据据此认得这次算的是哪几个
    assert card["source"]["metrics"] == ["m2"]


async def test_an_unknown_selection_fails_the_node_loudly():
    up = await upstream_v1()
    row = await run(with_selection(S.chained_graph(up, version=1), ["没有这个"]))
    assert row.status == "failed"
    assert "没有指标" in str(row.error) and "没有这个" in str(row.error)
    assert "m1" in str(row.error) and "m2" in str(row.error)      # 说清卡上有哪些


async def test_an_empty_selection_is_refused_and_never_treated_as_the_whole_card():
    """显式空选择：画布校验就拦住（运行时那条兜底见 require_selection 的用例），都不当作整卡。"""
    up = await upstream_v1()
    with pytest.raises(ValueError, match="是空的"):
        await run(with_selection(S.chained_graph(up, version=1), []))


async def test_an_old_graph_without_a_selection_is_unchanged():
    """没写选择：整卡照算，来源里没有这项。"""
    up = await upstream_v1()
    row = await run(S.chained_graph(up, version=1))
    assert row.status == "succeeded", row.error
    card = await card_of(row.id)
    assert [m["id"] for m in card["metrics"]] == ["m1", "m2"]
    assert "metrics" not in card["source"]


async def test_a_shared_query_is_kept_for_the_selected_metric():
    """m1 / m2 共享同一条 SQL：只选 m1 时那条 SQL 照旧执行（不然 m1 取不到数）。"""
    up = await upstream_v1()
    row = await run(with_selection(S.chained_graph(up, version=1), ["m1"]))
    assert row.status == "succeeded", row.error
    card = await card_of(row.id)
    assert [m["id"] for m in card["metrics"]] == ["m1"] and card["metrics"][0]["value"] == 3
    # 取数真跑了：指标输入追到那一条查询快照
    [entry] = card["metrics"][0]["inputs"]
    assert entry["via"] == "tool_cell" and entry["artifact"]


# ==========================================================================
# 位置引用：nodes.<卡>.metrics[i] 的 i 落在有效数组上
# ==========================================================================


async def test_position_references_follow_the_effective_array():
    """卡上 [m1, m2]，只选 m2：有效数组第 0 个是 m2，出口按位置取 [0] 就该拿到 m2 的数。"""
    up = await upstream_v1()
    graph = with_selection(S.chained_graph(up, version=1), ["m2"])
    out = next(n for n in graph["nodes"] if n["id"] == "out")
    out["data"]["config"]["fields"] = [{"name": "销售额", "value": "{{ nodes.card.metrics[0].value }}"}]
    row = await run(graph)
    assert row.status == "succeeded", row.error
    assert float(row.output["销售额"]) == 900.0                 # m2 的数，不是全量定义里第 0 个 m1 的 3
    assert (await card_of(row.id))["metrics"][0]["id"] == "m2"


# ==========================================================================
# 绑定判定：只看过滤后的真实定义；caliber_from.nodes 改写照常
# ==========================================================================


async def test_binding_and_rename_apply_to_the_selected_metrics():
    up = await upstream_v1()
    graph = with_selection(S.chained_graph(up, version=2, local_values=S.NEW, rename={"fetch": "c1_fetch"}), ["m1"])
    row = await run(graph)
    assert row.status == "succeeded", row.error
    card = await card_of(row.id)
    assert [m["id"] for m in card["metrics"]] == ["m1"] and card["metrics"][0]["value"] == 2   # 新定义 2 单
    # 式子里的节点名按 caliber_from.nodes 映射成本地的 c1_fetch
    assert card["metrics"][0]["expression"] == "cell(nodes.c1_fetch, 0, 'orders')"
    assert card["source"]["nodes"] == {"fetch": "c1_fetch"} and card["source"]["metrics"] == ["m1"]


async def test_a_conflicting_sibling_does_not_block_the_selected_metric():
    """m1 说旧口径、m2 说新口径，本地那条 SQL 是旧写法：整卡拦住（说法打架），只选 m1 照常算。"""
    metrics = S.card_metrics()                                  # 两条都声明旧口径
    metrics[1]["time_role"] = "payment_time"
    metrics[1]["refund_rule"] = "exclude_full_refund"           # m2 改成新口径：和 SQL 对不上
    workflow_id = await S.publish(S.graph(values=S.OLD, metrics=metrics), name=f"{OWN_PREFIX}冲突上游")
    pin = {"workflow_id": workflow_id, "card": "card"}

    selected = with_selection(S.chained_graph(pin, version=1, rename={"fetch": "c1_fetch"}), ["m1"])
    row = await run(selected)
    assert row.status == "succeeded", row.error
    card = await card_of(row.id)
    assert [m["id"] for m in card["metrics"]] == ["m1"] and card["metrics"][0]["value"] == 3

    whole = await run(S.chained_graph(pin, version=1, rename={"fetch": "c1_fetch"}))
    assert whole.status == "failed" and "对不上" in str(whole.error)


# ==========================================================================
# 画布校验：选择的形状；双盲复算认得的指标清单
# ==========================================================================


def _errors(graph: dict) -> list[tuple[str | None, str]]:
    return [(i.field, i.message) for i in validate_graph(GraphSpec.model_validate(graph)).issues if i.level == "error"]


def _selection_errors(graph: dict) -> list[str]:
    return [m for f, m in _errors(graph) if (f or "").startswith("caliber_from.metrics")]


def test_schema_validates_the_selection_shape():
    base = S.chained_graph({"workflow_id": "wf-k08", "card": "card"}, version=1)
    with_selection(base, ["m1"])
    assert _selection_errors(base) == []

    for bad in ("m1", ["m1", 2], [""]):
        graph = S.chained_graph({"workflow_id": "wf-k08", "card": "card"}, version=1)
        with_selection(graph, bad)
        assert _selection_errors(graph), bad

    empty = S.chained_graph({"workflow_id": "wf-k08", "card": "card"}, version=1)
    with_selection(empty, [])
    assert any("空" in m for m in _selection_errors(empty))


def test_dual_check_sees_only_the_selected_metrics():
    """转抄卡带选择时，双盲复算认得的指标就是选中的那些：不在选择里的指标应当报错。"""
    def graph_with(selection: list[str]) -> dict:
        made = S.chain(
            S.node("start", "input"),
            S.node("fetch", "tool", tool=S.tool(), args={"sql": "SELECT 1 AS orders, 2 AS amount"}),
            S.node("card", "metrics", caliber_from={"workflow_id": "wf-k08", "workflow_version": 1,
                                                    "node_id": "card", "metrics": selection}),
            S.node("dual", "dual_check", card="card", metrics=["m1"]),
            S.node("out", "output", fields=[{"name": "结果", "value": "{{ nodes.dual.text }}"}]))
        return made

    assert not [m for f, m in _errors(graph_with(["m1"])) if f == "metrics"]
    assert any(f == "metrics" and "不是口径卡" in m for f, m in _errors(graph_with(["m2"])))


# ==========================================================================
# 对账 / 召回：按位置引用时用同一份有效数组
# ==========================================================================


def _pinned(ids: list[str], *, version: int = 1, selection: list[str] | None = None):
    """手工拼一个 Pinned（不进库）：卡上这些指标，可带一份选择的闭包。"""
    from dataclasses import replace

    from app.ledger.derive import Pinned

    metrics = [{"id": i, "name": i, "unit": "", "expression": f"cell(nodes.fetch, 0, '{i}')"} for i in ids]
    pin = Pinned(workflow_id="w", workflow_name="W", version=version, node_id="card", level="published",
                 published=True, retired=False,
                 card={"id": "card", "type": "metrics", "data": {"label": "卡", "config": {"metrics": metrics}}},
                 fetches=(), reads=(), entry_fields=(), defaults={})
    if selection is None:
        return pin

    class _Closure:
        whole_card = False

        def __init__(self) -> None:
            self.metric_ids = tuple(selection)

    return replace(pin, closure=_Closure())


def test_reconcile_and_recall_use_the_effective_array_for_positions():
    from app.engine.nodes.metrics import pinned_effective
    from app.ledger import recall, reconcile

    old = _pinned(["m1", "m2"], selection=["m2"])               # 旧版只算 m2
    new = _pinned(["m1", "m2"], version=2)                      # 新版整卡
    assert [m["id"] for m in pinned_effective(old)] == ["m2"]
    assert [m["id"] for m in pinned_effective(new)] == ["m1", "m2"]
    # 对账也走同一份解释（卡上没有选择时就是全量定义，行为不变）
    assert reconcile.pinned_effective(old) == pinned_effective(old)

    node, meta = recall.recall_card([recall.Pair(old=old, new=new)])
    by_id = {m["id"]: m for m in node["data"]["config"]["metrics"]}
    # 有效数组：旧版 m2 在第 0 个、新版 m2 在第 1 个——位置引用必须落在这一份上
    assert by_id["old_0"]["expression"] == "nodes.old_card.metrics[0].value"
    assert by_id["new_0"]["expression"] == "nodes.new_card.metrics[1].value"
    assert [m["metric"] for m in meta] == ["m2"]


def test_recall_without_a_selection_keeps_whole_card_positions():
    """没给选择：位置照全量定义那份，一字不变。"""
    from app.ledger import recall

    node, meta = recall.recall_card([recall.Pair(old=_pinned(["m1", "m2"]), new=_pinned(["m1", "m2"], version=2))])
    by_id = {m["id"]: m for m in node["data"]["config"]["metrics"]}
    assert by_id["old_1"]["expression"] == "nodes.old_card.metrics[1].value"
    assert by_id["new_1"]["expression"] == "nodes.new_card.metrics[1].value"
    assert [m["metric"] for m in meta] == ["m1", "m2"]
