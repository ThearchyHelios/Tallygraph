"""K11 增量重导入与 B / L / R 三方合并：候选 R、当前图 L、字段基线 B 三者分开，候选不改 L，B 只在接受时推进。

这份文件盯住任务书 K11 的硬要求：

1. **只认稳定 metric_id 和来源证据对齐**，名称相不相似不参与自动合并；同名不同范围必须人来定（test_same_name_...）。
2. **逐字段三方状态**：无冲突更新、人工修改冲突、新增候选、缺失候选。漏提取不当删除（test_missing_...）；
   人工改过的 L 不会因为「同一原文再提一次」被悄悄改回去（test_a_manual_override_...）。
3. **候选不改 L，B 只在接受时推进**：建一次导入看得到工作项和冲突，但 L、B 一个字节不动；接受才落到图上并按被接受的
   那几格推进 B，未接受的冲突不动 B。
4. **baseline_unknown 必须人工对齐**：没有可比基线时逐字段判「需要人工对齐」，不假设 L = B，也不自动落。
5. **未提供给模型的段落不能作引用**（test_invisible_citation_...）。
6. **中断后可显式恢复**，不重复接受、不重复建版；request_id 幂等（重放不新增版本、同键不同内容报冲突）。
7. 走真实 HTTP；模型一律注入假提取器、试跑换成假的，不连真实业务库。
"""
from __future__ import annotations

import copy
import uuid

import pytest
from sqlalchemy import delete, select

from app.db.base import SessionLocal
from app.db.models import CaliberDraft, CaliberImportItem, CaliberImportRun, CaliberValidationRecord
from app.ledger import fieldbaseline, reimport
from tests.test_caliber_field_baseline import accepted_draft  # noqa: F401 - 与基线用例共用起草帮手
from tests.test_ledger_docdraft_api import (  # noqa: F401 - fixture 和起草用例共用
    client, datasource, documents, extract, fake_model, fake_trial, plan_reply,
)


@pytest.fixture(autouse=True)
def _fake_trial(fake_trial):
    """假试跑本模块要一直挂着：走接口起草时它会去打夹具里不存在的库。"""
    return fake_trial


@pytest.fixture(autouse=True)
def inline_import(monkeypatch):
    """把后台导入换成同步执行：deterministic 地等它跑完再看结果（测试不发真实请求）。"""
    async def run_now(run_id: str) -> None:
        await reimport.run_import(run_id)

    monkeypatch.setattr(reimport, "start", run_now)
    yield


@pytest.fixture(autouse=True)
async def _cleanup_imports():
    yield
    async with SessionLocal() as session:
        await session.execute(delete(CaliberImportItem))
        await session.execute(delete(CaliberImportRun))
        await session.execute(delete(CaliberValidationRecord))
        await session.commit()


def fake_extract(metrics: list[dict], visible: list[str], *, problems: list[dict] | None = None):
    """假提取器：交回固定的候选 R 和可见段落（不调模型、不查库）。"""
    async def run(**_kwargs):
        return {"metrics": copy.deepcopy(metrics), "problems": list(problems or []),
                "visible_chunk_ids": list(visible), "prompt_version": 1, "parse_version": 1,
                "model_id": "fake-model", "sql_groups": []}

    return run


def candidate(doc: str, *, metric_id: str = "", name: str = "已支付退款率", unit: str = "%", decimals: int = 2,
              text: str = "已支付订单里发生退款的比例", citations: list[dict] | None = None,
              drop: tuple[str, ...] = ()) -> dict:
    """一条候选 R：默认和 K05 基线一模一样（引用 doc-2）。drop 里的字段整格不交（模拟漏提取）。"""
    definition = {"text": text, "scope": "全部门店", "exclusions": [], "owner": "运营",
                  "effective_from": "2026-01-01",
                  "citations": citations if citations is not None
                  else [{"chunk": f"{doc}-2", "quote": "只算已支付的订单", "document_id": doc, "span": [8, 16]}]}
    metric = {"id": metric_id, "name": name, "synonyms": ["退款率"], "unit": unit, "decimals": decimals,
              "expression": "cell(nodes.fetch, 0, 'refund_rate')", "definition": definition,
              "open_questions": []}
    for path in drop:
        head, _, tail = path[1:].partition("/")
        if tail:
            metric.get(head, {}).pop(tail, None)
        else:
            metric.pop(head, None)
    return metric


async def baseline_draft(client, documents, datasource, monkeypatch) -> tuple[dict, str]:
    """走一次真实起草，返回 (草稿详情, 文档 id)。草稿详情里有字段基线 B 和基准版本 revision。"""
    doc = await documents("k11-" + uuid.uuid4().hex[:8])
    source_id = await datasource()
    fake_model(monkeypatch, plan_reply(f"{doc}-2"))
    body = await extract(client, doc, source_id)
    made = (await client.get(f"/api/ledger/drafts/{body['draft_id']}")).json()
    return made, doc


async def make_import(client, draft_id: str, doc: str, metrics: list[dict], visible: list[str],
                      monkeypatch, *, request_id: str | None = None) -> dict:
    monkeypatch.setattr(reimport, "extract_candidates", fake_extract(metrics, visible))
    body = {"request_id": request_id} if request_id else {}
    created = await client.post(f"/api/ledger/drafts/{draft_id}/imports", json=body)
    assert created.status_code == 201, created.text
    run_id = created.json()["run_id"]
    got = await client.get(f"/api/ledger/imports/{run_id}")
    assert got.status_code == 200, got.text
    return got.json()


def states(item: dict) -> dict[str, str]:
    return {cell["path"]: cell["state"] for cell in item["fields"]}


# ==========================================================================
# 1. 无冲突更新 + B 只在接受时推进
# ==========================================================================

async def test_no_conflict_update_lands_on_accept_and_b_advances_only_then(client, documents, datasource,
                                                                          monkeypatch):
    made, doc = await baseline_draft(client, documents, datasource, monkeypatch)
    assert made["field_baseline"]["status"] == "known"

    run = await make_import(client, made["id"], doc, [
        candidate(doc, text="已支付订单里退款的比例（新说法）")], [f"{doc}-1", f"{doc}-2"], monkeypatch)
    assert run["status"] == "succeeded" and len(run["items"]) == 1
    # R 的来源可还原：不可变快照 + 实际进提示的段落 + 解析/模型/提示版本
    assert run["source_snapshot"]["artifact_id"] and run["source_snapshot"]["content_hash"]
    assert run["visible_chunk_ids"] == [f"{doc}-1", f"{doc}-2"]
    assert run["prompt_version"] == 1 and run["parse_version"] == 1 and run["model_id"] == "fake-model"
    item = run["items"][0]
    assert item["metric_id"] == "m1" and item["align"]["mode"] == "evidence"
    assert item["status"] == "update"
    by_path = states(item)
    assert by_path["/definition/text"] == "update"
    assert by_path["/name"] == "unchanged"

    # 候选生成不改 L、不改 B：图上的内容还是起草时那一份，基线引用一个字没动
    before = (await client.get(f"/api/ledger/drafts/{made['id']}")).json()
    assert before["metrics"][0]["definition"]["text"] == "已支付订单里发生退款的比例"
    assert before["revision"] == made["revision"]
    assert before["field_baseline"]["artifact_id"] == made["field_baseline"]["artifact_id"]

    accepted = await client.post(f"/api/ledger/imports/{run['run_id']}/accept", json={
        "items": [{"metric_id": "m1", "auto": True}], "expected_revision": made["revision"],
        "request_id": "k11-accept-1"})
    assert accepted.status_code == 200, accepted.text
    body = accepted.json()
    assert body["applied"] is True and body["replayed"] is False
    assert body["revision"] == made["revision"] + 1

    after = (await client.get(f"/api/ledger/drafts/{made['id']}")).json()
    assert after["metrics"][0]["definition"]["text"] == "已支付订单里退款的比例（新说法）"
    assert after["field_baseline"]["status"] == "known"
    assert after["field_baseline"]["artifact_id"] != made["field_baseline"]["artifact_id"]
    values = fieldbaseline.values({"artifact_id": after["field_baseline"]["artifact_id"]}, "m1")
    assert values["/definition/text"] == "已支付订单里退款的比例（新说法）"    # 接受的那一格推进了 B
    assert values["/name"] == "已支付退款率"                                  # 没提的格子原样
    assert after["field_baseline"]["accepted_revision"] == made["revision"] + 1
    # 历史按接受追加：上次那条基线还在
    assert [entry["artifact_id"] for entry in after["field_baseline_history"]][-1] \
        == after["field_baseline"]["artifact_id"]


# ==========================================================================
# 2. 人工修改冲突 / 人工覆盖不被同一原文重提悄悄改回去
# ==========================================================================

async def test_a_manual_edit_becomes_a_conflict_and_only_moves_when_accepted(client, documents, datasource,
                                                                            monkeypatch):
    made, doc = await baseline_draft(client, documents, datasource, monkeypatch)
    edited = await client.patch(f"/api/ledger/drafts/{made['id']}", json={
        "operations": [{"op": "set", "metric_id": "m1", "path": "/unit", "value": "单"}],
        "expected_revision": made["revision"], "request_id": "k11-edit-1"})
    assert edited.status_code == 200 and edited.json()["change"]["applied"] is True
    revision = edited.json()["revision"]

    run = await make_import(client, made["id"], doc, [candidate(doc, unit="百分比")],
                            [f"{doc}-1", f"{doc}-2"], monkeypatch)
    item = run["items"][0]
    assert states(item)["/unit"] == "conflict"
    assert item["status"] == "conflict"

    # 没接受之前 B 不动（冲突的那一格还在老基线上）
    mid = (await client.get(f"/api/ledger/drafts/{made['id']}")).json()
    assert fieldbaseline.values({"artifact_id": mid["field_baseline"]["artifact_id"]}, "m1")["/unit"] == "%"

    accepted = await client.post(f"/api/ledger/imports/{run['run_id']}/accept", json={
        "items": [{"metric_id": "m1", "fields": ["/unit"]}], "expected_revision": revision,
        "request_id": "k11-edit-accept"})
    assert accepted.status_code == 200, accepted.text

    after = (await client.get(f"/api/ledger/drafts/{made['id']}")).json()
    assert after["metrics"][0]["unit"] == "百分比"
    assert fieldbaseline.values({"artifact_id": after["field_baseline"]["artifact_id"]}, "m1")["/unit"] == "百分比"


async def test_a_manual_override_is_not_silently_reverted_by_re_importing_the_same_text(
        client, documents, datasource, monkeypatch):
    """人工把 L 改成了「单」，重导入还是原文里的「%」（= B）：这不算新说法，既不报冲突也不动 L。"""
    made, doc = await baseline_draft(client, documents, datasource, monkeypatch)
    edited = await client.patch(f"/api/ledger/drafts/{made['id']}", json={
        "operations": [{"op": "set", "metric_id": "m1", "path": "/unit", "value": "单"}],
        "expected_revision": made["revision"], "request_id": "k11-override-1"})
    assert edited.json()["change"]["applied"] is True

    run = await make_import(client, made["id"], doc, [candidate(doc, unit="%")],
                            [f"{doc}-1", f"{doc}-2"], monkeypatch)
    assert states(run["items"][0])["/unit"] == "unchanged"
    assert run["items"][0]["status"] == "unchanged"

    # auto 收不掉任何东西（没有可接受的格子），L 还是人工改的那一份
    refused = await client.post(f"/api/ledger/imports/{run['run_id']}/accept", json={
        "items": [{"metric_id": "m1", "auto": True}], "expected_revision": edited.json()["revision"]})
    assert refused.status_code == 422 and refused.json()["code"] == reimport.IMPORT_NOTHING_SELECTED
    after = (await client.get(f"/api/ledger/drafts/{made['id']}")).json()
    assert after["metrics"][0]["unit"] == "单"


# ==========================================================================
# 3. 漏提取不当删除
# ==========================================================================

async def test_a_missing_field_is_never_a_deletion(client, documents, datasource, monkeypatch):
    made, doc = await baseline_draft(client, documents, datasource, monkeypatch)
    run = await make_import(client, made["id"], doc,
                            [candidate(doc, drop=("/definition/text",))], [f"{doc}-1", f"{doc}-2"], monkeypatch)
    item = run["items"][0]
    assert states(item)["/definition/text"] == "missing"
    assert item["status"] == "partial"

    refused = await client.post(f"/api/ledger/imports/{run['run_id']}/accept", json={
        "items": [{"metric_id": "m1", "fields": ["/definition/text"]}], "expected_revision": made["revision"]})
    assert refused.status_code == 422 and refused.json()["code"] == reimport.IMPORT_FIELD_NOT_ACCEPTABLE
    assert "漏提取不当删除" in refused.json()["detail"]

    after = (await client.get(f"/api/ledger/drafts/{made['id']}")).json()
    assert after["metrics"][0]["definition"]["text"] == "已支付订单里发生退款的比例"   # L 原样
    assert after["field_baseline"]["artifact_id"] == made["field_baseline"]["artifact_id"]


async def test_a_brand_new_field_is_a_candidate_that_can_be_accepted(client, documents, datasource, monkeypatch):
    """新增候选：R 提出了 B、L 都没有的字段（粒度），不是冲突，接受后落到 L 并推进 B。"""
    made, doc = await baseline_draft(client, documents, datasource, monkeypatch)
    extra = candidate(doc)
    extra["grain"] = "日"
    run = await make_import(client, made["id"], doc, [extra], [f"{doc}-1", f"{doc}-2"], monkeypatch)
    item = run["items"][0]
    assert states(item)["/grain"] == "new" and item["status"] == "new"

    accepted = await client.post(f"/api/ledger/imports/{run['run_id']}/accept", json={
        "items": [{"metric_id": "m1", "auto": True}], "expected_revision": made["revision"],
        "request_id": "k11-new-field"})
    assert accepted.status_code == 200, accepted.text
    after = (await client.get(f"/api/ledger/drafts/{made['id']}")).json()
    assert after["metrics"][0]["grain"] == "日"
    assert fieldbaseline.values({"artifact_id": after["field_baseline"]["artifact_id"]}, "m1")["/grain"] == "日"


# ==========================================================================
# 4. 同名不同范围：不按名字自动合并
# ==========================================================================

async def test_the_same_name_with_different_evidence_is_not_auto_merged(client, documents, datasource, monkeypatch):
    made, doc = await baseline_draft(client, documents, datasource, monkeypatch)
    other = candidate(doc, name="已支付退款率", text="另一段范围里的退款率",
                      citations=[{"chunk": f"{doc}-1", "quote": "退款率 = 已支付后退款的订单数 / 已支付订单数",
                                  "document_id": doc, "span": [0, 23]}])
    run = await make_import(client, made["id"], doc, [other], [f"{doc}-1", f"{doc}-2"], monkeypatch)
    item = run["items"][0]
    # 名字和图上那条一模一样，但引的原文不是同一段：不自动合并，交给人
    assert item["align"]["mode"] == "new" and item["metric_id"] == ""
    assert item["status"] == "new" and item["name"] == "已支付退款率"

    # 没对齐的不能直接接受
    refused = await client.post(f"/api/ledger/imports/{run['run_id']}/accept", json={
        "items": [{"metric_id": "已支付退款率", "auto": True}], "expected_revision": made["revision"]})
    assert refused.status_code == 409 and refused.json()["code"] == reimport.IMPORT_ITEM_UNKNOWN

    # 人来定：把它对到 m1 上，再按字段接受
    aligned = await client.post(f"/api/ledger/imports/{run['run_id']}/items/{item['item_id']}/align",
                                json={"metric_id": "m1"})
    assert aligned.status_code == 200, aligned.text
    after_align = aligned.json()["items"][0]
    assert after_align["metric_id"] == "m1" and after_align["align"]["manual"] is True
    assert states(after_align)["/definition/text"] == "update"


# ==========================================================================
# 5. 引用不可见：没给过模型的段落不能当引用
# ==========================================================================

async def test_citations_to_segments_the_model_never_saw_are_dropped(client, documents, datasource, monkeypatch):
    made, doc = await baseline_draft(client, documents, datasource, monkeypatch)
    hidden = {"chunk": "hidden-9", "quote": "模型没看到过的原文"}
    kept = {"chunk": f"{doc}-2", "quote": "退款率按门店拆", "document_id": doc, "span": [0, 7]}
    run = await make_import(client, made["id"], doc,
                            [candidate(doc, citations=[kept, hidden])], [f"{doc}-1", f"{doc}-2"], monkeypatch)
    item = run["items"][0]
    assert [entry["chunk"] for entry in item["align"]["invisible_citations"]] == ["hidden-9"]
    citations_field = next(cell for cell in item["fields"] if cell["path"] == "/definition/citations")
    assert [entry["chunk"] for entry in citations_field["r"]] == [f"{doc}-2"]

    accepted = await client.post(f"/api/ledger/imports/{run['run_id']}/accept", json={
        "items": [{"metric_id": "m1", "fields": ["/definition/citations"]}],
        "expected_revision": made["revision"], "request_id": "k11-cite-1"})
    assert accepted.status_code == 200, accepted.text
    after = (await client.get(f"/api/ledger/drafts/{made['id']}")).json()
    chunks = [entry.get("chunk") for entry in after["metrics"][0]["definition"]["citations"]]
    assert chunks == [f"{doc}-2"] and "hidden-9" not in chunks


async def test_a_candidate_whose_only_citation_is_invisible_fails(client, documents, datasource, monkeypatch):
    made, doc = await baseline_draft(client, documents, datasource, monkeypatch)
    run = await make_import(client, made["id"], doc,
                            [candidate(doc, citations=[{"chunk": "hidden-9", "quote": "没给模型的原文"}])],
                            [f"{doc}-1"], monkeypatch)
    item = run["items"][0]
    assert item["status"] == "failed" and item["failed"] is True
    assert "没有提供给模型" in item["error"]


async def test_a_failed_item_is_never_acceptable_and_never_clears_citations(client, documents, datasource,
                                                                            monkeypatch):
    """整条失败的候选（引用一段都没核对上）：任何一格都不能接受，更不能用空的 R 把 citations 清掉。"""
    made, doc = await baseline_draft(client, documents, datasource, monkeypatch)
    run = await make_import(client, made["id"], doc,
                            [candidate(doc, citations=[{"chunk": "hidden-9", "quote": "没给模型的原文"}])],
                            [f"{doc}-1"], monkeypatch)
    item = run["items"][0]
    assert item["status"] == "failed" and item["failed"] is True
    # UI 读得到的普通字段：整条失败的一格都不可接受
    assert all(cell["acceptable"] is False for cell in item["fields"])

    revision = made["revision"]
    for selection, code in (
            ({"item_id": item["item_id"], "fields": ["/definition/citations"]}, reimport.IMPORT_ITEM_FAILED),
            ({"item_id": item["item_id"], "fields": ["/unit"]}, reimport.IMPORT_ITEM_FAILED),
            ({"item_id": item["item_id"], "auto": True}, reimport.IMPORT_NOTHING_SELECTED)):
        refused = await client.post(f"/api/ledger/imports/{run['run_id']}/accept",
                                    json={"items": [selection], "expected_revision": revision})
        assert refused.status_code == 422 and refused.json()["code"] == code, refused.text

    after = (await client.get(f"/api/ledger/drafts/{made['id']}")).json()
    assert after["metrics"][0]["definition"]["citations"]        # L 的出处没被清掉
    assert after["revision"] == revision
    assert after["field_baseline"]["artifact_id"] == made["field_baseline"]["artifact_id"]


# ==========================================================================
# 4b. 工作项定位：按 item_id，不按 metric_id / 位置；跨组重号不当稳定身份
# ==========================================================================

async def test_accept_and_reject_locate_by_item_id_and_never_take_the_last_duplicate(client, documents,
                                                                                    datasource, monkeypatch):
    """两条候选都对到 m1：按 metric_id 定位会歧义（回 409，不取最后一条），按 item_id 才落得下去。"""
    made, doc = await baseline_draft(client, documents, datasource, monkeypatch)
    run = await make_import(client, made["id"], doc,
                            [candidate(doc, text="第一种说法"), candidate(doc, text="第二种说法")],
                            [f"{doc}-1", f"{doc}-2"], monkeypatch)
    items = run["items"]
    assert len(items) == 2 and {item["metric_id"] for item in items} == {"m1"}

    ambiguous = await client.post(f"/api/ledger/imports/{run['run_id']}/accept", json={
        "items": [{"metric_id": "m1", "fields": ["/definition/text"]}], "expected_revision": made["revision"]})
    assert ambiguous.status_code == 409 and ambiguous.json()["code"] == reimport.IMPORT_ITEM_AMBIGUOUS
    assert sorted(ambiguous.json()["item_ids"]) == sorted(item["item_id"] for item in items)
    assert (await client.get(f"/api/ledger/drafts/{made['id']}")).json()["revision"] == made["revision"]

    rejected = await client.post(f"/api/ledger/imports/{run['run_id']}/reject", json={
        "items": [{"metric_id": "m1"}]})
    assert rejected.status_code == 409 and rejected.json()["code"] == reimport.IMPORT_ITEM_AMBIGUOUS

    # 按 item_id 定位：只动那一条
    first = items[0]
    done = await client.post(f"/api/ledger/imports/{run['run_id']}/accept", json={
        "items": [{"item_id": first["item_id"], "fields": ["/definition/text"]}],
        "expected_revision": made["revision"], "request_id": "k11-item-id-1"})
    assert done.status_code == 200, done.text
    after = (await client.get(f"/api/ledger/imports/{run['run_id']}")).json()["items"]
    decided = next(item for item in after if item["item_id"] == first["item_id"])
    other = next(item for item in after if item["item_id"] != first["item_id"])
    assert "/definition/text" in decided["accepted"] and other["accepted"] == []


async def test_a_cross_group_duplicate_candidate_id_is_not_a_stable_identity(client, documents, datasource,
                                                                            monkeypatch):
    """模型在两组里各自从 m1 编起（跨组重号）：这个编号不当稳定身份，只按来源证据对。"""
    made, doc = await baseline_draft(client, documents, datasource, monkeypatch)
    first = candidate(doc, metric_id="m1", text="说法 A")               # 引 doc-2，和图上那条同源
    second = candidate(doc, metric_id="m1", name="另一条指标", text="说法 B",  # 同一个编号，但引 doc-1
                       citations=[{"chunk": f"{doc}-1", "quote": "退款率 = 已支付后退款的订单数 / 已支付订单数",
                                   "document_id": doc, "span": [0, 23]}])
    run = await make_import(client, made["id"], doc, [first, second], [f"{doc}-1", f"{doc}-2"], monkeypatch)
    by_name = {item["name"]: item for item in run["items"]}
    aligned = by_name["已支付退款率"]
    assert aligned["metric_id"] == "m1" and aligned["align"]["mode"] == "evidence"
    assert "重号" in aligned["align"]["identity_note"] and aligned["align"]["identity_note"]
    # 另一条虽然也叫 m1，但证据不同：没有被当成「m1」对上
    other = by_name["另一条指标"]
    assert other["metric_id"] == "" and other["align"]["mode"] == "new"


# ==========================================================================
# 6. baseline_unknown：必须人工对齐，不能假设 L = B
# ==========================================================================

async def _clear_baseline(draft_id: str) -> None:
    async with SessionLocal() as session:
        draft = await session.get(CaliberDraft, draft_id)
        payload = dict(draft.payload)
        payload.pop(fieldbaseline.REF_KEY, None)
        payload.pop(fieldbaseline.HISTORY_KEY, None)
        draft.payload = payload
        await session.commit()


async def test_baseline_unknown_needs_manual_alignment_and_builds_b_from_accepted_fields(
        client, documents, datasource, monkeypatch):
    made, doc = await baseline_draft(client, documents, datasource, monkeypatch)
    await _clear_baseline(made["id"])
    assert (await client.get(f"/api/ledger/drafts/{made['id']}")).json()["field_baseline"]["status"] \
        == "baseline_unknown"

    run = await make_import(client, made["id"], doc, [candidate(doc, text="新的一版说明")],
                            [f"{doc}-1", f"{doc}-2"], monkeypatch)
    item = run["items"][0]
    assert item["status"] == "manual" and states(item)["/definition/text"] == "manual"

    # 没有可比基线：auto 一格都不收（不假设 L = B）
    refused = await client.post(f"/api/ledger/imports/{run['run_id']}/accept", json={
        "items": [{"metric_id": "m1", "auto": True}], "expected_revision": made["revision"]})
    assert refused.status_code == 422 and refused.json()["code"] == reimport.IMPORT_NOTHING_SELECTED

    # 人明确点名接受这一格：B 从这一格建起来（只有接受的那几格有基线）
    accepted = await client.post(f"/api/ledger/imports/{run['run_id']}/accept", json={
        "items": [{"metric_id": "m1", "fields": ["/definition/text"]}], "expected_revision": made["revision"],
        "request_id": "k11-unknown-1"})
    assert accepted.status_code == 200, accepted.text
    after = (await client.get(f"/api/ledger/drafts/{made['id']}")).json()
    assert after["field_baseline"]["status"] == "known"
    values = fieldbaseline.values({"artifact_id": after["field_baseline"]["artifact_id"]}, "m1")
    assert values["/definition/text"] == "新的一版说明"
    assert "/name" not in values                     # 只接受了这一格，别的不倒填历史


# ==========================================================================
# 7. 中断恢复 + 幂等
# ==========================================================================

async def test_recovery_preserves_decisions_and_does_not_create_another_version(client, documents, datasource,
                                                                               monkeypatch):
    made, doc = await baseline_draft(client, documents, datasource, monkeypatch)
    aligned = candidate(doc, text="新的说明")
    unaligned = candidate(doc, name="另一条指标", citations=[
        {"chunk": f"{doc}-1", "quote": "退款率 = 已支付后退款的订单数 / 已支付订单数",
         "document_id": doc, "span": [0, 23]}])
    run = await make_import(client, made["id"], doc, [aligned, unaligned], [f"{doc}-1", f"{doc}-2"], monkeypatch)
    assert [item["status"] for item in run["items"]] == ["update", "new"]

    accepted = await client.post(f"/api/ledger/imports/{run['run_id']}/accept", json={
        "items": [{"metric_id": "m1", "fields": ["/definition/text"]}],
        "expected_revision": made["revision"], "request_id": "k11-resume-1"})
    assert accepted.status_code == 200, accepted.text
    revision = accepted.json()["revision"]

    # 模拟服务中断：状态记 interrupted，工作项留着
    async with SessionLocal() as session:
        row = await session.get(CaliberImportRun, run["run_id"])
        row.status, row.finished_at = "interrupted", None
        await session.commit()

    resumed = await client.post(f"/api/ledger/imports/{run['run_id']}/resume", json={})
    assert resumed.status_code == 200, resumed.text
    body = (await client.get(f"/api/ledger/imports/{run['run_id']}")).json()
    assert body["status"] in ("succeeded", "partial")
    # 已经接受的那一格还是接受的，没有多建版本
    decided = next(item for item in body["items"] if item["metric_id"] == "m1")
    assert decided["status"] == "accepted" and "/definition/text" in decided["accepted"]
    draft_now = (await client.get(f"/api/ledger/drafts/{made['id']}")).json()
    assert draft_now["revision"] == revision


async def test_startup_reaper_marks_a_running_import_interrupted_and_resume_continues(client, documents, datasource,
                                                                                     monkeypatch):
    """进程被杀之后：启动整理把排队/进行中的导入记成 interrupted，工作项留着；人点恢复接着做，不串项。"""
    made, doc = await baseline_draft(client, documents, datasource, monkeypatch)
    run = await make_import(client, made["id"], doc, [candidate(doc, text="新的说明")],
                            [f"{doc}-1", f"{doc}-2"], monkeypatch)
    run_id = run["run_id"]
    # 模拟「进程被强杀，库里还记着进行中」：属主是已经不在的进程，工作项还没决定
    async with SessionLocal() as session:
        row = await session.get(CaliberImportRun, run_id)
        row.status, row.owner, row.finished_at = "running", "a-dead-worker", None
        item = (await session.execute(select(CaliberImportItem).where(
            CaliberImportItem.run_id == run_id))).scalars().one()
        item.status = "pending"
        await session.commit()

    await reimport.startup()
    got = (await client.get(f"/api/ledger/imports/{run_id}")).json()
    assert got["status"] == "interrupted"
    assert got["items"][0]["status"] == "pending"           # 工作项原样留着，可恢复

    monkeypatch.setattr(reimport, "extract_candidates",
                        fake_extract([candidate(doc, text="新的说明")], [f"{doc}-1", f"{doc}-2"]))
    resumed = await client.post(f"/api/ledger/imports/{run_id}/resume", json={})
    assert resumed.status_code == 200, resumed.text
    final = (await client.get(f"/api/ledger/imports/{run_id}")).json()
    assert final["status"] == "succeeded" and len(final["items"]) == 1
    assert final["items"][0]["status"] == "update" and final["items"][0]["metric_id"] == "m1"


async def test_resume_matches_items_by_alignment_key_and_never_moves_decisions(client, documents, datasource,
                                                                              monkeypatch):
    """恢复不能按候选位置搬决定：顺序倒过来、条数变少，工作项还是原来那几条，决定仍挂在各自身上。"""
    made, doc = await baseline_draft(client, documents, datasource, monkeypatch)
    first = candidate(doc, text="说法 A")                    # 引 doc-2 → 对到 m1
    second = candidate(doc, name="另一条指标", citations=[
        {"chunk": f"{doc}-1", "quote": "退款率 = 已支付后退款的订单数 / 已支付订单数",
         "document_id": doc, "span": [0, 23]}])              # 引 doc-1 → 没对上
    run = await make_import(client, made["id"], doc, [first, second], [f"{doc}-1", f"{doc}-2"], monkeypatch)
    items = {item["name"]: item for item in run["items"]}
    aligned, other = items["已支付退款率"], items["另一条指标"]

    rejected = await client.post(f"/api/ledger/imports/{run['run_id']}/reject",
                                 json={"items": [{"item_id": other["item_id"]}]})
    assert rejected.status_code == 200, rejected.text

    # 中断之后重抽：这次候选**倒着来**（顺序、条数都和上一次不同）
    async with SessionLocal() as session:
        row = await session.get(CaliberImportRun, run["run_id"])
        row.status = "interrupted"
        await session.commit()
    monkeypatch.setattr(reimport, "extract_candidates", fake_extract([second, first], [f"{doc}-1", f"{doc}-2"]))
    assert (await client.post(f"/api/ledger/imports/{run['run_id']}/resume", json={})).status_code == 200

    after = (await client.get(f"/api/ledger/imports/{run['run_id']}")).json()
    by_id = {item["item_id"]: item for item in after["items"]}
    assert set(by_id) == {aligned["item_id"], other["item_id"]}      # 没有多出新项，也没有替换掉
    assert by_id[other["item_id"]]["status"] == "rejected"           # 拒绝仍挂在它自己身上
    assert by_id[other["item_id"]]["metric_id"] == ""
    assert by_id[aligned["item_id"]]["rejected"] == [] and by_id[aligned["item_id"]]["metric_id"] == "m1"
    # 每项自己的来源依据没被新一轮覆盖
    assert by_id[other["item_id"]]["align"]["evidence"] == [f"{doc}-1"]
    assert by_id[aligned["item_id"]]["align"]["evidence"] == [f"{doc}-2"]

    # 条数再变少：这次只抽到对到 m1 的那一条，别的项原样留着（决定不丢）
    async with SessionLocal() as session:
        row = await session.get(CaliberImportRun, run["run_id"])
        row.status = "interrupted"
        await session.commit()
    monkeypatch.setattr(reimport, "extract_candidates", fake_extract([first], [f"{doc}-1", f"{doc}-2"]))
    assert (await client.post(f"/api/ledger/imports/{run['run_id']}/resume", json={})).status_code == 200
    shrunk = (await client.get(f"/api/ledger/imports/{run['run_id']}")).json()["items"]
    kept = {item["item_id"]: item for item in shrunk}
    assert other["item_id"] in kept and kept[other["item_id"]]["status"] == "rejected"


async def test_auto_only_takes_undecided_cells_and_skips_decided_ones(client, documents, datasource, monkeypatch):
    """auto 只收还没决定的「无冲突更新 / 新增候选」：已经拒绝过的那一格不再被 auto 碰。"""
    made, doc = await baseline_draft(client, documents, datasource, monkeypatch)
    run = await make_import(client, made["id"], doc, [candidate(doc, text="新的说明", unit="百分比")],
                            [f"{doc}-1", f"{doc}-2"], monkeypatch)
    item = run["items"][0]
    assert states(item)["/unit"] == "update" and states(item)["/definition/text"] == "update"

    rejected = await client.post(f"/api/ledger/imports/{run['run_id']}/reject", json={
        "items": [{"item_id": item["item_id"], "fields": ["/unit"]}]})
    assert rejected.status_code == 200, rejected.text

    accepted = await client.post(f"/api/ledger/imports/{run['run_id']}/accept", json={
        "items": [{"item_id": item["item_id"], "auto": True}], "expected_revision": made["revision"],
        "request_id": "k11-auto-1"})
    assert accepted.status_code == 200, accepted.text
    after_item = (await client.get(f"/api/ledger/imports/{run['run_id']}")).json()["items"][0]
    assert after_item["accepted"] == ["/definition/text"] and after_item["rejected"] == ["/unit"]

    after = (await client.get(f"/api/ledger/drafts/{made['id']}")).json()
    assert after["metrics"][0]["definition"]["text"] == "新的说明"   # auto 收了
    assert after["metrics"][0]["unit"] == "%"                       # 拒绝的那一格没被 auto 收掉


async def test_a_live_change_after_the_candidate_refuses_accept_until_refreshed(client, documents, datasource,
                                                                               monkeypatch):
    """候选算出来之后 L 被人改过：只把 expected_revision 更新到最新一版也不许覆盖，要先刷新三方再确认。"""
    made, doc = await baseline_draft(client, documents, datasource, monkeypatch)
    run = await make_import(client, made["id"], doc, [candidate(doc, text="新的说明")],
                            [f"{doc}-1", f"{doc}-2"], monkeypatch)
    item = run["items"][0]

    edited = await client.patch(f"/api/ledger/drafts/{made['id']}", json={
        "operations": [{"op": "set", "metric_id": "m1", "path": "/unit", "value": "单"}],
        "expected_revision": made["revision"], "request_id": "k11-touch-1"})
    assert edited.status_code == 200 and edited.json()["change"]["applied"] is True
    latest = edited.json()["revision"]

    stale = await client.post(f"/api/ledger/imports/{run['run_id']}/accept", json={
        "items": [{"item_id": item["item_id"], "fields": ["/definition/text"]}], "expected_revision": latest})
    assert stale.status_code == 409 and stale.json()["code"] == reimport.IMPORT_STALE_L, stale.text
    assert "刷新" in stale.json()["detail"]
    fresh = (await client.get(f"/api/ledger/drafts/{made['id']}")).json()
    assert fresh["metrics"][0]["definition"]["text"] == "已支付订单里发生退款的比例"   # 一点没落
    assert fresh["revision"] == latest

    # 刷新三方（按现在的 L 重算）之后，人重新确认再接受
    refreshed = await client.post(f"/api/ledger/imports/{run['run_id']}/refresh", json={})
    assert refreshed.status_code == 200, refreshed.text
    assert states(refreshed.json()["items"][0])["/unit"] == "unchanged"      # 三方跟着新的 L 走了
    ok = await client.post(f"/api/ledger/imports/{run['run_id']}/accept", json={
        "items": [{"item_id": item["item_id"], "fields": ["/definition/text"]}], "expected_revision": latest,
        "request_id": "k11-stale-1"})
    assert ok.status_code == 200 and ok.json()["applied"] is True, ok.text
    after = (await client.get(f"/api/ledger/drafts/{made['id']}")).json()
    assert after["metrics"][0]["definition"]["text"] == "新的说明"
    assert after["metrics"][0]["unit"] == "单"                              # 人工改的那一格没被覆盖


async def test_resume_refuses_while_queued_or_running_with_the_matching_wording(client, documents, datasource,
                                                                               monkeypatch):
    """恢复正在排队 / 正在运行的那一次导入：回 409，文案按状态说「排队中 / 正在运行」。"""
    made, doc = await baseline_draft(client, documents, datasource, monkeypatch)
    run = await make_import(client, made["id"], doc, [candidate(doc, text="新的说明")],
                            [f"{doc}-1", f"{doc}-2"], monkeypatch)
    for status, word in (("queued", "排队中"), ("running", "正在运行")):
        async with SessionLocal() as session:
            row = await session.get(CaliberImportRun, run["run_id"])
            row.status = status
            await session.commit()
        refused = await client.post(f"/api/ledger/imports/{run['run_id']}/resume", json={})
        assert refused.status_code == 409 and refused.json()["code"] == reimport.IMPORT_ALREADY_RUNNING
        assert word in refused.json()["detail"] and refused.json()["status"] == status


async def test_accept_is_idempotent_by_request_id(client, documents, datasource, monkeypatch):
    made, doc = await baseline_draft(client, documents, datasource, monkeypatch)
    run = await make_import(client, made["id"], doc, [candidate(doc, text="新的说明")],
                            [f"{doc}-1", f"{doc}-2"], monkeypatch)
    body = {"items": [{"metric_id": "m1", "fields": ["/definition/text"]}],
            "expected_revision": made["revision"], "request_id": "k11-idem-1"}
    first = await client.post(f"/api/ledger/imports/{run['run_id']}/accept", json=body)
    assert first.status_code == 200 and first.json()["applied"] is True
    revision = first.json()["revision"]

    replay = await client.post(f"/api/ledger/imports/{run['run_id']}/accept", json=body)
    assert replay.status_code == 200 and replay.json()["replayed"] is True
    assert replay.json()["revision"] == revision
    after = (await client.get(f"/api/ledger/drafts/{made['id']}")).json()
    assert after["revision"] == revision
    # 重放交回的是**已提交的结果**：B 读得回来，而且就是第一次那次接受推进出来的那一件
    assert after["field_baseline"]["status"] == "known" and after["field_baseline"]["readable"] is True
    assert fieldbaseline.values({"artifact_id": after["field_baseline"]["artifact_id"]}, "m1")["/definition/text"] \
        == "新的说明"

    clash = await client.post(f"/api/ledger/imports/{run['run_id']}/accept", json={
        **body, "items": [{"metric_id": "m1", "fields": ["/unit"]}]})
    assert clash.status_code == 409 and clash.json()["code"] == reimport.IMPORT_IDEMPOTENCY_CONFLICT


# ==========================================================================
# 8. 原子性：先写 B 工件，再一个事务提交 L + 工作项决定 + 新 B 引用
# ==========================================================================

async def _request_rows(request_id: str) -> list[Any]:
    from app.db.models import CaliberWriteRequest

    async with SessionLocal() as session:
        return list((await session.execute(select(CaliberWriteRequest).where(
            CaliberWriteRequest.request_id == request_id))).scalars())


async def _version_count(workflow_id: str) -> int:
    from app.db.models import WorkflowVersion

    async with SessionLocal() as session:
        return len(list((await session.execute(select(WorkflowVersion).where(
            WorkflowVersion.workflow_id == workflow_id))).scalars()))


async def _draft_revision(draft_id: str) -> int:
    async with SessionLocal() as session:
        draft = await session.get(CaliberDraft, draft_id)
        return draft.payload.get("version") if isinstance(draft.payload, dict) else None


async def test_a_baseline_write_failure_keeps_l_uncommitted_and_the_retry_succeeds(
        client, documents, datasource, monkeypatch):
    """故障注入：B 工件写不进去 → L 不提交（没有新版本、没有幂等记录），修好之后同一个 request_id 重试成功。"""
    from app.core import artifact_store

    made, doc = await baseline_draft(client, documents, datasource, monkeypatch)
    run = await make_import(client, made["id"], doc, [candidate(doc, text="新的说明")],
                            [f"{doc}-1", f"{doc}-2"], monkeypatch)
    before_versions = await _version_count(made["workflow_id"])
    request_id = "k11-atomic-baseline"
    body = {"items": [{"metric_id": "m1", "fields": ["/definition/text"]}],
            "expected_revision": made["revision"], "request_id": request_id}

    real_put_json = artifact_store.put_json

    async def boom(*_args, **_kwargs):
        raise OSError("工件库写不进去")

    monkeypatch.setattr(artifact_store, "put_json", boom)
    failed = await client.post(f"/api/ledger/imports/{run['run_id']}/accept", json=body)
    assert failed.status_code == 503 and failed.json()["code"] == reimport.IMPORT_BASELINE_UNAVAILABLE, failed.text

    # 新 L 一个字节都没可见：版本、图内容、B 引用都没动；幂等记录也没落下（重试仍是「第一次」）
    after = (await client.get(f"/api/ledger/drafts/{made['id']}")).json()
    assert after["revision"] == made["revision"] and await _version_count(made["workflow_id"]) == before_versions
    assert after["metrics"][0]["definition"]["text"] == "已支付订单里发生退款的比例"
    assert after["field_baseline"]["artifact_id"] == made["field_baseline"]["artifact_id"]
    assert await _request_rows(request_id) == []

    # 修好工件库：同一个 request_id 重试（不是回放，是第一次真正落库），只多一版，B 跟着推进
    monkeypatch.setattr(artifact_store, "put_json", real_put_json)
    retried = await client.post(f"/api/ledger/imports/{run['run_id']}/accept", json=body)
    assert retried.status_code == 200 and retried.json()["applied"] is True, retried.text
    assert await _version_count(made["workflow_id"]) == before_versions + 1
    after = (await client.get(f"/api/ledger/drafts/{made['id']}")).json()
    assert after["metrics"][0]["definition"]["text"] == "新的说明"
    assert fieldbaseline.values({"artifact_id": after["field_baseline"]["artifact_id"]}, "m1")["/definition/text"] \
        == "新的说明"


async def test_a_failure_after_the_baseline_write_rolls_l_back(client, documents, datasource, monkeypatch):
    """回滚：B 工件已经写了（孤儿，可以接受），但提交那一步失败 → L 不提交、幂等记录不落，重试仍能成功。"""
    from app.apps import versions as versions_mod
    from app.apps.versions import WorkflowChanged

    made, doc = await baseline_draft(client, documents, datasource, monkeypatch)
    run = await make_import(client, made["id"], doc, [candidate(doc, text="新的说明")],
                            [f"{doc}-1", f"{doc}-2"], monkeypatch)
    before_versions = await _version_count(made["workflow_id"])
    request_id = "k11-atomic-rollback"
    body = {"items": [{"metric_id": "m1", "fields": ["/definition/text"]}],
            "expected_revision": made["revision"], "request_id": request_id}

    real_save_graph = versions_mod.save_graph

    async def boom(*_args, **_kwargs):
        raise WorkflowChanged("人工制造的并发写入失败")

    monkeypatch.setattr(versions_mod, "save_graph", boom)
    failed = await client.post(f"/api/ledger/imports/{run['run_id']}/accept", json=body)
    assert failed.status_code == 409 and failed.json()["code"] == reimport.IMPORT_STALE_REVISION, failed.text

    after = (await client.get(f"/api/ledger/drafts/{made['id']}")).json()
    assert after["revision"] == made["revision"] and await _version_count(made["workflow_id"]) == before_versions
    assert after["metrics"][0]["definition"]["text"] == "已支付订单里发生退款的比例"
    assert after["field_baseline"]["artifact_id"] == made["field_baseline"]["artifact_id"]
    assert await _request_rows(request_id) == []

    monkeypatch.setattr(versions_mod, "save_graph", real_save_graph)
    retried = await client.post(f"/api/ledger/imports/{run['run_id']}/accept", json=body)
    assert retried.status_code == 200 and retried.json()["applied"] is True, retried.text
    assert await _version_count(made["workflow_id"]) == before_versions + 1


async def test_partial_baseline_coverage_is_reported_and_other_fields_stay_manual(
        client, documents, datasource, monkeypatch):
    """baseline_unknown 上人工逐字段接受：B 只覆盖被接受的那几格，coverage 是 partial（不能报成整卡 known）。"""
    made, doc = await baseline_draft(client, documents, datasource, monkeypatch)
    await _clear_baseline(made["id"])
    run = await make_import(client, made["id"], doc, [candidate(doc, text="新的一版说明", unit="百分比")],
                            [f"{doc}-1", f"{doc}-2"], monkeypatch)
    accepted = await client.post(f"/api/ledger/imports/{run['run_id']}/accept", json={
        "items": [{"metric_id": "m1", "fields": ["/definition/text"]}], "expected_revision": made["revision"],
        "request_id": "k11-partial-1"})
    assert accepted.status_code == 200, accepted.text

    after = (await client.get(f"/api/ledger/drafts/{made['id']}")).json()
    baseline = after["field_baseline"]
    assert baseline["status"] == "known" and baseline["readable"] is True
    coverage = baseline["coverage"]
    assert coverage["status"] == "partial"
    assert "/name" in coverage["metrics"]["m1"]["missing"]        # 没接受的格照实说没有基线
    assert coverage["metrics"]["m1"]["covered"] == 1
    assert coverage["metrics"]["m1"]["expected"] > 1

    # 还有别的说法、又没有可比基线的格子仍旧是「需要人工对齐」，不会被悄悄升格成「无冲突更新」或「冲突」
    item = (await client.get(f"/api/ledger/imports/{run['run_id']}")).json()["items"][0]
    by_path = states(item)
    assert by_path["/unit"] == "manual"
    assert "/definition/text" in item["accepted"]

    # auto 收不掉没有基线的格子：还要人逐条点名
    refused = await client.post(f"/api/ledger/imports/{run['run_id']}/accept", json={
        "items": [{"metric_id": "m1", "auto": True}], "expected_revision": after["revision"]})
    assert refused.status_code == 422 and refused.json()["code"] == reimport.IMPORT_NOTHING_SELECTED
    assert (await client.get(f"/api/ledger/drafts/{made['id']}")).json()["metrics"][0]["unit"] == "%"


async def test_the_first_accept_keeps_a_complete_coverage(client, documents, datasource, monkeypatch):
    """首次接受导入收的是整张卡：coverage 是 complete（和按字段推进出来的 partial 分开说）。"""
    made, doc = await baseline_draft(client, documents, datasource, monkeypatch)
    assert made["field_baseline"]["coverage"]["status"] == "complete"
    run = await make_import(client, made["id"], doc, [candidate(doc, text="新的说明")],
                            [f"{doc}-1", f"{doc}-2"], monkeypatch)
    accepted = await client.post(f"/api/ledger/imports/{run['run_id']}/accept", json={
        "items": [{"metric_id": "m1", "fields": ["/definition/text"]}], "expected_revision": made["revision"],
        "request_id": "k11-complete-1"})
    assert accepted.status_code == 200, accepted.text
    after = (await client.get(f"/api/ledger/drafts/{made['id']}")).json()
    assert after["field_baseline"]["coverage"]["status"] == "complete"
    assert after["field_baseline"]["coverage"]["metrics"]["m1"]["missing"] == []


async def test_create_run_is_idempotent_by_request_id(client, documents, datasource, monkeypatch):
    made, doc = await baseline_draft(client, documents, datasource, monkeypatch)
    monkeypatch.setattr(reimport, "extract_candidates", fake_extract([candidate(doc)], [f"{doc}-2"]))
    first = await client.post(f"/api/ledger/drafts/{made['id']}/imports", json={"request_id": "k11-run-1"})
    again = await client.post(f"/api/ledger/drafts/{made['id']}/imports", json={"request_id": "k11-run-1"})
    assert first.json()["run_id"] == again.json()["run_id"]
    listed = await client.get(f"/api/ledger/drafts/{made['id']}/imports")
    assert listed.status_code == 200 and len(listed.json()["runs"]) == 1
    async with SessionLocal() as session:
        rows = list((await session.execute(select(CaliberImportRun).where(
            CaliberImportRun.draft_id == made["id"]))).scalars())
    assert len(rows) == 1


async def test_stale_revision_is_refused(client, documents, datasource, monkeypatch):
    made, doc = await baseline_draft(client, documents, datasource, monkeypatch)
    run = await make_import(client, made["id"], doc, [candidate(doc, text="新的说明")],
                            [f"{doc}-1", f"{doc}-2"], monkeypatch)
    stale = await client.post(f"/api/ledger/imports/{run['run_id']}/accept", json={
        "items": [{"metric_id": "m1", "fields": ["/definition/text"]}], "expected_revision": 99})
    assert stale.status_code == 409 and stale.json()["code"] == reimport.IMPORT_STALE_REVISION
    assert stale.json()["current_version"] == made["revision"]

    missing = await client.post(f"/api/ledger/imports/{run['run_id']}/accept", json={
        "items": [{"metric_id": "m1", "fields": ["/definition/text"]}]})
    assert missing.status_code == 409 and missing.json()["code"] == reimport.IMPORT_REVISION_REQUIRED


async def test_reject_keeps_l_and_b_untouched(client, documents, datasource, monkeypatch):
    made, doc = await baseline_draft(client, documents, datasource, monkeypatch)
    run = await make_import(client, made["id"], doc, [candidate(doc, text="新的说明")],
                            [f"{doc}-1", f"{doc}-2"], monkeypatch)
    rejected = await client.post(f"/api/ledger/imports/{run['run_id']}/reject", json={
        "items": [{"metric_id": "m1", "fields": ["/definition/text"]}], "note": "先不改"})
    assert rejected.status_code == 200, rejected.text
    body = rejected.json()
    assert body["items"][0]["status"] == "rejected" and "/definition/text" in body["items"][0]["rejected"]
    after = (await client.get(f"/api/ledger/drafts/{made['id']}")).json()
    assert after["metrics"][0]["definition"]["text"] == "已支付订单里发生退款的比例"
    assert after["field_baseline"]["artifact_id"] == made["field_baseline"]["artifact_id"]
    assert after["revision"] == made["revision"]
