"""K05 的字段基线 B：首次接受导入时把**被接受的导入值**按 metric_id + field_path 存成不可变工件。

这份文件盯住任务书 4.7 的三件硬要求：

1. **首次接受导入就是 B 的那一次。** 导入成功规范化、落进初始草稿图的那一刻，被接受的字段值（含引用原文这类不可变
   引用）按 metric_id + field_path 存下来，逐格带值哈希，并连着来源快照与 accepted_revision。读得回来——不是只
   存一个哈希（哈希还原不了 B）。
2. **人工改 L 不动 B。** 字段操作（K03）改的是当前内容；B 还是当初被接受的那一份，一个字不动（也不因为改过了就
   把当前值写回基线）。
3. **旧资产 baseline_unknown。** 保存基线之前建的草稿没有引用：如实标 unknown，不拿它现在的内容倒填历史基线。

模型一律注入假模型、试跑换成假的；这里不连真实业务库。
"""
from __future__ import annotations

import copy
import uuid

import pytest
from sqlalchemy import delete, select

from app.db.base import SessionLocal
from app.db.models import CaliberDraft
from app.ledger import fieldbaseline
from tests.test_ledger_docdraft_api import (  # noqa: F401 - fixture 和起草用例共用
    client, datasource, documents, extract, fake_model, fake_trial, plan_reply,
)


async def accepted_draft(client, documents, datasource, monkeypatch) -> dict:
    """走一次真实起草（假模型 + 假试跑），返回草稿详情。"""
    doc = await documents("baseline-" + uuid.uuid4().hex[:8])
    source_id = await datasource()
    fake_model(monkeypatch, plan_reply(f"{doc}-2"))
    body = await extract(client, doc, source_id)
    return (await client.get(f"/api/ledger/drafts/{body['draft_id']}")).json()


@pytest.fixture(autouse=True)
def _fake_trial(fake_trial):
    """本模块的草稿都走接口起草：试跑必须换成假的，不然会去打夹具里不存在的库。

    （fake_trial 是从别的模块 import 进来的普通夹具，它自带的 autouse 在本模块不生效；这里替本模块的每个用例把它拉起来。）"""
    return fake_trial


async def test_the_first_accepted_import_is_stored_as_a_readable_value_level_baseline(
        client, documents, datasource, monkeypatch):
    """接受导入那一刻存下的是**逐字段的值**：读得回来、哈希对得上、连着来源快照和接受版本。"""
    made = await accepted_draft(client, documents, datasource, monkeypatch)

    baseline = made["field_baseline"]
    assert baseline["status"] == "known" and baseline["accepted_revision"] == 1
    assert baseline["metric_ids"] == ["m1"] and baseline["field_count"] >= 5
    # payload 里另有一份追加式的历史（这次接受是第一条）
    assert [item["artifact_id"] for item in made["field_baseline_history"]] == [baseline["artifact_id"]]
    assert baseline["source_snapshot_artifact_id"] == made["source_snapshot"]["artifact_id"]

    # 按引用读回工件：内容就是当时被接受的那一份，逐格有值、有哈希（不是一串哈希）
    stored = fieldbaseline.read({"artifact_id": baseline["artifact_id"]})
    assert stored["card_id"] == made["card_id"] and stored["accepted_revision"] == 1
    fields = stored["metrics"]["m1"]["fields"]
    assert fields["/name"]["value"] == "已支付退款率"
    assert fields["/unit"]["value"] == "%" and fields["/decimals"]["value"] == 2
    assert fields["/definition/text"]["value"] == "已支付订单里发生退款的比例"
    assert fields["/synonyms"]["value"] == ["退款率"]
    # 引用原文是「不可变值引用」：整条留着（哪一段、原话、span），重导入要拿它比
    citation = fields["/definition/citations"]["value"][0]
    assert citation["quote"] == "只算已支付的订单" and citation["span"] == [8, 16]
    assert all(cell["hash"] for cell in fields.values())
    # 同一份材料只存一份：引用里的 content_hash 就是工件 id（内容寻址）
    assert baseline["content_hash"] == baseline["artifact_id"]
    assert fieldbaseline.values({"artifact_id": baseline["artifact_id"]}, "m1")["/name"] == "已支付退款率"


async def test_editing_fields_moves_l_and_never_touches_b(client, documents, datasource, monkeypatch):
    """人工按字段改（K03）只改 L：B 的引用、值、哈希一个字不动，覆盖判断跟着定义指纹走。"""
    made = await accepted_draft(client, documents, datasource, monkeypatch)
    before_ref = copy.deepcopy(made["field_baseline"])
    before_fields = fieldbaseline.values({"artifact_id": before_ref["artifact_id"]})
    assert made["validation"]["covers_current"] is True

    changed = await client.patch(f"/api/ledger/drafts/{made['id']}", json={
        "operations": [{"op": "set", "metric_id": "m1", "path": "/name", "value": "退款率（人工改过）"},
                       {"op": "set", "metric_id": "m1", "path": "/definition/text", "value": "人工写的新说明"}],
        "expected_revision": made["revision"], "request_id": "baseline-edit-1"})
    assert changed.status_code == 200, changed.text
    assert changed.json()["change"]["applied"] is True

    after = (await client.get(f"/api/ledger/drafts/{made['id']}")).json()
    assert after["metrics"][0]["name"] == "退款率（人工改过）"          # L 变了
    assert after["metrics"][0]["definition"]["text"] == "人工写的新说明"
    assert after["field_baseline"] == before_ref                        # B 的引用原样
    assert fieldbaseline.values({"artifact_id": after["field_baseline"]["artifact_id"]}) == before_fields
    # B 里还是当初被接受的那一份，不是「现在长什么样」
    assert fieldbaseline.values({"artifact_id": after["field_baseline"]["artifact_id"]}, "m1")["/name"] \
        == "已支付退款率"
    # 定义改了，旧验证记录不再覆盖当前版本（覆盖是读的时候按指纹现算的）
    assert after["validation"]["covers_current"] is False
    assert after["validation"]["verdict"] == "stale"


async def test_an_old_draft_reads_as_baseline_unknown_and_is_never_backfilled(client):
    """没有可还原基线的老草稿：如实说 unknown，不拿它现在的内容当 B，也不因为读一次就补一份出来。"""
    draft_id = "old-" + uuid.uuid4().hex[:10]
    async with SessionLocal() as session:
        session.add(CaliberDraft(id=draft_id, title="老草稿", status="path_pending",
                                 payload={"metrics": [{"id": "m1", "name": "旧指标",
                                                       "definition": {"text": "老草稿里现在写的说明"}}]}))
        await session.commit()
    try:
        detail = (await client.get(f"/api/ledger/drafts/{draft_id}")).json()
        assert detail["field_baseline"]["status"] == "baseline_unknown"
        assert "artifact_id" not in detail["field_baseline"]
        assert "无法还原" in detail["field_baseline"]["reason"]

        listed = (await client.get(f"/api/ledger/drafts/{draft_id}/validations")).json()
        assert listed["field_baseline"]["status"] == "baseline_unknown"
        assert listed["records"] == [] and listed["summary"]["verdict"] == "none"

        # 再读一次也不会「补」出一份基线：草稿里现在有指标，B 仍旧是 unknown（当前值不是历史基线）
        again = (await client.get(f"/api/ledger/drafts/{draft_id}")).json()
        assert again["field_baseline"]["status"] == "baseline_unknown"
        assert fieldbaseline.ref_of(again) is None
    finally:
        async with SessionLocal() as session:
            row = await session.get(CaliberDraft, draft_id)
            if row is not None:
                await session.delete(row)
            await session.commit()


async def test_the_baseline_is_content_addressed_and_keeps_every_acceptance(client, documents, datasource,
                                                                           monkeypatch):
    """同一份被接受的材料只存一份工件；每次接受追加一条引用，当前 B 是最后一条。"""
    made = await accepted_draft(client, documents, datasource, monkeypatch)
    ref = dict(made["field_baseline"])
    metrics = [{"id": "m1", "name": "已支付退款率", "unit": "%", "decimals": 2,
                "definition": {"text": "已支付订单里发生退款的比例"}}]
    again = await fieldbaseline.put_metrics(metrics=metrics, card_id=ref["card_id"], workflow_id=ref["workflow_id"],
                                            accepted_revision=1, draft_id=made["id"])
    assert again is not None and again["artifact_id"] != ref["artifact_id"]  # 字段少了，内容不同 → 另一份工件
    assert ref["artifact_id"] not in (again["artifact_id"],)
    history = fieldbaseline.history({**made, fieldbaseline.HISTORY_KEY: [ref, again]})
    assert [item["artifact_id"] for item in history] == [ref["artifact_id"], again["artifact_id"]]


async def test_a_baseline_that_cannot_be_stored_leaves_the_draft_readable(monkeypatch, client, documents,
                                                                         datasource):
    """基线落库失败不把草稿判死：照实说没有基线（baseline_unknown），不假装有。"""
    from app.core import artifact_store

    async def boom(*args, **kwargs):
        raise OSError("工件库写不进去")

    monkeypatch.setattr(artifact_store, "put_json", boom)
    broken = await accepted_draft(client, documents, datasource, monkeypatch)
    assert broken["status"] == "ready"                    # 草稿照出
    assert broken["field_baseline"]["status"] == "baseline_unknown"
    assert "无法还原" in broken["field_baseline"]["reason"]


@pytest.fixture(autouse=True)
async def _cleanup_records():
    """本模块造的草稿由 documents 夹具统一清掉；这里兜一下记录表，避免留下跨用例的残行。"""
    yield
    from app.db.models import CaliberReviewDecision, CaliberValidationRecord

    async with SessionLocal() as session:
        drafts = list((await session.execute(select(CaliberDraft.id))).scalars())
        if drafts:
            await session.execute(delete(CaliberValidationRecord).where(
                CaliberValidationRecord.draft_id.in_(drafts)))
            await session.execute(delete(CaliberReviewDecision).where(
                CaliberReviewDecision.draft_id.in_(drafts)))
        await session.commit()
