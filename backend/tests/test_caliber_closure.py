"""K08 的依赖闭包解析器（app/ledger/closure.py）：S1 的解析、S2 的选择位置、S4 的摘要。

这一份**只按纯函数用**：不建库、不起引擎、不发模型请求（接线在 ledger/derive 与 ledger/compose——按选择裁要执行的取数
与入口字段；运行时按口径卡上的 caliber_from.metrics 读 effective_metrics）。合成 A/B 两张卡的形状（两指标两张表的口径、
指标类型）都在这里现搭，复用 tests/binding_samples 的受控模板与绑定写法。

覆盖任务书要的四类验收（§5 K08：A/B 独立、共享查询、全局约束、未知依赖），外加这里必须锁住的身份与确定性：

- 选 A 时 B 独有且无依赖的查询与参数不进这次范围；共享的那条 SQL 保留，并显式声明它还在算未选中的 B
  （computes_unselected），不做投影；
- 共享参数、卡 on_missing、图 defaults 照样带上；
- 未知指标 / 式子读不出来 / 引用不存在的节点 / 参数没声明 / 绑定对不上 → unresolved，require() 抛错，绝不静默退回整卡；
- 缺省整卡、显式整卡、显式空选择（不当整卡）、显式与整卡冲突（不放宽）；
- 确定性、metric_ids 重排不影响结果；
- 两条 SQL 内容一样的节点不合并（不拿内容哈希当身份）；
- query_id 跨版本稳定、改名会变、显式 query_id 改名也稳定；别名不是源身份（source 记真实节点与版本）；
- 绑定变化只动 closure_hash：selection_hash 不变（绑定指纹不进它）。
"""
from __future__ import annotations

import copy

import pytest

from app.data.caliber_identity import card_hash_of
from app.ledger import bindings, closure
from tests import binding_samples as S

#: 入口字段：start / end 是两张表都要的共享参数，store 只有 B 那张表要
FIELDS = [{"name": "start", "type": "string", "required": True, "description": "开始日期（含）"},
          {"name": "end", "type": "string", "required": True, "description": "结束日期（不含）"},
          {"name": "store", "type": "string", "required": False, "description": "门店（B 那条查询要）"}]

#: B 那张表比 A 多一个门店过滤（params 槽位：store 只有它用）
TEMPLATE_B = S.TEMPLATE + " AND o.store = '{{ input.store }}'"


def fetch(node_id: str, template: str = S.TEMPLATE, values: dict[str, str] | None = None, *,
          query_id: str | None = None, binding: bool = True) -> dict:
    """一个取数节点：受控模板按当前业务值展开成 args.sql，并写一份对得上的绑定。"""
    values = dict(values or S.OLD)
    sql = bindings.render(template, S.SNIPPETS, values)
    config: dict = {"tool": S.tool(), "args": {"sql": sql}}
    if binding:
        config[bindings.KEY] = S.binding(values, template=template, sql=sql)
    if query_id:
        config["query_id"] = query_id
    return S.node(node_id, "tool", **config)


def metric(metric_id: str, node_id: str, *, column: str = "orders", time_role: str | None = S.OLD["time_role"],
           refund_rule: str | None = S.OLD["refund_rule"], **extra) -> dict:
    made: dict = {"id": metric_id, "name": metric_id, "unit": "单",
                  "expression": f"cell(nodes.{node_id}, 0, '{column}')"}
    if time_role:
        made["time_role"] = time_role
    if refund_rule:
        made["refund_rule"] = refund_rule
    made.update(extra)
    return made


def graph(*, metrics: list[dict], fetches: list[dict], fields: list[dict] | None = None,
          card_config: dict | None = None, defaults: dict | None = None) -> dict:
    """入口 → 各条取数 → 口径卡 → 出口；defaults 给了就是图级 defaults。"""
    made = S.chain(S.node("start", "input", fields=copy.deepcopy(fields if fields is not None else FIELDS)),
                   *copy.deepcopy(fetches),
                   S.node("card", "metrics", caliber="订单口径", caliber_version="v1",
                          metrics=copy.deepcopy(metrics), **copy.deepcopy(card_config or {})),
                   S.node("out", "output", fields=[{"name": "结果", "value": "{{ nodes.card.text }}"}]))
    if defaults:
        made["defaults"] = copy.deepcopy(defaults)
    return made


def _metrics_for(values: dict[str, str], pairs: list[tuple[str, str]]) -> list[dict]:
    """按当前业务值给指标声明（定义和那份 SQL 自洽）：换个 values 就是自洽的新一版。"""
    return [metric(metric_id, node_id, time_role=values.get("time_role"),
                   refund_rule=values.get("refund_rule")) for metric_id, node_id in pairs]


def graph_ab(*, values: dict[str, str] | None = None, card_config: dict | None = None,
             defaults: dict | None = None) -> dict:
    """A 读 q_a、B 读 q_b：两条独立查询；store 只有 B 用。默认两张表都和定义同步。"""
    values = dict(values or S.OLD)
    return graph(metrics=_metrics_for(values, [("a", "q_a"), ("b", "q_b")]),
                 fetches=[fetch("q_a", S.TEMPLATE, values), fetch("q_b", TEMPLATE_B, values)],
                 card_config=card_config, defaults=defaults)


def graph_shared(*, values: dict[str, str] | None = None, card_config: dict | None = None,
                 defaults: dict | None = None) -> dict:
    """A 和 B 读同一条取数（共享 SQL）：一条 SQL 上两个指标。"""
    values = dict(values or S.OLD)
    return graph(metrics=_metrics_for(values, [("a", "q_shared"), ("b", "q_shared")]),
                 fetches=[fetch("q_shared", S.TEMPLATE, values)],
                 card_config=card_config, defaults=defaults)


def codes(problems) -> list[str]:
    return [p["code"] for p in problems]


# ==========================================================================
# A/B 独立：选 A 不带 B 的查询和参数
# ==========================================================================


def test_independent_selection_drops_the_other_metrics_query_and_params():
    """A/B 独立：选 A 只要 q_a，q_b 排除；store 只有 B 用，不算这次要的参数。"""
    made = graph_ab()
    whole = closure.build(made, "wf-ab", 1, "card").require()
    assert whole.mode == closure.WHOLE_CARD and whole.whole_card
    assert whole.card_metric_ids == ("a", "b") and whole.metric_ids == ("a", "b")
    assert whole.query_ids == ("wf-ab:q_a", "wf-ab:q_b") and whole.excluded_query_ids == ()
    assert [f["name"] for f in whole.required_entry_fields] == ["start", "end", "store"]

    a = closure.build(made, "wf-ab", 1, "card", metric_ids=["a"]).require()
    assert a.mode == closure.EXPLICIT and not a.whole_card
    assert a.card_metric_ids == ("a", "b") and a.metric_ids == ("a",)
    assert a.query_ids == ("wf-ab:q_a",) and a.excluded_query_ids == ("wf-ab:q_b",)
    assert [f["name"] for f in a.required_entry_fields] == ["start", "end"]     # store 被排除
    assert a.entry_fields and [f["name"] for f in a.entry_fields] == ["start", "end", "store"]
    assert a.metric_query_ids("a") == ["wf-ab:q_a"] and a.metric_query_ids("b") == []
    assert [q.computes_unselected for q in a.queries] == [()]                    # q_a 只算 A
    assert a.binding_fingerprints and set(a.binding_fingerprints) == {"wf-ab:q_a"}

    b = closure.build(made, "wf-ab", 1, "card", metric_ids=["b"]).require()
    assert b.query_ids == ("wf-ab:q_b",) and b.excluded_query_ids == ("wf-ab:q_a",)
    assert [f["name"] for f in b.required_entry_fields] == ["start", "end", "store"]


def test_selection_is_normalized_by_source_card_order():
    """显式选择按源卡顺序归一：调用方传的顺序和重复项都不影响结果。"""
    made = graph_ab()
    forward = closure.build(made, "wf-ab", 1, "card", metric_ids=["a", "b"]).require()
    reverse = closure.build(made, "wf-ab", 1, "card", metric_ids=["b", "a", "b"]).require()
    assert forward.metric_ids == reverse.metric_ids == ("a", "b")
    assert forward.query_ids == reverse.query_ids
    assert forward.selection_hash == reverse.selection_hash
    assert forward.closure_hash == reverse.closure_hash
    assert forward.as_dict() == reverse.as_dict()


# ==========================================================================
# 共享查询：保留了就要声明它还在算谁
# ==========================================================================


def test_a_shared_query_is_kept_and_declares_what_it_still_computes():
    """共享 SQL 选 A 也保留，并显式声明它还在算未选中的 B；共享参数一并保留（不做投影）。"""
    made = graph_shared()
    a = closure.build(made, "wf-shared", 1, "card", metric_ids=["a"]).require()
    assert a.metric_ids == ("a",) and a.query_ids == ("wf-shared:q_shared",) and a.excluded_query_ids == ()
    shared = a.queries[0]
    assert shared.metric_ids == ("a",) and shared.computes_unselected == ("b",)
    # 共享参数照样要（这条 SQL 用 start/end 渲染）：共享约束不能被裁掉
    assert [f["name"] for f in a.required_entry_fields] == ["start", "end"]
    assert a.binding_fingerprints             # 共享节点上的绑定指纹还在

    whole = closure.build(made, "wf-shared", 1, "card").require()
    assert [q.computes_unselected for q in whole.queries] == [()]              # 整卡模式下没有「未选中」


def test_two_nodes_with_the_same_sql_stay_two_queries():
    """不拿内容哈希当身份：两条 SQL 一字不差，也是两条查询（不合并、各有各的 query_id）。"""
    made = graph(metrics=[metric("a", "q_a"), metric("b", "q_b")],
                 fetches=[fetch("q_a", S.TEMPLATE), fetch("q_b", S.TEMPLATE)])
    assert made["nodes"][1]["data"]["config"]["args"]["sql"] == made["nodes"][2]["data"]["config"]["args"]["sql"]
    got = closure.build(made, "wf-same", 1, "card").require()
    assert got.query_ids == ("wf-same:q_a", "wf-same:q_b")
    assert [q.sql_hash for q in got.queries][0] == [q.sql_hash for q in got.queries][1]
    assert [q.node_id for q in got.queries] == ["q_a", "q_b"]


# ==========================================================================
# 全局约束：图 defaults、卡 on_missing
# ==========================================================================


def test_global_constraints_travel_with_the_selection():
    """选一小部分指标也要带上图 defaults 与卡的「缺输入时」——那是全局必要约束，不被裁剪。"""
    made = graph_ab(card_config={"on_missing": "null"}, defaults={"tool": S.tool(), "args": {"limit": 500}})
    a = closure.build(made, "wf-cfg", 1, "card", metric_ids=["a"]).require()
    assert a.on_missing == "null"
    assert a.defaults == {"tool": S.tool(), "args": {"limit": 500}}
    assert a.as_dict()["on_missing"] == "null" and a.as_dict()["defaults"]["args"]["limit"] == 500


# ==========================================================================
# 零查询：external 指标没有取数，但不是未决项
# ==========================================================================


def test_an_external_metric_needs_no_query_and_is_not_unresolved():
    """external 型指标本来就不查库：进 metric_ids，query_ids 为空，unresolved 为空。"""
    made = graph(metrics=[metric("a", "q_a"),
                          {"id": "e", "name": "e", "kind": "external",
                           "external": {"value": 1.5, "source": "监管口径", "as_of": "2026-10-01"}}],
                 fetches=[fetch("q_a", S.TEMPLATE)])
    whole = closure.build(made, "wf-ext", 1, "card").require()
    assert whole.metric_ids == ("a", "e")
    assert whole.query_ids == ("wf-ext:q_a",)
    assert whole.queries[0].metric_ids == ("a",)
    assert [q.computes_unselected for q in whole.queries] == [()]
    assert whole.unresolved == ()

    only_external = graph(metrics=[{"id": "e", "name": "e", "kind": "external",
                                    "external": {"value": 1, "source": "监管口径", "as_of": "2026-10-01"}}],
                          fetches=[fetch("q_a", S.TEMPLATE)])
    empty = closure.build(only_external, "wf-ext", 1, "card", metric_ids=["e"]).require()
    assert empty.metric_ids == ("e",) and empty.query_ids == () and empty.excluded_query_ids == ()


# ==========================================================================
# 未知依赖：进 unresolved，require() 抛错，绝不静默整卡
# ==========================================================================


def test_an_unknown_metric_is_unresolved_and_never_falls_back_to_the_whole_card():
    made = graph_ab()
    got = closure.build(made, "wf-unknown", 1, "card", metric_ids=["a", "没有这个"])
    assert [p["code"] for p in got.unresolved] == [closure.METRIC_UNKNOWN]
    assert got.unresolved[0]["metric_id"] == "没有这个"
    assert got.metric_ids == ("a",)                       # 只按点名的那一个走，不放大成整卡
    assert got.whole_card is False
    with pytest.raises(closure.UnresolvedClosure) as raised:
        got.require()
    assert raised.value.problems == got.unresolved


def test_an_unknown_node_reference_is_unresolved():
    made = graph(metrics=[metric("a", "ghost")], fetches=[fetch("q_a", S.TEMPLATE)])
    got = closure.build(made, "wf-ghost", 1, "card")
    assert codes(got.unresolved) == [closure.UNKNOWN_NODE]
    assert got.unresolved[0]["node_id"] == "ghost"
    assert got.query_ids == ()
    with pytest.raises(closure.UnresolvedClosure):
        got.require()


def test_a_metric_with_an_unreadable_definition_is_unresolved():
    """kind 不认识（或该类型下式子是空的）：解析不了依赖，直接未决，不猜。"""
    made = graph(metrics=[metric("a", "q_a", kind="nope")], fetches=[fetch("q_a", S.TEMPLATE)])
    got = closure.build(made, "wf-bad", 1, "card")
    assert codes(got.unresolved) == [closure.EXPRESSION_UNREADABLE]
    assert got.unresolved[0]["metric_id"] == "a" and got.query_ids == ()


def test_an_undeclared_parameter_is_unresolved():
    """SQL 引用了入口没声明的参数：未决，不按空串发出去（K01 的同一套判定）。"""
    made = graph(metrics=[metric("a", "q_a")],
                 fetches=[fetch("q_a", S.TEMPLATE + " AND o.region = '{{ input.region }}'")])
    got = closure.build(made, "wf-param", 1, "card")
    assert closure.UNDECLARED_PARAMETER in codes(got.unresolved)
    assert [f["name"] for f in got.required_entry_fields] == ["start", "end"]


def test_an_unaligned_binding_is_unresolved_but_only_for_the_selected_metrics():
    """K04 的定义 vs 绑定对不上：整卡未决；只选 A 时，B 那条对不上的查询不牵连 A。"""
    made = graph(metrics=[metric("a", "q_a"),
                          # B 的定义已经改成支付时间，q_b 那条 SQL 还停在按下单时间：对不上
                          metric("b", "q_b", time_role="payment_time", refund_rule="exclude_full_refund")],
                 fetches=[fetch("q_a", S.TEMPLATE), fetch("q_b", TEMPLATE_B)])
    whole = closure.build(made, "wf-align", 1, "card")
    assert codes(whole.unresolved) == [closure.BINDING_UNRESOLVED]
    assert whole.unresolved[0]["node_id"] == "q_b"
    with pytest.raises(closure.UnresolvedClosure):
        whole.require()

    a = closure.build(made, "wf-align", 1, "card", metric_ids=["a"]).require()
    assert a.unresolved == () and a.query_ids == ("wf-align:q_a",) and a.excluded_query_ids == ("wf-align:q_b",)


def test_a_conflict_on_a_shared_query_is_only_reported_when_the_other_metric_is_selected():
    """同一条共享 SQL 上两个指标说法打架：整卡未决；只选 A（且 A 的说法就是 SQL 现在的说法）时不牵连 A。

    这是有意的分寸：共享 SQL 只是「还算了 B」——如实写进 computes_unselected，B 的数不交回，A 的结论不受 B 的
    说法牵连（反例「B 独有错误导致 A 失败」）。"""
    made = graph(metrics=[metric("a", "q_shared"),
                          metric("b", "q_shared", time_role="payment_time",
                                 refund_rule="exclude_full_refund")],
                 fetches=[fetch("q_shared", S.TEMPLATE)])
    whole = closure.build(made, "wf-conflict", 1, "card")
    assert closure.BINDING_UNRESOLVED in codes(whole.unresolved)

    a = closure.build(made, "wf-conflict", 1, "card", metric_ids=["a"]).require()
    assert a.queries[0].computes_unselected == ("b",)
    assert a.binding_fingerprints and set(a.binding_fingerprints) == {"wf-conflict:q_shared"}


def test_a_chained_card_cannot_be_resolved_without_the_pinned_definitions():
    """转抄别处的卡（caliber_from）本图里没有定义：照实说未决，不假装解析出依赖。"""
    made = graph_ab()
    card = next(n for n in made["nodes"] if n["id"] == "card")
    card["data"]["config"].pop("metrics")
    card["data"]["config"]["caliber_from"] = {"workflow_id": "up", "workflow_version": 2, "node_id": "card"}
    got = closure.build(made, "wf-chain", 1, "card")
    assert codes(got.unresolved) == [closure.CARD_CHAINED]
    assert got.card_metric_ids == () and got.query_ids == ()


# ==========================================================================
# 整卡与空选择
# ==========================================================================


def test_explicit_whole_card_and_an_empty_selection_are_different_things():
    made = graph_ab()
    whole = closure.build(made, "wf-mode", 1, "card", whole_card=True).require()
    assert whole.mode == closure.WHOLE_CARD and whole.whole_card and whole.metric_ids == ("a", "b")
    assert whole.excluded_query_ids == ()

    empty = closure.build(made, "wf-mode", 1, "card", metric_ids=[])
    assert codes(empty.unresolved) == [closure.EMPTY_SELECTION]
    assert empty.metric_ids == () and empty.whole_card is False      # 空选择不当整卡
    with pytest.raises(closure.UnresolvedClosure):
        empty.require()


def test_asking_for_both_is_reported_and_does_not_widen_anything():
    made = graph_ab()
    both = closure.build(made, "wf-both", 1, "card", metric_ids=["a"], whole_card=True)
    assert closure.CONFLICTING_SELECTION in codes(both.unresolved)
    assert both.metric_ids == ("a",) and both.whole_card is False     # 冲突时按更窄的一边
    assert both.excluded_query_ids == ("wf-both:q_b",)
    with pytest.raises(closure.UnresolvedClosure):
        both.require()


def test_an_empty_card_in_whole_card_mode_is_unresolved():
    made = graph(metrics=[], fetches=[fetch("q_a", S.TEMPLATE)])
    got = closure.build(made, "wf-empty", 1, "card")
    assert codes(got.unresolved) == [closure.CARD_EMPTY] and got.query_ids == ()


# ==========================================================================
# 身份：query_id、source、整卡指纹
# ==========================================================================


def test_query_id_is_stable_across_versions_and_changes_on_rename():
    """query_id 是逻辑身份：不随版本变；节点改名会变；显式 query_id 改名也稳定；别名不是源身份。"""
    made = graph_ab()
    v1 = closure.build(made, "wf-id", 1, "card").require()
    v9 = closure.build(made, "wf-id", 9, "card").require()
    assert v1.query_ids == v9.query_ids == ("wf-id:q_a", "wf-id:q_b")
    assert v1.source == {"workflow_id": "wf-id", "version": 1, "card_id": "card"}
    assert v9.source["version"] == 9 and v9.selection_hash != v1.selection_hash

    renamed = graph_ab()
    next(n for n in renamed["nodes"] if n["id"] == "q_a")["id"] = "q_a_renamed"
    next(n for n in renamed["nodes"] if n["id"] == "card")["data"]["config"]["metrics"][0]["expression"] = \
        "cell(nodes.q_a_renamed, 0, 'orders')"
    after = closure.build(renamed, "wf-id", 1, "card").require()
    assert after.query_ids == ("wf-id:q_a_renamed", "wf-id:q_b")

    # 节点上显式声明 query_id（别名）：改名之后逻辑身份不变，但 source 记的是真实节点
    aliased = graph_ab()
    next(n for n in aliased["nodes"] if n["id"] == "q_a")["data"]["config"]["query_id"] = "orders_total"
    one = closure.build(aliased, "wf-id", 3, "card", metric_ids=["a"]).require()
    assert one.query_ids == ("orders_total",)
    assert one.queries[0].source == {"workflow_id": "wf-id", "version": 3, "node_id": "q_a"}
    assert one.queries[0].node_id == "q_a" and one.queries[0].query_id == "orders_total"
    assert closure.query_id_of("wf-id", "q_a", {"query_id": "orders_total"}) == "orders_total"
    assert closure.query_id_of("wf-id", "q_a", {}) == "wf-id:q_a"


def test_two_queries_with_the_same_logical_identity_are_unresolved():
    made = graph(metrics=[metric("a", "q_a")],
                 fetches=[fetch("q_a", S.TEMPLATE, query_id="orders"),
                          fetch("q_b", TEMPLATE_B, query_id="orders")])
    got = closure.build(made, "wf-dup", 1, "card", metric_ids=["a"])
    assert closure.DUPLICATE_QUERY_ID not in codes(got.unresolved)   # q_b 没被选中，不进这次范围
    both = graph(metrics=[metric("a", "q_a"), metric("b", "q_b")],
                 fetches=[fetch("q_a", S.TEMPLATE, query_id="orders"),
                          fetch("q_b", TEMPLATE_B, query_id="orders")])
    dup = closure.build(both, "wf-dup", 1, "card")
    assert codes(dup.unresolved) == [closure.DUPLICATE_QUERY_ID]
    assert dup.unresolved[0]["query_id"] == "orders"


def test_the_source_card_hash_covers_the_whole_card_not_the_selection():
    """整卡身份（source_card_hash）和本次选择身份（selection_hash）都可追踪：选一半，整卡指纹不变。"""
    made = graph_ab()
    whole = closure.build(made, "wf-hash", 1, "card").require()
    part = closure.build(made, "wf-hash", 1, "card", metric_ids=["a"]).require()
    card = next(n for n in made["nodes"] if n["id"] == "card")["data"]["config"]
    expected = card_hash_of({"metrics": card["metrics"], "caliber": card["caliber"],
                             "caliber_version": card["caliber_version"]})
    assert whole.source_card_hash == part.source_card_hash == expected
    assert whole.selection_hash != part.selection_hash and whole.closure_hash != part.closure_hash


# ==========================================================================
# 跨指标契约：照实列出来，选一半时不悄悄丢掉
# ==========================================================================


def test_cross_metric_contracts_are_listed():
    """选中指标声明的比例契约、图上出具契约点名的指标，都在 contract_dependencies 里如实列出。"""
    made = graph_ab(card_config={"on_missing": "null"})
    card = next(n for n in made["nodes"] if n["id"] == "card")
    card["data"]["config"]["metrics"][0]["contract"] = {
        "shape": "scalar", "ratio": {"numerator": "orders", "denominator": "total"}}
    out = next(n for n in made["nodes"] if n["id"] == "out")
    out["data"]["config"]["contract"] = {"metrics_from": "card", "required": ["a", "b"]}

    a = closure.build(made, "wf-contract", 1, "card", metric_ids=["a"]).require()
    assert a.contract_dependencies == (
        {"kind": "ratio", "metric_id": "a", "numerator": "orders", "denominator": "total"},
        {"kind": "report_contract", "node_id": "out", "metric_ids": ["a", "b"], "unselected": ["b"]},
    )
    whole = closure.build(made, "wf-contract", 1, "card").require()
    assert whole.contract_dependencies[1]["unselected"] == []        # 整卡模式下没有「没选中的」


# ==========================================================================
# 确定性与绑定变化
# ==========================================================================


def test_the_same_input_always_gives_the_same_closure():
    made = graph_ab(card_config={"on_missing": "null"}, defaults={"x": 1})
    first = closure.build(made, "wf-det", 2, "card", metric_ids=["a", "b"])
    second = closure.build(copy.deepcopy(made), "wf-det", 2, "card", metric_ids=["b", "a"])
    assert first.as_dict() == second.as_dict()
    assert first.selection_hash == second.selection_hash and first.closure_hash == second.closure_hash


def test_a_binding_change_moves_closure_hash_but_not_selection_hash():
    """同一份选择、同一批查询，绑定（业务值）从旧定义同步到新定义：selection_hash 不变，closure_hash 变。"""
    old = closure.build(graph_shared(values=S.OLD), "wf-bind", 2, "card", metric_ids=["a"]).require()
    new = closure.build(graph_shared(values=S.NEW), "wf-bind", 2, "card", metric_ids=["a"]).require()
    assert old.metric_ids == new.metric_ids == ("a",)
    assert old.query_ids == new.query_ids == ("wf-bind:q_shared",)
    assert old.binding_fingerprints != new.binding_fingerprints
    assert old.selection_hash == new.selection_hash            # 绑定指纹不进 selection_hash
    assert old.closure_hash != new.closure_hash                # 但它进 closure_hash
    assert old.source_card_hash != new.source_card_hash


# ==========================================================================
# 输入本身不可用：抛 ClosureRefused（不是 unresolved）
# ==========================================================================


@pytest.mark.parametrize("call", [
    lambda: closure.build(None, "wf", 1, "card"),
    lambda: closure.build(graph_ab(), "wf", 1, "没有这张卡"),
    lambda: closure.build(graph_ab(), "wf", "不是版本", "card"),
    lambda: closure.build(graph_ab(), "wf", 1, "out"),               # 不是口径卡
    lambda: closure.build(graph_ab(), "wf", 1, "card", metric_ids="a"),
])
def test_bad_inputs_are_refused(call):
    with pytest.raises(closure.ClosureRefused):
        call()


def test_a_duplicated_metric_id_is_unresolved_and_never_resolves_to_one_of_them():
    """同一张卡上两个指标用同一个 id、各读一条查询：整卡与点名都未决（duplicate_metric_id）、require 失败。

    不能出现「闭包取最后那一份、运行时取第一份」：重复的 id 说不清算的是哪一个，所以哪种模式都照实报（K08 复核）。"""
    made = graph(metrics=[metric("a", "q_a"), metric("a", "q_b")],
                 fetches=[fetch("q_a", S.TEMPLATE), fetch("q_b", TEMPLATE_B)])
    whole = closure.build(made, "wf-dup-metric", 1, "card")
    assert codes(whole.unresolved) == [closure.DUPLICATE_METRIC_ID]
    assert whole.unresolved[0]["metric_id"] == "a" and "重复" in whole.unresolved[0]["message"]
    with pytest.raises(closure.UnresolvedClosure):
        whole.require()
    # 点名这个 id 也说不清算的是哪一份：照样未决（不静默挑一个）
    one = closure.build(made, "wf-dup-metric", 1, "card", metric_ids=["a"])
    assert codes(one.unresolved) == [closure.DUPLICATE_METRIC_ID]
    with pytest.raises(closure.UnresolvedClosure):
        one.require()
    # 卡上另一个（不重的）id 也照实拒：这是卡的身份问题，不是某个指标的问题
    other = graph(metrics=[metric("a", "q_a"), metric("a", "q_b"), metric("b", "q_b")],
                  fetches=[fetch("q_a", S.TEMPLATE), fetch("q_b", TEMPLATE_B)])
    assert codes(closure.build(other, "wf-dup-metric", 1, "card", metric_ids=["b"]).unresolved) \
        == [closure.DUPLICATE_METRIC_ID]
    # 判定只有这一处：空 id 不算重复，重复的按第一次出现的先后只报一次
    assert closure.duplicate_metric_ids([metric("a", "q_a"), metric("a", "q_b"), {"name": "没有 id"}, metric("a", "q_c"),
                                         metric("b", "q_c")]) == ["a"]


# ==========================================================================
# 这次算哪些指标：选择在源卡里的位置（运行时统一的那一份）
# ==========================================================================


def test_effective_metrics_keeps_the_source_card_order_and_reports_unknowns():
    """按卡上记的选择筛定义：保持源卡相对顺序，调用方传的顺序与重复项都不影响；认不出的 id 照实交回。"""
    definitions = [metric("a", "q_a"), metric("b", "q_b"), metric("c", "q_c")]
    index = closure.selection_index(definitions, ["c", "a", "c", "没有这个"])
    assert [d["id"] for d in index.definitions] == ["a", "c"]
    assert index.positions == (0, 2) and index.metric_ids == ("a", "c")
    assert index.unknown == ("没有这个",) and index.whole_card is False
    assert index.as_dict() == {"metric_ids": ["a", "c"], "positions": [0, 2], "whole_card": False,
                               "unknown": ["没有这个"]}
    assert [d["id"] for d in closure.effective_metrics(definitions, ["c", "a"])] == ["a", "c"]
    assert closure.effective_metrics(definitions, ["c", "a"]) == closure.effective_metrics(definitions, ["a", "c"])
    # 交回的是源卡里的那几条定义本身（不是拷贝）：运行时拿它算，别的键一个不动
    assert closure.effective_metrics(definitions, ["c", "a"]) == [definitions[0], definitions[2]]


def test_no_selection_is_the_whole_card_and_an_empty_one_selects_nothing():
    """不给选择就是整卡（顺序与条目一个不动）；显式空选择是一个也不选，不静默当整卡。"""
    definitions = [metric("a", "q_a"), {"name": "没有 id 的一条"}]
    whole = closure.selection_index(definitions)
    assert whole.whole_card and whole.positions == (0, 1) and whole.unknown == ()
    assert closure.effective_metrics(definitions) == definitions
    assert closure.effective_metrics(definitions, ["a"]) == [definitions[0]]
    empty = closure.selection_index(definitions, [])
    assert empty.definitions == () and empty.positions == () and empty.whole_card is False
    assert empty.unknown == () and closure.effective_metrics(definitions, []) == []


def test_a_repeated_metric_id_in_the_card_is_taken_once():
    """同一张卡上重复的 id 只算第一次那个（口径卡的 id 该唯一；这一层不替它去重之后算两遍）。"""
    first, second = metric("a", "q_a"), metric("a", "q_b")
    index = closure.selection_index([first, second, metric("b", "q_c")], ["a", "b"])
    assert [d["id"] for d in index.definitions] == ["a", "b"] and index.positions == (0, 2)
    assert index.definitions[0] is first


# ==========================================================================
# 闭包摘要（S4）：身份、查询、字段、指纹各一份，但没有 SQL
# ==========================================================================


def test_the_summary_carries_the_identity_without_any_sql():
    """闭包摘要（K08 S4）给目录、选用选项与口径描述读：identity 与依赖全在，**没有 SQL 正文、没有参数值**。"""
    import json

    got = closure.build(graph_ab(), "wf-summary", 3, "card", metric_ids=["a"]).require()
    summary = got.summary()
    assert summary["mode"] == closure.EXPLICIT and summary["whole_card"] is False
    assert summary["metric_ids"] == ["a"] and summary["card_metric_ids"] == ["a", "b"]
    assert summary["query_ids"] == ["wf-summary:q_a"] and summary["excluded_query_ids"] == ["wf-summary:q_b"]
    assert summary["required_entry_fields"] == ["start", "end"]
    [query] = summary["queries"]
    assert query["query_id"] == "wf-summary:q_a" and query["node_id"] == "q_a"
    assert query["metric_ids"] == ["a"] and query["computes_unselected"] == []
    assert query["required_entry_fields"] == ["start", "end"] and query["sql_hash"] == got.queries[0].sql_hash
    assert summary["binding_fingerprints"] == dict(got.binding_fingerprints) and got.binding_fingerprints
    assert summary["selection_hash"] == got.selection_hash and summary["closure_hash"] == got.closure_hash
    assert summary["source_card_hash"] == got.source_card_hash and summary["unresolved"] == []
    assert summary["resolver_version"] == closure.RESOLVER_VERSION
    # 整卡那一份也照样给（不点名：本次默认那份）
    whole = closure.build(graph_ab(), "wf-summary", 3, "card").require().summary()
    assert whole["whole_card"] is True and whole["query_ids"] == ["wf-summary:q_a", "wf-summary:q_b"]
    # 摘要里没有 SQL 正文与参数值：条目是白的（从模板里挑几个一定只在 SQL 里出现的字）
    blob = json.dumps([summary, whole], ensure_ascii=False)
    bad = [n for n in ("SUM(", "FROM", "input.start", "{{", '"sql"', "refund_state") if n in blob]
    assert bad == [], blob
    # 交回的是新的一份，不把闭包内部的对象漏出去
    assert summary["queries"][0] is not got.queries[0]
    assert summary["queries"][0] is not whole["queries"][0]
    assert got.queries[0].sql_hash and "SUM(" in graph_ab()["nodes"][1]["data"]["config"]["args"]["sql"]
