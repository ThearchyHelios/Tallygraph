"""真实 Agent 循环：模型提前结束时继续调查，不能补齐证据时明确降级。"""
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.core import artifact_store
from app.engine.evidence_reader import read_sealed
from app.eval.score import read_answer
from app.providers import mock_model
from tests.test_analysis_tools import (DECOMPOSE, DECOMPOSE_ARGS, SQL, TOOL, agent_graph, engine_up,
                                       events, run_graph, shop)


async def test_runtime_continues_after_premature_answer_then_records_the_remaining_gap(monkeypatch, shop):
    calls = [(TOOL, {"sql": SQL}), (DECOMPOSE, DECOMPOSE_ARGS)]
    nudges = []

    def decide(model, messages):
        continued = [m for m in messages if isinstance(m, HumanMessage) and "调查尚未完成" in str(m.content)]
        nudges[:] = continued
        if not continued:
            return AIMessage(content="销售额下降。")
        done = sum(isinstance(m, ToolMessage) for m in messages)
        if done < len(calls):
            name, args = calls[done]
            return AIMessage(content="", tool_calls=[{"id": f"c{done}", "name": name, "args": args}])
        return AIMessage(content="已按地区拆解，仍需核对总数。")

    monkeypatch.setattr(mock_model.MockChatModel, "_decide", decide)
    row = await run_graph(agent_graph(max_steps=8))
    sealed = await read_sealed(row.id)
    bot = artifact_store.load(sealed.outputs["bot"])
    assert len(nudges) == 2
    assert [c["tool"] for c in bot["tool_calls"]] == [TOOL, DECOMPOSE]
    assert bot["investigation"]["status"] == "partial"
    assert bot["investigation"]["stage"] == "closure"
    assert bot["investigation"]["stop_reason"] == "evidence_insufficient"
    assert "现有证据不足以确认根因" in bot["text"]
    assert any(e.get("code") == "investigation_incomplete" for e in await events(row.id, "log", "bot"))
    answer = await read_answer(row.id)
    assert answer.investigations[0]["query_count"] == 1


async def test_unfinished_investigation_cannot_receive_formal_issuance(monkeypatch, shop):
    monkeypatch.setattr(mock_model.MockChatModel, "_decide", lambda *_: AIMessage(content="尚未取得证据。"))
    graph = agent_graph(max_steps=4)
    graph["nodes"].insert(-1, {"id": "card", "type": "metrics", "data": {"config": {
        "caliber": "调查", "metrics": [{"id": "n", "expression": "1"}]}}})
    graph["edges"] = [{"source": a["id"], "target": b["id"]}
                      for a, b in zip(graph["nodes"], graph["nodes"][1:])]
    graph["nodes"][-1]["data"]["config"]["contract"] = {"metrics_from": "card"}
    row = await run_graph(graph)
    assert row.output["_issuance"]["tier"] != "formal"
    assert any("异常调查未完成" in g for g in row.output["_issuance"]["gaps"])
