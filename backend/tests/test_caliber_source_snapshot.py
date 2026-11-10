"""K05 的来源侧前置切片：提取时把来源存成不可篡改的工件，引用只认实际进提示的材料。

这份文件盯住几件 K05 明确要求、而其它测试没覆盖的事：

1. **快照可还原，不只是哈希。** 提取时把逐份原文、逐段原文、目录文本按内容寻址存进工件库；草稿 payload 只记引用。
   读的时候按 id 回读，原文一模一样，哈希对得上（app/core/artifact_store.load 会复验）。
2. **chunk id 换了，快照照旧读得回来。** 文档被重新切块、段落 id 换了一批，旧草稿记的引用仍指向当初那一份材料。
3. **可见的引用才核得上。** 材料超限时 fit_material 从后往前丢段落；被丢掉的段落不在提示里，模型引用它也核不上。
   同一段话在「全量 chunks」下核得上、在「实际进提示的可见 chunks」下核不上。
4. **每次提示的可见范围有记录。** payload.prompts 逐次记下 visible_chunk_ids、提示词/解析版本、模型、截断情况。
5. **checkpoint 只是任务恢复，不冒充 B。** checkpoint 里只有「哪些组起草好了」，把它当 payload 读，来源基线仍
   是 baseline_unknown；来源的还原只走工件。
6. **旧草稿标记 baseline_unknown。** 没有快照的老草稿不拿当前内容伪造历史基线。

模型一律注入假模型（monkeypatch resolve_draft_model），试跑换成假的，测试不发真实请求、不连真实业务库。
"""
from __future__ import annotations

import copy
import json
import uuid

import pytest
from sqlalchemy import delete, select

from app.core.artifact_store import canonical_json, content_hash, load
from app.db.base import SessionLocal
from app.db.models import CaliberDraft, Chunk, Document, Workflow, WorkflowVersion
from app.ledger import authoring, docdraft, draftplan
from tests.test_ledger_docdraft_api import client, datasource, fake_model, fake_trial  # noqa: F401 (fixtures)

SMALL_ONE = "退款率按已支付订单统计，退款订单不计入。"
SMALL_TWO = "门店维度只保留已支付订单，未支付不进分子分母。"


@pytest.fixture(autouse=True)
def _fake_trial(fake_trial):
    """本模块的草稿都走接口起草：试跑必须换成假的，不然会去打夹具里不存在的库。

    （fake_trial 是从别的模块 import 进来的普通夹具，它自带的 autouse 在本模块不生效；这里替本模块的每个用例把它拉起来。）"""
    return fake_trial


# ==========================================================================
# 夹具：可自定义段落的文档 + 收尾清理
# ==========================================================================


@pytest.fixture
async def docs():
    """按给定的分段建一份文档；收尾删掉它、它的段落、以及测试期间建的草稿/工作流/运行。"""
    made: list[str] = []

    async def make(doc_id: str, chunks: list[tuple[int, str]], *, title: str = "口径.md") -> str:
        async with SessionLocal() as session:
            session.add(Document(id=doc_id, collection="default", title=title, source=title,
                                 content="".join(content for _, content in chunks), chunk_count=len(chunks)))
            for ordinal, content in chunks:
                session.add(Chunk(id=f"{doc_id}-{ordinal}", document_id=doc_id, ordinal=ordinal,
                                  content=content))
            await session.commit()
        made.append(doc_id)
        return doc_id

    yield make

    async with SessionLocal() as session:
        drafts = (await session.execute(select(CaliberDraft))).scalars().all()
        workflow_ids = [draft.workflow_id for draft in drafts if draft.workflow_id]
        workflow_ids += [draft.payload.get("workflow_id") for draft in drafts
                         if isinstance(draft.payload, dict) and draft.payload.get("workflow_id")]
        workflow_ids = list(dict.fromkeys(wid for wid in workflow_ids if wid))
        for draft in drafts:
            await session.delete(draft)
        if workflow_ids:
            await session.execute(delete(WorkflowVersion).where(WorkflowVersion.workflow_id.in_(workflow_ids)))
            await session.execute(delete(Workflow).where(Workflow.id.in_(workflow_ids)))
        for doc_id in made:
            await session.execute(delete(Chunk).where(Chunk.document_id == doc_id))
            row = await session.get(Document, doc_id)
            if row is not None:
                await session.delete(row)
        await session.commit()


async def _extract(client, doc: str, source_id: str, **extra) -> dict:
    body = (await client.post("/api/ledger/drafts/extract",
                              json={"documents": [doc], "datasource_id": source_id, **extra})).json()
    return body


async def _draft(client, draft_id: str) -> dict:
    return (await client.get(f"/api/ledger/drafts/{draft_id}")).json()


def _reply(chunk_id: str, *, quote: str, name: str = "退款率", sql: str | None = None) -> dict:
    """一条指标的回复：引用逐字抄原文。"""
    return {
        "entry_fields": [],
        "sql": sql or "SELECT COUNT(*) AS refund_rate FROM orders",
        "metrics": [{
            "id": "m1", "name": name, "synonyms": [], "unit": "%", "decimals": 2,
            "expression": "cell(nodes.fetch, 0, 'refund_rate')",
            "definition": {"text": "已支付订单里发生退款的比例",
                           "citations": [{"chunk": chunk_id, "quote": quote}]},
            "open_questions": [],
        }],
        "open_questions": [],
    }


# ==========================================================================
# 1. 快照可还原、不可变
# ==========================================================================


async def test_the_first_extraction_persists_a_restorable_source_snapshot(client, docs, datasource,
                                                                         monkeypatch):
    """提取时把原文存进工件库：payload 只有引用，按引用回读，逐份、逐段原文一模一样，哈希对得上。"""
    doc = await docs("snap-1", [(0, SMALL_ONE), (1, SMALL_TWO)], title="退款口径.md")
    source_id = await datasource()
    fake_model(monkeypatch, json.dumps(_reply(f"{doc}-0", quote="退款率按已支付订单统计"), ensure_ascii=False))

    made = await _draft(client, (await _extract(client, doc, source_id))["draft_id"])
    ref = made["source_snapshot"]
    assert ref["version"] == docdraft.SNAPSHOT_VERSION and ref["kind"] == docdraft.SNAPSHOT_KIND
    assert ref["chunk_ids"] == [f"{doc}-0", f"{doc}-1"] and ref["documents"] == [doc]
    # 引用里的 id 就是规范化内容的 sha256：内容不变，id 不变；内容变了，读的时候复验哈希会当场报错
    assert ref["content_hash"] == ref["artifact_id"]

    snapshot = load(ref["artifact_id"])
    assert snapshot is not None
    assert content_hash(canonical_json(snapshot)) == ref["artifact_id"]
    assert snapshot["chunks"][f"{doc}-0"]["content"] == SMALL_ONE
    assert snapshot["chunks"][f"{doc}-1"]["content"] == SMALL_TWO
    assert snapshot["documents"][0]["content"] == SMALL_ONE + SMALL_TWO
    assert snapshot["documents"][0]["content_hash"] == content_hash(SMALL_ONE + SMALL_TWO)
    assert snapshot["catalog"].strip(), "目录文本也要存：它改了，模型看到的材料和写出的 SQL 就变了"
    # 基线状态：有可还原快照 → known（只说明材料还原得出来，不等于字段基线，更不等于业务验证通过）
    assert made["source_baseline"] == {"status": "known", "readable": True, "artifact_id": ref["artifact_id"],
                                       "version": docdraft.SNAPSHOT_VERSION,
                                       "chunk_ids": [f"{doc}-0", f"{doc}-1"]}
    assert docdraft.source_baseline(made) == made["source_baseline"]


async def test_the_snapshot_still_reads_after_the_document_is_rechunked(client, docs, datasource,
                                                                       monkeypatch):
    """重导入会重新切块、换一批 chunk id；旧草稿记的引用仍指向当初那一份材料（按内容寻址，不按名字）。"""
    doc = await docs("snap-2", [(0, SMALL_ONE), (1, SMALL_TWO)])
    source_id = await datasource()
    fake_model(monkeypatch, json.dumps(_reply(f"{doc}-0", quote="退款率按已支付订单统计"), ensure_ascii=False))
    made = await _draft(client, (await _extract(client, doc, source_id))["draft_id"])
    artifact_id = made["source_snapshot"]["artifact_id"]

    # 文档被重新切块：旧段落删掉，换成三个新 id、顺序和内容也变了
    async with SessionLocal() as session:
        await session.execute(delete(Chunk).where(Chunk.document_id == doc))
        for ordinal, content in enumerate([SMALL_TWO, "新加的一段说明。", SMALL_ONE]):
            session.add(Chunk(id=f"re-{doc}-{ordinal}", document_id=doc, ordinal=ordinal, content=content))
        await session.commit()

    snapshot = load(artifact_id)
    assert snapshot is not None, "旧引用必须还读得回来"
    assert set(snapshot["chunks"]) == {f"{doc}-0", f"{doc}-1"}, "当初收进去的段落一个都不能少"
    assert snapshot["chunks"][f"{doc}-0"]["content"] == SMALL_ONE
    assert snapshot["chunks"][f"{doc}-1"]["content"] == SMALL_TWO
    # 草稿里那条指标仍指着当初的段落 id 和原文，不受重新切块影响
    assert made["metrics"][0]["definition"]["citations"][0]["chunk"] == f"{doc}-0"


# ==========================================================================
# 2. 引用核对只认实际可见的材料
# ==========================================================================


async def test_a_citation_to_a_truncated_away_chunk_is_not_accepted(client, docs, datasource, monkeypatch):
    """材料超限时从后往前丢段落：被丢掉的段落没进提示，模型引用它也核不上，只留能核对的那条指标。"""
    tail = "这一段很长，超限时会被整段丢掉。" * 3000  # 六万字，稳稳超过一次提示的上限
    doc = await docs("snap-3", [(0, SMALL_ONE), (1, tail)])
    source_id = await datasource()
    reply = _reply(f"{doc}-0", quote="退款率按已支付订单统计", name="退款率")
    reply["metrics"].append({**reply["metrics"][0], "id": "m2", "name": "长文里的指标",
                             "definition": {"text": "引用被丢段的原文",
                                            "citations": [{"chunk": f"{doc}-1",
                                                           "quote": "这一段很长，超限时会被整段丢掉"}]}})
    fake_model(monkeypatch, json.dumps(reply, ensure_ascii=False))

    made = await _draft(client, (await _extract(client, doc, source_id))["draft_id"])

    assert made["doc_truncated"] is True
    assert [m["name"] for m in made["metrics"]] == ["退款率"], "引用不到可见材料的指标不该留下"
    assert any(item["name"] == "长文里的指标" and "引用" in item["reason"] for item in made["dropped"])
    # 提示记录里也如实写着：只看得见第一段
    [trace] = [t for t in made["prompts"] if t["purpose"] == "plan"]
    assert trace["visible_chunk_ids"] == [f"{doc}-0"]
    assert trace["doc_truncated"] is True
    assert set(trace["visible_chars"]) == {f"{doc}-0"}


def test_normalize_plan_checks_citations_only_against_what_the_model_saw():
    """同一段话：在「全量 chunks」下核得上，在「实际进提示的可见 chunks」下核不上——引用核对以可见范围为准。"""
    full = {"c1": {"ordinal": 0, "content": "退款率按已支付订单统计", "document_id": "d"},
            "c2": {"ordinal": 1, "content": "门店维度只保留已支付订单", "document_id": "d"}}
    visible = {"c1": full["c1"]}  # c2 被截断丢掉了
    raw = {"entry_fields": [], "sql": "SELECT COUNT(*) AS n FROM orders",
           "metrics": [{"id": "m1", "name": "退款率", "expression": "cell(nodes.fetch, 0, 'n')",
                        "definition": {"text": "口径", "citations": [{"chunk": "c2",
                                                                     "quote": "门店维度只保留已支付订单"}]}}],
           "open_questions": []}

    with_visibility, dropped = draftplan.normalize_plan(raw, visible, allowed_tables={"orders"})
    assert with_visibility["metrics"] == [] and dropped and "引用" in dropped[0]["reason"]

    without_visibility, dropped = draftplan.normalize_plan(raw, full, allowed_tables={"orders"})
    assert dropped == [] and [m["name"] for m in without_visibility["metrics"]] == ["退款率"]


async def test_plan_groups_reports_the_same_visible_chunks_it_sends_to_the_model():
    """draftplan 自己算材料时，报出来的 visible_chunk_ids 就是真正进提示的那几段（与提示文本一致）。"""
    from types import SimpleNamespace

    class Model:
        model_name = "visible-model"

        def __init__(self) -> None:
            self.messages: list = []

        async def ainvoke(self, messages):
            self.messages = messages
            return SimpleNamespace(content='{"groups": [], "open_questions": [], "uncovered": []}')

    first = "第一段：" + "退款率按已支付订单统计。" * 3000
    second = "第二段：" + "门店维度只保留已支付订单。" * 3000
    chunks = {"c1": {"ordinal": 0, "content": first, "document_id": "d"},
              "c2": {"ordinal": 1, "content": second, "document_id": "d"}}
    model = Model()
    result = await draftplan.plan_groups(None, docs=[], chunks=chunks, tables_text="表 orders", model=model)
    sent = "\n".join(str(getattr(message, "content", "")) for message in model.messages)
    assert result["visible_chunk_ids"] == ["c1"] and result["doc_truncated"] is True
    assert "第一段" in sent and "第二段" not in sent
    assert result["prompt_version"] == draftplan.PROMPT_VERSION
    assert result["parse_version"] == docdraft.PARSE_VERSION


# ==========================================================================
# 3. 提示记录
# ==========================================================================


async def test_every_model_prompt_records_what_it_saw_and_which_versions(client, docs, datasource,
                                                                        monkeypatch):
    """payload.prompts 逐次记下 visible_chunk_ids、模型、提示词/解析版本：历史结果才知道是按哪一版抽的。"""
    doc = await docs("snap-4", [(0, SMALL_ONE), (1, SMALL_TWO)])
    source_id = await datasource()
    fake_model(monkeypatch, json.dumps(_reply(f"{doc}-0", quote="退款率按已支付订单统计"), ensure_ascii=False))

    made = await _draft(client, (await _extract(client, doc, source_id))["draft_id"])
    [trace] = made["prompts"]
    assert trace["purpose"] == "plan" and trace["round"] == 1
    assert trace["visible_chunk_ids"] == [f"{doc}-0", f"{doc}-1"]
    assert trace["model_id"] == "fake-model"
    assert trace["prompt_version"] == draftplan.PROMPT_VERSION
    assert trace["parse_version"] == docdraft.PARSE_VERSION
    assert trace["doc_truncated"] is False and trace["catalog_truncated"] is False
    assert trace["visible_chars"][f"{doc}-0"] == len(SMALL_ONE)


async def test_the_auxiliary_extract_path_reports_visibility_and_versions():
    """docdraft.extract（辅助路径）也交回可见段落与版本，和主路径一套说法。"""
    from types import SimpleNamespace

    class Model:
        model_name = "aux-model"

        async def ainvoke(self, _messages):
            return SimpleNamespace(content=json.dumps(
                {"metrics": [{"name": "退款率", "definition": {"text": "口径", "citations": [
                    {"chunk": "c1", "quote": "退款率按已支付订单统计"}]}}]}, ensure_ascii=False))

    result = await docdraft.extract(None, docs=[{"document_id": "d", "title": "口径.md"}],
                                    chunks={"c1": {"ordinal": 0, "content": SMALL_ONE, "document_id": "d"}},
                                    model=Model())
    assert result["visible_chunk_ids"] == ["c1"]
    assert result["prompt_version"] == docdraft.PROMPT_VERSION
    assert result["parse_version"] == docdraft.PARSE_VERSION
    assert [m["name"] for m in result["metrics"]] == ["退款率"]


# ==========================================================================
# 4. checkpoint 只是任务恢复，不冒充 B
# ==========================================================================


async def _run_extract(draft_id: str) -> dict:
    """直接调后台起草，试跑换成不启动引擎的桩（不经 HTTP、不建任务）。"""
    trial: dict = {}

    async def run_trial(**_kwargs):
        return {"run_id": "", "status": "succeeded", "error": ""}

    async def save_trial(_draft_id, value):
        trial.update(value or {})

    def binding(*_args):
        return {}

    await authoring.extract(draft_id, run_trial=run_trial, save_trial=save_trial, trial_binding=binding)
    return trial


async def test_the_checkpoint_is_recovery_only_and_never_masquerades_as_a_baseline(client, docs, datasource,
                                                                                  monkeypatch):
    """材料指纹相同的重试接续 checkpoint；checkpoint 只记「哪些组起草好了」，把它当 payload 读仍是 baseline_unknown。"""
    doc = await docs("snap-5", [(0, SMALL_ONE), (1, SMALL_TWO)])
    source_id = await datasource()
    group = _reply(f"{doc}-0", quote="退款率按已支付订单统计")
    calls: list[list[str]] = []

    async def interrupted(_session, **kwargs):
        calls.append(list(kwargs["covered"]))
        if len(calls) == 1:
            return {"groups": [group], "uncovered": ["门店数"]}
        raise RuntimeError("这一轮被打断")

    monkeypatch.setattr(draftplan, "plan_groups", interrupted)
    extracted = await _extract(client, doc, source_id)
    draft_id = extracted["draft_id"]
    await _run_extract(draft_id)

    async with SessionLocal() as session:
        row = await session.get(CaliberDraft, draft_id)
        payload = copy.deepcopy(row.payload)
    # 被打断的那一轮之前，第一组和来源快照都已经写下来了
    assert payload["checkpoint"]["groups"][0]["metrics"][0]["name"] == "退款率"
    assert payload["source_snapshot"]["artifact_id"] and payload["source_baseline"]["status"] == "known"
    # checkpoint 里只有恢复用的东西：把它当成一份 payload 去读来源基线，只会得到 baseline_unknown
    assert set(payload["checkpoint"]) == {"groups"}
    assert docdraft.source_baseline(payload["checkpoint"])["status"] == "baseline_unknown"
    assert "source_snapshot" not in payload["checkpoint"]

    # 重试：材料没变，checkpoint 里的组接续下去；来源快照仍从这次读到的材料现取（不来自 checkpoint）
    second = copy.deepcopy(group)
    second["metrics"][0].update(id="m2", name="门店数")

    async def resume(_session, **kwargs):
        assert kwargs["covered"] == ["退款率"], "重试要接着上次起草过的地方，不重头再来"
        return {"groups": [second], "uncovered": []}

    monkeypatch.setattr(draftplan, "plan_groups", resume)
    async with SessionLocal() as session:  # 重试会先把草稿置回 drafting（api/ledger 的 retry 就是这样）
        row = await session.get(CaliberDraft, draft_id)
        row.status = "drafting"
        await session.commit()
    await _run_extract(draft_id)
    async with SessionLocal() as session:
        row = await session.get(CaliberDraft, draft_id)
        resumed = copy.deepcopy(row.payload)
    assert [m["name"] for m in resumed["metrics"]] == ["退款率", "门店数"]
    assert resumed["source_snapshot"]["artifact_id"] == payload["source_snapshot"]["artifact_id"]
    assert load(resumed["source_snapshot"]["artifact_id"])["chunks"][f"{doc}-1"]["content"] == SMALL_TWO


# ==========================================================================
# 5. 旧草稿：baseline_unknown，不拿当前值伪造历史
# ==========================================================================


async def test_an_old_draft_without_a_snapshot_reads_as_baseline_unknown(client, docs):
    """保存来源快照之前起草的草稿没有引用：如实标记 baseline_unknown，不能拿它现在的内容当历史基线。"""
    draft_id = f"old-{uuid.uuid4().hex[:10]}"
    old_payload = {"metrics": [{"id": "m1", "name": "旧指标", "expression": "cell(nodes.fetch, 0, 'n')"}],
                   "sql": "SELECT 1 AS n", "version": 1, "workflow_id": None}
    async with SessionLocal() as session:
        session.add(CaliberDraft(id=draft_id, title="老草稿", status="path_pending",
                                 payload=copy.deepcopy(old_payload)))
        await session.commit()

    async with SessionLocal() as session:
        stored = (await session.get(CaliberDraft, draft_id)).payload
    baseline = docdraft.source_baseline(stored)
    assert baseline["status"] == "baseline_unknown" and "无法还原" in baseline["reason"]
    assert "artifact_id" not in baseline

    body = await _draft(client, draft_id)
    assert body["status"] == "path_pending" and body["metrics"][0]["name"] == "旧指标"
    # 老草稿里根本没有快照，也没有凭空补一条：当前内容照旧显示，但它不是「上次被接受的导入值」。
    # 读接口如实给出 baseline_unknown（没有快照本身就是要说的话，不能让客户端从「缺字段」去猜）
    assert "source_snapshot" not in body
    assert body["source_baseline"]["status"] == "baseline_unknown"
    assert "无法还原" in body["source_baseline"]["reason"] and "artifact_id" not in body["source_baseline"]
    assert docdraft.source_baseline(body)["status"] == "baseline_unknown"


async def test_a_snapshot_is_not_invented_for_a_draft_without_one(client, docs, datasource, monkeypatch):
    """新草稿照常记快照；同一批材料被再次读取时快照不变（按内容寻址，同一份材料只存一份、id 相同）。"""
    doc = await docs("snap-6", [(0, SMALL_ONE)])
    source_id = await datasource()
    fake_model(monkeypatch, json.dumps(_reply(f"{doc}-0", quote="退款率按已支付订单统计"), ensure_ascii=False))
    first = await _draft(client, (await _extract(client, doc, source_id))["draft_id"])

    fake_model(monkeypatch, json.dumps(_reply(f"{doc}-0", quote="退款率按已支付订单统计",
                                              name="退款率（第二份）"), ensure_ascii=False))
    second = await _draft(client, (await _extract(client, doc, source_id))["draft_id"])

    assert first["source_snapshot"]["artifact_id"] == second["source_snapshot"]["artifact_id"]
    assert docdraft.source_baseline(second)["status"] == "known"


# ==========================================================================
# 6. 装快照失败时如实说「没有基线」，不假装有
# ==========================================================================


async def test_a_snapshot_that_cannot_be_stored_is_reported_as_baseline_unknown(client, docs, datasource,
                                                                               monkeypatch):
    """落工件失败（磁盘/库出错）不让整份草稿失败，但也绝不假装有基线：source_snapshot 为空、状态 unknown。"""
    import app.core.artifact_store as store

    doc = await docs("snap-7", [(0, SMALL_ONE)])
    source_id = await datasource()
    fake_model(monkeypatch, json.dumps(_reply(f"{doc}-0", quote="退款率按已支付订单统计"), ensure_ascii=False))

    async def broken(*_args, **_kwargs):
        raise OSError("磁盘满了")

    # _capture_source 在函数里 `from app.core.artifact_store import put_json`，换掉模块属性就换掉了它
    monkeypatch.setattr(store, "put_json", broken)

    made = await _draft(client, (await _extract(client, doc, source_id))["draft_id"])
    assert made["status"] in ("ready", "questions_open"), made.get("error")
    assert made["workflow_id"], "快照存不下不该把草稿判死：图和试跑照旧"
    assert made["source_snapshot"] is None
    assert made["source_baseline"]["status"] == "baseline_unknown"
