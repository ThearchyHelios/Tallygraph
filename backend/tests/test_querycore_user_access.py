"""K14：普通使用者按口径取数的统一授权、服务端补上的工具绑定、只列有授权的口径清单、服务端准备的那条执行路。

这份文件盯住这几条：

1. **授权在 QueryCore 一处兜底。** compose 这一档（问数、固定请求路由、自动路由、父 Run 的工具调用）在**固定 workflow +
   version 之后、起 Run 之前**核资产授权（apps/access.caliber_role：同一张 AppGrant + 应用当前发布版精确固定着这一版）：
   use 及以上才放行，没有授权当场 403 `querycore_not_authorized`，**一条运行都不起**；builder / admin / 关登录由调用方
   显式放行；actor 缺失（认不出是谁）按普通使用者算，照拒。
2. **行级范围照授权的那一档。** use 只看自己提交的记录，observe 看全部（records/access.scope_of_run：口径里的记录表数据源）。
3. **工具是服务端绑上的、模型看得见。** 有授权口径可用时 chat/launch 把 `caliber_query` 补进 Agent 的 tools（用户不用在
   画布上画）；auth/chat_policy 的白名单允许这个只读工具（模型清单里列得出来），能不能执行仍由 QueryCore 按授权判。
4. **口径清单只列有授权的版本。** look/calibers 对普通使用者只列 `usable_calibers` 里那些版本（builder 不变）。
5. **服务端准备的执行有显式绑定、白名单、遮罩、只读与父 Run 证据。** `execute_prepared` 搭最小的父图、经 `user_chat_run`
   起一次探索父 Run，实际从 `caliber_query` 工具走（派生的子运行就是那次固定执行），交回与 `execute` 同形的 Outcome。

数据全是合成的（临时 SQLite 库、合成标量指标），不连真实业务库；这些指标不是任何真实业务口径。
"""
from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.api import deps
from app.api.deps import Principal
from app.apps.access import usable_calibers
from app.auth import chat_policy
from app.chat.launch import bind_caliber_tool, user_chat_run
from app.db.base import SessionLocal
from app.db.models import Run
from app.engine.runner import run_manager
from app.ledger import nlchange, querycore
from app.look.calibers import CaliberKind, permitted_versions
from app.main import app
from app.tools.datasource import MASK_RESULTS_OPTION, READ_ONLY_OPTION
from tests import ledger_samples as S
from tests.fakes import auth_env
from tests.publication_samples import add_record, add_version, confirm, make_workflow, publish
from tests.test_caliber_query_tool import grant_caliber, records_caliber_graph, records_source

#: 口径卡的节点 id（源头口径卡）
CARD = "card"
#: 取数窗口：start（含）/ end（不含）
WINDOW = "AND paid_at >= '{{ input.start }}' AND paid_at < '{{ input.end }}'"
#: 这次入口取值：2026-09-01 ≤ paid_at < 2026-09-08，只算 paid：100 + 50.5 + 20
INPUTS = {"start": "2026-09-01", "end": "2026-09-08"}
WINDOW_SUM = 170.5


def upload_graph(*, include_open: bool = False) -> dict[str, Any]:
    """入口 → 一条取数（上传表格）→ 口径卡 → 报告 → 出口。这张表是 ledger_samples 的虚构店铺库。

    include_open 再加一档状态（v2 的口径）：换个算法就换了取数（等价于「图上改了东西」），让两版各有自己的清单与结果。"""
    status = "status IN ('paid', 'open')" if include_open else "status = 'paid'"
    return S.chain(
        S.node("start", "input", fields=[{"name": "start", "type": "string", "required": True},
                                         {"name": "end", "type": "string", "required": True}]),
        S.node("sales", "tool", tool=S.tool(),
               args={"sql": f"SELECT SUM(amount) AS gmv, count(*) AS orders FROM orders "
                            f"WHERE {status} {WINDOW}"}),
        S.node(CARD, "metrics", caliber="合成周销售", caliber_version="v1", metrics=[
            {"id": "gmv", "name": "销售额", "unit": "元", "decimals": 2, "expression": "cell(nodes.sales, 0, 'gmv')"},
            {"id": "orders", "name": "单数", "unit": "单", "expression": "cell(nodes.sales, 0, 'orders')"},
        ]),
        S.node("write", "report", instructions="写周销售"),
        S.node("out", "output", fields=[{"name": "报告", "value": "{{ nodes.write.text }}"}]),
    )


def agent_graph(tools: tuple[str, ...] = ()) -> dict[str, Any]:
    """一张普通使用者搭得出来的问数图：入口 → Agent（绑数据源查询）→ 出口。"""
    return S.chain(
        S.node("start", "input", fields=[{"name": "question"}]),
        S.node("ask", "agent", prompt="{{ input.question }}", tools=list(tools) or [S.tool()], max_steps=4),
        S.node("out", "output", fields=[{"name": "回答", "value": "{{ nodes.ask.text }}"}]),
    )


def values_of(outcome: querycore.Outcome) -> dict[str, Any]:
    return {m["id"]: m["value"] for m in outcome.as_dict()["metrics"]}


async def execute(asset: dict, *, metric_ids: list[str] | None = None, inputs: dict[str, Any] | None = None,
                  **kw: Any) -> querycore.Outcome:
    """走服务入口跑一次（agent 之外的那条路）：不给 actor 时按当前请求的身份算（HTTP 入口就是这样）。"""
    async with SessionLocal() as session:
        return await querycore.execute(session, workflow_id=asset["workflow_id"], card_id=CARD,
                                       metric_ids=metric_ids or ["gmv"],
                                       inputs=INPUTS if inputs is None else inputs, **kw)


def as_user(name: str = "uma", role: str = "user") -> Any:
    """这次请求的人是这个人（HTTP 中间件写的就是它）；返回 token，用完 reset。"""
    return deps.set_principal(Principal(username=name, role=role, verified=True, mode="local"))


async def runs_matching(*, workflow_id: str | None = None, started_by: str | None = None) -> int:
    """库里符合这几个条件的运行条数：用例按**自己**的口径或身份数，别的用例、别的 worker 留下的行串不进来。"""
    stmt = select(Run.id)
    if workflow_id is not None:
        stmt = stmt.where(Run.workflow_id == workflow_id)
    if started_by is not None:
        stmt = stmt.where(Run.started_by == started_by)
    async with SessionLocal() as session:
        return len(list((await session.execute(stmt)).scalars()))


async def add_record_row(source_id: str, actor: str) -> None:
    """往这张记录表里追加一行（谁提交的）：行级范围是 mine 时别人看不见它。"""
    from app.records import store
    from app.records.tables import columns_of, path_of, table_of_source

    async with SessionLocal() as session:
        table = await table_of_source(session, source_id)
    store.append(path_of(table), columns_of(table), {"门店": "二店", "数量": 2}, version=table.version,
                 run_id=None, actor=actor, action_key=uuid.uuid4().hex)


async def app_of_source(source_id: str) -> str:
    from app.records.tables import table_of_source

    async with SessionLocal() as session:
        table = await table_of_source(session, source_id)
        return str(table.app_id)


@pytest.fixture(autouse=True)
async def engine_up(tmp_path):
    await run_manager.setup()
    await S.fresh_source(tmp_path / "user_access_orders.db")
    yield
    await run_manager.shutdown()


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


# ==========================================================================
# 1、授权：use 能用、observe 看全部、没有授权当场拒
# ==========================================================================


async def test_use_sees_only_their_own_rows_and_observe_sees_all(client):
    """有授权（use）才跑得成，且只看得到自己提交的记录；换成 observe 就看全部——两处都是同一份授权判出来的。"""
    from app.db.models_apps import AppGrant

    admin = await auth_env.turn_on_local()
    try:
        await auth_env.make_user("uma", "user")
        source_id, name = await records_source(grants={"uma": "use"})
        await add_record_row(source_id, "别人")         # 别人的一行：use 看不见，observe 看得见
        asset = await make_workflow(graph=records_caliber_graph(name))
        await publish(admin, asset)
        await grant_caliber(asset["workflow_id"], 1, "uma", "use")

        token = as_user("uma")
        try:
            use = await execute(asset, metric_ids=["n"], inputs={}, actor="uma")
        finally:
            deps.reset_principal(token)
        assert use.ok, use.problems
        assert values_of(use) == {"n": 1}, "use 档只看自己提交的那一行"

        async with SessionLocal() as session:
            session.add(AppGrant(app_id=await app_of_source(source_id), principal_kind="user", principal="uma",
                                 role="observe"))
            await session.commit()
        token = as_user("uma")
        try:
            observe = await execute(asset, metric_ids=["n"], inputs={}, actor="uma")
        finally:
            deps.reset_principal(token)
        assert observe.ok, observe.problems
        assert values_of(observe) == {"n": 2}, "observe 档看全部"
    finally:
        await admin.aclose()
        await auth_env.reset()


async def test_an_unauthorized_caliber_is_refused_before_any_run(client):
    """没有授权：上传表格那一档口径也当场拒（403 querycore_not_authorized），**一条运行都不起**。"""
    admin = await auth_env.turn_on_local()
    try:
        await auth_env.make_user("uma", "user")
        asset = await make_workflow(graph=upload_graph())
        await publish(admin, asset)
        before = await runs_matching(workflow_id=asset["workflow_id"])
        token = as_user("uma")
        try:
            with pytest.raises(querycore.QueryCoreRefused) as caught:
                await execute(asset, actor=None)
        finally:
            deps.reset_principal(token)
        assert caught.value.code == querycore.NOT_AUTHORIZED and caught.value.status_code == 403
        assert caught.value.problems[0]["version"] == 1 and caught.value.problems[0]["workflow_id"] == asset["workflow_id"]
        assert await runs_matching(workflow_id=asset["workflow_id"]) == before, "起 Run 之前就拒：这次没留下运行"

        # 给它一份授权（应用当前发布版固定着这一版 + 至少 use）之后同样这条路就通了
        await grant_caliber(asset["workflow_id"], 1, "uma", "use")
        token = as_user("uma")
        try:
            allowed = await execute(asset)
        finally:
            deps.reset_principal(token)
        assert allowed.ok and values_of(allowed) == {"gmv": WINDOW_SUM}
    finally:
        await admin.aclose()
        await auth_env.reset()


async def test_a_fixed_request_cannot_bypass_the_asset_grant(client):
    """固定请求路由（K12）交回的那条固定请求照样过同一道授权：换一个入口绕不过去，也不回退旧 Agent 路径。"""
    admin = await auth_env.turn_on_local()
    try:
        await auth_env.make_user("uma", "user")
        asset = await make_workflow(graph=upload_graph())
        await publish(admin, asset)
        request = {"workflow_id": asset["workflow_id"], "card_id": CARD, "metric_ids": ["gmv"], "inputs": INPUTS}
        async with SessionLocal() as session:
            routed = await nlchange.route(session, request=request)
            assert routed.mode == nlchange.ROUTE_EXECUTE and routed.request["version"] == 1
            call = {key: value for key, value in routed.request.items() if key != "purpose"}

        before = await runs_matching(workflow_id=asset["workflow_id"])
        token = as_user("uma")
        try:
            async with SessionLocal() as session:
                with pytest.raises(querycore.QueryCoreRefused) as caught:
                    await querycore.execute(session, purpose=str(routed.request["purpose"]), **call)
                assert caught.value.code == querycore.NOT_AUTHORIZED
                assert await runs_matching(workflow_id=asset["workflow_id"]) == before, "这条路上也没有起任何运行"
                await grant_caliber(asset["workflow_id"], 1, "uma", "use")
                outcome = await querycore.execute(session, purpose=str(routed.request["purpose"]), **call)
        finally:
            deps.reset_principal(token)
        assert outcome.ok and values_of(outcome) == {"gmv": WINDOW_SUM}
    finally:
        await admin.aclose()
        await auth_env.reset()


async def test_a_missing_actor_is_refused_when_login_is_on(client):
    """开了登录却认不出这次是谁：按普通使用者算——资产授权查不到主体就是没有授权，照拒（不拿「认不出」当放行）。"""
    admin = await auth_env.turn_on_local()
    try:
        asset = await make_workflow(graph=upload_graph())
        await publish(admin, asset)
        token = deps.set_principal(None)
        try:
            with pytest.raises(querycore.QueryCoreRefused) as caught:
                await execute(asset)
        finally:
            deps.reset_principal(token)
        assert caught.value.code == querycore.NOT_AUTHORIZED
    finally:
        await admin.aclose()
        await auth_env.reset()


async def test_builder_and_admin_are_explicitly_allowed(client):
    """builder / admin 由调用方显式放行：它们不按资产授权收窄（那几档不写进资产授权函数），没有授权也跑得成。"""
    admin = await auth_env.turn_on_local()
    try:
        builder = f"b{uuid.uuid4().hex[:8]}"          # 这两个身份自己的一份：别的用例留下的授权串不进来
        await auth_env.make_user(builder, "builder")
        top_name = f"a{uuid.uuid4().hex[:8]}"
        await auth_env.make_user(top_name, "admin")
        asset = await make_workflow(graph=upload_graph())
        await publish(admin, asset)
        async with SessionLocal() as session:
            # 按**这一版**看：别的工作流上「给所有人」的授权留在库里（测试会话共用一个库），不影响这一版没有授权
            assert (asset["workflow_id"], 1) not in await usable_calibers(session, builder)
        built = await execute(asset, actor=builder)
        assert built.ok and values_of(built) == {"gmv": WINDOW_SUM}
        token = as_user(top_name, role="admin")
        try:
            top = await execute(asset)
        finally:
            deps.reset_principal(token)
        assert top.ok and values_of(top) == {"gmv": WINDOW_SUM}
    finally:
        await admin.aclose()
        await auth_env.reset()


# ==========================================================================
# 2、工具是服务端绑的、白名单允许它
# ==========================================================================


async def test_the_agent_gets_the_caliber_tool_only_when_there_is_one_to_use(client):
    """有授权口径可用时把 caliber_query 补进 Agent 的 tools（改的是副本，用户的图不动）；一份也没有、或者不是普通使用者就不补。

    「一份也没有」用**停用**的身份来立：测试会话共用一个库，别的用例给「所有人」的授权（AppGrant.principal_kind=everyone）
    会留在库里、命中任何名字——停用的身份在资产授权那一层就是没有主体（apps/access._subject），不受那些授权影响。"""
    admin = await auth_env.turn_on_local()
    try:
        who = f"u{uuid.uuid4().hex[:8]}"          # 这个身份自己的一份
        await auth_env.make_user(who, "user")
        gone = f"u{uuid.uuid4().hex[:8]}"
        await auth_env.make_user(gone, "user", disabled=True)
        await auth_env.make_user("bob", "builder")
        asset = await make_workflow(graph=upload_graph())
        await publish(admin, asset)
        graph = agent_graph()
        async with SessionLocal() as session:
            # 一份也没有（停用的人）：原样交回（不给模型一个一调就被拒的工具）
            assert await bind_caliber_tool(session, graph, gone) == graph
            # builder：它本来就能自己在画布上绑，不替它补
            assert await bind_caliber_tool(session, graph, "bob") == graph
            await grant_caliber(asset["workflow_id"], 1, who, "use")
            bound = await bind_caliber_tool(session, graph, who)
        tools = bound["nodes"][1]["data"]["config"]["tools"]
        assert tools == [S.tool(), querycore.CALIBER_QUERY], tools
        assert graph["nodes"][1]["data"]["config"]["tools"] == [S.tool()], "补的是副本，调用方的图一个字不动"
        assert chat_policy.chat_graph_problem(bound) is None, "白名单允许这个只读工具（模型清单里列得出来）"
        assert querycore.CALIBER_QUERY in chat_policy.CHAT_BUILTIN_TOOLS
        from app.api.copilot import _tool_catalog

        listing = _tool_catalog([], builtins=chat_policy.CHAT_BUILTIN_TOOLS)
        assert querycore.CALIBER_QUERY in listing, "使用者的提示词里列得出口径查询：模型看得到它"

        async with SessionLocal() as session:
            memory_scope, options, running = await user_chat_run(session, graph, who)
        assert memory_scope == f"chat:{who}" and options == {MASK_RESULTS_OPTION: True, READ_ONLY_OPTION: True}
        assert querycore.CALIBER_QUERY in running["nodes"][1]["data"]["config"]["tools"]
    finally:
        await admin.aclose()
        await auth_env.reset()


# ==========================================================================
# 3、口径清单只列有授权的版本
# ==========================================================================


async def test_look_lists_only_authorized_versions_for_a_plain_user(client):
    """look/calibers：普通使用者只列有授权的那一版（别的连解析都不做）；builder 照旧列全部。"""
    admin = await auth_env.turn_on_local()
    try:
        who = f"u{uuid.uuid4().hex[:8]}"          # 这个身份自己的一份：别的工作留下的授权串不进来
        await auth_env.make_user(who, "user")
        mine = await make_workflow(graph=upload_graph())
        await publish(admin, mine)
        other = await make_workflow(graph=upload_graph())
        await publish(admin, other)
        await grant_caliber(mine["workflow_id"], 1, who, "use")

        person = Principal(username=who, role="user", verified=True, mode="local")
        async with SessionLocal() as session:
            permitted = await permitted_versions(session, person)
            assert (mine["workflow_id"], 1) in permitted, "有授权的那一版在清单里"
            seen = await CaliberKind.load(session, actor=person)
            assert seen.items, "有授权的那一版要列出来"
            assert mine["workflow_id"] in {item.workflow_id for item in seen.items}
            assert all((item.workflow_id, item.version) in permitted for item in seen.items)
            assert other["workflow_id"] not in {item.workflow_id for item in seen.items}
            # 搭建者不受这一层收窄（行为不变）
            builder = Principal(username="bob", role="builder", mode="local")
            assert await permitted_versions(session, builder) is None
            everything = await CaliberKind.load(session, actor=builder)
            assert {mine["workflow_id"], other["workflow_id"]} <= {item.workflow_id for item in everything.items}
    finally:
        await admin.aclose()
        await auth_env.reset()


# ==========================================================================
# 4、服务端准备的那条路：显式绑定、白名单、遮罩/只读、父 Run 证据
# ==========================================================================


async def test_the_prepared_path_runs_through_the_tool_and_keeps_the_parent_evidence(client):
    """execute_prepared：最小父图（入口 → caliber_query → 出口）、经 user_chat_run 起探索父 Run、真的从工具走一次。

    交回的 Outcome 与 execute 同形：run_id 是父 Run（显式绑定、遮罩/只读、每条 query artifact 的证据都在它上面），
    execution_run_id 是它派生出来真正跑那几条 SQL 的子运行。"""
    from app.engine.evidence_reader import read_sealed

    admin = await auth_env.turn_on_local()
    try:
        await auth_env.make_user("uma", "user")
        asset = await make_workflow(graph=upload_graph())
        await publish(admin, asset)
        await grant_caliber(asset["workflow_id"], 1, "uma", "use")

        async with SessionLocal() as session:
            outcome = await querycore.execute_prepared(
                session, workflow_id=asset["workflow_id"], card_id=CARD, metric_ids=["gmv", "orders"],
                inputs=INPUTS, actor="uma", origin={"conversation_id": "c1", "turn_id": "t1"})
        assert outcome.ok, outcome.problems
        assert values_of(outcome) == {"gmv": WINDOW_SUM, "orders": 3}
        assert outcome.version == 1 and outcome.purpose == querycore.DEFAULT_PURPOSE
        assert outcome.execution_run_id and outcome.execution_run_id != outcome.run_id

        async with SessionLocal() as session:
            parent = await session.get(Run, outcome.run_id)
            child = await session.get(Run, outcome.execution_run_id)
        assert parent.run_class == "exploratory" and parent.origin_kind == "chat"
        assert parent.started_by == "uma" and parent.origin["conversation_id"] == "c1"
        # 遮罩 + 只读：和普通问数一样的两条边界；预算也记在父 Run 上（子运行照它走）
        assert parent.options[MASK_RESULTS_OPTION] is True and parent.options[READ_ONLY_OPTION] is True
        assert parent.options[querycore.BUDGET_OPTION]["max_queries"]
        assert parent.memory_scope == "chat:uma"
        # 显式绑定：父图上那个节点就是口径查询，参数写死在图上（不是模型挑的）
        [bound] = [n for n in parent.graph["nodes"] if n["data"]["config"].get("tool") == querycore.CALIBER_QUERY]
        assert bound["data"]["config"]["args"] == {"workflow": asset["workflow_id"], "card": CARD,
                                                  "metric_ids": ["gmv", "orders"], "inputs": INPUTS}
        # 派生的子运行：明确的父子关系，跑的是那一次固定执行
        assert child is not None and child.origin["parent_run_id"] == parent.id
        assert child.origin["service"] == "querycore" and child.options[READ_ONLY_OPTION] is True
        # 证据：父 Run 的台账里逐条记着这次口径查询的查询快照（工件、来源、执行身份）
        sealed = await read_sealed(parent.id)
        payload = sealed.payload_of([querycore.PREPARED_NODE])
        assert payload and payload["kind"] == querycore.CALIBER_QUERY and payload["run_id"] == child.id
        assert [q["execution_id"] for q in payload["queries"]] == [q["execution_id"] for q in outcome.queries]
        assert {q["artifact"] for q in outcome.queries} <= set(sealed.queries)
        # 普通使用者读得到这次运行（它自己发起、origin_kind=chat、在工具边界遮过）
        assert await deps.run_events_readable(Principal(username="uma", role="user", verified=True, mode="local"),
                                              parent)
    finally:
        await admin.aclose()
        await auth_env.reset()


async def test_a_moved_recommended_version_is_reported_not_substituted(client):
    """点名的那一版固定并授权过，工具那条路固定的是**推荐版**：推荐版中途换过时照实记下来、不把另一版的数当这一版交出。

    （这里让这个人两版都有授权，好让执行真的走到「跑的是另一版」那一步；只有一版授权时另一次执行会先在授权那道门被拒。）"""
    admin = await auth_env.turn_on_local()
    try:
        who = f"u{uuid.uuid4().hex[:8]}"
        await auth_env.make_user(who, "user")
        first = await make_workflow(graph=upload_graph())
        await publish(admin, first)                                  # v1
        second = await add_version(first, upload_graph(include_open=True), version=2)
        await publish(admin, second, version=2)                      # v2 成了推荐版
        await grant_caliber(first["workflow_id"], 1, who, "use")
        await grant_caliber(first["workflow_id"], 2, who, "use")

        async with SessionLocal() as session:
            outcome = await querycore.execute_prepared(session, workflow_id=first["workflow_id"], card_id=CARD,
                                                       metric_ids=["gmv"], inputs=INPUTS, version=1, actor=who)
        assert outcome.ok is False and outcome.version == 2
        assert querycore.VERSION_MOVED in [p["code"] for p in outcome.problems]
        assert values_of(outcome)["gmv"] != WINDOW_SUM, "跑的是另一版：值也不是点名那一版的"
    finally:
        await admin.aclose()
        await auth_env.reset()


async def test_the_prepared_path_refuses_before_launching_and_masks_the_result(client, tmp_path):
    """没有授权：这条路也在起任何运行之前拒；有授权但列被遮罩：照实交回遮罩过的结果（值不交出去）。"""
    admin = await auth_env.turn_on_local()
    try:
        who = f"u{uuid.uuid4().hex[:8]}"          # 这个身份自己的一份：别的用例留下的运行串不进来
        await auth_env.make_user(who, "user")
        asset = await make_workflow(graph=upload_graph())
        await publish(admin, asset)
        before = await runs_matching(started_by=who)
        async with SessionLocal() as session:
            with pytest.raises(querycore.QueryCoreRefused) as caught:
                await querycore.execute_prepared(session, workflow_id=asset["workflow_id"], card_id=CARD,
                                                 metric_ids=["gmv"], inputs=INPUTS, actor=who)
        assert caught.value.code == querycore.NOT_AUTHORIZED and caught.value.status_code == 403
        assert await runs_matching(started_by=who) == before, "没授权就连父 Run 都不起"

        await grant_caliber(asset["workflow_id"], 1, who, "use")
        await S.set_source(tmp_path / "user_access_orders.db", options={"mask_columns": ["gmv"]})
        async with SessionLocal() as session:
            masked = await querycore.execute_prepared(session, workflow_id=asset["workflow_id"], card_id=CARD,
                                                      metric_ids=["gmv"], inputs=INPUTS, actor=who)
        [metric] = masked.metrics
        assert metric.get("masked") is True and metric["value"] is None
        assert masked.ok is False and querycore.METRIC_MASKED in [p["code"] for p in masked.problems]
        # 父 Run 上留给证据的那一份也只有遮罩后的说法，原值一个字都不在
        from app.engine.evidence_reader import read_sealed

        payload = (await read_sealed(masked.run_id)).payload_of([querycore.PREPARED_NODE])
        assert payload["metrics"][0]["masked"] is True and payload["metrics"][0]["value"] is None
    finally:
        await admin.aclose()
        await auth_env.reset()


# ==========================================================================
# 5、正式那一档也要授权（安全复核 1）
# ==========================================================================


async def test_the_formal_path_also_needs_the_asset_grant(client):
    """正式运行**也**要这一版口径的资产授权：没有授权当场 403，起了任何运行都算漏；授权之后还要发布依据齐。"""
    from app.apps import publications

    admin = await auth_env.turn_on_local()
    try:
        who = f"u{uuid.uuid4().hex[:8]}"
        await auth_env.make_user(who, "user")
        asset = await make_workflow(graph=upload_graph())
        await publish(admin, asset)                    # 有清单、还没有验证记录与审阅
        before = await runs_matching(workflow_id=asset["workflow_id"])

        async with SessionLocal() as session:
            with pytest.raises(querycore.QueryCoreRefused) as caught:
                await querycore.execute_formal(session, workflow_id=asset["workflow_id"], card_id=CARD,
                                               metric_ids=["gmv"], inputs=INPUTS, actor=who)
        assert caught.value.code == querycore.NOT_AUTHORIZED and caught.value.status_code == 403
        assert caught.value.problems[0]["purpose"] == publications.PURPOSE_FORMAL
        assert await runs_matching(workflow_id=asset["workflow_id"]) == before, "正式这一档也在起 Run 之前拒"

        # 授权之后是**另一道**（发布依据）：机器码是 judge 的原码，不是 not_authorized
        await grant_caliber(asset["workflow_id"], 1, who, "use")
        async with SessionLocal() as session:
            with pytest.raises(querycore.QueryCoreRefused) as proof:
                await querycore.execute_formal(session, workflow_id=asset["workflow_id"], card_id=CARD,
                                               metric_ids=["gmv"], inputs=INPUTS, actor=who)
        assert proof.value.code == publications.RECORD_MISSING and proof.value.code != querycore.NOT_AUTHORIZED
        assert await runs_matching(workflow_id=asset["workflow_id"]) == before

        # 两道都过：发布依据补齐（独立核算 + 人工确认）之后正式跑得成
        record = await add_record(asset, metric_ids=("gmv",))
        await confirm(asset, record)
        await publish(admin, asset)
        async with SessionLocal() as session:
            outcome = await querycore.execute_formal(session, workflow_id=asset["workflow_id"], card_id=CARD,
                                                     metric_ids=["gmv"], inputs=INPUTS, actor=who)
        assert outcome.ok and values_of(outcome) == {"gmv": WINDOW_SUM}
        assert outcome.publication["identity"] == publications.PROVEN
    finally:
        await admin.aclose()
        await auth_env.reset()


async def test_a_formal_parent_run_by_a_plain_user_is_refused_without_a_grant(client):
    """应用图里引用一份没授权给它的口径：正式父 Run 里那一次调用照样被拒（工具那一层也过同一道授权）。"""
    from app.apps import publications
    from tests.test_caliber_query_tool import gated_parent_graph

    admin = await auth_env.turn_on_local()
    try:
        who = f"u{uuid.uuid4().hex[:8]}"
        await auth_env.make_user(who, "user")
        asset = await make_workflow(graph=upload_graph())
        graph = gated_parent_graph(asset["workflow_id"], metric_ids=["gmv"])
        run = await run_manager.start(graph=graph, input_payload={}, run_class="formal", started_by=who)
        await _wait_for(run.id, ("interrupted",))
        await confirm(asset, await add_record(asset, metric_ids=("gmv",)))     # 依据齐（不给资产授权）
        await publish(admin, asset)
        await run_manager.resume(run.id, {"approved": True})
        done = await _settled(run.id)
        assert done.status == "failed" and querycore.NOT_AUTHORIZED in str(done.error or ""), done.error
        assert "正式" in str(done.error or ""), "说法里说清这是正式那一档"
    finally:
        await admin.aclose()
        await auth_env.reset()


# ==========================================================================
# 6、普通使用者读运行：图上、事件里不留口径的内部身份（安全复核 2）
# ==========================================================================

#: 事件里**不许**出现的键：口径身份、发布清单、selection、哈希、工件 id、参数材料
INTERNAL_EVENT_KEYS = ("args", "preview", "artifact", "query_artifact", "query_artifacts", "schema_artifact",
                       "evidence", "parameters", "origin", "workflow_id", "card_id", "version", "manifest",
                       "selection", "identity", "execution_hash", "graph_hash", "selection_hash", "closure_hash",
                       "vars")


def _cookie(client: Any) -> str:
    return f"agentlab_session={client.cookies.get('agentlab_session')}"


async def _ws(path: str, cookie: str | None) -> list[dict]:
    """把一次 WebSocket 握手原样交给整个应用（中间件 + 路由级依赖 + 路由），记下发出去的帧。"""
    inbox: Any = asyncio.Queue()
    await inbox.put({"type": "websocket.connect"})
    sent: list[dict] = []

    async def receive():
        return await inbox.get()

    async def send(message):
        sent.append(message)
        if message["type"] == "websocket.close":
            await inbox.put({"type": "websocket.disconnect", "code": 1000})

    headers = [(b"host", b"test")] + ([(b"cookie", cookie.encode())] if cookie else [])
    scope = {"type": "websocket", "asgi": {"version": "3.0"}, "scheme": "ws", "http_version": "1.1", "path": path,
             "raw_path": path.encode(), "query_string": b"", "root_path": "", "headers": headers,
             "client": ("127.0.0.1", 50000), "server": ("test", 80), "subprotocols": [], "state": {}}
    await asyncio.wait_for(app(scope, receive, send), 10)
    return sent


async def _wait_for(run_id: str, statuses: tuple[str, ...]) -> Any:
    deadline = asyncio.get_running_loop().time() + 30.0
    while asyncio.get_running_loop().time() < deadline:
        async with SessionLocal() as session:
            row = await session.get(Run, run_id)
        if row is not None and row.status in statuses:
            return row
        await asyncio.sleep(0.05)
    raise AssertionError(f"运行 {run_id} 没有进入 {statuses}")


async def _settled(run_id: str) -> Any:
    return await _wait_for(run_id, ("succeeded", "failed", "cancelled", "interrupted"))


async def _allow_chat(admin: Any) -> None:
    r = await admin.put("/api/auth/settings", json={"user_can_chat": True})
    assert r.status_code == 200, r.text


async def prepared_parent(client, *, who: str) -> tuple[dict, Any]:
    """一份合成口径 + 一次普通使用者的 prepared 执行：交回 (资产, Outcome)。调用方自己开登录、给授权。

    client 是**发布用的**客户端（开登录之后发请求要带 CSRF 头，admin 那个就是）。"""
    asset = await make_workflow(graph=upload_graph())
    await publish(client, asset)
    await grant_caliber(asset["workflow_id"], 1, who, "use")
    async with SessionLocal() as session:
        outcome = await querycore.execute_prepared(
            session, workflow_id=asset["workflow_id"], card_id=CARD, metric_ids=["gmv", "orders"], inputs=INPUTS,
            actor=who, origin={"conversation_id": "c1", "turn_id": "t1"})
    assert outcome.ok, outcome.problems
    return asset, outcome


async def test_a_plain_user_reads_the_run_without_the_internal_caliber(client):
    """普通使用者读得到自己这次运行，但运行图与事件里读不到口径的内部身份；指标值、状态、进度、别的节点都在。"""
    admin = await auth_env.turn_on_local()
    try:
        who = f"u{uuid.uuid4().hex[:8]}"
        await auth_env.make_user(who, "user")
        await _allow_chat(admin)
        asset, outcome = await prepared_parent(admin, who=who)
        person = await auth_env.login(who)

        graph_body = (await person.get(f"/api/runs/{outcome.run_id}/graph")).json()
        [node] = [n for n in graph_body["graph"]["nodes"] if n["id"] == querycore.PREPARED_NODE]
        args = node["data"]["config"]["args"]
        assert set(args) == {"caliber"}, args
        assert asset["workflow_id"] not in json.dumps(graph_body, ensure_ascii=False)
        assert graph_body["workflow_id"] is None and graph_body["version"] is None

        events = (await person.get(f"/api/runs/{outcome.run_id}/events")).json()
        dumped = json.dumps(events, ensure_ascii=False)
        assert asset["workflow_id"] not in dumped and f'"{CARD}"' not in dumped, "事件里不许出现口径身份"
        mine = [e for e in events if e["node_id"] == querycore.PREPARED_NODE]
        assert mine, "口径查询节点的事件要在（进度看得见）"
        for event in mine:
            assert not [k for k in event["data"] if k in INTERNAL_EVENT_KEYS], event
        ends = [e for e in mine if e["type"] == "tool.end"]
        assert ends and ends[0]["data"]["tool"] == querycore.CALIBER_QUERY
        assert ends[0]["data"]["duration_ms"] is not None, "花了多久留着"
        assert [m["id"] for m in ends[0]["data"]["result"]["metrics"]] == ["gmv", "orders"], "指标值留着"
        assert {m["value"] for m in ends[0]["data"]["result"]["metrics"]} == {WINDOW_SUM, 3}
        assert ends[0]["data"]["result"]["ok"] is True and ends[0]["data"]["result"]["status"] == "succeeded"
        # 别的节点的事件原样：不因为这一条口径查询就把普通聊天的事件清空
        others = [e for e in events if e["node_id"] and e["node_id"] != querycore.PREPARED_NODE]
        assert others, "别的节点的事件还在"
        assert any("artifact" in e["data"] or "preview" in e["data"] for e in others), "别的节点照原样给（没被清空）"

        # 搭建者读到的还是原件：图上参数、事件里的工件都在
        raw = (await admin.get(f"/api/runs/{outcome.run_id}/graph")).json()
        [raw_node] = [n for n in raw["graph"]["nodes"] if n["id"] == querycore.PREPARED_NODE]
        assert raw_node["data"]["config"]["args"]["workflow"] == asset["workflow_id"]
        raw_events = (await admin.get(f"/api/runs/{outcome.run_id}/events")).json()
        assert asset["workflow_id"] in json.dumps(raw_events, ensure_ascii=False)
        await person.aclose()
    finally:
        await admin.aclose()
        await auth_env.reset()


async def test_the_websocket_stream_goes_through_the_same_projection(client):
    """实时流和事件接口是同一条路：WS 补的历史、推的实时事件都过同一份投影。"""
    admin = await auth_env.turn_on_local()
    try:
        who = f"u{uuid.uuid4().hex[:8]}"
        await auth_env.make_user(who, "user")
        await _allow_chat(admin)
        asset, outcome = await prepared_parent(admin, who=who)
        person = await auth_env.login(who)
        frames = await _ws(f"/api/runs/{outcome.run_id}/stream", _cookie(person))
        assert frames[0]["type"] == "websocket.accept"
        texts = [f.get("text") or "" for f in frames if f["type"] == "websocket.send"]
        assert any("stream.end" in text for text in texts), texts[-3:]
        dumped = "\n".join(texts)
        assert asset["workflow_id"] not in dumped and f'"{CARD}"' not in dumped, "实时流里也不许出现口径身份"
        assert querycore.CALIBER_QUERY in dumped, "口径查询那一步在（进度看得见）"
        results = [json.loads(text)["data"]["result"] for text in texts
                   if '"result"' in text and isinstance(json.loads(text).get("data", {}).get("result"), dict)]
        assert results and results[0]["ok"] is True, "结果摘要里留着算成没算成"
        assert [m["id"] for m in results[0]["metrics"]] == ["gmv", "orders"], "指标值留着"

        # 实时那一段（_pump）收的是总线上推来的事件：过的是同一个投影
        from app.api import runs as runs_api
        from app.core.events import RunEventModel

        live: list[dict] = []

        class _Socket:
            async def send_json(self, payload: dict[str, Any]) -> None:
                live.append(payload)

            async def close(self) -> None:
                return None

        async def _live():
            yield RunEventModel(run_id=outcome.run_id, type="tool.end", node_id=querycore.PREPARED_NODE,
                                data={"tool": querycore.CALIBER_QUERY, "provided": "args",
                                      "args": {"workflow": asset["workflow_id"]}, "preview": "{}"})

        graph = querycore.prepared_graph(asset["workflow_id"], card_id=CARD, metric_ids=["gmv"], inputs=INPUTS)
        token = deps.set_principal(Principal(username=who, role="user", verified=True, mode="local"))
        try:
            await runs_api._pump(_Socket(), _live(), outcome.run_id, runs_api.run_projection(graph))
        finally:
            deps.reset_principal(token)
        assert live[0]["data"]["tool"] == querycore.CALIBER_QUERY and "args" not in live[0]["data"]
        await person.aclose()
    finally:
        await admin.aclose()
        await auth_env.reset()


def test_the_projection_only_applies_to_ordinary_readers():
    """谁拿投影：开登录后的使用者要，关登录 / 搭建者不要；补固定那条日志里的固定目标也不给。"""
    from app.api import runs as runs_api

    graph = querycore.prepared_graph("wf-1", card_id=CARD, metric_ids=["gmv"], inputs=INPUTS)
    token = as_user("carol")
    try:
        projection = runs_api.run_projection(graph)
        assert projection is not None
        assert projection.graph(graph)["nodes"][1]["data"]["config"]["args"].get("workflow") is None
        pin = {"type": "log", "node_id": None,
               "data": {"code": "late_pin", "source": "门店报损", "rows": "mine", "caliber": "wf-1@1:card",
                        "workflow_id": "wf-1", "version": 1, "late": True, "pinned_by": "caliber_query"}}
        safe = projection.event(pin)["data"]
        assert safe["source"] == "门店报损" and safe["rows"] == "mine" and "caliber" not in safe
        assert "workflow_id" not in safe and "version" not in safe
    finally:
        deps.reset_principal(token)
    assert runs_api.run_projection(graph) is None, "关登录：照原样给"


# ==========================================================================
# 7、角色以身份为准：访问密钥 / 角色快照不一致时不放行（安全复核 3）
# ==========================================================================


async def test_a_key_capped_identity_is_not_raised_by_the_database_role(client):
    """访问密钥认出来的身份封顶在使用者：它的用户名在库里是管理员，也不能因此被抬成放行。"""
    admin = await auth_env.turn_on_local()
    try:
        await auth_env.make_user("rootish", "admin")
        asset = await make_workflow(graph=upload_graph())
        await publish(admin, asset)
        key_like = Principal(username="rootish", role="user", verified=True, mode="local", via="key")
        token = deps.set_principal(key_like)
        try:
            async with SessionLocal() as session:
                assert await chat_policy.restricted_actor(session, key_like) is True
                # 形参给的是名字（工具那条路拿到的就是父 Run 上记的名字）时，请求那一档更严 → 取更严的
                assert await chat_policy.restricted_actor(session, "rootish") is True
            with pytest.raises(querycore.QueryCoreRefused) as caught:
                await execute(asset)
        finally:
            deps.reset_principal(token)
        assert caught.value.code == querycore.NOT_AUTHORIZED

        # 反过来：身份说自己是搭建者、库里是使用者 —— **发起前那一道**以身份为准（和 deps.restricted 同一条）；但运行里
        # 记下来的只有**名字**，运行到「转抄口径」那一步时只有库里的角色可认（metrics 节点、工具路径都是这个规则），
        # 所以这一次运行会在节点上失败。两条规则各自按它拿得到的那一份证据判，不互相冒充。
        await auth_env.make_user("mismatch", "user")
        builder_like = Principal(username="mismatch", role="builder", verified=True, mode="local")
        async with SessionLocal() as session:
            assert await chat_policy.restricted_actor(session, builder_like) is False, "身份说自己是搭建者：以身份为准"
            # 只给名字（工具那条路拿到的就是名字）时只有库里的角色可认：库里是使用者 → 按使用者算，不因身份那一次放行
            assert await chat_policy.restricted_actor(session, "mismatch") is True
        outcome = await execute(asset, actor=builder_like)
        assert outcome.status == "failed" and querycore.NOT_AUTHORIZED in str(
            [p.get("message") for p in outcome.problems]), "运行里只剩名字：口径那一步按库里那一档判"
    finally:
        await admin.aclose()
        await auth_env.reset()


async def test_a_disabled_person_stays_limited_by_name(client):
    """手上只有用户名时仍回库里认停用：停用的人按普通使用者算（拿不到授权就是拒）。"""
    from app.db.models import User

    admin = await auth_env.turn_on_local()
    try:
        await auth_env.make_user("gone", "builder")
        asset = await make_workflow(graph=upload_graph())
        await publish(admin, asset)
        async with SessionLocal() as session:
            assert await chat_policy.restricted_actor(session, "gone") is False
            user = (await session.execute(select(User).where(User.username == "gone"))).scalar_one()
            user.disabled = True
            await session.commit()
            assert await chat_policy.restricted_actor(session, "gone") is True, "停用的人不按角色放行"
        with pytest.raises(querycore.QueryCoreRefused) as caught:
            await execute(asset, actor="gone")
        assert caught.value.code == querycore.NOT_AUTHORIZED
    finally:
        await admin.aclose()
        await auth_env.reset()


# ==========================================================================
# 8、手搭的转抄卡（metrics.caliber_from）：运行时按同一份资产授权核（安全复核 1）
# ==========================================================================


def chained_graph(workflow_id: str, *, card: str = CARD) -> dict[str, Any]:
    """一张自己搭的转抄卡图：拷一份源口径的取数节点，口径卡用 `caliber_from` 钉到那一版的卡。

    普通使用者可以在画布上（或问数据里生成的图里）这么搭：节点类型、工具都在白名单里，**口径定义本身**是从别的工作流那一版
    读进来的——所以这一道门只能在运行时判（`engine/nodes/metrics._require_own_caliber`）。"""
    import copy

    source = upload_graph()
    fetch = copy.deepcopy(next(n for n in source["nodes"] if n["id"] == "sales"))
    fields = next(n for n in source["nodes"] if n["id"] == "start")["data"]["config"]["fields"]
    return S.chain(
        S.node("start", "input", fields=copy.deepcopy(fields)),
        fetch,
        S.node(CARD, "metrics", caliber="转抄的合成周销售", caliber_version="v1",
               caliber_from={"workflow_id": workflow_id, "workflow_version": 1, "node_id": card}),
        S.node("out", "output", fields=[{"name": "答", "value": "运行结束"}]))


async def run_chained(who: str, workflow_id: str, *, card: str = CARD, **kw: Any) -> Any:
    run = await run_manager.start(graph=chained_graph(workflow_id, card=card), input_payload=dict(INPUTS),
                                  started_by=who, **kw)
    return await _settled(run.id)


async def test_a_chained_card_is_refused_without_the_asset_grant(client):
    """没授权：转抄卡那一步在**读定义、算数之前**失败（机器码同一条），别人的口径定义一个字都读不进来。"""
    from app.engine.evidence_reader import read_sealed

    admin = await auth_env.turn_on_local()
    try:
        who = f"u{uuid.uuid4().hex[:8]}"
        await auth_env.make_user(who, "user")
        asset = await make_workflow(graph=upload_graph())
        await publish(admin, asset)
        before = await runs_matching(started_by=who)

        row = await run_chained(who, asset["workflow_id"])
        assert row.status == "failed" and querycore.NOT_AUTHORIZED in str(row.error or ""), row.error
        assert asset["workflow_id"] not in str(row.error or "") and CARD not in str(row.error or ""), row.error
        sealed = await read_sealed(row.id)
        assert [e for e in sealed.events if e[1] == "node.finished" and e[2] == CARD] == [], \
            "在算数之前失败：口径卡节点一次都没跑完"
        assert await runs_matching(started_by=who) == before + 1, "这次运行本身是发起过的（不是发起被拒）"

        # 那一条失败事件过一遍投影（普通使用者读得到的运行事件）：口径名与卡都不带出来，机器码留着
        from app.api import runs as runs_api
        from app.db.models import RunEvent, Workflow

        async with SessionLocal() as session:
            workflow = await session.get(Workflow, asset["workflow_id"])
            rows = list((await session.execute(select(RunEvent).where(RunEvent.run_id == row.id))).scalars())
        token = as_user(who)
        try:
            projection = runs_api.run_projection(row.graph)
            failures = [projection.event({"type": e.type, "node_id": e.node_id, "data": e.data}) for e in rows
                        if e.type in runs_api.FAILURE_EVENTS]
        finally:
            deps.reset_principal(token)
        assert failures, "这一次运行有失败事件"
        dumped = json.dumps(failures, ensure_ascii=False)
        assert workflow.name not in dumped, dumped
        assert querycore.NOT_AUTHORIZED in dumped and "转抄的口径没有授权" not in dumped, dumped

        # 给了授权就照常读那一版的定义、算出那一版的数
        await grant_caliber(asset["workflow_id"], 1, who, "use")
        ok = await run_chained(who, asset["workflow_id"])
        assert ok.status == "succeeded", ok.error
        payload = json.dumps((await read_sealed(ok.id)).payload_of([CARD]) or {}, default=str)
        assert str(WINDOW_SUM) in payload, payload[:400]
    finally:
        await admin.aclose()
        await auth_env.reset()


async def test_a_system_run_and_builders_are_not_judged_by_the_caliber_rule(client):
    """服务端自己发起的运行（不记发起人：按需比对、口径召回、目录变化后的重算）与 builder 照旧跑得完。

    只有「认得出是普通使用者、又没拿到这一版的授权」才拒（上一条用例）。这一条盯住另外两档不被误伤：
    召回那批后台运行就是不带发起人的（`ledger/recall.launch(actor=None)`）。"""
    admin = await auth_env.turn_on_local()
    try:
        builder = f"b{uuid.uuid4().hex[:8]}"
        await auth_env.make_user(builder, "builder")
        asset = await make_workflow(graph=upload_graph())
        await publish(admin, asset)

        token = deps.set_principal(None)            # 没有请求身份、运行也不记发起人：服务端自己发起的
        try:
            run = await run_manager.start(graph=chained_graph(asset["workflow_id"]), input_payload=dict(INPUTS))
            system = await _settled(run.id)
        finally:
            deps.reset_principal(token)
        assert system.status == "succeeded", system.error

        built = await run_chained(builder, asset["workflow_id"])
        assert built.status == "succeeded", built.error
    finally:
        await admin.aclose()
        await auth_env.reset()


async def test_the_chained_card_only_uses_the_pinned_version(client):
    """授权是按（工作流, 版本）给的：钉住的那一版有授权才跑得成；同一张卡改钉另一版、那一版没授权照样拒。"""
    admin = await auth_env.turn_on_local()
    try:
        who = f"u{uuid.uuid4().hex[:8]}"
        await auth_env.make_user(who, "user")
        first = await make_workflow(graph=upload_graph())
        await publish(admin, first)                                  # v1
        second = await add_version(first, upload_graph(include_open=True), version=2)
        await publish(admin, second, version=2)                      # v2 成了推荐版
        await grant_caliber(first["workflow_id"], 1, who, "use")     # 只给 v1

        one = await run_chained(who, first["workflow_id"])
        assert one.status == "succeeded", one.error

        graph = chained_graph(first["workflow_id"])
        next(n for n in graph["nodes"] if n["id"] == CARD)["data"]["config"]["caliber_from"]["workflow_version"] = 2
        run = await run_manager.start(graph=graph, input_payload=dict(INPUTS), started_by=who)
        other = await _settled(run.id)
        assert other.status == "failed" and querycore.NOT_AUTHORIZED in str(other.error or "")
    finally:
        await admin.aclose()
        await auth_env.reset()


# ==========================================================================
# 9、图与失败说法的投影：提示词、转抄卡映射、失败白话（安全复核 2）
# ==========================================================================


def _ask_graph(workflow_id: str) -> dict[str, Any]:
    """普通使用者的图：agent 绑口径查询（提示词里照抄了 look 交回的「取数写法」）+ 一张转抄卡。"""
    call = json.dumps({"workflow": workflow_id, "card": CARD, "metric_ids": ["gmv"],
                       "inputs": {"start": "2026-09-01", "end": "2026-10-01"}}, ensure_ascii=False)
    return S.chain(
        S.node("start", "input", fields=[{"name": "question"}]),
        S.node("ask", "agent", prompt=f"回答问题：照这一行调 {querycore.CALIBER_QUERY} 取数（不要自己写取数 SQL）：{call}",
               tools=[S.tool(), querycore.CALIBER_QUERY], max_steps=4),
        S.node("out", "output", fields=[{"name": "答案", "value": "{{ nodes.ask.text }}"}]),
        S.node(CARD, "metrics", caliber_from={"workflow_id": workflow_id, "workflow_version": 1, "node_id": CARD,
                                              "nodes": {"sales": "sales"}}, metrics=[]),
    )


def test_the_projection_redacts_prompts_and_chained_maps():
    """图上与口径有关的内部写法：提示词里的取数写法（身份的值）、转抄卡的身份映射——键、说明、inputs 的值都留着。"""
    from app.api import runs as runs_api

    graph = _ask_graph("wf-1")
    token = as_user("carol")
    try:
        projected = runs_api.run_projection(graph).graph(graph)
    finally:
        deps.reset_principal(token)
    prompt = projected["nodes"][1]["data"]["config"]["prompt"]
    assert "不要自己写取数 SQL" in prompt and '"metric_ids"' in prompt, "说明与要算的指标留着"
    assert '"start": "2026-09-01"' in prompt, "要填的入口取值留着"
    assert "wf-1" not in prompt, "口径身份那几个值换掉了"
    assert f'"workflow": "（有授权的口径）"' in prompt and f'"card": "（有授权的口径）"' in prompt, prompt
    assert projected["nodes"][3]["data"]["config"]["caliber_from"] == runs_api.CALIBER_FROM_NOTE
    # 原件不动
    assert graph["nodes"][1]["data"]["config"]["prompt"].count("wf-1") == 1
    assert graph["nodes"][3]["data"]["config"]["caliber_from"]["workflow_version"] == 1
    # builder 及以上拿到的是原件
    assert runs_api.run_projection(graph) is None


def test_a_failure_says_it_without_the_caliber_identity():
    """失败说法的安全白话：机器码留着，口径名、版本、card 不带出来；不是口径那一档的原样给。"""
    from app.api import runs as runs_api

    message = ("工具 caliber_query 执行失败：没有权限：口径「周合成-abc」v1 没有授权给你使用"
               "（querycore_not_authorized）。请检查节点中填写的参数")
    assert runs_api.safe_failure(message) == f"{runs_api.CALIBER_FAILED_TEXT}（querycore_not_authorized）"
    assert runs_api.safe_failure("模型调用失败：接入返回了 502") is None
    assert runs_api.safe_failure(None) is None


# ==========================================================================
# 10、普通使用者读得到的运行：失败说法与账本视图（安全复核 2、3）
# ==========================================================================


async def test_a_plain_user_sees_a_safe_failure_on_the_run_and_its_events(client):
    """自己发起的问数据运行里口径查询失败：运行记录与事件里的说法都只留「没成功 + 机器码」。"""
    admin = await auth_env.turn_on_local()
    try:
        who = f"u{uuid.uuid4().hex[:8]}"
        await auth_env.make_user(who, "user")
        await _allow_chat(admin)
        asset = await make_workflow(graph=upload_graph())
        await publish(admin, asset)                     # 不给授权：口径查询在工具那一层被拒
        graph = S.chain(
            S.node("start", "input", fields=[{"name": "question"}]),
            S.node("look", "tool", tool=querycore.CALIBER_QUERY,
                   args={"workflow": asset["workflow_id"], "card": CARD, "metric_ids": ["gmv"], "inputs": INPUTS}),
            S.node("out", "output", fields=[{"name": "答", "value": "{{ nodes.look.ok }}"}]))
        run = await run_manager.start(graph=graph, input_payload={}, run_class="exploratory", started_by=who,
                                      origin_kind="chat", options={MASK_RESULTS_OPTION: True, READ_ONLY_OPTION: True})
        row = await _settled(run.id)
        assert row.status == "failed" and querycore.NOT_AUTHORIZED in str(row.error or "")

        person = await auth_env.login(who)
        detail = (await person.get(f"/api/runs/{run.id}")).json()
        assert detail["error"] == f"按口径取数没有成功（querycore_not_authorized）", detail["error"]
        events = (await person.get(f"/api/runs/{run.id}/events")).json()
        dumped = json.dumps(events, ensure_ascii=False)
        assert asset["workflow_id"] not in dumped and CARD not in dumped
        [failed] = [e for e in events if e["type"] == "node.failed"]
        assert failed["data"]["error"] == "按口径取数没有成功（querycore_not_authorized）"
        [whole_run] = [e for e in events if e["type"] == "run.failed"]
        assert whole_run["data"]["error"] == "按口径取数没有成功（querycore_not_authorized）", whole_run
        assert whole_run["data"]["label"], "失败在哪一步（节点的叫法）留着"
        # 搭建者看到的还是原话（口径名、卡、码都在）
        raw = (await admin.get(f"/api/runs/{run.id}/events")).json()
        [raw_failed] = [e for e in raw if e["type"] == "node.failed"]
        assert asset["workflow_id"] in json.dumps(raw, ensure_ascii=False) or CARD in raw_failed["data"]["error"]
        await person.aclose()
    finally:
        await admin.aclose()
        await auth_env.reset()


async def matched_run(monkeypatch, who: str) -> str:
    """一次与「周销售」等价、封存后比对过的运行（发起人是这个使用者）：账本视图用它。"""
    from app.ledger import match as ledger_match

    S.script(monkeypatch, text="本周销售额 [[v:Q1.r0.销售额]]。")
    run = await run_manager.start(graph=S.explore_graph(S.EQUIVALENT), input_payload={}, run_class="exploratory",
                                  origin_kind="canvas", started_by=who)
    row = await S.wait(run.id)          # 等封存（比对读的是封存范围，只等状态会偶发地读到 pending）
    assert row.status == "succeeded" and row.manifest_seq is not None, row.error
    await ledger_match.drain()
    return row.id


async def test_the_ledger_view_drops_the_caliber_identity_for_a_plain_user(client, monkeypatch):
    """普通使用者的口径账本：内部身份（工作流 id / 版本 / 哈希 / 卡节点 id）不出去，人读的那一套一个字不动。"""
    admin = await auth_env.turn_on_local()
    try:
        who = f"u{uuid.uuid4().hex[:8]}"
        await auth_env.make_user(who, "user")
        workflow_id = await S.publish()
        run_id = await matched_run(monkeypatch, who)

        person = await auth_env.login(who)
        res = await person.get(f"/api/runs/{run_id}/ledger")
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["state"] == "matched"
        dumped = json.dumps(body, ensure_ascii=False)
        assert workflow_id not in dumped, "工作流 id 不出去（这一次的出具被拒：只说明，不给身份）"
        for key in ("workflow_id", "graph_hash", "closure_hash", "selection_hash", "execution_hash"):
            assert f'"{key}"' not in dumped, key
        assert body["actions"] and body["actions"][0]["available"] is False, body["actions"]
        # 人读的：口径名、级别叫法、指标标签与绑定、每条查询的结果、证据关系
        [caliber] = body["calibers"]
        assert caliber["workflow_name"] == "周销售" and caliber["level_text"] == "受管" and caliber["caliber"]
        [label] = body["reports"]["write"]["labels"].values()
        assert label["kind"] == "caliber" and label["workflow_name"] == "周销售" and label["binding"]
        assert "node_id" not in label and "version" not in label
        [query] = body["queries"]
        assert query["node_id"] and query["artifact"] and query["status"] == "proven", "证据关系留着"
        assert "node_id" not in query["candidates"][0]
        assert [a["kind"] for a in body["actions"]] == ["issue"] and body["whole"]["equivalent"] is True

        # 搭建者拿到的是原件：工作流 id、版本、哈希、卡节点 id 都在
        raw = (await admin.get(f"/api/runs/{run_id}/ledger")).json()
        raw_dumped = json.dumps(raw, ensure_ascii=False)
        assert workflow_id in raw_dumped and '"graph_hash"' in raw_dumped
        assert "node_id" in raw["reports"]["write"]["labels"][next(iter(raw["reports"]["write"]["labels"]))]

        # 有权出具的人（应用授权把这一版固定给它）：出具那一项要带着身份去发请求——只有**确实给它做**的那一项带
        await grant_caliber(workflow_id, 1, who, "use")
        allowed = (await person.get(f"/api/runs/{run_id}/ledger")).json()
        [action] = allowed["actions"]
        assert action["available"] is True and action["workflow_id"] == workflow_id and action["version"] == 1
        assert allowed["calibers"][0].get("workflow_id") is None, "别处照旧不给身份"
        assert allowed["reports"]["write"]["labels"][next(iter(allowed["reports"]["write"]["labels"]))].get("version") is None

        # 证据片段 / 报告导出读的是同一份标签（ledger/view.report_labels）：同一条投影
        from app.api.evidence import _sealed
        from app.ledger.view import report_labels

        sealed = await _sealed(run_id)
        token = as_user(who)
        try:
            labels = (await report_labels(sealed, "write"))["labels"]
        finally:
            deps.reset_principal(token)
        [label] = labels.values()
        assert label["kind"] == "caliber" and label["workflow_name"] == "周销售"
        assert "workflow_id" not in label and "node_id" not in label and "version" not in label
        await person.aclose()
    finally:
        await admin.aclose()
        await auth_env.reset()
