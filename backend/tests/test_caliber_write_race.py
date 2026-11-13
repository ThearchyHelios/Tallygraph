"""K02 复核补充：已接管资产的判定、幂等的并发收敛、写入被拒之后的恢复路径。

- taken_over：文档起草建出来的（origin.kind=doc_draft）和 docattach 建出来的（旧产物没写 kind，按 tags 认）
  都要保护；手建、蓝图、从运行提取、固化的都不算；
- docattach 产物照样要带基准版本，旧写 409；
- 同键并发：两路都写完之前谁也看不到对方的记录，靠 CAS / 唯一键收敛——只有确认是同一件事才算重放，别的完整性
  错误照旧抛出去，不能假成功。
"""
from __future__ import annotations

import copy
import uuid

import pytest
from sqlalchemy.exc import IntegrityError

from app.api import ledger as api_ledger
from app.apps import versions as app_versions
from app.db.base import SessionLocal
from app.db.models import CaliberWriteRequest, Workflow
from tests.test_caliber_write_guard import (  # noqa: F401 - client / documents / datasource 是 fixture
    _graph_of, _versions, cfg, client, datasource, documents, extract, fake_model, fake_trial, plan_reply,
    ready_draft,
)


# ==========================================================================
# taken_over：新旧 docattach 产物一致，不误伤别的来源
# ==========================================================================

def test_taken_over_covers_both_doc_draft_products_and_nothing_else():
    """判定是纯函数，按工作流的形状认：kind=doc_draft、或者 docattach 那样的 tags + origin。"""
    from app.apps.versions import taken_over

    class Row:
        def __init__(self, origin, tags=None):
            self.origin, self.tags = origin, tags

    # 新起草：origin 里写了 kind（app/ledger/authoring.py）
    assert taken_over(Row({"kind": "doc_draft", "draft_id": "d1", "documents": ["doc-1"]}, ["doc_draft"]))
    # docattach（含以前建的）：没有 kind，只有建的时候打的标签和那几项来源（run/sources/draft_id/documents）
    assert taken_over(Row({"run_id": "run-1", "freeze": True, "sources": [{"run_id": "run-1"}],
                           "draft_id": "d1", "documents": ["doc-1"]}, ["doc_draft"]))
    assert taken_over(Row({"run_id": "run-1"}, ["doc_draft"]))
    # 别的来源都不是：手建（没有 tags/origin）、蓝图、从运行提取、固化、老数据的 None
    assert not taken_over(Row(None))
    assert not taken_over(Row({}))
    assert not taken_over(Row(None, []))
    assert not taken_over(Row({"blueprint_id": "bp-1", "template": "board@1"}, ["blueprint"]))
    assert not taken_over(Row({"run_id": "run-1", "conversation_id": "conv-1"}, ["extracted"]))
    assert not taken_over(Row({"run_id": "run-1"}, ["certified"]))
    assert not taken_over(Row({"sources": [{"run_id": "run-1"}]}))


async def test_an_attached_caliber_cannot_be_written_without_a_base_revision(client):
    """docattach 建出来的口径卡（没有 origin.kind）：画布上照旧写不进去，要带基准版本。"""
    wid = (await client.post("/api/workflows", json={"name": "K02 接上的 " + uuid.uuid4().hex[:6],
                                                     "graph": {"nodes": [
                                                         {"id": "card", "type": "metrics", "position": {"x": 0, "y": 0},
                                                          "data": {"label": "口径卡",
                                                                   "config": {"metrics": [{"id": "m1", "name": "指标一",
                                                                                           "expression": "cell(nodes.q, 0, 'v')"}]}}},
                                                     ], "edges": []}})).json()["id"]
    async with SessionLocal() as session:
        workflow = await session.get(Workflow, wid)
        # 和 app/ledger/docattach.py 建出来的一模一样：tags 有 doc_draft，origin 里没有 kind
        workflow.tags = ["doc_draft"]
        workflow.origin = {"run_id": "run-1", "freeze": True, "sources": [{"run_id": "run-1"}],
                           "draft_id": "d-1", "documents": []}
        await session.commit()

    url = f"/api/workflows/{wid}"
    graph = {"nodes": [*copy.deepcopy((await _graph_of(wid))["nodes"])], "edges": []}
    cfg(graph, "card")["metrics"][0]["name"] = "旧页面改的"
    refused = await client.patch(url, json={"graph": graph})
    assert refused.status_code == 409, refused.text
    assert refused.json()["code"] == "revision_required"
    assert await _versions(wid) == [1]

    ok = await client.patch(url, json={"graph": graph, "expected_revision": 1})
    assert ok.status_code == 200 and ok.json()["version"] == 2, ok.text
    assert await _versions(wid) == [1, 2]


# ==========================================================================
# 幂等的并发收敛：只有确认是同一件事才算重放
# ==========================================================================

async def test_a_duplicate_request_row_settles_into_a_replay(client, documents, datasource, monkeypatch, fake_trial):
    """两路真并发都没查到记录时后到的那一路撞唯一键：回滚自己写的图，按赢的那一路交回重放（不报成新的一版）。

    注入方式：记录行事先就在，但把 claim_write 换成「一律当第一次见」——等于两路都走过了「还没写过」的判断。
    """
    body = await ready_draft(client, documents, datasource, monkeypatch)
    request_id = "race-0001"
    metrics = copy.deepcopy(body["metrics"])
    metrics[0]["name"] = "并发改的"
    payload = api_ledger.DraftPatchIn(metrics=metrics, expected_revision=body["revision"], request_id=request_id,
                                      target=api_ledger.DraftTargetIn(card_id=body["card_id"]))
    async with SessionLocal() as session:
        claim = await app_versions.claim_write(session, actor=None, workflow_id=body["workflow_id"],
                                              target=body["card_id"], request_id=request_id,
                                              payload_hash=api_ledger._patch_fingerprint(payload, body["card_id"]))
    assert claim.state == app_versions.WRITE_NEW

    # 先埋下「赢的那一路」写过的记录（内容和这次一样）
    async with SessionLocal() as session:
        session.add(CaliberWriteRequest(actor="", workflow_id=body["workflow_id"], card_id=body["card_id"],
                                       request_id=request_id,
                                       payload_hash=api_ledger._patch_fingerprint(payload, body["card_id"]),
                                       result={"revision": 2}))
        await session.commit()
    original = app_versions.claim_write
    calls = {"n": 0}

    async def blind_once(*args, **kwargs):
        # 只让这一次请求的「先查一遍」看不见记录（等于两路并发都没查到）；回滚之后的重查走真的
        calls["n"] += 1
        if calls["n"] == 1:
            return app_versions.WriteClaim(app_versions.WRITE_NEW)
        return await original(*args, **kwargs)

    monkeypatch.setattr(app_versions, "claim_write", blind_once)
    async with SessionLocal() as session:
        out = await api_ledger.patch_draft(body["id"], payload, session=session)
    monkeypatch.setattr(app_versions, "claim_write", original)
    assert calls["n"] == 2, calls
    assert out.get("replayed") is True, out
    # 自己那一版回滚掉了：谁都没多写
    assert await _versions(body["workflow_id"]) == [1]


async def test_an_integrity_error_without_a_record_is_not_reported_as_success(client, documents, datasource,
                                                                             monkeypatch, fake_trial):
    """记请求时撞完整性错误、可记录又不在（不是同一件事）：照旧抛出去，不能假成功。"""
    body = await ready_draft(client, documents, datasource, monkeypatch)
    metrics = copy.deepcopy(body["metrics"])
    metrics[0]["name"] = "不该成功"
    payload = api_ledger.DraftPatchIn(metrics=metrics, expected_revision=body["revision"],
                                      request_id="race-0002")

    async def boom(*args, **kwargs):
        raise IntegrityError("insert", {}, Exception("UNIQUE constraint failed: workflow_versions.workflow_id"))

    monkeypatch.setattr(app_versions, "remember_write", boom)
    with pytest.raises(IntegrityError):
        async with SessionLocal() as session:
            await api_ledger.patch_draft(body["id"], payload, session=session)
    # 报错的那一次一条也没落库
    assert await _versions(body["workflow_id"]) == [1]
    async with SessionLocal() as session:
        rows = list((await _rows_of_write(session, body["workflow_id"])))
    assert rows == []
    assert (await client.get(f"/api/ledger/drafts/{body['id']}")).json()["revision"] == 1


async def _rows_of_write(session, workflow_id: str):
    from sqlalchemy import select

    return (await session.execute(select(CaliberWriteRequest.id).where(
        CaliberWriteRequest.workflow_id == workflow_id))).scalars()


async def test_two_requests_with_the_same_key_at_once_write_once(client, documents, datasource, monkeypatch, fake_trial):
    """同一个 request_id 的两路请求同时发：只落一版，两边都是 200。"""
    import asyncio

    body = await ready_draft(client, documents, datasource, monkeypatch)
    metrics = copy.deepcopy(body["metrics"])
    metrics[0]["name"] = "同键并发"
    payload = {"metrics": metrics, "expected_revision": body["revision"], "request_id": "race-0003"}
    url = f"/api/ledger/drafts/{body['id']}"
    first, second = await asyncio.gather(client.patch(url, json=payload), client.patch(url, json=payload))
    assert sorted(r.status_code for r in (first, second)) == [200, 200], (first.text, second.text)
    assert await _versions(body["workflow_id"]) == [1, 2]
    assert [r.json().get("replayed") for r in (first, second)].count(True) <= 1
