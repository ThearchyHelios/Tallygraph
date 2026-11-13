"""K12 受限的自然语言修改与固定请求路由（app/ledger/nlchange.py、api/ledger.py、api/copilot.py 的接缝）。

这份文件盯住这几条：

1. **自然语言只产出 K03 同一份提案。** 一句话提到已声明能力（统计时间口径、退款处理）就落成 K03 同一形状的 operations，
   影响摘要和失效/绑定结论由 `changes.summary` 在同一批操作上现算——和表单路径同一份；一句话说不清（「排除退款」，词汇表
   里只有「排除全额退款」和「全部计入」）就只给澄清项，一条操作也不产出，不替人猜、也不擅自改口径。
2. **应用只有一条路。** 提案不落库；confirm=false 只预览；confirm=true 也只是薄适配，翻成一次 DraftPatchIn 交给现有
   变更服务（patch_draft）——expected_revision / request_id / CAS / 绑定同步 / 校验一条不少，失败不擅自改口径。
3. **模型只能提建议。** from_model 里的 operations 照样过 K03 的白名单和值域；actor / review / verified / approved 这类
   自报批准的键一律丢掉，影响摘要和失效结论一律由平台现算。
4. **Ask Data 固定请求路由。** 已经明确 workflow/card/metric_ids/inputs 的请求直接进 QueryCore（路线只把版本固定一次，
   QueryCore 不再另挑一次，执行路径内部 LLM 仍为 0）；没有匹配口径就进探索、含糊业务条件先澄清、请求里的业务条件和
   匹配到的口径对不上就明确说「先改口径」——绝不静默换计算规则。
5. **真实 HTTP 与权限。** 走真实接口，不连真实业务库（合成 SQLite 库）；开登录后 user 角色调这几个接口是 403。

模型一律不调用（路由本身零模型：注入会炸的假模型）；试跑/执行走真实引擎但只用合成库。
"""
from __future__ import annotations

import contextlib
import copy
import json
import uuid
from collections.abc import Mapping
from typing import Any

import pytest
from fastapi import HTTPException
from sqlalchemy import delete, func, select

from app.db.base import SessionLocal
from app.db.models import (CaliberDraft, CaliberWriteRequest, DataSource, Run, Workflow, WorkflowPublication,
                           WorkflowVersion)
from app.ledger import changes, nlchange
from tests import binding_samples as B
from tests.test_ledger_docdraft_api import client  # noqa: F401 - fixture 和别的账本用例共用
from tests.publication_samples import add_version, make_workflow, publish
from tests.test_querycore_execute import WINDOW_SUM  # noqa: F401 - 合成库的独立预期
from tests.test_querycore_execute import graph as qc_graph
from tests.test_querycore_execute import metrics as qc_metrics
from tests.test_querycore_execute import fresh_source as qc_source

#: 这个文件自己建的工作流（连同版本、草稿、幂等记录、发布事件、数据源、应用）跑完删掉，不给别的用例留行
OWN_PREFIX = "K12 "
_MADE: dict[str, list[str]] = {"workflows": [], "sources": [], "apps": []}


@pytest.fixture(autouse=True)
async def cleanup_own_rows():
    yield
    async with SessionLocal() as session:
        ids = list(_MADE["workflows"])
        if ids:
            for model in (CaliberWriteRequest, CaliberDraft, WorkflowPublication, WorkflowVersion):
                await session.execute(delete(model).where(model.workflow_id.in_(ids)))
            await session.execute(delete(Workflow).where(Workflow.id.in_(ids)))
        for name in _MADE["sources"]:
            await session.execute(delete(DataSource).where(DataSource.name == name))
        if _MADE["apps"]:
            from app.db.models_apps import App

            # 授权和应用版本都挂在应用上（ondelete=CASCADE）：删应用就清干净
            await session.execute(delete(App).where(App.id.in_(list(_MADE["apps"]))))
        _MADE["workflows"].clear()
        _MADE["sources"].clear()
        _MADE["apps"].clear()
        await session.commit()


# ==========================================================================
# 1、一句话 → K03 提案（单元；不落库、不调模型）
# ==========================================================================

def _order_graph(*, values: dict | None = None) -> dict:
    """K04 的合成订单口径：两个共享一条受控模板 SQL 的指标，各自声明时间口径和退款规则。"""
    return B.graph(values=values if values is not None else B.OLD)


def test_declared_capabilities_are_recognized_and_previewed():
    """「按支付时间统计，排除全额退款」→ 两个指标各两条 K03 操作，预览里定义变了、绑定能确定性同步。"""
    graph = _order_graph()
    proposal = nlchange.propose(graph, "card", "把订单口径改成按支付时间统计，排除全额退款")
    assert proposal.ready and not proposal.clarifications
    assert [(op["metric_id"], op["path"], op["value"]) for op in proposal.operations] == [
        ("m1", "/time_role", "payment_time"), ("m2", "/time_role", "payment_time"),
        ("m1", "/refund_rule", "exclude_full_refund"), ("m2", "/refund_rule", "exclude_full_refund")]
    assert proposal.metrics == ("m1", "m2")
    preview = proposal.preview
    assert preview["preview"] is True and preview["persisted"] is False and preview["applied"] is True
    assert preview["classification"] == "definition"
    assert preview["definition_hash"]["changed"] is True
    assert preview["binding"]["status"] == "aligned" and preview["binding"]["synced"] is True
    assert preview["binding"]["metrics"] == ["m1", "m2"]
    # 预览不改原图：原图里那条 SQL 还是旧写法
    assert B.render(B.OLD) in str(graph)


def test_the_same_operations_from_nl_and_from_a_form_give_the_same_receipt():
    """同一批 operations，表单路径和自然语言路径在原图深拷贝上得到同一份回执（同一失效与绑定结论）。"""
    graph = _order_graph()
    proposal = nlchange.propose(graph, "card", "按支付时间统计，排除全额退款")
    form_change = changes.apply_operations(graph, card_id="card", operations=list(proposal.operations))
    form_receipt = changes.summary(form_change, applied=bool(form_change.applied), revision=None)
    preview = {key: value for key, value in proposal.preview.items() if key not in ("preview", "persisted")}
    assert preview == form_receipt


def test_an_ambiguous_condition_is_clarified_and_produces_no_operations():
    """「排除退款」没区分全额/部分：先澄清，一条操作都不产出（不替人猜、不擅自改口径）。"""
    proposal = nlchange.propose(_order_graph(), "card", "排除退款后重新算")
    assert proposal.ready is False and proposal.operations == () and proposal.preview is None
    [item] = proposal.clarifications
    assert item.kind == nlchange.KIND_AMBIGUOUS and item.field == "refund_rule"
    assert {option["value"] for option in item.options} == {"include_all", "exclude_full_refund"}
    assert "排除全额退款" in item.question and "全部计入" in item.question


def _refund_rate_graph() -> dict:
    """一张指标名里带「退款」的口径卡（退款率）：名词里带「退款」不等于在谈退款规则。"""
    metrics = B.card_metrics()
    metrics[0]["name"] = "退款率"
    metrics[0]["synonyms"] = ["退款比例"]
    return B.graph(values=B.OLD, metrics=metrics)


def test_a_metric_name_containing_refund_does_not_ask_about_the_refund_rule():
    """「上个月退款率是多少」「把退款率改成按支付时间」不能因为指标名里有「退款」就误问退款处理。"""
    graph = _refund_rate_graph()
    # 只是问退款率：不是改口径，更不是问退款规则——澄清项是「没匹配到能力」，不是退款处理
    asked = nlchange.propose(graph, "card", "上个月退款率是多少")
    assert asked.ready is False and [item.field for item in asked.clarifications] == [None]
    assert asked.clarifications[0].kind == nlchange.KIND_NO_MATCH
    # 改的是时间口径：只产出 time_role 的操作，不为指标名里的「退款」产出澄清项
    changed = nlchange.propose(graph, "card", "把退款率改成按支付时间")
    assert changed.ready and not changed.clarifications
    assert [(op["path"], op["value"]) for op in changed.operations] == [("/time_role", "payment_time")] * 2
    # 指标名在卡上（含同义词「退款比例」）时也一样：识别前先把指标名挖掉
    for text in ("退款比例改成按支付时间", "退款率按支付时间口径算"):
        one = nlchange.propose(graph, "card", text)
        assert one.ready and {op["path"] for op in one.operations} == {"/time_role"}
    # 真的在谈退款规则时照旧识别/澄清
    assert nlchange.propose(graph, "card", "退款率改成排除全额退款").operations[0]["path"] == "/refund_rule"
    # 指标名恰好是取值说法的一部分（「支付」）时不会被挖掉：「按支付时间」照旧认得出
    partial = B.card_metrics()
    partial[0]["name"] = "支付"
    shifted = nlchange.propose(B.graph(values=B.OLD, metrics=partial), "card", "支付这个口径改成按支付时间")
    assert shifted.ready and {op["path"] for op in shifted.operations} == {"/time_role"}


def test_a_negated_exclusion_means_include_all_not_the_opposite():
    """「不要排除」「不排除」「无需排除全额退款」都是「全部计入」，绝不生成相反的「排除全额退款」。"""
    for text in ("不要排除", "不排除", "无需排除全额退款", "不要排除全额退款", "不排除退款", "不排除退货"):
        matches, clarifications = nlchange.match_conditions(text)
        assert [(m.field, m.value) for m in matches] == [("refund_rule", "include_all")], text
        assert clarifications == [], text
    # 正着说的排除照旧是排除全额退款
    matches, clarifications = nlchange.match_conditions("排除全额退款")
    assert [(m.field, m.value) for m in matches] == [("refund_rule", "exclude_full_refund")]
    assert clarifications == []
    # 「排除退款」没分全额/部分：仍然澄清
    assert [item.field for item in nlchange.match_conditions("排除退款")[1]] == ["refund_rule"]
    # 落成操作时也是 include_all（不是 exclude）
    proposal = nlchange.propose(_order_graph(), "card", "把订单数改成不要排除退款")
    assert {op["value"] for op in proposal.operations} == {"include_all"}


def test_other_negations_do_not_flip_to_the_opposite_value():
    """别的否定不倒过来认：「不按支付时间」不等于「按下单时间」，只报说不清。"""
    for text in ("不按支付时间统计", "不要按下单时间", "取消按支付时间"):
        matches, clarifications = nlchange.match_conditions(text)
        assert matches == [], text
        assert [item.field for item in clarifications] == ["time_role"], text
    assert nlchange.propose(_order_graph(), "card", "不按支付时间统计").operations == ()


def test_an_unrelated_sentence_is_clarified_not_silently_ignored():
    """没有提到任何已声明能力（更不是通用聊天 DSL）：只给澄清项，不静默当成「不用改」。"""
    proposal = nlchange.propose(_order_graph(), "card", "把销售额乘以二再上报")
    assert proposal.ready is False
    [item] = proposal.clarifications
    assert item.kind == nlchange.KIND_NO_MATCH
    assert {option["value"] for option in item.options} == {"time_role", "refund_rule"}


def test_a_model_suggestion_cannot_self_approve_or_write_forbidden_keys():
    """模型自报批准无效：actor / review / verified / approved 这类键一律丢掉，失效结论由平台现算。"""
    graph = _order_graph()
    payload = {"approved": True, "actor": "mallory", "verified": True, "summary": "不会失效",
               "operations": [{"op": "set", "metric_id": "m1", "path": "/time_role", "value": "payment_time",
                               "reviewer": "mallory", "approved": True}]}
    proposal = nlchange.from_model(graph, "card", payload)
    assert proposal.ready
    assert set(proposal.ignored_claims) >= {"approved", "actor", "verified", "summary", "reviewer"}
    # 操作里只剩 K03 的四个键；自报的「批准」不在其中
    assert proposal.operations == ({"op": "set", "metric_id": "m1", "path": "/time_role", "value": "payment_time"},)
    # 平台按图现算：改了定义，旧验证一律保守失效——模型说「不会失效」不算数
    assert proposal.preview["validation"]["invalidated"] is True
    assert proposal.preview["applied"] is True


def test_a_model_suggested_operation_is_still_validated():
    """模型提的操作照样过白名单与值域：不认识的路径拒。"""
    payload = {"operations": [{"op": "set", "metric_id": "m1", "path": "/definition/citations", "value": []}]}
    with pytest.raises(changes.ChangeRefused) as caught:
        nlchange.from_model(_order_graph(), "card", payload)
    assert caught.value.code == changes.OPERATION_PATH


# ==========================================================================
# 2、HTTP：提案只读、confirm 才落版，且落回现有变更服务
# ==========================================================================

async def _order_draft() -> dict:
    """一份已接管的订单口径草稿（K04 的合成图，形状同 docattach 的产物）。"""
    body = await B.draft(_order_graph(), name=f"{OWN_PREFIX}订单口径")
    _MADE["workflows"].append(body["workflow_id"])
    return body


async def versions_of(workflow_id: str) -> list[int]:
    async with SessionLocal() as session:
        return list((await session.execute(select(WorkflowVersion.version).where(
            WorkflowVersion.workflow_id == workflow_id).order_by(WorkflowVersion.version))).scalars())


async def graph_of(workflow_id: str) -> dict:
    async with SessionLocal() as session:
        workflow = await session.get(Workflow, workflow_id)
        return copy.deepcopy(workflow.graph)


def fetch_config(graph: dict) -> dict:
    return next(n for n in graph["nodes"] if n["id"] == "fetch")["data"]["config"]


async def test_the_change_proposal_endpoint_previews_and_only_applies_on_confirm(client):
    """change-proposal 只读；nl-change 默认只预览；confirm=true 才落一版，且真的改到图上（定义 + 受控 SQL 一起）。"""
    body = await _order_draft()
    url = f"/api/ledger/drafts/{body['id']}"

    preview = await client.post(f"{url}/change-proposal", json={"instruction": "按支付时间统计，排除全额退款"})
    assert preview.status_code == 200, preview.text
    proposal = preview.json()["proposal"]
    assert proposal["ready"] and len(proposal["operations"]) == 4
    assert proposal["preview"]["binding"]["synced"] is True
    assert await versions_of(body["workflow_id"]) == [1], "提案是只读的，不新增版本"

    draft_only = await client.post(f"{url}/nl-change", json={"instruction": "按支付时间统计，排除全额退款"})
    assert draft_only.status_code == 200, draft_only.text
    assert draft_only.json()["applied"] is False and draft_only.json()["persisted"] is False
    assert await versions_of(body["workflow_id"]) == [1], "默认不自动落版"

    applied = await client.post(f"{url}/nl-change",
                                json={"instruction": "按支付时间统计，排除全额退款",
                                      "expected_revision": body["revision"], "confirm": True,
                                      "request_id": "k12-0001"})
    assert applied.status_code == 200, applied.text
    change = applied.json()["change"]
    assert change["classification"] == "definition" and change["revision"] == 2
    assert await versions_of(body["workflow_id"]) == [1, 2]
    graph = await graph_of(body["workflow_id"])
    config = fetch_config(graph)
    assert config["args"]["sql"] == B.render(B.NEW), "受控模板按新业务值确定性展开"
    assert config["binding"]["values"] == B.NEW and config["hints"] == {"keep": "me"}

    # 那句「按支付时间统计」说的话和真落库那条路同一次：批过的回执是补上的
    replay = await client.post(f"{url}/nl-change",
                               json={"instruction": "按支付时间统计，排除全额退款",
                                     "expected_revision": body["revision"], "confirm": True,
                                     "request_id": "k12-0001"})
    assert replay.status_code == 200 and replay.json()["replayed"] is True
    assert await versions_of(body["workflow_id"]) == [1, 2], "同键重放不新增版本"


async def test_the_preview_matches_the_change_the_patch_returns(client):
    """提案的预览和 PATCH 真落库的回执逐字段一致（除 revision / preview 标记）。"""
    body = await _order_draft()
    url = f"/api/ledger/drafts/{body['id']}"
    proposal = (await client.post(f"{url}/change-proposal",
                                  json={"instruction": "按支付时间统计，排除全额退款"})).json()["proposal"]
    patch = await client.patch(url, json={"operations": proposal["operations"],
                                          "expected_revision": body["revision"]})
    assert patch.status_code == 200, patch.text
    change = patch.json()["change"]
    preview = {key: value for key, value in proposal["preview"].items() if key not in ("preview", "persisted",
                                                                                      "revision")}
    assert {key: value for key, value in change.items() if key != "revision"} == preview


async def test_an_ambiguous_instruction_over_http_writes_nothing(client):
    """含糊业务条件：HTTP 回 409 + 澄清项，图一个字节都不动、不新增版本。"""
    body = await _order_draft()
    before = await graph_of(body["workflow_id"])
    res = await client.post(f"/api/ledger/drafts/{body['id']}/nl-change",
                            json={"instruction": "把退款排除掉", "expected_revision": body["revision"],
                                  "confirm": True})
    assert res.status_code == 409, res.text
    assert res.json()["code"] == nlchange.PROPOSAL_AMBIGUOUS
    assert res.json()["clarifications"][0]["field"] == "refund_rule"
    assert await versions_of(body["workflow_id"]) == [1]
    assert await graph_of(body["workflow_id"]) == before, "失败不擅自改口径"


# ==========================================================================
# 3、Ask Data 固定请求路由
# ==========================================================================

async def _published_querycore_asset(*, graph: dict | None = None, version: int = 1) -> dict:
    """一份已发布、有发布清单的固定口径（合成库），可以按固定口径执行。"""
    asset = await make_workflow(graph=graph if graph is not None else qc_graph(), version=version)
    _MADE["workflows"].append(asset["workflow_id"])
    return asset


@pytest.fixture
async def engine_up(tmp_path):
    """执行走真实引擎（合成 SQLite 库）。"""
    from app.engine.runner import run_manager

    await run_manager.setup()
    name = await qc_source(tmp_path)
    _MADE["sources"].append(name)
    yield
    await run_manager.shutdown()


def _no_model(monkeypatch) -> None:
    """QueryCore / 路由内部 LLM = 0：任何模型调用都让这次执行炸掉。"""
    from app.providers import mock_model

    def boom(self, messages):
        raise AssertionError("固定请求路由与 QueryCore 不该调用模型")

    monkeypatch.setattr(mock_model.MockChatModel, "_decide", boom)


async def runs_total() -> int:
    async with SessionLocal() as session:
        return int(await session.scalar(select(func.count(Run.id))) or 0)


async def test_a_determined_request_routes_to_querycore_and_executes(client, engine_up, monkeypatch):
    """已经明确 workflow/card/metric_ids/inputs 的请求直接进 QueryCore：交回类型化结果，运行来源是口径固定执行。"""
    _no_model(monkeypatch)
    asset = await _published_querycore_asset()
    await publish(client, asset)
    metric_ids = [m["id"] for m in qc_metrics() if m["id"] in ("gmv", "units")]

    res = await client.post("/api/ledger/ask", json={
        "workflow_id": asset["workflow_id"], "card_id": "card", "metric_ids": metric_ids,
        "inputs": {"start": "2026-09-01", "end": "2026-10-01"}})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["route"]["mode"] == nlchange.ROUTE_EXECUTE
    assert body["route"]["request"]["version"] == 1
    result = body["result"]
    assert result["ok"] is True and result["version"] == 1
    assert {m["id"]: m["value"] for m in result["metrics"]}["gmv"] == WINDOW_SUM
    async with SessionLocal() as session:
        run = await session.get(Run, result["run_id"])
    assert run.origin["service"] == "querycore" and run.origin["purpose"] == "compose"


async def test_a_pinned_version_is_not_resolved_again(client, engine_up):
    """点名的那一版被固定下来：不重新挑推荐版、不重复规划（v2 的值和 v1 不同）。"""
    asset = await _published_querycore_asset()
    await publish(client, asset)
    asset = await add_version(asset, qc_graph(sales_sql=(
        "SELECT SUM(amount) AS gmv, SUM(qty) AS units, '合成标签' AS label FROM orders "
        "WHERE status IN ('paid', 'open') AND paid_at >= '{{ input.start }}' AND paid_at < '{{ input.end }}'")),
        version=2)
    _MADE["workflows"].append(asset["workflow_id"])
    await publish(client, asset, version=2)
    await publish(client, asset, version=1)          # 推荐版回到 v1

    named = await client.post("/api/ledger/ask", json={
        "workflow_id": asset["workflow_id"], "card_id": "card", "metric_ids": ["gmv"], "version": 2,
        "inputs": {"start": "2026-09-01", "end": "2026-10-01"}})
    assert named.status_code == 200, named.text
    assert named.json()["route"]["request"]["version"] == 2
    assert named.json()["result"]["version"] == 2 and named.json()["result"]["metrics"][0]["value"] != WINDOW_SUM

    followed = await client.post("/api/ledger/ask", json={
        "workflow_id": asset["workflow_id"], "card_id": "card", "metric_ids": ["gmv"],
        "inputs": {"start": "2026-09-01", "end": "2026-10-01"}})
    assert followed.json()["route"]["request"]["version"] == 1
    assert followed.json()["result"]["metrics"][0]["value"] == WINDOW_SUM


async def test_a_decimal_string_version_is_a_pinned_version_not_the_recommended_one(client):
    """fixed_request 里 version 写成十进制数字字符串时按**显式版本**处理：固定到那一版，不回退推荐版。"""
    asset = await _published_querycore_asset()
    await publish(client, asset)
    asset = await add_version(asset, qc_graph(sales_sql=(
        "SELECT SUM(amount) AS gmv, SUM(qty) AS units, '合成标签' AS label FROM orders "
        "WHERE status IN ('paid', 'open') AND paid_at >= '{{ input.start }}' AND paid_at < '{{ input.end }}'")),
        version=2)
    _MADE["workflows"].append(asset["workflow_id"])
    await publish(client, asset, version=2)
    await publish(client, asset, version=1)          # 推荐版回到 v1

    async with SessionLocal() as session:
        pinned = await nlchange.route(session, request={
            "workflow_id": asset["workflow_id"], "card_id": "card", "metric_ids": ["gmv"], "version": "2",
            "inputs": {"start": "2026-09-01", "end": "2026-10-01"}})
        followed = await nlchange.route(session, request={
            "workflow_id": asset["workflow_id"], "card_id": "card", "metric_ids": ["gmv"],
            "inputs": {"start": "2026-09-01", "end": "2026-10-01"}})
    assert pinned.mode == nlchange.ROUTE_EXECUTE and pinned.request["version"] == 2
    assert followed.request["version"] == 1, "没写版本才读推荐版"


async def test_an_invalid_version_is_refused_not_silently_recommended(client):
    """非法版本（不是正整数）先澄清/拒绝，绝不静默按推荐版执行。"""
    asset = await _published_querycore_asset()
    await publish(client, asset)
    before = await runs_total()
    async with SessionLocal() as session:
        for bad, _why in (("abc", "文字"), ("2.5", "小数"), ("0", "零"), ("-1", "负数"),
                          ("", None), (None, None)):
            routed = await nlchange.route(session, request={
                "workflow_id": asset["workflow_id"], "card_id": "card", "metric_ids": ["gmv"],
                "version": bad, "inputs": {}})
            if _why is None:
                assert routed.mode == nlchange.ROUTE_EXECUTE, bad
                assert routed.request["version"] == 1, "空版本才读推荐版"
                continue
            assert routed.mode == nlchange.ROUTE_CLARIFY and routed.code == nlchange.ROUTE_VERSION_INVALID, bad
            assert routed.request == {} and routed.clarifications[0].field == "version", bad
            assert "整数" in routed.reason or "整数" in routed.clarifications[0].question, bad
    assert await runs_total() == before, "非法版本不发起任何运行"


async def test_the_copilot_seam_clarifies_an_invalid_version(monkeypatch):
    """接缝路径（fixed_request 原样传字典）：非法版本先澄清，不静默换成推荐版。"""
    _no_model(monkeypatch)
    from app.api import copilot

    payload = copilot.GenerateIn(instruction="上个月销售额", intent="answer",
                                 fixed_request={"workflow_id": "wf-x", "card_id": "card",
                                                "metric_ids": ["m1"], "version": "abc"})
    ops = [op async for op in copilot.answer_ops(payload)]
    assert len(ops) == 1 and ops[0]["op"] == "reply"
    assert "整数" in ops[0]["text"]


async def test_the_copilot_seam_executes_a_decimal_string_version(client, engine_up, monkeypatch):
    """接缝路径真的按十进制字符串版本固定执行：跑到 v2，不是推荐版 v1。"""
    _no_model(monkeypatch)
    from app.api import copilot

    asset = await _published_querycore_asset()
    await publish(client, asset)
    asset = await add_version(asset, qc_graph(sales_sql=(
        "SELECT SUM(amount) AS gmv, SUM(qty) AS units, '合成标签' AS label FROM orders "
        "WHERE status IN ('paid', 'open') AND paid_at >= '{{ input.start }}' AND paid_at < '{{ input.end }}'")),
        version=2)
    _MADE["workflows"].append(asset["workflow_id"])
    await publish(client, asset, version=2)
    await publish(client, asset, version=1)          # 推荐版回到 v1

    payload = copilot.GenerateIn(instruction="上个月销售额", intent="answer", fixed_request={
        "workflow_id": asset["workflow_id"], "card_id": "card", "metric_ids": ["gmv"], "version": "2",
        "inputs": {"start": "2026-09-01", "end": "2026-10-01"}})
    ops = [op async for op in copilot.answer_ops(payload) if op.get("op") != "heartbeat"]
    [final] = ops
    assert final["op"] == "final" and final["execution"]["ok"] is True
    assert final["execution"]["metrics"][0]["value"] != WINDOW_SUM, "v2 把 open 也算进去，值应该不一样"
    async with SessionLocal() as session:
        run = await session.get(Run, final["run_id"])
    assert run.version == 2, "按点名的字符串版本固定执行，不是推荐版"


async def test_no_matching_caliber_goes_to_exploration(client, monkeypatch):
    """指不到口径就明确进探索：不静默挑一份口径执行、也不发起任何运行。"""
    _no_model(monkeypatch)
    before = await runs_total()
    res = await client.post("/api/ledger/ask", json={"workflow_id": "nope-not-a-workflow", "card_id": "card",
                                                     "metric_ids": ["gmv"]})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["route"]["mode"] == nlchange.ROUTE_EXPLORE
    assert body["route"]["code"] == nlchange.ROUTE_NO_CALIBER and body["result"] is None
    assert await runs_total() == before, "进探索不发起运行"


async def test_an_ambiguous_business_condition_clarifies_before_executing(client):
    """含糊业务条件先澄清：不按旧口径静默执行、不发起运行。"""
    before = await runs_total()
    res = await client.post("/api/ledger/ask", json={"workflow_id": "wf-x", "card_id": "card",
                                                     "metric_ids": ["gmv"], "instruction": "排除退款"})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["route"]["mode"] == nlchange.ROUTE_CLARIFY
    assert body["route"]["code"] == nlchange.ROUTE_AMBIGUOUS
    assert body["route"]["clarifications"][0]["field"] == "refund_rule"
    assert body["result"] is None and await runs_total() == before


async def test_a_contradicting_condition_does_not_swap_the_rule(client):
    """请求里的业务条件和匹配到的口径对不上：明确进探索说「先改口径」，绝不静默换计算规则。"""
    workflow_id = await B.publish(_order_graph(), name=f"{OWN_PREFIX}订单口径")
    _MADE["workflows"].append(workflow_id)
    before = await runs_total()
    res = await client.post("/api/ledger/ask", json={"workflow_id": workflow_id, "card_id": "card",
                                                     "metric_ids": ["m1"], "instruction": "按支付时间统计"})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["route"]["mode"] == nlchange.ROUTE_EXPLORE
    assert body["route"]["code"] == nlchange.ROUTE_CONDITION_MISMATCH
    assert "不静默换计算规则" in body["route"]["reason"]
    assert body["result"] is None and await runs_total() == before


async def test_missing_metric_ids_clarifies_instead_of_whole_card(client):
    """没点名指标就先澄清：空的选择不静默当整卡。"""
    workflow_id = await B.publish(_order_graph(), name=f"{OWN_PREFIX}订单口径")
    _MADE["workflows"].append(workflow_id)
    res = await client.post("/api/ledger/ask", json={"workflow_id": workflow_id, "card_id": "card"})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["route"]["mode"] == nlchange.ROUTE_CLARIFY
    assert body["route"]["code"] == nlchange.ROUTE_METRICS_REQUIRED
    assert {option["value"] for option in body["route"]["clarifications"][0]["options"]} == {"m1", "m2"}


async def test_the_copilot_seam_routes_a_fixed_request_without_a_model(monkeypatch):
    """问数据那条路的接缝：带 fixed_request 时不经模型、不建图，直接交路由结论（含糊条件先澄清）。"""
    _no_model(monkeypatch)
    from app.api import copilot

    payload = copilot.GenerateIn(instruction="订单口径里排除退款之后是多少", intent="answer",
                                 fixed_request={"workflow_id": "wf-x", "card_id": "card",
                                                "metric_ids": ["m1"], "instruction": "排除退款"})
    ops = [op async for op in copilot.answer_ops(payload)]
    assert len(ops) == 1 and ops[0]["op"] == "reply"
    assert "排除全额退款" in ops[0]["text"]


# ==========================================================================
# 4、权限：开登录后这几个接口只给搭建者
# ==========================================================================

async def test_the_k12_endpoints_need_a_builder():
    from tests.fakes import auth_env

    await auth_env.reset()
    admin = await auth_env.turn_on_local()
    await auth_env.make_user("plain", role="user")
    user = await auth_env.login("plain")
    try:
        ask = await user.post("/api/ledger/ask", json={"workflow_id": "x"})
        assert ask.status_code == 403, ask.text
        proposal = await user.post(f"/api/ledger/drafts/{uuid.uuid4().hex}/change-proposal",
                                   json={"instruction": "排除全额退款"})
        assert proposal.status_code == 403, proposal.text
        ok = await admin.post("/api/ledger/ask", json={"workflow_id": "nope"})
        assert ok.status_code == 200, ok.text
    finally:
        await user.aclose()
        await admin.aclose()
        await auth_env.reset()


# ==========================================================================
# 5、普通问数的保守默认路由（K12）：高置信才自动进 QueryCore，其余回旧路
# ==========================================================================

#: 自动路由专用的指标名：全局唯一（别的用例的指标名都不是它的子串，它也不含别人的名字）
AUTO_NAME = "自动路由专用净额甲"


def auto_graph(metric_name: str = AUTO_NAME, *, sales_sql: str | None = None,
               fields: list[dict] | None = None) -> dict:
    """把合成口径的 gmv 指标换成唯一名字，交给自动路由匹配用。"""
    made = qc_graph(sales_sql=sales_sql)
    start = next(n for n in made["nodes"] if n["id"] == "start")
    if fields is not None:
        start["data"]["config"]["fields"] = fields
    card = next(n for n in made["nodes"] if n["id"] == "card")
    card["data"]["config"]["metrics"][0]["name"] = metric_name
    return made


async def _auto_asset(*, metric_name: str = AUTO_NAME, graph: dict | None = None, version: int = 1) -> dict:
    asset = await make_workflow(graph=graph if graph is not None else auto_graph(metric_name), version=version)
    _MADE["workflows"].append(asset["workflow_id"])
    return asset


def _answer(question: str) -> Any:
    from app.api import copilot

    return copilot.GenerateIn(instruction=question, intent="answer")


def _fixed(asset: dict, **extra: Any) -> Any:
    """一条已经明确的固定请求（Ask Data 的接缝）：口径 + 卡 + 指标 + 入口取值。"""
    from app.api import copilot

    request = {"workflow_id": asset["workflow_id"], "card_id": "card", "metric_ids": ["gmv"],
               "inputs": {"start": "2026-09-01", "end": "2026-10-01"}, **extra}
    return copilot.GenerateIn(instruction="上个月销售额是多少", intent="answer", fixed_request=request)


async def _ops(question: str) -> list[dict]:
    from app.api import copilot

    return [op async for op in copilot.answer_ops(_answer(question)) if op.get("op") != "heartbeat"]


async def _answer_ops(payload: Any) -> list[dict]:
    from app.api import copilot

    return [op async for op in copilot.answer_ops(payload) if op.get("op") != "heartbeat"]


@contextlib.contextmanager
def _acting(username: str, role: str = "user"):
    """这一次请求是谁：开登录后的普通 user（restricted()），或者搭建者及以上。"""
    from app.api import deps

    token = deps.set_principal(deps.Principal(username=username, role=role, verified=True, mode="local"))
    try:
        yield
    finally:
        deps.reset_principal(token)


async def _grant_app(workflow_id: str, version: int, *, who: str, role: str = "use") -> str:
    """一份把这一版口径给这个使用者的应用：未归档、当前发布版钉住它，授权一条（AppGrant）。

    资产授权就是这么来的（app/apps/access.usable_calibers：授权给它的应用当前版本钉住的那些口径版本）。"""
    from app.db.models_apps import App, AppGrant, AppVersion

    async with SessionLocal() as session:
        app = App(name=f"{OWN_PREFIX}口径看板", draft={}, published_version=1)
        session.add(app)
        await session.flush()
        pins = [{"alias": "main", "workflow_id": workflow_id, "version": version, "use": "view"}]
        session.add(AppVersion(app_id=app.id, version=1, spec={"pins": pins}, spec_hash="x", pins=pins))
        session.add(AppGrant(app_id=app.id, principal_kind="user", principal=who, role=role))
        await session.commit()
        _MADE["apps"].append(app.id)
        return app.id


#: 普通 user 的操作里不该出现的**键**（口径身份、清单、选择、表、参数、查询与哈希）
_FORBIDDEN_KEYS = ("card_id", "workflow_id", "version", "publication", "manifest", "selection", "tables",
                   "parameters", "queries", "sql", "graph_hash", "hash")


def _no_internals(ops: list[dict], *, workflow_id: str) -> None:
    """普通 user 拿到的操作里不该出现任何内部引用：口径身份、清单、选择、表、参数、查询与 SQL 正文。

    机器码（比如 querycore_manifest_required）不算内部引用：它给程序认，不带着任何身份或语句。"""
    text = json.dumps(ops, ensure_ascii=False)
    assert workflow_id not in text, f"操作里出现了工作流 id：{text}"
    found = _keys_of(ops) & set(_FORBIDDEN_KEYS)
    assert not found, f"操作里出现了内部字段：{found}"
    assert "select " not in text.lower(), f"操作里出现了 SQL 正文：{text}"

def _keys_of(value: Any) -> set[str]:
    """这份操作里出现过的所有键（递归）：按**键**判内部引用，不按子串判（机器码里带 manifest 不算泄露）。"""
    if isinstance(value, Mapping):
        found = {str(key) for key in value}
        for item in value.values():
            found |= _keys_of(item)
        return found
    if isinstance(value, (list, tuple)):
        found = set()
        for item in value:
            found |= _keys_of(item)
        return found
    return set()


@pytest.fixture
def model_unconfigured(monkeypatch):
    """助手模型「没配好」：旧路径到这里会回 400；自动路由命中时根本不取模型。"""
    from app.api import copilot
    from app.providers.factory import ProviderNotConfigured

    async def boom(*a, **kw):
        raise ProviderNotConfigured("测试里没有配助手模型")

    monkeypatch.setattr(copilot, "_op_model", boom)
    return monkeypatch


async def test_a_unique_exact_metric_auto_routes_to_querycore_without_a_model(client, engine_up, monkeypatch):
    """唯一精确指标：answer_ops 不经模型直接进 QueryCore，final 标出固定执行与 run_id/manifest/selection。"""
    _no_model(monkeypatch)
    asset = await _auto_asset()
    await publish(client, asset)

    ops = await _ops(f"{AUTO_NAME} 2026年9月是多少")
    assert len(ops) == 1, f"自动路由命中后不该再有别的操作：{ops}"
    [final] = ops
    assert final["op"] == "final" and final["execution"]["via"] == "querycore"
    assert final["execution"]["ok"] is True and final["execution"]["version"] == 1
    assert final["execution"]["card_id"] == "card" and final["run_id"]
    assert final["execution"]["publication"]["manifest"], "final 要带上这次固定口径的发布清单引用"
    assert final["execution"]["selection"]["metric_ids"] == ["gmv"]
    assert "QueryCore" in final["explanation"]
    async with SessionLocal() as session:
        run = await session.get(Run, final["run_id"])
    assert run.origin["service"] == "querycore" and run.origin["purpose"] == "compose"


async def test_a_single_month_and_a_single_day_map_to_the_half_open_window(client, engine_up, monkeypatch):
    """明确完整的单月/单日映射到开始/结束字段（半开区间）：单月整段、单日一天。"""
    _no_model(monkeypatch)
    asset = await _auto_asset()
    await publish(client, asset)

    month = await _ops(f"{AUTO_NAME} 2026年9月是多少")
    assert month[0]["execution"]["metrics"][0]["value"] == WINDOW_SUM
    async with SessionLocal() as session:
        first = await session.get(Run, month[0]["run_id"])
    assert first.input == {"start": "2026-09-01", "end": "2026-10-01"}

    day = await _ops(f"{AUTO_NAME} 2026-09-02 的数字")
    [final] = day
    assert final["execution"]["metrics"][0]["value"] == 100.0
    async with SessionLocal() as session:
        second = await session.get(Run, final["run_id"])
    assert second.input == {"start": "2026-09-02", "end": "2026-09-03"}


async def test_a_name_shared_across_cards_clarifies_without_model(client, model_unconfigured):
    """同名跨卡不自动：已知口径需澄清，不重新规划取数。"""
    shared = "同名歧义甲"
    for _ in range(2):
        asset = await _auto_asset(metric_name=shared)
        await publish(client, asset)
    async with SessionLocal() as session:
        assert (await nlchange.auto_route(session, question=f"{shared} 2026年9月是多少")).mode == nlchange.ROUTE_CLARIFY
    ops = await _ops(f"{shared} 2026年9月是多少")
    assert len(ops) == 1 and ops[0]["op"] == "reply"


async def test_a_missing_non_date_parameter_falls_back(client, model_unconfigured):
    """缺一个不能确定的非日期必填值（门店）：不自动，回旧路径。"""
    sql = ("SELECT SUM(amount) AS gmv, SUM(qty) AS units, '合成标签' AS label FROM orders "
           "WHERE status = 'paid' AND store = '{{ input.store }}' "
           "AND paid_at >= '{{ input.start }}' AND paid_at < '{{ input.end }}'")
    fields = [{"name": "start", "type": "string", "required": True, "description": "开始日期（含）"},
              {"name": "end", "type": "string", "required": True, "description": "结束日期（不含）"},
              {"name": "store", "type": "string", "required": True, "description": "门店"}]
    asset = await _auto_asset(graph=auto_graph(sales_sql=sql, fields=fields))
    await publish(client, asset)
    async with SessionLocal() as session:
        assert await nlchange.auto_route(session, question=f"{AUTO_NAME} 2026年9月是多少") is None
    with pytest.raises(HTTPException) as caught:
        await _ops(f"{AUTO_NAME} 2026年9月是多少")
    assert caught.value.status_code == 400


async def test_an_unmatched_question_still_takes_the_old_path_for_an_ordinary_user(client, engine_up,
                                                                                  model_unconfigured):
    """普通 user 也走这条接缝：**没匹配到**口径的话照旧走旧路径（模型没配好时照旧 400）。

    新契约：普通 user 不再一律回旧路——命中了授权范围内的口径就按口径取数（见下面几条），这里盯的是「没匹配到」那
    一半仍旧回旧路。"""
    asset = await _auto_asset()
    await publish(client, asset)
    await _grant_app(asset["workflow_id"], 1, who="plain")
    with _acting("plain"):
        with pytest.raises(HTTPException) as caught:
            await _ops("跟我们说说最近有什么值得关注的变化")
        assert caught.value.status_code == 400
    # 换成搭建者及以上（关登录就是 admin）照旧自动命中
    builder = await _ops(f"{AUTO_NAME} 2026年9月是多少")
    assert builder[0]["execution"]["via"] == "querycore"


async def test_auto_route_refuses_a_unique_hit_outside_the_allowed_set(client):
    """授权闸在 auto_route 这一层：唯一命中却不在 allowed 里交回**终止型** ROUTE_REFUSED。

    这不是「未匹配」（未匹配才是 None，那会被调用方当成没匹配到、照旧走旧 Agent 路径去翻底表）；授的是别的版本不算数；
    allowed 为 None 是调用方已放行（搭建者及以上）。"""
    asset = await _auto_asset()
    await publish(client, asset)
    question = f"{AUTO_NAME} 2026年9月是多少"
    async with SessionLocal() as session:
        refused = await nlchange.auto_route(session, question=question, allowed=set())
        assert refused.mode == nlchange.ROUTE_REFUSED and refused.code == nlchange.ROUTE_NOT_PERMITTED
        assert refused.request == {} and refused.reason == nlchange.NOT_PERMITTED
        assert asset["workflow_id"] not in refused.reason, "拒绝的说法里不出现内部 id"
        # 授权范围里没有这一版（授的是别的版本）：同样拒绝，绝不静默按推荐版执行
        other = await nlchange.auto_route(session, question=question, allowed={(asset["workflow_id"], 2)})
        assert other.mode == nlchange.ROUTE_REFUSED
        # 授权范围里有这一版：照旧交回固定请求
        hit = await nlchange.auto_route(session, question=question, allowed={(asset["workflow_id"], 1)})
        assert hit.mode == nlchange.ROUTE_EXECUTE and hit.request["version"] == 1
        assert hit.request["metric_ids"] == ["gmv"]
        # allowed 为 None：调用方已放行，不做这道闸
        opened = await nlchange.auto_route(session, question=question)
        assert opened.mode == nlchange.ROUTE_EXECUTE
        # 真含糊（不是「命中但无权」）：照样交回 None，调用方走旧路
        assert await nlchange.auto_route(session, question="排除退款后的" + question, allowed=set()) is None


async def test_a_hit_without_a_grant_is_refused_and_never_explores_the_tables(client, engine_up,
                                                                             model_unconfigured):
    """命中了但这个人没有使用权：终止型结局（route_not_permitted / next=fail），不发起运行，也不回退旧 Agent 路径。

    「不回退」是可证的：模型没配好，一旦落到旧路径就一定会 400——这里拿到的是 error 操作，不是 400。"""
    asset = await _auto_asset()
    await publish(client, asset)
    before = await runs_total()
    with _acting("plain"):
        ops = await _ops(f"{AUTO_NAME} 2026年9月是多少")
    [failed] = ops
    assert failed["op"] == "error" and failed["code"] == nlchange.ROUTE_NOT_PERMITTED
    assert failed["next"] == "fail" and "没有授权" in failed["message"]
    assert await runs_total() == before, "没授权不发起任何运行（更不翻底表）"
    _no_internals(ops, workflow_id=asset["workflow_id"])


async def test_an_authorised_user_asks_without_a_canvas_and_gets_the_caliber_answer(client, engine_up, monkeypatch):
    """授权 user 没有画布也按口径取数：走 prepare 好的父图 + 真实 caliber_query 工具，遮罩与只读由那条路保证。"""
    _no_model(monkeypatch)
    from app.ledger.querycore import CALIBER_QUERY
    from app.tools.datasource import MASK_RESULTS_OPTION, READ_ONLY_OPTION

    asset = await _auto_asset()
    await publish(client, asset)
    await _grant_app(asset["workflow_id"], 1, who="plain")
    with _acting("plain"):
        ops = await _ops(f"{AUTO_NAME} 2026年9月是多少")
    [final] = ops
    assert final["op"] == "final" and final["execution"]["via"] == "querycore"
    assert final["execution"]["status"] == "succeeded"
    assert {m["name"]: m["value"] for m in final["execution"]["metrics"]} == {AUTO_NAME: WINDOW_SUM}
    async with SessionLocal() as session:
        run = await session.get(Run, final["run_id"])
    assert run.started_by == "plain", "运行记在发起的使用者名下"
    assert run.options.get(MASK_RESULTS_OPTION) is True, "使用者的取数一律在工具边界遮"
    assert run.options.get(READ_ONLY_OPTION) is True
    assert CALIBER_QUERY in json.dumps(run.graph, ensure_ascii=False), "父图里绑的是真实口径工具"
    _no_internals(ops, workflow_id=asset["workflow_id"])


async def test_compose_does_not_need_a_formal_review(client, engine_up, monkeypatch):
    """普通问数是 compose：这一版没有验证记录、没有认证审阅，照样按口径取数（formal 才要那一份依据）。"""
    _no_model(monkeypatch)
    from app.apps import publications

    asset = await _auto_asset()
    await publish(client, asset)
    await _grant_app(asset["workflow_id"], 1, who="plain")
    async with SessionLocal() as session:
        workflow = await session.get(Workflow, asset["workflow_id"])
        judged = await publications.judge(session, workflow, version=1, purpose=publications.PURPOSE_FORMAL)
    assert judged is not None and judged.refusal is not None, "这一版没有验证审阅：正式用途一定拒"
    with _acting("plain"):
        ops = await _ops(f"{AUTO_NAME} 2026年9月是多少")
    [final] = ops
    assert final["op"] == "final" and final["execution"]["via"] == "querycore", ops


async def test_a_fixed_request_goes_through_the_same_gate(client, engine_up, monkeypatch):
    """fixed_request 对普通 user 走**同一道授权闸**：没授权既不执行也不降级 Agent，有授权才按口径执行（带遮罩）。"""
    _no_model(monkeypatch)
    from app.tools.datasource import MASK_RESULTS_OPTION

    asset = await _auto_asset()
    await publish(client, asset)
    payload = _fixed(asset)
    before = await runs_total()
    with _acting("plain"):
        refused = await _answer_ops(payload)
    [failed] = refused
    assert failed["op"] == "error" and failed["code"] == nlchange.ROUTE_NOT_PERMITTED
    assert failed["next"] == "fail" and "没有授权" in failed["message"]
    assert await runs_total() == before
    _no_internals(refused, workflow_id=asset["workflow_id"])

    await _grant_app(asset["workflow_id"], 1, who="plain")
    with _acting("plain"):
        allowed = await _answer_ops(payload)
    [final] = allowed
    assert final["op"] == "final" and final["execution"]["via"] == "querycore"
    assert {m["name"]: m["value"] for m in final["execution"]["metrics"]} == {AUTO_NAME: WINDOW_SUM}
    async with SessionLocal() as session:
        run = await session.get(Run, final["run_id"])
    assert run.started_by == "plain" and run.options.get(MASK_RESULTS_OPTION) is True
    _no_internals(allowed, workflow_id=asset["workflow_id"])


async def test_a_manifest_failure_after_a_hit_is_reported_not_degraded(client):
    """命中之后执行失败（这一版没有发布清单）：原样报错，不静默回退旧 Agent。"""
    from app.ledger import querycore

    workflow_id = await B.publish(auto_graph("无清单口径甲"), name=f"{OWN_PREFIX}无清单口径")
    _MADE["workflows"].append(workflow_id)
    ops = await _ops("无清单口径甲 2026年9月是多少")
    [op] = ops
    assert op["op"] == "error" and op["code"] == querycore.MANIFEST_REQUIRED, ops
    assert op["next"] == "fail", "命中了就不再回旧路径，也不重来"


async def test_a_manifest_failure_after_a_hit_does_not_switch_paths_for_a_user(client, engine_up):
    """同一件事对普通 user：终止型结局 + 安全白话（内部原话里带着工作流 id 与版本号，不外发）。"""
    from app.api import copilot

    workflow_id = await B.publish(auto_graph("无清单口径乙"), name=f"{OWN_PREFIX}无清单口径乙")
    _MADE["workflows"].append(workflow_id)
    await _grant_app(workflow_id, 1, who="plain")
    with _acting("plain"):
        ops = await _ops("无清单口径乙 2026年9月是多少")
    [op] = ops
    assert op["op"] == "error" and op["next"] == "fail", ops
    assert op["code"], "机器码照旧给，程序据此认这一档"
    assert op["message"] == copilot.USER_FAILED, "只给安全白话，不把内部原话外发"
    _no_internals(ops, workflow_id=workflow_id)


async def test_a_metric_whose_name_contains_refund_does_not_trip_the_refund_rule(client, engine_up, monkeypatch):
    """指标名里带「退款」不影响自动路由；但问题里真带上含糊的退款条件就不自动（回旧路径）。"""
    _no_model(monkeypatch)
    asset = await _auto_asset(metric_name="退款率自动甲")
    await publish(client, asset)
    hit = await _ops("退款率自动甲 2026年9月是多少")
    assert hit[0]["execution"]["via"] == "querycore"
    async with SessionLocal() as session:
        assert await nlchange.auto_route(session, question="排除退款后的退款率自动甲 2026年9月是多少") is None


async def test_the_model_not_configured_still_executes_a_determined_request(client, engine_up, model_unconfigured):
    """模型没配好也照样执行确定请求；同一份设置下不指向任何口径的话照旧 400（旧路径）。"""
    asset = await _auto_asset()
    await publish(client, asset)
    ops = await _ops(f"{AUTO_NAME} 2026年9月是多少")
    [final] = ops
    assert final["op"] == "final" and final["execution"]["ok"] is True
    with pytest.raises(HTTPException) as caught:
        await _ops("跟我们说说最近有什么值得关注的变化")
    assert caught.value.status_code == 400


def defaulted_graph(metric_name: str = AUTO_NAME) -> dict:
    """口径卡的开始/结束日期字段声明了缺省值：不给日期时也拿得全（「查一下销售额」这类问法用它）。"""
    made = auto_graph(metric_name)
    start = next(n for n in made["nodes"] if n["id"] == "start")
    for field in start["data"]["config"]["fields"]:
        if field["name"] == "start":
            field["default"] = "2026-09-01"
        elif field["name"] == "end":
            field["default"] = "2026-10-01"
    return made


def empty_window_graph(metric_name: str = AUTO_NAME) -> dict:
    """这一版声明「缺输入按空值算」：窗口里没有数据时跑完了但指标没有值（跑成功 ≠ 算成了）。"""
    made = auto_graph(metric_name)
    card = next(node for node in made["nodes"] if node["id"] == "card")
    card["data"]["config"]["on_missing"] = "null"
    return made


def test_the_remainder_keeps_only_the_business_semantics():
    """拆完之后剩下的只有业务语义：通用问法/查询/统计措辞与完整日期都去掉，别的一个字不动。"""
    assert nlchange._auto_remainder("2026年10月销售额是多少", []) == "销售额"
    assert nlchange._auto_remainder("查一下销售额", []) == "销售额"
    assert nlchange._auto_remainder("2026-10-01 的销售额", []) == "销售额"
    # 指标名一字不差是有边界的：多一个分析意图词、或者一个相对说法，都不是「销售额」
    assert nlchange._auto_remainder("销售额增长率是多少", []) == "销售额增长率"
    assert nlchange._auto_remainder("销售额占比", []) == "销售额占比"
    assert nlchange._auto_remainder("上个月销售额是多少", []) == "上个月销售额"
    # 分析意图词刻意不算通用措辞（列在 _AUTO_ANALYSIS_WORDS 里，只为让人看到这条边界）
    assert not set(nlchange._AUTO_ANALYSIS_WORDS) & set(nlchange._AUTO_FILLERS)


@pytest.mark.parametrize("extra", ["增长率", "占比", "同比", "环比", "趋势", "明细", "排行", "拆解"])
async def test_an_extra_analysis_intent_falls_back_to_the_old_path(client, engine_up, model_unconfigured, extra):
    """「销售额增长率/占比/趋势…」这类带额外分析意图的问法不直达：剩下的话和指标名不等，回旧路径。"""
    asset = await _auto_asset()
    await publish(client, asset)
    for question in (f"{AUTO_NAME}{extra}是多少", f"{AUTO_NAME}的{extra}"):
        async with SessionLocal() as session:
            assert await nlchange.auto_route(session, question=question) is None, question
    with pytest.raises(HTTPException) as caught:
        await _ops(f"{AUTO_NAME}的{extra}是多少")
    assert caught.value.status_code == 400


async def test_natural_questions_use_declared_defaults_when_no_date_is_given(client, engine_up, monkeypatch):
    """「查一下销售额」这类自然问法拆完就是指标名：日期没给、但字段声明了缺省值时照旧直达。"""
    _no_model(monkeypatch)
    asset = await _auto_asset(graph=defaulted_graph())
    await publish(client, asset)
    for question in ("查一下" + AUTO_NAME, "帮我看看" + AUTO_NAME + "是多少", AUTO_NAME,
                     "2026年10月" + AUTO_NAME + "是多少"):
        ops = await _ops(question)
        assert len(ops) == 1 and ops[0]["op"] == "final", question
        assert ops[0]["execution"]["via"] == "querycore", question
    async with SessionLocal() as session:
        first = await session.get(Run, (await _ops("查一下" + AUTO_NAME))[0]["run_id"])
    assert first.input == {"start": "2026-09-01", "end": "2026-10-01"}, "没给日期就用声明的缺省值"
    # 相对说法（上个月）不是「销售额」：即使字段有缺省值也不直达
    async with SessionLocal() as session:
        assert (await nlchange.auto_route(session, question=f"上个月{AUTO_NAME}是多少")).mode == nlchange.ROUTE_CLARIFY


async def test_a_hit_that_produces_nothing_is_reported_as_a_failure_for_a_user(client, engine_up, monkeypatch):
    """命中之后「跑完了但没算成」（这一版声明缺输入按空值算、窗口里没有数据）：普通 user 也拿到终止型结局 + 安全白话。

    这条盯的是 ok=False 那一档：运行是发生了，但一个可用的数也没有，不能当成给了答案，也不换路。"""
    _no_model(monkeypatch)
    from app.api import copilot

    asset = await _auto_asset(graph=empty_window_graph())
    await publish(client, asset)
    await _grant_app(asset["workflow_id"], 1, who="plain")
    with _acting("plain"):
        ops = await _ops(f"2030年1月{AUTO_NAME}是多少")
    [op] = ops
    assert op["op"] == "error" and op["code"] == copilot.RESULT_FAILED, ops
    assert op["next"] == "fail" and op["message"] == copilot.USER_FAILED
    assert op["execution"]["status"] == "succeeded", "运行跑完了，只是没算成"
    assert op["execution"]["run_id"]
    _no_internals(ops, workflow_id=asset["workflow_id"])

