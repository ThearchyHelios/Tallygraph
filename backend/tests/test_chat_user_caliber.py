"""K14 问数据集成：普通使用者一轮里看得见哪些口径、非精确问法怎么按口径取数、固定执行那一轮怎么收尾。

这份文件走**真实的一轮**（POST /api/conversations + POST /api/chat/turns → chat/job.ChatDriver → api/copilot.answer_ops →
look → 建图 / 固定执行 → 收尾写库），不只测 helper：

1. **授权 look**：使用者这一轮的 system 里列着他有授权的口径（入口字段 + 按口径查询工具取数的写法），没有授权的口径一个字
   都不出现；他这一轮也**没有** use_caliber 那段协议（把口径的取数 SQL 拷进图里那条路只给搭建者及以上）；
2. **非精确问法**（「上个月<指标>是多少」）：模型按查看结果搭「入口 → agent（绑 caliber_query）→ 出口」，运行真的调这个
   工具取到数、交回的值写进答案——不是只能查底表；
3. **next 与运行归属**：命中固定执行（「<指标> 2026年9月是多少」）时 final.next = done（不是 ready），driver 把那次运行
   收进这一轮（turn.run_id / meta.runId / run 事件）、页面据此重载查看，答案用 final 那句白话，整轮零模型、不再整轮复核；
4. **对话来源**：固定执行起的那次运行的 origin 记着这一轮的 conversation_id / turn_id，子运行记着父运行（execute_prepared）。

数据全是合成的（临时 SQLite 库、合成标量指标），不连真实业务库；这些指标不是任何真实业务口径。研究所用名字全是编的。
"""
from __future__ import annotations

import json
import uuid
from typing import Any

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, ToolMessage
from sqlalchemy import delete, select

from app.chat import job as chat_job
from app.chat import log as turn_log
from app.chat import turn as policy
from app.db.base import SessionLocal
from app.db.models import (CaliberDraft, Conversation, ConversationTurn, DataSource, Run, Workflow,
                           WorkflowPublication, WorkflowVersion)
from app.db.models_apps import App, AppGrant, AppVersion
from app.db.models_chat import ChatTurnJob
from app.engine.runner import run_manager
from app.ledger import querycore
from app.tools.datasource import MASK_RESULTS_OPTION, READ_ONLY_OPTION
from tests import eval_samples as es
from tests.fakes import auth_env
from tests.fixtures.sources import drop_source
from tests.publication_samples import make_workflow, publish
from tests.test_querycore_execute import WINDOW_SUM, fresh_source as qc_source, graph as qc_graph

#: 口径卡的节点 id（源头口径卡）
CARD = "card"
#: 这一轮专用（全局唯一）的指标名：非精确问法和精确问法都用它，别的用例的指标名不会撞进来
NAME = "集成口径专用净额甲"
#: 合成库的窗口：2026-09-01（含）≤ paid_at < 2026-10-01（不含）
WINDOW_INPUTS = {"start": "2026-09-01", "end": "2026-10-01"}
#: 非精确问法（remainder 拆出来是「上个月<NAME>」，和指标名不等：走模型那条路）
QUESTION_FUZZY = f"上个月{NAME}是多少"
#: 精确问法（拆出来正是指标名：走固定执行那条路）
QUESTION_EXACT = f"{NAME} 2026年9月是多少"

#: 这个文件自己建的行（跑完删掉，不给别的用例留）
_MADE: dict[str, list[str]] = {"workflows": [], "sources": [], "apps": [], "conversations": []}


@pytest.fixture(autouse=True)
async def engine_up(tmp_path):
    await run_manager.setup()
    yield
    await chat_job.shutdown()
    await run_manager.shutdown()
    async with SessionLocal() as session:
        await session.execute(delete(Run).where(Run.started_by == "uma"))
        ids = list(_MADE["workflows"])
        if ids:
            for model in (WorkflowPublication, WorkflowVersion, CaliberDraft):
                await session.execute(delete(model).where(model.workflow_id.in_(ids)))
            await session.execute(delete(Workflow).where(Workflow.id.in_(ids)))
        if _MADE["apps"]:
            await session.execute(delete(App).where(App.id.in_(list(_MADE["apps"]))))
        if _MADE["conversations"]:
            convs = list(_MADE["conversations"])
            await session.execute(delete(ChatTurnJob).where(ChatTurnJob.conversation_id.in_(convs)))
            await session.execute(delete(ConversationTurn).where(ConversationTurn.conversation_id.in_(convs)))
            await session.execute(delete(Conversation).where(Conversation.id.in_(convs)))
        names = list(_MADE["sources"])
        await session.commit()
    for name in names:
        await _drop_source(name)
    for key, rows in _MADE.items():
        rows.clear()


async def _drop_source(name: str) -> None:
    async with SessionLocal() as session:
        row = (await session.execute(select(DataSource).where(DataSource.name == name))).scalars().first()
        if row is not None:
            await drop_source(row.id)


def caliber_graph() -> dict[str, Any]:
    """合成口径（K09 那一份）里的指标换个唯一名字：非精确问法要它不精确、精确问法要它唯一。"""
    made = qc_graph()
    card = next(node for node in made["nodes"] if node["id"] == CARD)
    card["data"]["config"]["metrics"][0]["name"] = NAME
    return made


async def _asset(tmp_path) -> dict:
    """一份还没发布的口径资产 + 一个合成 SQLite 数据源（不连真实业务库）。"""
    name = await qc_source(tmp_path)
    _MADE["sources"].append(name)
    asset = await make_workflow(graph=caliber_graph())
    _MADE["workflows"].append(asset["workflow_id"])
    return {**asset, "source": name}


async def _grant(workflow_id: str, version: int, who: str, role: str = "use") -> str:
    """把这一版口径授权给这个人：未归档应用 + 当前发布版精确固定这一版 + 一条 AppGrant（资产授权就是这么来的）。"""
    async with SessionLocal() as session:
        row = App(name=f"集成口径授权-{uuid.uuid4().hex[:6]}", status="active", published_version=1)
        session.add(row)
        await session.flush()
        session.add(AppVersion(app_id=row.id, version=1, spec={}, spec_hash="x",
                               pins=[{"alias": "cal", "workflow_id": workflow_id, "version": int(version)}]))
        session.add(AppGrant(app_id=row.id, principal_kind="user", principal=who, role=role))
        await session.commit()
        _MADE["apps"].append(row.id)
        return row.id


# ==========================================================================
# 助手剧本与运行里的模型
# ==========================================================================


def _line(op: dict[str, Any]) -> AIMessageChunk:
    return AIMessageChunk(content=json.dumps(op, ensure_ascii=False) + "\n")


class CaliberCopilot:
    """助手剧本：第一轮按需求 look 一次口径，第二轮按查看结果搭「入口 → agent（绑 caliber_query）→ 出口」。

    reply_after_look 为真时第二轮直接回答（只看这一轮给了模型什么、不发起运行）。
    calls 记下每次收到的整串消息（用例据此核对：使用者的 system 里有没有口径、有没有 use_caliber 那段协议、
    查看结果里给的是哪几个口径）。"""

    def __init__(self, source: str, *, question: str = QUESTION_FUZZY, reply_after_look: bool = False) -> None:
        self.source = source
        self.question = question
        self.reply_after_look = reply_after_look
        self.calls: list[list] = []

    async def astream(self, messages):
        self.calls.append(list(messages))
        if "上线前自查" in messages[1][1]:
            yield _line({"op": "done"})
            return
        if len(messages) == 2:
            yield _line({"op": "plan", "summary": "先看有没有对得上的口径"})
            yield _line({"op": "look", "need": self.question, "targets": [{"kind": "caliber", "name": NAME}]})
            return
        if self.reply_after_look:
            yield _line({"op": "reply", "text": "这一轮只是看一眼口径。"})
            return
        for op in self._graph_ops():
            yield _line(op)

    def _graph_ops(self) -> list[dict[str, Any]]:
        def node(nid: str, ntype: str, label: str, **config: Any) -> dict[str, Any]:
            return {"op": "add_node", "node": {"id": nid, "type": ntype, "label": label, "config": config}}

        def edge(a: str, b: str) -> dict[str, Any]:
            return {"op": "add_edge", "edge": {"source": a, "target": b}}

        ask = (f"回答 {{{{ input.question }}}}：先照 system 里那些口径调 {querycore.CALIBER_QUERY} 工具取数"
               "（参数照查看结果里的取数写法，inputs 按问题里的时间填，start 含、end 不含），再按交回的指标值作答")
        return [node("start", "input", "输入", fields=[{"name": "question"}]),
                node("ask", "agent", "按口径取数", prompt=ask,
                     tools=[f"db_query__{self.source}", querycore.CALIBER_QUERY], max_steps=4),
                edge("start", "ask"),
                node("out", "output", "答案", fields=[{"name": "答案", "value": "{{ nodes.ask.text }}"}]),
                edge("ask", "out"),
                {"op": "done", "explanation": "按口径取数", "run": True}]


def _run_model(monkeypatch, workflow_id: str, metric_ids: list[str], inputs: dict[str, Any]) -> None:
    """运行里的模型：第一拍按口径调 caliber_query，拿到交回的结果后按它作答。"""
    from app.providers import mock_model

    def decide(self, messages):
        if any(isinstance(m, ToolMessage) for m in messages):
            return AIMessage(content=f"{NAME} 是 170.00 元。")
        return AIMessage(content="", tool_calls=[{
            "name": querycore.CALIBER_QUERY, "id": "call_1",
            "args": {"workflow": workflow_id, "card": CARD, "metric_ids": metric_ids, "inputs": inputs}}])

    monkeypatch.setattr(mock_model.MockChatModel, "_decide", decide)


def _no_model(monkeypatch) -> None:
    """任何模型调用都让这一轮炸掉：固定执行那条路应当一次模型都不调（含复核、含运行里的模型）。"""
    from app.api import copilot as copilot_api
    from app.providers import mock_model

    def boom(*_a, **_k):
        raise AssertionError("这一轮不该调用模型")

    monkeypatch.setattr(mock_model.MockChatModel, "_decide", boom)
    monkeypatch.setattr(copilot_api, "get_chat_model", boom)


# ==========================================================================
# 一轮：HTTP 发起，服务端跑完（不依赖前端）
# ==========================================================================


async def _conversation(client, title: str = "集成：口径问数") -> str:
    res = await client.post("/api/conversations", json={"kind": "chat", "title": title})
    assert res.status_code == 201, res.text
    conversation_id = res.json()["id"]
    _MADE["conversations"].append(conversation_id)
    return conversation_id


async def _ask(client, conversation_id: str, question: str) -> str:
    res = await client.post("/api/chat/turns", json={"conversation_id": conversation_id, "question": question})
    assert res.status_code == 202, res.text
    turn_id = res.json()["turn_id"]
    await chat_job.manager.wait(turn_id, timeout=90.0)
    return turn_id


async def _row(turn_id: str) -> ConversationTurn:
    async with SessionLocal() as session:
        row = await session.get(ConversationTurn, turn_id)
        assert row is not None
        return row


async def _job(turn_id: str) -> ChatTurnJob:
    async with SessionLocal() as session:
        job = await session.get(ChatTurnJob, turn_id)
        assert job is not None and job.status not in ("queued", "running"), (job.status, job.stage)
        return job


async def _events(turn_id: str) -> list[dict[str, Any]]:
    return await turn_log.read(turn_id, 0, limit=10_000)


def _look_text(script: Any) -> str:
    """最近一次查看交回给模型的那段文字（最后一次调用的最后一条消息）。"""
    return str(script.calls[-1][-1][1])


def _ops(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [event["data"] for event in events if event["type"] == "op"]


async def _child_runs(parent_run_id: str) -> list[Run]:
    """这次运行**派生的子运行**（口径工具真跑起来的那次固定执行，K10：origin 里记着 parent_run_id）。"""
    async with SessionLocal() as session:
        rows = list((await session.execute(select(Run))).scalars())
    return [row for row in rows
            if isinstance(row.origin, dict) and str(row.origin.get(querycore.PARENT_RUN_KEY) or "") == parent_run_id]


async def _unauthorized_asset(world: dict) -> dict:
    """再建一份**没有**授权给 uma 的口径（同一个数据源，另一份资产）：它的指标名不该出现在使用者这一轮里。"""
    made = await make_workflow(graph=caliber_graph())
    _MADE["workflows"].append(made["workflow_id"])
    await publish(world["admin"], made)
    return made


@pytest.fixture
async def world(tmp_path, monkeypatch):
    """开登录的本地环境：一个普通使用者 uma + 一位管理员；合成数据源 + 一份已发布、已授权给 uma 的口径。"""
    await auth_env.reset()
    admin = await auth_env.turn_on_local()
    await auth_env.make_user("uma", "user")
    uma = await auth_env.login("uma")
    assert (await admin.put("/api/auth/settings", json={"user_can_chat": True})).status_code == 200
    asset = await _asset(tmp_path)
    await publish(admin, asset)
    await _grant(asset["workflow_id"], 1, "uma")
    try:
        yield {"admin": admin, "uma": uma, "asset": asset}
    finally:
        await uma.aclose()
        await admin.aclose()
        await auth_env.reset()


# ==========================================================================
# 1、非精确问法：授权 look + 真实 caliber_query 工具调用
# ==========================================================================


async def test_a_vague_question_uses_the_authorized_caliber_through_the_tool(world, monkeypatch):
    """「上个月<指标>是多少」不精确：助手看得见**有授权的**口径与参数，按它搭 agent 调 caliber_query 取数。"""
    asset = world["asset"]
    # 另一份口径没授权给它：一个字都不该出现在这一轮里（清单按资产授权收窄）
    hidden = await _unauthorized_asset(world)
    script = CaliberCopilot(asset["source"])
    es.use_copilot(monkeypatch, script)
    _run_model(monkeypatch, asset["workflow_id"], ["gmv"], WINDOW_INPUTS)

    conversation_id = await _conversation(world["uma"])
    turn_id = await _ask(world["uma"], conversation_id, QUESTION_FUZZY)
    await _job(turn_id)
    row = await _row(turn_id)
    assert row.status == "done", row.error or row.answer
    assert "170" in row.answer, row.answer

    # 授权 look：system 里列着口径这一档、写着按口径查询工具取数的写法，没有 use_caliber 那段协议
    system = str(script.calls[0][0])
    assert "口径" in system and querycore.CALIBER_QUERY in system
    assert "取数写法" in system and "use_caliber" not in system, "使用者这一轮不该有拷 SQL 那条路"
    # 查看结果里给的是有授权的口径：工作流 id、卡、指标 id、入口字段都在
    looked = _look_text(script)
    assert asset["workflow_id"] in looked and f'"card": "{CARD}"' in looked, looked
    assert '"metric_ids": [' in looked and '"gmv"' in looked and '"start"' in looked and '"end"' in looked, looked
    assert hidden["workflow_id"] not in looked, "没授权的口径不该出现在查看结果里"

    # 运行真的调了这个工具：父运行是这一轮的运行，子运行按这一版口径算的数
    job = await _job(turn_id)
    assert job.run_id
    async with SessionLocal() as session:
        run = await session.get(Run, job.run_id)
    [child] = await _child_runs(run.id)
    assert child.workflow_id == asset["workflow_id"] and child.version == 1
    assert run.started_by == "uma" and run.options.get(MASK_RESULTS_OPTION) is True
    assert run.options.get(READ_ONLY_OPTION) is True


# ==========================================================================
# 2、精确问法：固定执行，next = done，运行归属这一轮
# ==========================================================================


async def test_an_exact_question_settles_with_its_own_run(world, monkeypatch):
    """命中固定执行：一整轮零模型，final.next = done（不是 ready），那次运行归属这一轮、页面能重载查看。"""
    asset = world["asset"]
    _no_model(monkeypatch)
    conversation_id = await _conversation(world["uma"])
    turn_id = await _ask(world["uma"], conversation_id, QUESTION_EXACT)

    job = await _job(turn_id)
    row = await _row(turn_id)
    events = await _events(turn_id)
    ops = _ops(events)
    [final] = ops
    assert final["op"] == "final", ops
    execution = final["execution"]
    assert execution["via"] == "querycore" and execution["status"] == "succeeded"
    assert exec_metric(execution) == WINDOW_SUM
    assert final["next"] == policy.NEXT_DONE, "执行已经跑完，不能再说 ready"

    # 使用者拿到的投影：没有卡 id / 工作流 id / 版本 / 清单 / 选择 / 表 / 参数 / 查询
    for key in ("card_id", "workflow_id", "version", "publication", "selection", "queries", "parameters"):
        assert key not in execution, key
    assert asset["workflow_id"] not in json.dumps(final, ensure_ascii=False)

    # 那次运行归属这一轮：run 事件、任务、那一行的 run_id/meta，页面据此重载查看
    [launched] = [event for event in events if event["type"] == "run"]
    assert launched["data"]["run_id"] == job.run_id == row.run_id
    assert row.meta["runId"] == job.run_id and row.status == "done"
    assert row.answer == final["explanation"] and "170" in row.answer
    assert [event["data"]["stage"] for event in events if event["type"] == "stage"] == ["build", "run"], \
        "固定执行不再整轮复核"
    assert row.review == {}

    async with SessionLocal() as session:
        run = await session.get(Run, job.run_id)
    assert run.started_by == "uma"
    assert run.options.get(MASK_RESULTS_OPTION) is True and run.options.get(READ_ONLY_OPTION) is True
    # 对话来源：这次运行记着是哪段对话的哪一轮（和问数据自己发起的运行同一份）
    assert run.origin["conversation_id"] == conversation_id and run.origin["turn_id"] == turn_id
    [child] = await _child_runs(run.id)
    assert child.workflow_id == asset["workflow_id"] and child.version == 1


def exec_metric(execution: dict[str, Any]) -> Any:
    """这次固定执行交回的那个数（按指标名取，使用者看到的就是它）。"""
    return {m["name"]: m["value"] for m in execution["metrics"]}[NAME]


# ==========================================================================
# 3、搭建者及以上那一档不变：还是 use_caliber + 选用写法
# ==========================================================================


async def test_a_builder_still_gets_use_caliber_and_the_compose_view(world, monkeypatch):
    """搭建者及以上照旧：system 里有 use_caliber 那段协议，查看结果给的是选用写法（带版本），不是按口径查询工具那一套。"""
    asset = world["asset"]
    script = CaliberCopilot(asset["source"], reply_after_look=True)
    es.use_copilot(monkeypatch, script)
    conversation_id = await _conversation(world["admin"], title="集成：口径问数（搭建者）")
    turn_id = await _ask(world["admin"], conversation_id, QUESTION_FUZZY)
    row = await _row(turn_id)
    assert row.status == "done", row.error

    system = str(script.calls[0][0])
    assert "选用已发布口径" in system and '"op":"use_caliber"' in system
    looked = _look_text(script)
    assert '"op": "use_caliber"' in looked and f'"version": {asset["version"]}' in looked, looked
    assert "取数写法" not in looked and querycore.CALIBER_QUERY not in looked, "搭建者那一档不讲按口径查询工具"
