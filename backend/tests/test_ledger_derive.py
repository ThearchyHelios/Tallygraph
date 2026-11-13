"""从钉住版本派生只读图（app/ledger/derive.py；总方案 X3：评测的口径题、账本的对账与召回共用这一个生成器）。

- load_pinned：只认可复用的源头口径卡；工作流、版本、节点不在、不是口径卡、转抄别处、输入不是查库的格、取数节点引用了
  入口字段以外的值，照实拒收（NotDerivable，带机器码）。
- caliber_probe：入口字段照抄、取数节点 id 和配置原样、口径卡 caliber_from 固定到那一版、「缺输入时」照抄；图能通过
  校验；同样的输入永远得到同一张图。
- 扩展点：带前缀的段改名并写出 caliber_from.nodes 映射；两份口径不带前缀放进一张图会撞 id、直接报错。

工作流、库、表都是虚构的。
"""
from __future__ import annotations

import pytest

from app.db.base import SessionLocal
from app.db.models import Workflow, WorkflowVersion
from app.engine.schema import GraphSpec, validate_graph
from app.ledger import bindings, closure, derive


def node(nid, ntype, label=None, **config):
    return {"id": nid, "type": ntype, "position": {"x": 7, "y": 9}, "data": {"label": label or nid, "config": config}}


def chain(*nodes, defaults=None):
    graph = {"nodes": list(nodes), "edges": [{"source": a["id"], "target": b["id"]} for a, b in zip(nodes, nodes[1:])]}
    if defaults:
        graph["defaults"] = defaults
    return graph


FIELDS = [{"name": "start", "required": True, "type": "date"}, {"name": "region", "default": "华东"}]
FETCH_SQL = ("SELECT SUM(amount) AS gmv, COUNT(*) AS n FROM orders WHERE paid_date >= '{{ input.start }}' "
             "AND region = '{{ input.region | trim }}'")


def weekly(fetch_sql: str = FETCH_SQL, **card) -> dict:
    return chain(node("start", "input", fields=FIELDS),
                 node("fetch", "tool", "取数", tool="db_query__shop_x", args={"sql": fetch_sql}, assign_to="rows"),
                 node("card", "metrics", "周销售口径卡", caliber="周销售", caliber_version="v2", on_missing="null",
                      metrics=[{"id": "gmv", "name": "销售额", "expression": "cell(nodes.fetch, 0, 'gmv')"},
                               {"id": "avg", "expression": "cell(nodes.fetch, 0, 'gmv') / cell(nodes.fetch, 0, 'n')"}],
                      **card),
                 node("write", "report", instructions="写周报"),
                 node("out", "output"), defaults={"approval": "dangerous"})


async def publish(graph: dict, *, level: str | None = "published") -> str:
    async with SessionLocal() as session:
        wf = Workflow(name=f"派生_{id(graph)}", graph=graph, status="published", published_version=1, version=1)
        session.add(wf)
        await session.flush()
        session.add(WorkflowVersion(workflow_id=wf.id, version=1, graph=graph, level=level))
        await session.commit()
        return wf.id


async def pinned_of(graph: dict, **kw) -> derive.Pinned:
    wf_id = await publish(graph, **kw)
    async with SessionLocal() as session:
        return await derive.load_pinned(session, wf_id, 1, "card")


async def test_the_probe_copies_the_fetch_and_pins_the_card():
    pinned = await pinned_of(weekly())
    assert pinned.published and not pinned.retired and pinned.reads == (("fetch", "db_query__shop_x", ("gmv", "n")),)
    assert [m["id"] for m in pinned.metrics] == ["gmv", "avg"] and pinned.caliber == "周销售 @ v2"
    probe = derive.caliber_probe(pinned)
    nodes = {n["id"]: n for n in probe.graph["nodes"]}
    assert [n["id"] for n in probe.graph["nodes"]] == [derive.ENTRY_ID, "fetch", "card", derive.EXIT_ID]
    # 入口字段照抄（必填、缺省值、类型都不变）
    assert nodes[derive.ENTRY_ID]["data"]["config"]["fields"] == FIELDS
    # 取数节点 id、配置原样
    original = weekly()["nodes"][1]
    assert nodes["fetch"]["data"] == original["data"] and nodes["fetch"]["type"] == "tool"
    # 口径卡固定到那一版那张卡；「缺输入时」照抄；本地不写指标式
    card = nodes["card"]["data"]["config"]
    assert card == {"caliber_from": {"workflow_id": pinned.workflow_id, "workflow_version": 1, "node_id": "card"},
                    "on_missing": "null"}
    # 图级 defaults 照抄：节点上没写的项按同样的规则取值
    assert probe.graph["defaults"] == {"approval": "dangerous"}
    assert nodes[derive.EXIT_ID]["data"]["config"]["fields"] == [{"name": "周销售口径卡",
                                                                    "value": "{{ nodes.card.text }}"}]
    assert probe.card_ids == ("card",) and probe.origin["kind"] == "caliber_probe"
    assert probe.origin["from"][0]["workflow_version"] == 1
    assert validate_graph(GraphSpec.model_validate(probe.graph)).ok
    # 确定性：同样的版本永远得到同一张图
    assert derive.caliber_probe(pinned).graph == probe.graph
    # 处置写在本地口径卡上，交给和正式运行同一个升版判断
    assert derive.caliber_probe(pinned, upgrade_policy="dual").graph["nodes"][2]["data"]["config"][
        "upgrade_policy"] == "dual"


@pytest.mark.parametrize("graph, code, words", [
    (chain(node("start", "input"), node("fetch", "transform", mode="expression", expression="{}", assign_to="k"),
           node("card", "metrics", metrics=[{"id": "x", "expression": "vars.k.x"}]), node("out", "output")),
     derive.NOT_REUSABLE, "输入不全是查库结果"),
    (chain(node("start", "input"), node("fetch", "tool", tool="db_query__shop_x", args={"sql": "SELECT 1 AS v"}),
           node("card", "metrics", metrics=[{"id": "x", "expression": "cell(nodes.fetch, 1, 'v')"}]),
           node("out", "output")), derive.NOT_REUSABLE, "别的行号"),
    (chain(node("start", "input"), node("card", "metrics", caliber_from={"workflow_id": "w", "workflow_version": 1,
                                                                         "node_id": "c"}), node("out", "output")),
     derive.NOT_REUSABLE, "转抄"),
    (chain(node("start", "input"), node("card", "report"), node("out", "output")), derive.NOT_CARD, "不是口径卡"),
    (weekly("SELECT SUM(amount) AS gmv, COUNT(*) AS n FROM orders WHERE region = '{{ vars.region }}'"),
     derive.TEMPLATE, "vars.region"),
    (weekly("SELECT SUM(amount) AS gmv, COUNT(*) AS n FROM {{ nodes.pick.table }}"), derive.TEMPLATE, "nodes.pick"),
    (weekly("SELECT SUM(amount) AS gmv, COUNT(*) AS n FROM orders -- {{ file_text('x') }}"), derive.TEMPLATE,
     "file_text"),
])
async def test_cards_that_cannot_be_derived_say_why(graph, code, words):
    with pytest.raises(derive.NotDerivable) as caught:
        await pinned_of(graph)
    assert caught.value.code == code and words in caught.value.message


async def test_missing_workflows_versions_and_nodes():
    wf_id = await publish(weekly())
    async with SessionLocal() as session:
        for args, words in (((("nope", 1, "card")), "不存在"), (((wf_id, 9, "card")), "没有 v9"),
                            (((wf_id, 1, "ghost")), "没有节点 ghost")):
            with pytest.raises(derive.NotDerivable, match=words) as caught:
                await derive.load_pinned(session, *args)
            assert caught.value.code == derive.GONE


async def test_draft_versions_load_but_say_they_are_not_published():
    pinned = await pinned_of(weekly(), level=None)
    async with SessionLocal() as session:
        wf = await session.get(Workflow, pinned.workflow_id)
        wf.published_version = None
        await session.commit()
        again = await derive.load_pinned(session, pinned.workflow_id, 1, "card")
    assert again.published is False


async def test_prefixed_blocks_rename_and_map_for_two_calibers_in_one_graph():
    """召回（第 3 波）要把新旧两版放进同一张图：带前缀的段改名，口径卡写出改名映射。"""
    pinned = await pinned_of(weekly())
    old = derive.caliber_block(pinned, prefix="old_")
    new = derive.caliber_block(pinned, prefix="new_")
    assert [n["id"] for n in old.nodes] == ["old_fetch", "old_card"] and old.rename == {"fetch": "old_fetch"}
    assert old.nodes[-1]["data"]["config"]["caliber_from"]["nodes"] == {"fetch": "old_fetch"}
    graph = derive.assemble("recall", [old, new], fields=pinned.entry_fields)
    assert graph.card_ids == ("old_card", "new_card") and len(graph.origin["from"]) == 2
    names = [f["name"] for f in graph.graph["nodes"][-1]["data"]["config"]["fields"]]
    assert names == ["周销售口径卡", "周销售口径卡_2"]
    # 不带前缀放两份：节点 id 撞了，直接报错，不悄悄拼出一张错图
    with pytest.raises(ValueError, match="重复"):
        derive.assemble("recall", [derive.caliber_block(pinned), derive.caliber_block(pinned)])
    with pytest.raises(ValueError, match="不认识"):
        derive.assemble("nope", [old])


def test_template_problem_allows_only_entry_fields():
    assert derive.template_problem("q", {"args": {"sql": "SELECT '{{ input.start }}', '{{ input.x | trim }}'"}}) is None
    assert derive.template_problem("q", {"args": {"sql": "SELECT 1"}, "limit": 5}) is None
    assert "vars.a" in derive.template_problem("q", {"args": {"sql": "{{ vars.a }}"}})
    assert "input" in derive.template_problem("q", {"args": {"sql": "{{ input }}"}})


# ==========================================================================
# 只算一部分指标（K08 S2）：按选择裁取数、入口字段与身份
# ==========================================================================

#: region 只有 B 那条查询用：只选 A 时它不该进这次的范围
FIELDS_AB = [{"name": "start", "required": True, "type": "date"},
             {"name": "end", "required": True, "type": "date"},
             {"name": "region", "required": True}]
SQL_A = ("SELECT SUM(amount) AS gmv FROM orders WHERE paid_date >= '{{ input.start }}' "
         "AND paid_date < '{{ input.end }}'")
SQL_B = SQL_A + " AND region = '{{ input.region }}'"


def two_metric_card(b_sql: str = SQL_B) -> dict:
    """一张卡两个指标、两条各自独立的查询。"""
    return chain(node("start", "input", fields=FIELDS_AB),
                 node("fetch_a", "tool", tool="db_query__shop_x", args={"sql": SQL_A}),
                 node("fetch_b", "tool", tool="db_query__shop_x", args={"sql": b_sql}),
                 node("card", "metrics", "双指标口径卡", caliber="双指标", caliber_version="v1",
                      metrics=[{"id": "a", "name": "销售额", "expression": "cell(nodes.fetch_a, 0, 'gmv')"},
                               {"id": "b", "name": "区域单量", "expression": "cell(nodes.fetch_b, 0, 'gmv')"}]),
                 node("out", "output"))


def shared_card() -> dict:
    """两个指标读同一条取数（共享 SQL）。"""
    return chain(node("start", "input", fields=FIELDS_AB[:2]),
                 node("fetch", "tool", tool="db_query__shop_x", args={"sql": SQL_A}),
                 node("card", "metrics", "共享取数口径卡", caliber="共享", caliber_version="v1",
                      metrics=[{"id": "a", "name": "销售额", "expression": "cell(nodes.fetch, 0, 'gmv')"},
                               {"id": "b", "name": "单量", "expression": "cell(nodes.fetch, 0, 'gmv')"}]),
                 node("out", "output"))


async def test_without_a_selection_nothing_changes_and_a_whole_card_closure_comes_along():
    """没给选择：照旧整卡——取数、入口字段、探测图一字不变，另附一份整卡闭包供调用方读身份。"""
    pinned = await pinned_of(two_metric_card())
    assert [n["id"] for n in pinned.fetches] == ["fetch_a", "fetch_b"]
    assert [f["name"] for f in pinned.entry_fields] == ["start", "end", "region"]
    assert pinned.reads == (("fetch_a", "db_query__shop_x", ("gmv",)),
                            ("fetch_b", "db_query__shop_x", ("gmv",)))
    assert pinned.closure is not None and pinned.closure.whole_card
    assert pinned.closure.mode == closure.WHOLE_CARD and pinned.closure.metric_ids == ("a", "b")
    assert pinned.binding["status"] == bindings.UNBOUND             # 没声明业务字段：旧资产照旧放行
    probe = derive.caliber_probe(pinned)
    assert [n["id"] for n in probe.graph["nodes"]] == [derive.ENTRY_ID, "fetch_a", "fetch_b", "card",
                                                       derive.EXIT_ID]
    assert probe.graph["nodes"][0]["data"]["config"]["fields"] == FIELDS_AB
    assert "metrics" not in probe.graph["nodes"][-2]["data"]["config"]["caliber_from"]


async def test_selecting_one_metric_copies_only_its_query_and_shortens_the_entry():
    """选 A：只拷 fetch_a，入口字段收窄到这次用的那两个，探测图的入口表单跟着变；口径卡记下这次算哪个指标。"""
    pinned = await pinned_of(two_metric_card())
    one = derive.select_pinned(pinned, metric_ids=["a"])
    assert [n["id"] for n in one.fetches] == ["fetch_a"]
    assert one.reads == (("fetch_a", "db_query__shop_x", ("gmv",)),)
    assert [f["name"] for f in one.entry_fields] == ["start", "end"]
    assert one.closure.metric_ids == ("a",) and one.closure.excluded_query_ids == (f"{pinned.workflow_id}:fetch_b",)
    assert one.closure.selection_hash and one.closure.closure_hash != pinned.closure.closure_hash
    probe = derive.caliber_probe(one)
    nodes = {n["id"]: n for n in probe.graph["nodes"]}
    assert [n["id"] for n in probe.graph["nodes"]] == [derive.ENTRY_ID, "fetch_a", "card", derive.EXIT_ID]
    assert nodes[derive.ENTRY_ID]["data"]["config"]["fields"] == FIELDS_AB[:2]        # 入口表单同步收窄
    assert nodes["card"]["data"]["config"]["caliber_from"] == {
        "workflow_id": pinned.workflow_id, "workflow_version": 1, "node_id": "card", "metrics": ["a"]}
    assert validate_graph(GraphSpec.model_validate(probe.graph)).ok
    # 同一份选择从 load_pinned 走一遍：拿到同一份（闭包身份也一样）
    async with SessionLocal() as session:
        again = await derive.load_pinned(session, pinned.workflow_id, 1, "card", metric_ids=["a"])
    assert again.fetches == one.fetches and again.entry_fields == one.entry_fields
    assert again.closure.closure_hash == one.closure.closure_hash
    # 显式整卡：和上面同一道门禁，闭包是整卡的（仍是同一张图，只是不写选择）
    async with SessionLocal() as session:
        whole = await derive.load_pinned(session, pinned.workflow_id, 1, "card", whole_card=True)
    assert [n["id"] for n in whole.fetches] == ["fetch_a", "fetch_b"] and whole.closure.whole_card


async def test_a_broken_query_of_the_other_metric_does_not_touch_the_selected_one():
    """B 那条查询引用了入口没声明的字段：整卡照实拒；只选 A 时它根本不在这次范围里。"""
    graph = two_metric_card(b_sql=SQL_B + " AND note = '{{ input.nope }}'")
    with pytest.raises(derive.NotDerivable) as whole:
        await pinned_of(graph)
    assert whole.value.code == derive.UNDECLARED_PARAMETER and "nope" in whole.value.message

    wf_id = await publish(graph)
    async with SessionLocal() as session:
        one = await derive.load_pinned(session, wf_id, 1, "card", metric_ids=["a"])
        assert [n["id"] for n in one.fetches] == ["fetch_a"] and one.closure.unresolved == ()
        with pytest.raises(derive.NotDerivable) as bad:
            await derive.load_pinned(session, wf_id, 1, "card", metric_ids=["b"])
    assert bad.value.code == derive.SELECTION
    assert "undeclared_parameter" in bad.value.message and "nope" in bad.value.message


async def test_an_unknown_metric_is_refused_and_never_widens_to_the_whole_card():
    pinned = await pinned_of(two_metric_card())
    with pytest.raises(derive.NotDerivable) as e:
        derive.select_pinned(pinned, metric_ids=["a", "没有这个"])
    assert e.value.code == derive.SELECTION and "metric_unknown" in e.value.message
    with pytest.raises(derive.NotDerivable):                       # 空选择：不当整卡
        derive.select_pinned(pinned, metric_ids=[])
    assert derive.select_pinned(pinned).closure.whole_card          # 不给选择：整卡闭包，照旧


async def test_a_shared_query_is_kept_and_says_what_else_it_still_computes():
    """共享 SQL：选 a 时那条查询保留，闭包如实声明它还在算 b（不做投影）。"""
    pinned = await pinned_of(shared_card())
    one = derive.select_pinned(pinned, metric_ids=["a"])
    assert [n["id"] for n in one.fetches] == ["fetch"]
    assert one.closure.excluded_query_ids == ()
    [query] = one.closure.queries
    assert query.metric_ids == ("a",) and query.computes_unselected == ("b",)
    block = derive.caliber_block(one, prefix="c1_")
    assert [n["id"] for n in block.nodes] == ["c1_fetch", "c1_card"]
    assert block.rename == {"fetch": "c1_fetch"}
    assert block.nodes[-1]["data"]["config"]["caliber_from"]["metrics"] == ["a"]
    # 走参数也一样：caliber_block(pinned, metrics=[…]) 和先裁一份等价
    assert derive.caliber_block(pinned, prefix="c1_", metrics=["a"]).nodes == block.nodes


async def test_a_card_with_a_duplicated_metric_id_is_refused_on_every_path():
    """同一张卡上两个指标用同一个 id、各读一条查询：整卡、点名、选择裁一份都不放行（K08 复核）。

    严格说这也拦住了同卡上不重的那些指标——重复的 id 是卡本身的身份问题（闭包按定义解析、运行时按指标集筛、下游按位置取
    指标各算一份），不许悄悄挑一份。"""
    made = two_metric_card()
    card = next(n for n in made["nodes"] if n["id"] == "card")
    card["data"]["config"]["metrics"][1]["id"] = "a"                    # 两个 "a"：一个读 fetch_a、一个读 fetch_b
    wf_id = await publish(made)
    async with SessionLocal() as session:
        with pytest.raises(derive.NotDerivable) as whole:
            await derive.load_pinned(session, wf_id, 1, "card")
        assert whole.value.code == closure.DUPLICATE_METRIC_ID and "重复" in whole.value.message
        with pytest.raises(derive.NotDerivable) as picked:
            await derive.load_pinned(session, wf_id, 1, "card", metric_ids=["a"])
        assert picked.value.code == closure.DUPLICATE_METRIC_ID
        base = await derive.read_pinned(session, wf_id, 1, "card")      # 只读这一版、不判依赖：底稿读得出来
        with pytest.raises(derive.NotDerivable) as one:
            derive.select_pinned(base, metric_ids=["a"])
    assert one.value.code == derive.SELECTION and "duplicate_metric_id" in one.value.message


async def test_read_pinned_reads_a_version_without_judging_its_dependencies():
    """read_pinned（K08 S4）只读这一版、不判依赖：B 那条查询再坏也读得出来，入口字段与图照抄、取数节点空着。

    look 的按指标可组合性从这一份出发——B 独有那条查询坏了不该牵连 A，所以它不能走整卡门禁那一条。"""
    graph = two_metric_card(b_sql=SQL_B + " AND note = '{{ input.nope }}'")
    wf_id = await publish(graph)
    async with SessionLocal() as session:
        base = await derive.read_pinned(session, wf_id, 1, "card")
    assert base.fetches == () and base.closure is None and base.binding == {}
    assert [f["name"] for f in base.entry_fields] == ["start", "end", "region"]
    assert base.graph and base.card["id"] == "card" and base.node_id == "card"
    # reads 是整卡认得出的那一份（只看指标式，不看 SQL 好不好）：两条都在
    assert [r[0] for r in base.reads] == ["fetch_a", "fetch_b"]
    # 整卡门禁照旧在 load_pinned：这一版整体派生不出来
    async with SessionLocal() as session:
        with pytest.raises(derive.NotDerivable) as whole:
            await derive.load_pinned(session, wf_id, 1, "card")
    assert whole.value.code == derive.UNDECLARED_PARAMETER
    # 按选择裁照旧能裁出 A；B 点不出来（逐项硬校验）
    one = derive.select_pinned(base, metric_ids=["a"])
    assert [n["id"] for n in one.fetches] == ["fetch_a"]
    with pytest.raises(derive.NotDerivable) as bad:
        derive.select_pinned(base, metric_ids=["b"])
    assert bad.value.code == derive.SELECTION and "undeclared_parameter" in bad.value.message
