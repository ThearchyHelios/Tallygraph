"""组合式问数：use_caliber 选用已发布口径，服务端按那一版确定性内联实例化（next-round 02 做法 4，ledger/compose.py）。

- 实例化是确定的：同一版、同一个操作得到一模一样的节点；取数节点照抄、id 加前缀、模板按参数渲染成字面 SQL，口径卡 id
  就是操作 id、caliber_from 带改名映射；取数首尾相接再接口径卡，入口连到第一个取数节点；
- 参数按口径入口的字段校验（未知字段、缺必填、类型、引号里的值带引号、裸写的值不是数），不合规拒收、原因交回模型；
  只能选这一轮查看到的那一版、能组合的卡、卡上的指标，id 不能撞；
- 问数据的查看对象多了口径：给入口字段和「能不能组合」，不给 SQL；「什么时候该看」换成问数据那一份；使用者不给；
- K08 S4：op.metrics 按**源卡顺序**归一（Composed.metrics、口径卡上记的 metrics、selection_hash 与执行数组都按那一份，
  调用方传的顺序与重复项都不影响）；选用选项按**每个指标**判能不能组合（B 独有那条查询坏了只让 B 不可选，A 照旧给组合
  写法），真正的实例化仍按同一条选择逐项硬校验；
- 走一遍问数据的生成（剧本模型）再真跑出来：比对器把这些数标成 composed「助手选用」，整题 how=composed。
数据是虚构店铺库。
"""
from __future__ import annotations

import hashlib
import json

import pytest
from httpx import ASGITransport, AsyncClient

from app.api import copilot_context
from app.api.copilot_gen import GraphApplier, GraphChecker
from app.db.base import SessionLocal
from app.engine.runner import run_manager
from app.engine.schema import GraphSpec, validate_graph
from app.ledger import compose
from app.ledger import match as ledger_match
from app.ledger import params, store
from app.look.calibers import CaliberKind
from app.main import app
from tests import ledger_samples as S


@pytest.fixture(autouse=True)
async def engine_up(tmp_path):
    await run_manager.setup()
    await S.fresh_source(tmp_path / "shop.db")
    yield
    await ledger_match.drain()
    await run_manager.shutdown()


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


def use(wf: str, **extra) -> dict:
    return {"op": "use_caliber", "id": "c1", "workflow_id": wf, "version": 1, "metrics": ["gmv"],
            "inputs": {"start": "2026-09-01", "end": "2026-09-08"}, **extra}


async def composer() -> compose.Composer:
    async with SessionLocal() as session:
        kind = await CaliberKind.load(session, compose=True)
    return compose.Composer(list(kind.options))


# --------------------------------------------------------------------------
# 实例化
# --------------------------------------------------------------------------


async def test_instantiation_is_deterministic_and_copies_the_pinned_fetch():
    wf = await S.publish()
    made = (await composer()).compose(use(wf), taken={"start"})
    again = (await composer()).compose(use(wf), taken={"start"})
    assert made.nodes == again.nodes and made.edges == again.edges
    fetch, card = made.nodes
    assert fetch["id"] == "c1_fetch" and fetch["type"] == "tool"
    assert fetch["data"]["config"]["tool"] == S.tool()
    assert fetch["data"]["config"]["args"]["sql"] == S.TEMPLATE.replace("{{ input.start }}", "2026-09-01").replace(
        "{{ input.end }}", "2026-09-08")
    # 值渲染进去的那一次已经发生过：节点上记一份参数契约（ledger/params 的 marker），运行时的模板渲染不再碰 args——
    # 不然值里的 {{ }} 会被再解释一遍（K01 的二次渲染反例）
    marker = fetch["data"]["config"]["parameters"]
    assert marker["mode"] == params.LITERAL_ARGS and marker["adapter"] == params.ADAPTER == "inline_text"
    # 源模板正文不写在节点上（engine/variables 会扫节点配置里的 {{ }}，把占位符报成没人产出）：记哈希 + 出处
    assert "template" not in marker
    assert marker["template_hash"] == hashlib.sha256(S.TEMPLATE.encode("utf-8")).hexdigest()
    # 契约与它注解的 args 绑定：哈希对得上才算（节点被复制、args 改过就作废，运行时回退正常渲染）
    assert marker["rendered_hash"] == params.rendered_hash(fetch["data"]["config"]["args"])
    assert params.literal_keys(fetch["data"]["config"], "tool") == ("args",)
    assert marker["parameters"] == [{"name": "start", "kind": "text", "declared": "string", "slot": "quoted"},
                                    {"name": "end", "kind": "text", "declared": "string", "slot": "quoted"}]
    assert [d["name"] for d in marker["declared"]] == ["start", "end", "store"]
    assert marker["source"]["workflow_id"] == wf and marker["source"]["node_id"] == "fetch"
    assert card["id"] == "c1" and card["type"] == "metrics" and card["data"]["label"] == "card"
    # 这张卡这次算哪个指标也记在 口径卡来源 上（K08 S2：metrics 是这次的选择；整卡时这一项不写）
    assert card["data"]["config"]["caliber_from"] == {"workflow_id": wf, "workflow_version": 1, "node_id": "card",
                                                     "nodes": {"fetch": "c1_fetch"}, "metrics": ["gmv"]}
    assert made.edges == [{"source": "c1_fetch", "target": "c1"}]
    # 这次要执行的那条取数只用到 start/end：没用到的 store 不进这次的值（也不填它的缺省值）
    assert made.inputs == {"start": "2026-09-01", "end": "2026-09-08"}
    result = made.result()
    assert result["status"] == "ok" and result["card_id"] == "c1" and result["fetches"] == ["c1_fetch"]
    assert result["level_text"] == "受管" and result["metrics"] == [{"id": "gmv", "name": "销售额"}]
    assert "sql" not in json.dumps(result).lower()


@pytest.mark.parametrize(("change", "words"), [
    ({"workflow_id": "nope"}, "只能选用这一轮查看到的已发布口径"),
    ({"version": 2}, "能选用的是 v1"),
    ({"card": "other"}, "没有口径卡「other」"),
    ({"id": "C 1"}, "简短的英文小写"),
    ({"metrics": []}, "metrics 要写"),
    ({"metrics": ["profit"]}, "没有指标「profit」"),
    ({"inputs": {"start": "2026-09-01", "end": "2026-09-08", "city": "x"}}, "入口没有字段「city」"),
    # 这一版入口声明过、只是不在这次选中的指标的范围里：说法不一样（K08 复核），照样拒
    ({"inputs": {"start": "2026-09-01", "end": "2026-09-08", "store": "east"}}, "不在这次选用的范围里"),
    ({"inputs": {"start": "2026-09-01"}}, "请填写「end」"),
    ({"inputs": {"start": "2026-09-01' OR '1'='1", "end": "2026-09-08"}}, "引号里，值里不能有引号"),
    ({"inputs": "start=2026-09-01"}, "inputs 要写成"),
])
async def test_bad_choices_are_refused_with_a_reason(change, words):
    wf = await S.publish()
    op = use(wf, **change) if change.get("workflow_id") != "nope" else {**use(wf), **change}
    with pytest.raises(compose.ComposeRefused) as e:
        (await composer()).compose(op, taken={"start"})
    assert words in e.value.message, e.value.message


async def test_bare_slots_take_numbers_only_and_ids_must_not_clash():
    graph = S.caliber_graph(sql="SELECT SUM(amount) AS gmv FROM orders WHERE status = 'paid' AND id > {{ input.after }}")
    # 字段没写类型（按文字收）：表单校验放行，裸写进 SQL 的只能是数
    graph["nodes"][0]["data"]["config"]["fields"] = [{"name": "after", "default": "0"}]
    wf = await S.publish(graph=graph)
    pool = await composer()
    with pytest.raises(compose.ComposeRefused, match="只能是数"):
        pool.compose({**use(wf), "inputs": {"after": "1 OR 1=1"}}, taken=set())
    made = pool.compose({**use(wf), "inputs": {"after": "3"}}, taken=set())
    fetch = made.nodes[0]["data"]["config"]
    assert fetch["args"]["sql"].endswith("id > 3")
    assert fetch["parameters"]["parameters"] == [{"name": "after", "kind": "text", "declared": "string", "slot": "bare"}]
    with pytest.raises(compose.ComposeRefused, match="撞了"):
        pool.compose({**use(wf), "inputs": {"after": "3"}}, taken={"c1"})


async def test_a_card_that_reads_model_output_is_listed_but_not_composable():
    graph = S.chain(S.node("start", "input"), S.node("ask", "agent", tools=[S.tool()], assign_to="kpi"),
                    S.node("card", "metrics", caliber="周销售", metrics=[{"id": "gmv", "expression": "vars.kpi.gmv"}]),
                    S.node("out", "output", fields=[{"name": "r", "value": "{{ nodes.card.text }}"}]))
    wf = await S.publish(graph=graph)
    async with SessionLocal() as session:
        kind = await CaliberKind.load(session, compose=True)
    [option] = [o for o in kind.options if o.workflow_id == wf]
    # 读模型写的数：这个指标一个也不可选（原因照实给，不是「没找到」）
    assert not option.composable and "输入不全是查库结果里的格" in option.reason
    assert [m["metric_id"] for m in option.unavailable_metrics] == ["gmv"]
    assert option.unavailable_metrics[0]["reason"] in option.reason
    text, _ = await kind.detail([(i, "named") for i in kind.items if i.workflow_id == wf], 4000)
    assert "不能组合" in text and "输入不全是查库结果里的格" in text
    with pytest.raises(compose.ComposeRefused, match="不能组合"):
        compose.Composer(list(kind.options)).compose(use(wf), taken=set())


# --------------------------------------------------------------------------
# 查看：入口字段、能不能组合，不给 SQL
# --------------------------------------------------------------------------


async def test_the_answer_look_shows_fields_and_how_to_compose_but_no_sql():
    wf = await S.publish()
    async with SessionLocal() as session:
        kind = await CaliberKind.load(session, compose=True)
        plain = await CaliberKind.load(session)
    objects = [(i, "named") for i in kind.items if i.workflow_id == wf]
    text, [record] = await kind.detail(objects, 4000)
    # 入口字段只列这次可选指标要用的那几个（store 没有指标用得到，不列）
    assert "入口字段：start（string，必填）；end（string，必填）" in text and "store" not in text
    assert "能选用的指标：销售额（gmv，要 start、end）" in text and "用不了的指标" not in text
    assert '"op": "use_caliber"' in text and f'"workflow_id": "{wf}"' in text and '"card": "card"' in text
    assert "SELECT" not in text.upper() and "orders" not in text
    assert record["kind"] == "caliber" and record["named"]
    # 蓝图助手那一支（不组合）照旧给复用写法，没有组合写法
    plain_text, _ = await plain.detail([(i, "named") for i in plain.items if i.workflow_id == wf], 4000)
    assert "复用写法" in plain_text and "use_caliber" not in plain_text


async def test_the_answer_prompt_switches_only_when_calibers_can_be_composed():
    from app.look.service import LookService

    assert copilot_context.LOOK_WHEN_ANSWER_PROMPT != copilot_context.LOOK_WHEN_PROMPT
    assert "use_caliber" in copilot_context.LOOK_WHEN_ANSWER_PROMPT and "含退款" in copilot_context.LOOK_WHEN_ANSWER_PROMPT
    async with SessionLocal() as session:
        composing = LookService([await CaliberKind.load(session, compose=True)])
        plain = LookService([await CaliberKind.load(session)])
    assert copilot_context.look_when(composing) == copilot_context.LOOK_WHEN_ANSWER_PROMPT
    assert copilot_context.look_when(plain) == copilot_context.LOOK_WHEN_PROMPT
    assert copilot_context.composer(plain) is None and copilot_context.composer(composing) is not None


# --------------------------------------------------------------------------
# 落地：GraphApplier
# --------------------------------------------------------------------------


async def test_the_applier_lands_the_nodes_wires_the_entry_and_reports_refusals():
    wf = await S.publish()
    nodes: dict = {}
    edges: list = []
    applier = GraphApplier(nodes, edges, composer=await composer())
    op = use(wf)
    applied = applier.apply(op)
    assert applied.changed and applied.forward and op["result"]["status"] == "ok"
    assert set(nodes) == {"c1_fetch", "c1"} and edges == [{"source": "c1_fetch", "target": "c1", "sourceHandle": None}]
    # 入口后加的：加上时接到第一个取数节点；模型再发一条同样的连线不重复
    applier.apply({"op": "add_node", "node": {"id": "start", "type": "input", "label": "入口", "config": {}}})
    assert {"source": "start", "target": "c1_fetch", "sourceHandle": None} in edges
    assert not applier.apply({"op": "add_edge", "edge": {"source": "start", "target": "c1_fetch"}}).changed
    assert len([e for e in edges if e["target"] == "c1_fetch"]) == 1
    # 接着写时重发同一个选用：不再落地
    applier.continuing()
    assert not applier.apply(use(wf)).changed
    assert "c1" in applier.landed()

    bad = use(wf, id="c2", inputs={"start": "2026-09-01"})
    refused = applier.apply(bad)
    assert not refused.changed and refused.forward and bad["result"]["status"] == "refused"
    checker = GraphChecker(applier)
    first = await checker.issues()
    assert [i["code"] for i in first if i["code"] == compose.REFUSED] == [compose.REFUSED]
    assert "请填写「end」" in first[0]["message"] and "重新输出 use_caliber" in first[0]["for_model"]
    # 交回过一次就不再交
    assert not [i for i in await checker.issues() if i["code"] == compose.REFUSED]

    # 兜底重放到上一轮的图上：拷进来的节点和接线照样在
    applier.replay_onto({"nodes": [{"id": "start", "type": "input", "data": {"config": {}}}], "edges": []})
    assert {"c1_fetch", "c1"} <= set(nodes) and any(e["target"] == "c1_fetch" for e in edges)


async def test_without_a_composer_use_caliber_is_dropped_like_any_unknown_op():
    nodes: dict = {}
    applier = GraphApplier(nodes, [])
    applied = applier.apply({"op": "use_caliber", "id": "c1", "workflow_id": "w", "version": 1})
    assert not applied.changed and not applied.forward and nodes == {}


# --------------------------------------------------------------------------
# 问数据走一遍：生成 → 运行 → 比对标 composed
# --------------------------------------------------------------------------


def _answer_script(wf: str) -> list[dict]:
    from tests.test_copilot_answer_ops import _edge, _node

    return [
        {"op": "plan", "summary": "选用周销售口径"},
        _node("start", "input", {}),
        use(wf),
        _node("write", "report", {"instructions": "回答：{{ input.question }}"}),
        _edge("c1", "write"),
        _node("out", "output", {"fields": [{"name": "答案", "value": "{{ nodes.write.text }}"}]}),
        _edge("write", "out"),
        {"op": "done", "explanation": "按口径周销售回答", "run": True},
    ]


async def test_an_answer_built_with_use_caliber_is_labelled_composed(client, monkeypatch):
    from tests.test_copilot_answer_ops import BODY, _Scripted, _sse, _use

    wf = await S.publish()
    _use(monkeypatch, _Scripted(_answer_script(wf), []))
    ops = await _sse(client, {**BODY, "instruction": "九月第一周卖了多少"})
    [chosen] = [o for o in ops if o["op"] == "use_caliber"]
    assert chosen["result"]["status"] == "ok" and chosen["result"]["workflow_name"] == "周销售"
    assert not [o for o in ops if o["op"] == "add_node" and o["node"]["id"] in ("c1", "c1_fetch")], \
        "拷进来的节点不另外转给界面：过程里只有 use_caliber 那一行"
    [final] = [o for o in ops if o["op"] == "final"]
    assert final["next"] == "run", final["issues"]
    graph = final["graph"]
    assert {n["id"] for n in graph["nodes"]} == {"start", "c1_fetch", "c1", "write", "out"}
    assert not [i for i in validate_graph(GraphSpec.model_validate(graph)).issues if i.level == "error"]
    # 走过应用器、写进最终图之后，参数契约还认得出它注解的就是这条 args（K01 的哈希绑定不靠内存里的对象）
    fetch = next(n for n in graph["nodes"] if n["id"] == "c1_fetch")["data"]["config"]
    assert params.literal_keys(fetch, "tool") == ("args",)
    assert fetch["parameters"]["rendered_hash"] == params.rendered_hash(fetch["args"])

    S.script(monkeypatch, "九月第一周销售额 [[m:gmv]]。")
    run = await run_manager.start(graph=graph, input_payload={"question": "九月第一周卖了多少"}, origin_kind="chat")
    row = await S.wait(run.id)
    assert row.status == "succeeded", row.error
    await ledger_match.drain()
    recorded = await store.latest(row.id)
    content = recorded.content
    assert content["algo"] == store.ALGO == "5"
    [query] = content["queries"]
    assert query["status"] == "composed" and query["node_id"] == "c1_fetch"
    [cand] = query["candidates"]
    assert cand["status"] == "proven" and cand["how"] == "composed" and cand["caliber"] == f"{wf}@1"
    [label] = content["reports"]["write"]["labels"].values()
    assert label["kind"] == "caliber" and label["how"] == "composed" and label["queries"] == [query["artifact"]]
    assert content["whole"]["equivalent"] is True and content["whole"]["how"] == "composed"
    assert recorded.content and (await store.latest(row.id)).trigger == "sealed"
    async with SessionLocal() as session:
        from app.db.models import Artifact

        ref = await session.get(Artifact, (recorded.artifact, row.id))
    assert ref.meta["calibers"] == [f"{wf}@1"] and ref.meta["counts"]["composed"] == 1


async def test_users_get_the_authorized_calibers_but_no_use_caliber(monkeypatch):
    """使用者（开登录后的 user 角色）：问数据里**看得到**自己有授权的口径（K14），但没有 use_caliber 那条路。

    看得到的是「按资产授权收窄过的清单 + 入口字段 + 按口径查询工具取数的写法」（look/calibers 的 query 那一档），不是把口径的
    取数 SQL 拷进自己的图（那是搭建者及以上那一档，compose）。"""
    import app.api.copilot as copilot
    from app.api.copilot import GenerateIn
    from app.api import copilot_context

    seen: dict = {}

    async def spy(session, payload, sources, **kw):
        seen.update(kw)
        raise RuntimeError("到这里就够了")

    monkeypatch.setattr(copilot, "_look_service", spy)
    monkeypatch.setattr("app.api.deps.restricted", lambda: True)
    with pytest.raises(RuntimeError):
        await copilot.answer_ops(GenerateIn(instruction="x", intent="answer")).__anext__()
    assert seen["kinds"] == ("table", "capability", "caliber")
    assert seen["caliber"] == copilot_context.CALIBER_QUERY, "使用者这一档只给按口径查询工具取数"
    monkeypatch.setattr("app.api.deps.restricted", lambda: False)
    with pytest.raises(RuntimeError):
        await copilot.answer_ops(GenerateIn(instruction="x", intent="answer")).__anext__()
    assert seen["kinds"] == ("table", "capability", "caliber")
    assert seen["caliber"] == copilot_context.CALIBER_COMPOSE, "搭建者及以上照旧能 use_caliber"
    with pytest.raises(RuntimeError):
        await copilot.answer_ops(GenerateIn(instruction="x", intent="build")).__anext__()
    assert seen["kinds"] == ("table", "capability")
    assert seen["caliber"] == copilot_context.CALIBER_LOOK, "画布那一档根本不看口径"


# --------------------------------------------------------------------------
# card 填什么：协议写清两个 id，写错了交回的原因能一次改对（校验不放宽）
# --------------------------------------------------------------------------


def test_the_protocol_says_card_is_the_cards_node_id_and_id_is_the_name_you_give():
    text = copilot_context.USE_CALIBER_PROMPT
    assert '"card":"card"' in text and '"card":"口径卡节点 id"' not in text
    assert "card 填那一版工作流图里口径卡的节点 id" in text and "不是你起的名字" in text
    assert "id 是你给这次选用起的名字" in text and "workflow_id、version、card 原样照抄" in text


def test_a_user_round_refuses_use_caliber_and_points_at_the_query_tool():
    """使用者那一轮看得见口径但不能选用：use_caliber 照实拒收、自查交回模型改用口径查询工具，不静默丢掉。

    没有口径这一档的入口（画布、发布前修复、升级）照旧：use_caliber 是认不出的操作，不进拒收。"""
    from app.api.copilot_gen import NO_COMPOSE_TEXT, GraphApplier, _refused_issues

    op = {"op": "use_caliber", "id": "c1", "workflow_id": "wf", "version": 1, "card": "card",
          "metrics": ["gmv"], "inputs": {}}
    user = GraphApplier({}, [], composer=None, no_compose=True)
    assert user.apply(dict(op)).changed is False
    [issue] = _refused_issues(user)
    assert issue["level"] == "error" and NO_COMPOSE_TEXT in issue["message"]
    assert "use_caliber" in issue["for_model"] and "caliber_query" in issue["for_model"]
    assert user.nodes == {} and user.edges == [], "拒收的选用一个节点、一条连线都不落地"

    plain = GraphApplier({}, [])
    plain.apply(dict(op))
    assert _refused_issues(plain) == []


async def test_writing_your_own_id_as_the_card_is_refused_with_what_to_write_instead():
    wf = await S.publish()
    pool = await composer()
    with pytest.raises(compose.ComposeRefused) as e:
        pool.compose(use(wf, card="c1"), taken={"start"})
    message = e.value.message
    assert "没有口径卡「c1」" in message and "card 要填那一版工作流图里口径卡的节点 id" in message
    assert "可填的有 「card」（口径卡「card」）" in message, message
    assert "「c1」是你给这次选用起的 id，不是口径卡的节点 id" in message
    # 别的写错（不是自己起的 id）不多那一句
    with pytest.raises(compose.ComposeRefused) as other:
        pool.compose(use(wf, card="other"), taken={"start"})
    assert "是你给这次选用起的 id" not in other.value.message
    # 校验不放宽：照着交回的原因改成节点 id 就过
    made = pool.compose(use(wf, card="card"), taken={"start"})
    assert made.op_id == "c1"


# --------------------------------------------------------------------------
# 只算选中的指标（K08 S2）：op.metrics 贯穿参数校验与拷贝
# --------------------------------------------------------------------------

#: 只有 B 那条查询用的字段（region）：只选 A 时它不该被要求
FIELDS_AB = [{"name": "start", "required": True}, {"name": "end", "required": True},
             {"name": "buyer", "required": True}]
SQL_A = ("SELECT SUM(amount) AS gmv FROM orders WHERE status = 'paid' "
         "AND paid_at >= '{{ input.start }}' AND paid_at < '{{ input.end }}'")
SQL_B = SQL_A + " AND region = '{{ input.buyer }}'"


def two_metric_graph(*, on_missing: bool = False, a_sql: str = SQL_A, b_sql: str = SQL_B) -> dict:
    """一张卡两个指标、两条各自独立的查询；buyer 只有 B 那条用（A 不需要它）。"""
    card: dict = {"caliber": "双指标", "caliber_version": "v1",
                  "metrics": [{"id": "gmv", "name": "销售额", "expression": "cell(nodes.fetch_a, 0, 'gmv')"},
                              {"id": "n", "name": "单量", "expression": "cell(nodes.fetch_b, 0, 'n')"}]}
    if on_missing:
        card["on_missing"] = "null"
    return S.chain(S.node("start", "input", fields=FIELDS_AB),
                   S.node("fetch_a", "tool", tool=S.tool(), args={"sql": a_sql}),
                   S.node("fetch_b", "tool", tool=S.tool(), args={"sql": b_sql}),
                   S.node("card", "metrics", **card),
                   S.node("out", "output", fields=[{"name": "报告", "value": "{{ nodes.write.text }}"}]),
                   S.node("write", "report", instructions="写周销售"))


def shared_graph() -> dict:
    """两个指标读同一条取数（共享 SQL）：选一个时那条 SQL 保留，还在算另一个。"""
    return S.chain(S.node("start", "input", fields=FIELDS_AB[:2]),
                   S.node("fetch", "tool", tool=S.tool(), args={"sql": SQL_A}),
                   S.node("card", "metrics", caliber="共享取数", caliber_version="v1",
                          metrics=[{"id": "gmv", "name": "销售额", "expression": "cell(nodes.fetch, 0, 'gmv')"},
                                   {"id": "n", "name": "单量", "expression": "cell(nodes.fetch, 0, 'n')"}]),
                   S.node("out", "output"))


def select(wf: str, metrics: list[str], inputs: dict | None = None, *, id: str = "c1") -> dict:
    return {"op": "use_caliber", "id": id, "workflow_id": wf, "version": 1, "card": "card",
            "metrics": metrics, "inputs": inputs if inputs is not None else
            {"start": "2026-09-01", "end": "2026-09-08"}}


async def test_selecting_one_metric_no_longer_asks_for_the_other_query_s_parameters():
    """op.metrics 决定这次真正要执行哪些取数：只选 A 时不再要求 B 的参数，也只拷 A 那条查询。"""
    wf = await S.publish(graph=two_metric_graph())
    pool = await composer()
    made = pool.compose(select(wf, ["gmv"]), taken={"start"})
    assert [n["id"] for n in made.nodes] == ["c1_fetch_a", "c1"]
    card = made.nodes[-1]["data"]["config"]
    assert card["caliber_from"] == {"workflow_id": wf, "workflow_version": 1, "node_id": "card",
                                   "nodes": {"fetch_a": "c1_fetch_a"}, "metrics": ["gmv"]}
    assert made.inputs == {"start": "2026-09-01", "end": "2026-09-08"}
    assert made.fetch_ids == ["c1_fetch_a"]
    # 整卡（两个指标）时还是要求 B 的参数：不收窄、也不静默放宽
    with pytest.raises(compose.ComposeRefused, match="buyer"):
        pool.compose(select(wf, ["gmv", "n"], id="c2"), taken={"start"})
    # 结果带上这次选择的身份与排除的查询
    result = made.result()
    assert result["selection"]["mode"] == "explicit" and result["selection"]["metric_ids"] == ["gmv"]
    assert result["selection"]["query_ids"] == [f"{wf}:fetch_a"]
    assert result["selection"]["excluded_query_ids"] == [f"{wf}:fetch_b"]
    assert result["selection"]["selection_hash"] and result["selection"]["closure_hash"]
    assert "shared_queries" not in result
    # 拷进来的图能过画布校验（口径卡上多记的选择不影响它）
    nodes = [S.node("start", "input", fields=[{"name": "x"}]), *made.nodes,
             S.node("write", "report", instructions="答"), S.node("out", "output")]
    issues = validate_graph(GraphSpec.model_validate(S.chain(*nodes))).issues
    assert not [i for i in issues if i.level == "error"], issues


async def test_a_shared_query_is_kept_and_declared_as_still_computing_the_unselected_metric():
    """A 和 B 共用一条 SQL：选 A 也保留那条 SQL，并在结果里声明它还在算 B（不做投影）。"""
    wf = await S.publish(graph=shared_graph())
    made = (await composer()).compose(select(wf, ["n"]), taken={"start"})
    assert [n["id"] for n in made.nodes] == ["c1_fetch", "c1"]
    assert made.nodes[-1]["data"]["config"]["caliber_from"]["metrics"] == ["n"]
    result = made.result()
    assert result["selection"]["metric_ids"] == ["n"] and result["selection"]["excluded_query_ids"] == []
    assert result["shared_queries"] == [{"query_id": f"{wf}:fetch", "node_id": "fetch",
                                         "computes_unselected": ["gmv"]}]


async def test_a_selection_whose_dependencies_do_not_resolve_is_refused_not_widened():
    """点名的指标一个取数也不依赖（解析不出闭包）：拒收，不悄悄退回整卡。"""
    graph = S.chain(S.node("start", "input", fields=FIELDS_AB[:2]),
                    S.node("fetch", "tool", tool=S.tool(), args={"sql": SQL_A}),
                    S.node("card", "metrics", caliber="半个常量", caliber_version="v1",
                           metrics=[{"id": "gmv", "name": "销售额", "expression": "cell(nodes.fetch, 0, 'gmv')"},
                                    {"id": "constant", "name": "常量", "expression": "1 + 1"}]),
                    S.node("out", "output"))
    wf = await S.publish(graph=graph)
    pool = await composer()
    # 整卡两张指标都在：gmv 那条查询照拷（常量指标不牵连它）
    ok = pool.compose(select(wf, ["gmv"]), taken={"start"})
    assert [n["id"] for n in ok.nodes] == ["c1_fetch", "c1"]
    with pytest.raises(compose.ComposeRefused) as caught:
        pool.compose(select(wf, ["constant"], id="c2"), taken={"start"})
    assert "no_query_dependency" in caught.value.message and "constant" in caught.value.message


async def test_check_inputs_takes_the_same_selection_by_itself():
    """check_inputs 也能直接按 metrics 收窄（callers 不必先裁一份）：只选 A 时不要求 B 的参数。"""
    from app.ledger import derive

    wf = await S.publish(graph=two_metric_graph())
    async with SessionLocal() as session:
        kind = await CaliberKind.load(session, compose=True)
    [option] = [o for o in kind.options if o.workflow_id == wf]
    pinned = option.pinned
    values = compose.check_inputs(pinned, {"start": "2026-09-01", "end": "2026-09-08"}, metric_ids=["gmv"])
    assert values == {"start": "2026-09-01", "end": "2026-09-08"}
    with pytest.raises(compose.ComposeRefused, match="buyer"):
        compose.check_inputs(pinned, {"start": "2026-09-01", "end": "2026-09-08"})
    # caliber_block 拿同一份选择：改名映射只列保留的那条查询，卡上记下这次算哪个指标
    block = derive.caliber_block(pinned, prefix="c1_", metrics=["gmv"])
    assert [n["id"] for n in block.nodes] == ["c1_fetch_a", "c1_card"]
    assert block.nodes[-1]["data"]["config"]["caliber_from"]["metrics"] == ["gmv"]


# --------------------------------------------------------------------------
# 按指标判可组合性（K08 S4）：B 用不了不牵连 A；一次选用按源卡顺序
# --------------------------------------------------------------------------


async def test_a_broken_query_of_one_metric_leaves_the_other_composable():
    """B 那条查询引用了入口没声明的字段：B 用不了，A 照旧可选、能组合、能实例化；不再把整张卡提前判死。"""
    wf = await S.publish(graph=two_metric_graph(b_sql=SQL_B + " AND note = '{{ input.nope }}'"))
    pool = await composer()
    [option] = [o for o in pool.options if o.workflow_id == wf]
    assert option.composable and [m["metric_id"] for m in option.selectable_metrics] == ["gmv"]
    assert [m["metric_id"] for m in option.unavailable_metrics] == ["n"]
    assert "undeclared_parameter" in option.unavailable_metrics[0]["reason"]
    # 不点名（整卡）那一份算不出来：原因照实给，但按指标还能用
    assert option.default_closure is None and "undeclared_parameter" in option.reason
    [sel] = option.selectable_metrics
    assert sel["query_ids"] == [f"{wf}:fetch_a"] and sel["required_entry_fields"] == ["start", "end"]
    blob = json.dumps(sel, ensure_ascii=False)
    assert "SUM(" not in blob and "orders" not in blob and "paid_at" not in blob and '"sql"' not in blob
    # 能选的照旧实例化（只拷 A 那条查询）；不能选的逐项硬校验拒收，不靠预筛放宽
    made = pool.compose(select(wf, ["gmv"]), taken={"start"})
    assert [n["id"] for n in made.nodes] == ["c1_fetch_a", "c1"]
    with pytest.raises(compose.ComposeRefused) as caught:
        pool.compose(select(wf, ["n"], id="c2"), taken={"start"})
    assert "undeclared_parameter" in caught.value.message
    # 查看结果：能选的写要哪些字段，用不了的写原因；整段没有 SQL
    async with SessionLocal() as session:
        kind = await CaliberKind.load(session, compose=True)
    text, _ = await kind.detail([(i, "named") for i in kind.items if i.workflow_id == wf], 4000)
    assert "能选用的指标：销售额（gmv，要 start、end）" in text
    assert "用不了的指标：单量（n）：" in text and "undeclared_parameter" in text
    assert "SELECT" not in text.upper() and "orders" not in text


async def test_a_metric_whose_binding_is_out_of_step_does_not_close_the_whole_card():
    """B 声明了业务字段（统计时间口径）但那条取数没有受控绑定：B 用不了，A 照旧可选、能实例化（K08 S4）。"""
    graph = two_metric_graph()
    card = next(n for n in graph["nodes"] if n["type"] == "metrics")
    card["data"]["config"]["metrics"][1]["time_role"] = "payment_time"          # B 声明了，可是 fetch_b 上没有绑定
    wf = await S.publish(graph=graph)
    pool = await composer()
    [option] = [o for o in pool.options if o.workflow_id == wf]
    assert option.composable and [m["metric_id"] for m in option.selectable_metrics] == ["gmv"]
    [bad] = option.unavailable_metrics
    assert bad["metric_id"] == "n" and "受控绑定" in bad["reason"]
    assert option.default_closure is None and "受控绑定" in option.reason
    made = pool.compose(select(wf, ["gmv"]), taken={"start"})
    assert [n["id"] for n in made.nodes] == ["c1_fetch_a", "c1"]
    with pytest.raises(compose.ComposeRefused) as caught:
        pool.compose(select(wf, ["n"], id="c2"), taken={"start"})
    assert "binding_unresolved" in caught.value.message


async def test_a_card_whose_metrics_all_fail_is_not_offered_for_composition():
    """两个指标都用不了：不提供组合写法、也不实例化，原因与每个指标的原因照实给。"""
    graph = two_metric_graph(a_sql=SQL_A + " AND note = '{{ input.nope }}'",
                             b_sql=SQL_B + " AND note = '{{ input.nope }}'")
    wf = await S.publish(graph=graph)
    pool = await composer()
    [option] = [o for o in pool.options if o.workflow_id == wf]
    assert not option.composable and option.pinned is None and option.selectable_metrics == ()
    assert [m["metric_id"] for m in option.unavailable_metrics] == ["gmv", "n"]
    assert "undeclared_parameter" in option.reason
    with pytest.raises(compose.ComposeRefused, match="不能组合"):
        pool.compose(select(wf, ["gmv"]), taken={"start"})
    async with SessionLocal() as session:
        kind = await CaliberKind.load(session, compose=True)
    text, _ = await kind.detail([(i, "named") for i in kind.items if i.workflow_id == wf], 4000)
    assert "不能组合" in text and "undeclared_parameter" in text and "SELECT" not in text.upper()


async def test_a_selection_is_normalized_to_the_source_card_order():
    """一次选用里的指标一律按源卡顺序：Composed.metrics、口径卡上记的 metrics、selection_hash 与执行数组都不随
    调用方传的顺序、重复项变（K08 S4）。"""
    wf = await S.publish(graph=two_metric_graph(on_missing=True))
    pool = await composer()
    both = {"start": "2026-09-01", "end": "2026-09-08", "buyer": "east"}
    forward = pool.compose(select(wf, ["gmv", "n"], both), taken=set())
    backward = pool.compose(select(wf, ["n", "gmv", "n"], both), taken=set())
    assert [m["id"] for m in forward.metrics] == [m["id"] for m in backward.metrics] == ["gmv", "n"]
    assert forward.result()["selection"]["metric_ids"] == ["gmv", "n"]
    assert forward.nodes[-1]["data"]["config"]["caliber_from"]["metrics"] == ["gmv", "n"]
    assert forward.nodes == backward.nodes and forward.edges == backward.edges
    assert forward.result()["selection"]["selection_hash"] == backward.result()["selection"]["selection_hash"]
    assert forward.result()["selection"]["closure_hash"] == backward.result()["selection"]["closure_hash"]


async def test_the_offered_option_follows_the_recommended_version():
    """K07：问数据里看到的是推荐发布版（Workflow.published_version），不是机械取最高已发布版；按指标的可选性也按那一版。"""
    from app.db.models import Workflow

    wf = await S.publish(graph=two_metric_graph())                                    # v1：两个指标都读得出来
    await S.publish(name="周销售", version=2, workflow_id=wf,
                    graph=two_metric_graph(b_sql=SQL_B + " AND note = '{{ input.nope }}'"))   # v2：B 坏了
    async with SessionLocal() as session:
        row = await session.get(Workflow, wf)
        row.published_version = 1                                                     # 重新推回 v1
        await session.commit()
        kind = await CaliberKind.load(session, compose=True)
    assert {i.version for i in kind.items if i.workflow_id == wf} == {1}
    [option] = [o for o in kind.options if o.workflow_id == wf]
    assert option.version == 1 and option.composable and option.unavailable_metrics == ()
    assert [m["metric_id"] for m in option.selectable_metrics] == ["gmv", "n"]
    assert option.default_closure["query_ids"] == [f"{wf}:fetch_a", f"{wf}:fetch_b"]


# --------------------------------------------------------------------------
# 复核：重复的指标 id、字段「不在本次选择范围」与「入口没这个字段」
# --------------------------------------------------------------------------


async def test_a_card_with_a_duplicated_metric_id_is_never_offered_for_composition():
    """同 id 指向两条不同查询的卡：look 一个指标也不提供（理由照实）、实例化也不放行（K08 复核）。"""
    graph = two_metric_graph()
    card = next(n for n in graph["nodes"] if n["type"] == "metrics")
    card["data"]["config"]["metrics"][1]["id"] = "gmv"          # 两个 gmv：一个读 fetch_a、一个读 fetch_b
    wf = await S.publish(graph=graph)
    pool = await composer()
    [option] = [o for o in pool.options if o.workflow_id == wf]
    assert not option.composable and option.pinned is None and option.selectable_metrics == ()
    assert [m["metric_id"] for m in option.unavailable_metrics] == ["gmv", "gmv"]
    assert all("duplicate_metric_id" in m["reason"] for m in option.unavailable_metrics)
    assert "duplicate_metric_id" in (option.reason or "")
    with pytest.raises(compose.ComposeRefused, match="不能组合"):
        pool.compose(select(wf, ["gmv"]), taken={"start"})
    async with SessionLocal() as session:
        kind = await CaliberKind.load(session, compose=True)
    text, _ = await kind.detail([(i, "named") for i in kind.items if i.workflow_id == wf], 4000)
    assert "不能组合" in text and "duplicate_metric_id" in text and "SELECT" not in text.upper()


async def test_a_field_of_another_metric_is_out_of_scope_not_missing():
    """这一版入口声明过、只是不在这次选中的指标的范围里：说法是「不在这次选用的范围里」，不是「入口没有字段」；
    真的没声明过的字段照旧说「入口没有字段」（K08 复核）。两种都拒，行为不变。"""
    wf = await S.publish(graph=two_metric_graph(on_missing=True))
    pool = await composer()
    both_fields = {"start": "2026-09-01", "end": "2026-09-08", "buyer": "east"}
    with pytest.raises(compose.ComposeRefused) as caught:
        pool.compose(select(wf, ["gmv"], both_fields), taken=set())
    message = caught.value.message
    assert "「buyer」这一版入口声明过，但不在这次选用的范围里" in message
    assert "这次算 gmv，只要 start、end" in message and "入口没有字段" not in message
    with pytest.raises(compose.ComposeRefused) as unknown:
        pool.compose(select(wf, ["gmv"], {"start": "2026-09-01", "end": "2026-09-08", "买了没": "x"}, id="c2"),
                     taken=set())
    assert "入口没有字段「买了没」" in unknown.value.message and "不在这次选用的范围里" not in unknown.value.message
    # 两张指标都选上时 buyer 就在这次的范围里：照旧收（整卡老行为不变）
    made = pool.compose(select(wf, ["gmv", "n"], both_fields, id="c3"), taken=set())
    assert made.inputs == both_fields
