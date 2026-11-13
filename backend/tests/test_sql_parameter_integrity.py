"""SQL 模板参数完整性（开工任务书 v3 · K01，§4.4）：ledger/params 一套语义，各入口同一结果。

- 认模板里的参数（引号里的字 / 裸写 / 注释里）、未声明的字段、缺值的字段（含声明成可选、又没有缺省值的）；写在表名列名
  里、认不出写法的照实拒（单元一层，函数返回机器码）；
- 组合式问数拷进来的取数节点：值在拷的时候渲染进 SQL（只渲染这一次），节点上写一份与**这条渲染好的 args** 绑定的
  参数契约：跑一次真实的问数运行，断言**实际发给数据库的那条 SQL** 就带着值里的 {{ }} 文本（字面量，不再被解释）；
- 契约与它注解的语句绑定哈希：复制走、改过 SQL 之后契约作废，那个节点回退成普通模板（正常渲染、正常判参数，带 {{ }}
  的语句绝不原样发出去）；
- 派生图（口径探针）同样：入口参数值里的 {{ }} 是字面量，各入口同值同语义；
- 缺参数（声明成可选、没有缺省值；开放入口里显式传了空串）、未声明参数：**零 SQL**——数一下 run_query 被调了几次，就是 0；
  显式空串算没给，作者声明的 `default=""` 才算声明式省略（两条都有真实运行反例）；
- 查询快照记源模板哈希、规范化输入哈希、参数契约与写在哪儿，承载方式照实写 inline_text（不假装驱动绑定）；
- 对账图里「这次的取数」放的是执行过的语句：运行时的模板渲染不再碰它。

数据是虚构店铺库（tests/ledger_samples.py）。
"""
from __future__ import annotations

import hashlib

import pytest
from sqlalchemy import select

from app.db.base import SessionLocal
from app.db.models import Artifact
from app.engine.runner import run_manager
from app.ledger import compose, derive, params
from app.ledger import match as ledger_match
from app.look.calibers import CaliberKind
from tests import ledger_samples as S

TEMPLATE = S.TEMPLATE


@pytest.fixture(autouse=True)
async def engine_up(tmp_path):
    await run_manager.setup()
    await S.fresh_source(tmp_path / "shop.db")
    yield
    await ledger_match.drain()
    await run_manager.shutdown()


@pytest.fixture
def executed(monkeypatch) -> list[str]:
    """这一次测试里真正发给数据库的语句（内联渲染之后的那一条），按先后。"""
    from app.tools import datasource

    seen: list[str] = []
    real = datasource.run_query

    async def spy(source, sql, **kw):
        seen.append(sql)
        return await real(source, sql, **kw)

    monkeypatch.setattr(datasource, "run_query", spy)
    return seen


async def query_snapshots(run_id: str) -> list[dict]:
    """这次运行落下的查询快照内容（按工件 id）。"""
    from app.core.artifact_store import load
    from app.data.catalog import QUERY_SNAPSHOT_KIND

    async with SessionLocal() as session:
        rows = (await session.execute(select(Artifact.id).where(
            Artifact.run_id == run_id, Artifact.kind == QUERY_SNAPSHOT_KIND))).scalars().all()
    return [payload for payload in (load(rid) for rid in rows) if isinstance(payload, dict)]


# ==========================================================================
# 认得模板里的参数（单元一层：机器码 + 写在哪儿）
# ==========================================================================


def test_parameters_are_read_where_they_are_written():
    slots = params.slots_of("SELECT * FROM t WHERE d >= '{{ input.start }}' AND n > {{ input.n }} "
                            "AND r = '{{ input.region | trim }}' -- {{ input.note }}")
    assert [(s.field, s.context, s.filters) for s in slots] == [
        ("start", "quoted", ()), ("n", "bare", ()), ("region", "quoted", ("trim",)), ("note", "comment", ())]


@pytest.mark.parametrize(("sql", "words"), [
    ('SELECT * FROM "{{ input.table }}"', "表名或列名"),
    ("SELECT {{ vars.pick }} FROM t", "运行时才有的变量"),
    ("SELECT {{ file_text(input.f) }} FROM t", "附件"),
    ("SELECT * FROM t WHERE a = '{{ input.x | upper }}'", "upper"),
    ("SELECT {{ input }} FROM t", "认不出"),
])
def test_unreadable_parameter_writings_are_refused(sql, words):
    problems = params.check_template(sql, fields={"x": {"name": "x"}}, values={"x": "v"})
    assert [p.code for p in problems] == [params.UNSUPPORTED] and words in problems[0].message


def test_undeclared_and_missing_are_two_different_findings():
    fields = {"start": {"name": "start", "required": True}}
    problems = params.check_template("SELECT '{{ input.start }}', '{{ input.nope }}'", fields=fields, values={},
                                     where="口径「周销售」的入口")
    assert [(p.code, p.name) for p in problems] == [(params.MISSING, "start"), (params.UNDECLARED, "nope")]
    assert "（undeclared_parameter）" in problems[1].message and "nope" in problems[1].message
    assert "不能当空串用（missing_parameter）" in problems[0].message
    # 模板和声明对得上（还没到判值的那一步）：读一版口径时只判这一层
    assert params.check_template("SELECT '{{ input.start }}'", fields=fields) == []


def test_an_optional_parameter_without_a_default_is_missing_not_empty():
    fields = {"store": {"name": "store"}}
    [problem] = params.check_template("SELECT * FROM t WHERE s = '{{ input.store }}'", fields=fields, values={})
    assert problem.code == params.MISSING and problem.name == "store"
    # 声明了缺省值（含 `""`：作者写的就是空值不过滤）就不算缺
    assert params.check_template("SELECT * FROM t WHERE s = '{{ input.store }}'",
                                 fields={"store": {"name": "store", "default": ""}}, values={}) == []
    assert params.check_template("SELECT * FROM t WHERE s = '{{ input.store }}'",
                                 fields={"store": {"name": "store", "default": "all"}}, values={}) == []


@pytest.mark.parametrize(("sql", "value", "code", "words"), [
    ("SELECT * FROM t WHERE s = '{{ input.x }}'", "O'Brien", params.LITERAL, "值里不能有引号"),
    ("SELECT * FROM t WHERE s = '{{ input.x }}'", "a\\b", params.LITERAL, "反斜杠"),
    ("SELECT * FROM t WHERE n > {{ input.x }}", "1 OR 1=1", params.LITERAL, "只能是数"),
    ("SELECT * FROM t -- {{ input.x }}", "*/ DROP TABLE t", params.LITERAL, "注释"),
])
def test_values_the_inline_adapter_cannot_carry_are_refused(sql, value, code, words):
    fields = {"x": {"name": "x"}}
    [problem] = params.check_template(sql, fields=fields, values={"x": value})
    assert problem.code == code and words in problem.message


def test_one_field_with_two_slots_reports_one_problem():
    """同一个字段写了两处槽位（引号里一处、裸写一处）：承载不了只报一条，不重复报。"""
    [problem] = params.check_template("SELECT '{{ input.x }}' AS a, {{ input.x }} AS b",
                                      fields={"x": {"name": "x"}}, values={"x": "O'Brien"})
    assert (problem.code, problem.name) == (params.LITERAL, "x")


@pytest.mark.parametrize(("sql", "fields", "dynamic"), [
    ("SELECT '{{ input.store }}'", ("store",), ()),
    ("SELECT '{{ input['store'] }}'", ("store",), ()),
    ('SELECT {{ input["store"] }}', ("store",), ()),
    ("SELECT {{ input[0] }}", ("0",), ()),
    ("SELECT {{ input.rows[0].name }}", ("rows",), ()),
    ("SELECT '{{ input['store'] | trim }}'", ("store",), ()),
    ("SELECT {{ file_text(input['附件']) }}", ("附件",), ()),
    ("SELECT {{ input }}", (), ("input",)),
    ("SELECT '{{ input['a.b'] }}'", (), ("input['a.b']",)),
    ("SELECT {{ input[a[b]] }}", (), ("input[a[b]]",)),
    ("SELECT {{ input x }}", (), ("input x",)),
    ("SELECT {{ inputs.x }}, {{ input_value }}", (), ()),
    ("SELECT {{ vars.region }}, {{ nodes.q.text }}", (), ()),
])
def test_input_references_follow_the_same_path_walk_as_resolve_path(sql, fields, dynamic):
    """运行时认的入口引用和 engine/expressions.resolve_path 一个走法：点号与合法下标都算字段引用（取第一层），
    认不出的写法（会渲染成空串）单独交出来——vars./nodes. 不归这一趟管。"""
    assert params.input_refs(sql) == fields
    assert params.dynamic_input_writings(sql) == dynamic
    assert params.mentions_input(sql) is bool(fields or dynamic)


def test_declared_types_are_normalized_without_looking_at_the_value():
    assert params.field_type({"type": "integer"}) == params.field_type({"type": "int"}) == "integer"
    assert params.field_type({}) == params.field_type({"type": "nonsense"}) == "string"
    assert params.value_kind(3) == "number" and params.value_kind("3") == "text" and params.value_kind(None) == "null"


# ==========================================================================
# 组合式问数：值只渲染一次（E03）
# ==========================================================================


async def composed_answer_graph(workflow_id: str, inputs: dict) -> dict:
    """把口径拷进问数据的图（同 tests/test_ledger_compose.py 的做法，这里直接走 compose）：args 是字面语句 + 参数契约。"""
    from app.ledger import compose

    async with SessionLocal() as session:
        kind = await CaliberKind.load(session, compose=True)
    made = compose.Composer(list(kind.options)).compose(
        {"op": "use_caliber", "id": "c1", "workflow_id": workflow_id, "version": 1, "metrics": ["gmv"],
         "inputs": inputs}, taken=set())
    return S.chain(S.node("start", "input", fields=[{"name": "question"}]), *made.nodes,
                   S.node("write", "report", instructions="写周销售"),
                   S.node("out", "output", fields=[{"name": "报告", "value": "{{ nodes.write.text }}"}]))


async def test_a_value_that_looks_like_a_template_stays_literal_in_the_executed_sql(monkeypatch, executed):
    """E03：参数值里的 {{ }} 是数据，不被二次解释。

    这里挑的是**在答数图里取得出值**的那一种写法（{{ vars.question }} 指向入口字段）：万一运行时又渲染一遍，值会被换成
    问题的原文，SQL 就被数据改掉了——正是这条反例要拦住的那条路。"""
    wf = await S.publish()
    graph = await composed_answer_graph(wf, {"start": "{{ vars.question }}", "end": "2026-09-08"})
    S.script(monkeypatch, "本周销售额 [[m:gmv]]。")
    run = await run_manager.start(graph=graph, input_payload={"question": "九月第一周卖了多少"}, origin_kind="chat")
    row = await S.wait(run.id)
    assert row.status == "succeeded", row.error
    [sql] = executed
    assert sql == TEMPLATE.replace("{{ input.start }}", "{{ vars.question }}").replace("{{ input.end }}", "2026-09-08")
    assert "'{{ vars.question }}'" in sql and "九月第一周卖了多少" not in sql


async def test_a_value_naming_a_variable_that_does_not_exist_runs_as_a_literal(monkeypatch, executed):
    """反例的另一半：值里写的是图里根本没有的变量（{{ vars.region }}）——照样按字面量跑，不判成「没人产出的引用」。

    画布上那条变量校验（engine/variables）会把节点配置里带 {{ }} 的字当成活引用；组合式问数拷进来的取数节点是服务端
    渲染好的**数据**，它不该被那条规则拦下——否则「值里恰好有几个花括号」就等于这张图跑不了。"""
    wf = await S.publish()
    graph = await composed_answer_graph(wf, {"start": "{{ vars.region }}", "end": "2026-09-08"})
    S.script(monkeypatch, "本周销售额 [[m:gmv]]。")
    run = await run_manager.start(graph=graph, input_payload={"question": "九月第一周卖了多少"}, origin_kind="chat")
    row = await S.wait(run.id)
    assert row.status == "succeeded", row.error
    [sql] = executed
    assert "'{{ vars.region }}'" in sql and "'2026-09-08'" in sql


async def test_a_copied_node_edited_back_into_a_template_renders_normally(monkeypatch, executed):
    """复制一个拷进来的取数节点、把 SQL 改回普通模板：契约作废，回退正常渲染——只渲染一次，不发带 {{ }} 的语句。"""
    wf = await S.publish()
    graph = await composed_answer_graph(wf, {"start": "2026-09-01", "end": "2026-09-08"})
    fetch = next(n for n in graph["nodes"] if n["id"] == "c1_fetch")
    assert params.literal_keys(fetch["data"]["config"], "tool") == ("args",)
    fetch["data"]["config"]["args"]["sql"] += " AND '{{ input.question }}' <> ''"
    assert params.literal_keys(fetch["data"]["config"], "tool") == ()
    S.script(monkeypatch, "本周销售额 [[m:gmv]]。")
    run = await run_manager.start(graph=graph, input_payload={"question": "九月第一周卖了多少"}, origin_kind="chat")
    row = await S.wait(run.id)
    assert row.status == "succeeded", row.error
    [sql] = executed
    assert "'九月第一周卖了多少'" in sql and "{{" not in sql


async def test_a_copied_node_with_an_incomplete_edit_sends_no_sql(monkeypatch, executed):
    """复制后改过的 SQL 引用了入口声明过、这次没给的参数：运行时按同一套参数完整性拒，零 SQL。

    契约在这里也帮不上忙（哈希对不上就当没写）：走的是普通模板那条路——渲染前判缺参数，绝不把带 {{ }} 的语句发出去。"""
    wf = await S.publish()
    graph = await composed_answer_graph(wf, {"start": "2026-09-01", "end": "2026-09-08"})
    start = next(n for n in graph["nodes"] if n["id"] == "start")
    start["data"]["config"]["fields"].append({"name": "store"})      # 可选、没有缺省值
    fetch = next(n for n in graph["nodes"] if n["id"] == "c1_fetch")
    fetch["data"]["config"]["args"]["sql"] += " AND region = '{{ input.store }}'"
    assert params.literal_keys(fetch["data"]["config"], "tool") == ()
    S.script(monkeypatch, "本周销售额 [[m:gmv]]。")
    run = await run_manager.start(graph=graph, input_payload={"question": "九月第一周卖了多少"}, origin_kind="chat")
    row = await S.wait(run.id)
    assert row.status == "failed" and "store" in row.error and "missing_parameter" in row.error, row.error
    assert executed == []


async def test_the_probe_path_renders_a_template_like_value_once_too(executed):
    """派生图（口径探针、试跑走的就是它）同值同语义：入口值里的 {{ }} 也是字面量。"""
    wf = await S.publish()
    async with SessionLocal() as session:
        pinned = await derive.load_pinned(session, wf, 1, "card")
    run = await run_manager.start(graph=derive.caliber_probe(pinned).graph,
                                  input_payload={"start": "{{ vars.region }}", "end": "2026-09-08"})
    row = await S.wait(run.id)
    assert row.status == "succeeded", row.error
    [sql] = executed
    assert "'{{ vars.region }}'" in sql and "'2026-09-08'" in sql


async def test_the_query_snapshot_records_the_template_hash_types_and_no_fake_binding(monkeypatch, executed):
    """快照记源模板哈希、规范化输入哈希、参数契约与写在哪儿；承载方式照实写内联（不假装驱动绑定）。"""
    wf = await S.publish()
    graph = await composed_answer_graph(wf, {"start": "2026-09-01", "end": "2026-09-08"})
    S.script(monkeypatch, "本周销售额 [[m:gmv]]。")
    run = await run_manager.start(graph=graph, input_payload={"question": "九月第一周卖了多少"}, origin_kind="chat")
    row = await S.wait(run.id)
    assert row.status == "succeeded", row.error
    [payload] = await query_snapshots(run.id)
    material = payload["parameters"]
    assert material["adapter"] == params.ADAPTER == "inline_text"
    assert material["binding"] == "values_in_statement"
    # 源模板正文不在节点上（engine/variables 会扫节点配置里的 {{ }}）：记哈希 + 那一版取数节点的固定引用，
    # 模板本体按引用从不可变版本快照还原，哈希核对
    assert material["template_hash"] == hashlib.sha256(TEMPLATE.encode("utf-8")).hexdigest()
    assert len(material["inputs_hash"]) == 64 and "template" not in material
    assert [(p["name"], p["kind"], p["declared"], p["slot"]) for p in material["parameters"]] == [
        ("start", "text", "string", "quoted"), ("end", "text", "string", "quoted")]
    # 契约跟着材料一起记；绑定值不另存一份：它就是内联进实际语句里的那些字，实际语句本身在快照里
    assert [d["name"] for d in material["declared"]] == ["start", "end", "store"]
    assert set(material) == {"adapter", "binding", "template_hash", "inputs_hash", "declared", "parameters",
                             "source"}
    assert payload["sql"] == TEMPLATE.replace("{{ input.start }}", "2026-09-01").replace("{{ input.end }}",
                                                                                         "2026-09-08")
    assert material["source"]["node_id"] == "fetch" and material["source"]["workflow_version"] == 1


# ==========================================================================
# 缺参数、未声明参数：零 SQL（E04）
# ==========================================================================


def optional_field_graph(sql: str, fields: list[dict]) -> dict:
    graph = S.caliber_graph(sql=sql)
    graph["nodes"][0]["data"]["config"]["fields"] = fields
    return graph


async def test_a_missing_optional_parameter_sends_no_sql(executed):
    """可选、没有缺省值的参数这次没给：不发 SQL，运行说清是哪个参数（以前渲染成空串照发）。"""
    sql = ("SELECT SUM(amount) AS gmv FROM orders WHERE status = 'paid' AND region = '{{ input.store }}' "
           "AND paid_at >= '{{ input.start }}' AND paid_at < '{{ input.end }}'")
    graph = optional_field_graph(sql, [{"name": "start", "required": True}, {"name": "end", "required": True},
                                       {"name": "store"}])
    wf = await S.publish(graph=graph)
    async with SessionLocal() as session:
        pinned = await derive.load_pinned(session, wf, 1, "card")
    run = await run_manager.start(graph=derive.caliber_probe(pinned).graph,
                                  input_payload={"start": "2026-09-01", "end": "2026-09-08"})
    row = await S.wait(run.id)
    assert row.status == "failed" and "store" in row.error and "missing_parameter" in row.error, row.error
    assert executed == [] and await query_snapshots(run.id) == []


async def test_a_plain_canvas_query_with_a_missing_optional_parameter_sends_no_sql(executed):
    """普通画布上的一条取数：参数声明成可选、没有缺省值、这次没给——不发 SQL（以前渲染成空串照发）。"""
    graph = S.chain(S.node("start", "input", fields=[{"name": "start", "required": True},
                                                     {"name": "store"}]),
                    S.node("q", "tool", tool=S.tool(),
                           args={"sql": "SELECT COUNT(*) AS n FROM orders WHERE region = '{{ input.store }}' "
                                        "AND paid_at >= '{{ input.start }}'"}),
                    S.node("out", "output", fields=[{"name": "r", "value": "{{ nodes.q.text }}"}]))
    run = await run_manager.start(graph=graph, input_payload={"start": "2026-09-01"})
    row = await S.wait(run.id)
    assert row.status == "failed" and "store" in row.error and "missing_parameter" in row.error, row.error
    assert executed == [] and await query_snapshots(run.id) == []


async def test_an_undeclared_parameter_cannot_be_composed_or_derived():
    """模板引用了入口没声明的字段：读这一版（派生、组合）时就拒，机器码 undeclared_parameter。"""
    graph = optional_field_graph("SELECT SUM(amount) AS gmv FROM orders WHERE region = '{{ input.nope }}'",
                                 [{"name": "start", "required": True}, {"name": "end", "required": True}])
    wf = await S.publish(graph=graph)
    async with SessionLocal() as session:
        with pytest.raises(derive.NotDerivable) as caught:
            await derive.load_pinned(session, wf, 1, "card")
        assert caught.value.code == derive.UNDECLARED_PARAMETER and "nope" in caught.value.message
        assert "undeclared_parameter" in caught.value.message
        kind = await CaliberKind.load(session, compose=True)
    [option] = [o for o in kind.options if o.workflow_id == wf]
    assert not option.composable and "undeclared_parameter" in option.reason


async def test_a_query_referencing_a_parameter_nobody_declares_sends_no_sql(executed):
    """取数 SQL 引用了这次运行没有的参数：不发 SQL，说清是哪个参数。

    入口一个字段都没声明时（开放入口）engine/variables 不会拦这种引用——运行到取数这一步由参数完整性拦，以前渲染成
    空串照发。入口声明了字段的图更早就会被画布校验拦下（那是另一道，见下一条）。"""
    graph = S.chain(S.node("start", "input"),
                    S.node("q", "tool", tool=S.tool(),
                           args={"sql": "SELECT COUNT(*) AS n FROM orders WHERE region = '{{ input.store }}'"}),
                    S.node("out", "output", fields=[{"name": "r", "value": "{{ nodes.q.text }}"}]))
    run = await run_manager.start(graph=graph, input_payload={"question": "有多少单"})
    row = await S.wait(run.id)
    assert row.status == "failed" and "store" in row.error and "undeclared_parameter" in row.error, row.error
    assert executed == [] and await query_snapshots(run.id) == []


async def test_a_value_that_the_run_provides_is_enough_even_without_a_declared_field(executed):
    """入口没声明字段、值由发起时传进来（开放入口）：照常发出 SQL——判断看的是「这次有没有值」，不是只认声明。

    这是画布上很常见的写法（入口节点只写「开始」，参数由发起时传），拦掉它会把跑得好的图判成参数不完整。"""
    graph = S.chain(S.node("start", "input"),
                    S.node("q", "tool", tool=S.tool(),
                           args={"sql": "SELECT COUNT(*) AS n FROM orders WHERE paid_at >= '{{ input.start }}'"}),
                    S.node("out", "output", fields=[{"name": "r", "value": "{{ nodes.q.text }}"}]))
    run = await run_manager.start(graph=graph, input_payload={"start": "2026-09-01"})
    row = await S.wait(run.id)
    assert row.status == "succeeded", row.error
    assert executed == ["SELECT COUNT(*) AS n FROM orders WHERE paid_at >= '2026-09-01'"]


async def test_an_open_entry_with_an_explicit_empty_string_sends_no_sql(executed):
    """开放入口（入口节点一个字段都没声明、键由发起时传）传来空串：算没给，不发 SQL。

    空串不是值，是省略——和入口收值（params.provided）同一套，也和「声明了 default="" 才算声明式省略」同一套。以前只认
    None，这种图会渲染成 `AND region = ''` 发出去；作者要表达「空值不过滤」，得把字段声明出来并写 `default=""`（下一条用例）。"""
    graph = S.chain(S.node("start", "input"),
                    S.node("q", "tool", tool=S.tool(),
                           args={"sql": "SELECT COUNT(*) AS n FROM orders WHERE region = '{{ input.store }}'"}),
                    S.node("out", "output", fields=[{"name": "r", "value": "{{ nodes.q.text }}"}]))
    run = await run_manager.start(graph=graph, input_payload={"store": ""})
    row = await S.wait(run.id)
    assert row.status == "failed" and "store" in row.error and "missing_parameter" in row.error, row.error
    assert executed == [] and await query_snapshots(run.id) == []


async def test_a_declared_empty_default_still_runs_in_an_open_entry(executed):
    """作者把字段声明出来、写 `default=""`：那是声明的省略规则，照跑，空串不是「没给」。"""
    graph = S.chain(S.node("start", "input", fields=[{"name": "store", "default": ""}]),
                    S.node("q", "tool", tool=S.tool(),
                           args={"sql": "SELECT COUNT(*) AS n FROM orders WHERE ('{{ input.store }}' = '' "
                                        "OR region = '{{ input.store }}')"}),
                    S.node("out", "output", fields=[{"name": "r", "value": "{{ nodes.q.text }}"}]))
    run = await run_manager.start(graph=graph, input_payload={})
    row = await S.wait(run.id)
    assert row.status == "succeeded", row.error
    [sql] = executed
    assert "{{" not in sql and "('' = ''" in sql


async def test_an_executed_statement_carried_into_a_graph_is_not_templated_again(executed):
    """对账里「这次的取数」：放进去的是执行过的语句，运行时不再当模板解释一遍。

    语句文本里挑了在答数图里取得出值的写法（{{ vars.question }}）：真的被再渲染一次的话，这条 SQL 就变成别的语句了。"""
    text = "SELECT SUM(amount) AS gmv FROM orders WHERE region = '{{ vars.question }}' AND id > 1"
    block = derive.ours_block(text, S.tool())
    assert params.mode_of(block.nodes[0]["data"]["config"]) == params.LITERAL_ARGS
    graph = S.chain(S.node("start", "input", fields=[{"name": "question"}]),
                    block.nodes[0],
                    S.node("out", "output", fields=[{"name": "r", "value": "{{ nodes.ours_q1.text }}"}]))
    run = await run_manager.start(graph=graph, input_payload={"question": "九月第一周"})
    row = await S.wait(run.id)
    assert row.status == "succeeded", row.error
    assert executed == [text]


async def test_a_bracket_reference_without_a_value_sends_no_sql(executed):
    """`{{ input['store'] }}`（下标写法、SQL 里还嵌着引号）没给值：一样不发 SQL。

    运行时的兜底以前只认 `{{ input.字段 }}`，这种写法认不出来 → 渲染成 `''`，发出一条 `AND region = ''`。"""
    graph = S.chain(S.node("start", "input", fields=[{"name": "store"}]),
                    S.node("q", "tool", tool=S.tool(),
                           args={"sql": "SELECT COUNT(*) AS n FROM orders WHERE region = '{{ input['store'] }}'"}),
                    S.node("out", "output", fields=[{"name": "r", "value": "{{ nodes.q.text }}"}]))
    run = await run_manager.start(graph=graph, input_payload={"store": ""})
    row = await S.wait(run.id)
    assert row.status == "failed" and "store" in row.error and "missing_parameter" in row.error, row.error
    assert executed == [] and await query_snapshots(run.id) == []


async def test_a_bracket_reference_with_a_value_renders_and_runs(executed):
    """`{{ input['store'] }}` 正常给值：照常渲染进 SQL 发出去（不是拒绝一切下标写法）。"""
    graph = S.chain(S.node("start", "input", fields=[{"name": "store"}]),
                    S.node("q", "tool", tool=S.tool(),
                           args={"sql": "SELECT COUNT(*) AS n FROM orders WHERE region = '{{ input['store'] }}'"}),
                    S.node("out", "output", fields=[{"name": "r", "value": "{{ nodes.q.text }}"}]))
    run = await run_manager.start(graph=graph, input_payload={"store": "east"})
    row = await S.wait(run.id)
    assert row.status == "succeeded", row.error
    assert executed == ["SELECT COUNT(*) AS n FROM orders WHERE region = 'east'"]


async def test_a_bracket_reference_uses_the_declared_default(executed):
    """`{{ input['store'] }}` 配声明缺省值：没给值时按入口那套补上，渲染进去的是缺省值。"""
    graph = S.chain(S.node("start", "input", fields=[{"name": "store", "default": "east"}]),
                    S.node("q", "tool", tool=S.tool(),
                           args={"sql": 'SELECT COUNT(*) AS n FROM orders WHERE region = \'{{ input["store"] }}\''}),
                    S.node("out", "output", fields=[{"name": "r", "value": "{{ nodes.q.text }}"}]))
    run = await run_manager.start(graph=graph, input_payload={})
    row = await S.wait(run.id)
    assert row.status == "succeeded", row.error
    assert executed == ["SELECT COUNT(*) AS n FROM orders WHERE region = 'east'"]


@pytest.mark.parametrize("sql", ["SELECT COUNT(*) AS n FROM orders WHERE region = '{{ input }}'",
                                 "SELECT COUNT(*) AS n FROM orders WHERE region = '{{ input['a.b'] }}'",
                                 "SELECT COUNT(*) AS n FROM orders WHERE region = '{{ input[a[b]] }}'"])
async def test_an_unreadable_input_writing_sends_no_sql(executed, sql):
    """认不出取哪个字段的入口写法（光秃秃的 `{{ input }}`、下标里带点/套中括号）：明确拒，零 SQL。

    这些写法 resolve_path 取不到值 → 渲染成空串；当「没引用参数」放过去就等于发出一条条件被抹掉的查询。"""
    graph = S.chain(S.node("start", "input", fields=[{"name": "question"}]),
                    S.node("q", "tool", tool=S.tool(), args={"sql": sql}),
                    S.node("out", "output", fields=[{"name": "r", "value": "{{ nodes.q.text }}"}]))
    run = await run_manager.start(graph=graph, input_payload={"question": "有多少单"})
    row = await S.wait(run.id)
    assert row.status == "failed" and "unsupported_parameter" in row.error, row.error
    assert "看不出取的是哪个字段" in row.error
    assert executed == [] and await query_snapshots(run.id) == []


async def test_vars_and_nodes_are_left_alone_in_a_canvas_query(executed):
    """普通模板照旧：取数 SQL 引用 vars./nodes. 不受这一趟影响（照常渲染后发出去）。"""
    graph = S.chain(S.node("start", "input"),
                    S.node("pick", "transform", mode="expression", expression="'east'", assign_to="region"),
                    S.node("q", "tool", tool=S.tool(),
                           args={"sql": "SELECT COUNT(*) AS n FROM orders WHERE region = '{{ vars.region }}'"}),
                    S.node("out", "output", fields=[{"name": "r", "value": "{{ nodes.q.text }}"}]))
    run = await run_manager.start(graph=graph, input_payload={})
    row = await S.wait(run.id)
    assert row.status == "succeeded", row.error
    assert executed == ["SELECT COUNT(*) AS n FROM orders WHERE region = 'east'"]


# ==========================================================================
# 可复用的接口（试跑等入口接线用）
# ==========================================================================


def test_resolve_prefers_explicit_values_then_declared_defaults():
    """共享层定这一次的值：显式给的（含 0——0 是值）优先，否则用声明的缺省值（含 0 与空串）；没缺省值的照旧缺。"""
    fields = [{"name": "region", "default": "east"}, {"name": "min", "default": 0},
              {"name": "note", "default": ""}, {"name": "start"}]
    assert params.resolve(fields, {"start": "2026-09-01"}) == {
        "region": "east", "min": 0, "note": "", "start": "2026-09-01"}
    assert params.resolve(fields, {"region": "west", "min": 5, "note": "x", "start": "s"}) == {
        "region": "west", "min": 5, "note": "x", "start": "s"}
    # 显式空串算没给：回到声明的缺省值；没声明缺省值的（start）不只是空串问题——它压根没值，不在这份里
    assert params.resolve(fields, {"region": "", "min": "", "note": "", "start": ""}) == {
        "region": "east", "min": 0, "note": ""}


def test_plan_renders_the_declared_default_instead_of_an_empty_string():
    """声明了缺省值、调用方没给：渲染进去的必须是那个缺省值（以前 plan 拿原始 values 渲染，声明了却渲染成空）。"""
    template = ("SELECT * FROM orders WHERE region = '{{ input.region }}' AND amount > {{ input.min }} "
                "AND note = '{{ input.note }}'")
    fields = [{"name": "region", "default": "east"}, {"name": "min", "default": 0}, {"name": "note", "default": ""}]
    out = params.plan(template, fields=fields, values={})
    assert "'east'" in out.statement and "amount > 0" in out.statement and "note = ''" in out.statement
    assert "{{" not in out.statement
    # 检查、渲染、材料里的输入哈希用的是同一份 resolved values：显式给同样的值 → 同一份材料
    same = params.plan(template, fields=fields, values={"region": "east", "min": 0, "note": ""})
    assert same.material["inputs_hash"] == out.material["inputs_hash"]
    assert [p["kind"] for p in out.material["parameters"]] == ["text", "number", "text"]
    # 覆盖声明：显式给的值优先
    override = params.plan(template, fields=fields, values={"region": "west", "min": 10, "note": "x"})
    assert "'west'" in override.statement and "amount > 10" in override.statement
    assert override.material["inputs_hash"] != out.material["inputs_hash"]


@pytest.mark.parametrize("values", [{}, {"region": ""}, {"region": None}])
def test_plan_without_a_declared_default_still_reports_missing(values):
    """没有声明的缺省值：没给、给了空串、给了 None 都是缺参数（空串不是值）。"""
    with pytest.raises(params.ParameterRefused) as caught:
        params.plan("SELECT * FROM orders WHERE region = '{{ input.region }}'",
                    fields=[{"name": "region"}], values=values)
    assert caught.value.code == params.MISSING and caught.value.problems[0].name == "region"


def test_plan_renders_once_and_carries_the_snapshot_material():
    out = params.plan(TEMPLATE, fields=[{"name": "start", "required": True, "type": "date"},
                                        {"name": "end", "required": True, "type": "date"}],
                      values={"start": "{{ vars.x }}", "end": "2026-09-08"})
    assert "'{{ vars.x }}'" in out.statement
    assert out.material["parameters"][0]["declared"] == "date"
    with pytest.raises(params.ParameterRefused) as caught:
        params.plan(TEMPLATE, fields=[{"name": "start", "required": True}, {"name": "end", "required": True}],
                    values={"start": "2026-09-01"})
    assert caught.value.code == params.MISSING and "end" in caught.value.message


# ==========================================================================
# 只算选中的指标（K08 S2）：只选 A 时 B 那条 SQL 不进图，也不要求它的参数
# ==========================================================================

#: buyer 只有 B 那条查询用：只选 A 时这次不必给
FIELDS_AB = [{"name": "start", "required": True}, {"name": "end", "required": True},
             {"name": "buyer", "required": True}]
SQL_A = ("SELECT SUM(amount) AS gmv FROM orders WHERE status = 'paid' "
         "AND paid_at >= '{{ input.start }}' AND paid_at < '{{ input.end }}'")
SQL_B = SQL_A + " AND region = '{{ input.buyer }}'"


def two_metric_graph(*, on_missing: bool = True) -> dict:
    """一张卡两个指标、两条各自独立的查询（B 多要一个 buyer）。"""
    card: dict = {"caliber": "双指标", "caliber_version": "v1",
                  "metrics": [{"id": "gmv", "name": "销售额", "expression": "cell(nodes.fetch_a, 0, 'gmv')"},
                              {"id": "n", "name": "单量", "expression": "cell(nodes.fetch_b, 0, 'n')"}]}
    if on_missing:
        card["on_missing"] = "null"
    return S.chain(S.node("start", "input", fields=FIELDS_AB),
                   S.node("fetch_a", "tool", tool=S.tool(), args={"sql": SQL_A}),
                   S.node("fetch_b", "tool", tool=S.tool(), args={"sql": SQL_B}),
                   S.node("card", "metrics", **card),
                   S.node("out", "output", fields=[{"name": "r", "value": "{{ nodes.card.text }}"}]))


def shared_graph() -> dict:
    """两个指标读同一条取数（共享 SQL）。"""
    return S.chain(S.node("start", "input", fields=FIELDS_AB[:2]),
                   S.node("fetch", "tool", tool=S.tool(), args={"sql": SQL_A}),
                   S.node("card", "metrics", caliber="共享取数", caliber_version="v1",
                          metrics=[{"id": "gmv", "name": "销售额", "expression": "cell(nodes.fetch, 0, 'gmv')"},
                                   {"id": "n", "name": "单量", "expression": "cell(nodes.fetch, 0, 'n')"}]),
                   S.node("out", "output"))


async def composed_graph(workflow_id: str, *, metrics: list[str] | None = None) -> tuple[dict, "compose.Composed"]:
    """照 test_ledger_compose 的做法：把口径拷进问数据的图，交回 (图, 这次选用)。"""
    async with SessionLocal() as session:
        kind = await CaliberKind.load(session, compose=True)
    made = compose.Composer(list(kind.options)).compose(
        {"op": "use_caliber", "id": "c1", "workflow_id": workflow_id, "version": 1, "card": "card",
         "metrics": metrics or ["gmv"], "inputs": {"start": "2026-09-01", "end": "2026-09-08"}}, taken=set())
    return S.chain(S.node("start", "input", fields=[{"name": "question"}]), *made.nodes,
                   S.node("write", "report", instructions="写周销售"),
                   S.node("out", "output", fields=[{"name": "报告", "value": "{{ nodes.write.text }}"}])), made


def rendered_a() -> str:
    return SQL_A.replace("{{ input.start }}", "2026-09-01").replace("{{ input.end }}", "2026-09-08")


async def test_only_the_selected_metric_s_sql_is_executed(monkeypatch, executed):
    """组合式问数只选 gmv：B 那条查询不进图，实际发给数据库的只有 A 这一条（B 的参数这次不用给）。"""
    wf = await S.publish(graph=two_metric_graph())
    graph, made = await composed_graph(wf)
    assert [n["id"] for n in graph["nodes"] if n["type"] == "tool"] == ["c1_fetch_a"]
    assert made.result()["selection"]["metric_ids"] == ["gmv"]
    S.script(monkeypatch, "本周销售额 [[m:gmv]]。")
    run = await run_manager.start(graph=graph, input_payload={"question": "九月第一周卖了多少"}, origin_kind="chat")
    row = await S.wait(run.id)
    assert row.status == "succeeded", row.error
    assert executed == [rendered_a()]                       # 一条：B 的零条


async def test_a_shared_query_still_runs_and_declares_the_metric_it_used_to_compute(monkeypatch, executed):
    """A 和 B 共用一条 SQL：选 A 那条照样执行（一次），结果里如实声明它还在算 B。"""
    wf = await S.publish(graph=shared_graph())
    graph, made = await composed_graph(wf, metrics=["gmv"])
    [query] = made.closure.queries
    assert query.computes_unselected == ("n",) and made.closure.query_ids == (f"{wf}:fetch",)
    S.script(monkeypatch, "本周销售额 [[m:gmv]]。")
    run = await run_manager.start(graph=graph, input_payload={"question": "九月第一周卖了多少"}, origin_kind="chat")
    row = await S.wait(run.id)
    assert row.status == "succeeded", row.error
    assert executed == [rendered_a()]


async def test_the_probe_of_a_selection_carries_only_the_selected_query(executed):
    """派生探针按同一份选择裁：入口表单收窄到这次用的（不必给 buyer），拷出来的只有 A 那条查询。"""
    wf = await S.publish(graph=two_metric_graph())
    async with SessionLocal() as session:
        pinned = await derive.load_pinned(session, wf, 1, "card", metric_ids=["gmv"])
    probe = derive.caliber_probe(pinned)
    assert [n["id"] for n in probe.graph["nodes"]] == [derive.ENTRY_ID, "fetch_a", "card", derive.EXIT_ID]
    assert probe.graph["nodes"][0]["data"]["config"]["fields"] == FIELDS_AB[:2]
    run = await run_manager.start(graph=probe.graph, input_payload={"start": "2026-09-01", "end": "2026-09-08"})
    row = await S.wait(run.id)
    assert row.status == "succeeded", row.error
    assert executed == [rendered_a()]
