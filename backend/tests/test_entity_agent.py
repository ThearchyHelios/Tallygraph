"""标准业务实体 Agent 化治理单元测试（E1 + E2）：附件/知识库联动、增量补丁护栏、交互式实体助手 SSE 与后台扫描发现任务。"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from langchain_core.messages import AIMessage, AIMessageChunk
from sqlalchemy import delete, select

from app.data import catalog_assistant, entities, entity_assistant, entity_discovery, entity_docs, entity_patch
from app.data.engine import CATALOG_PROFILE_OPTION, MASK_COLUMNS_OPTION
from app.db.base import SessionLocal
from app.db.models import Chunk, Document, Workflow
from app.db.models_ontology import CanonicalEntity, EntityDiscoveryJob
from app.main import app
from tests.fixtures.sources import forget_source, sqlite_source


@dataclass
class _Turn:
    text: list[str] = field(default_factory=list)
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    usage: tuple[int, int] = (80, 30)


class _ScriptedModel:
    def __init__(self, turns: list[_Turn] | None = None, *, invoke_reply: str = "") -> None:
        self.turns = list(turns or [])
        self.invoke_reply = invoke_reply
        self.seen: list[list[Any]] = []
        self.tools: list[Any] | None = None

    def bind_tools(self, tools: list[Any], **_: Any) -> "_ScriptedModel":
        self.tools = tools
        return self

    async def astream(self, messages: list[Any]):
        self.seen.append(list(messages))
        turn = self.turns.pop(0)
        for piece in turn.text:
            yield AIMessageChunk(content=piece)
        for i, (name, args) in enumerate(turn.calls):
            yield AIMessageChunk(
                content="",
                tool_call_chunks=[
                    {
                        "name": name,
                        "args": json.dumps(args, ensure_ascii=False),
                        "id": f"tc_{len(self.seen)}_{i}",
                        "index": i,
                    }
                ],
            )
        yield AIMessageChunk(
            content="",
            usage_metadata={
                "input_tokens": turn.usage[0],
                "output_tokens": turn.usage[1],
                "total_tokens": sum(turn.usage),
            },
            response_metadata={"finish_reason": "stop"},
        )

    async def ainvoke(self, messages: list[Any]) -> AIMessage:
        self.seen.append(list(messages))
        return AIMessage(content=self.invoke_reply)


async def _make_sqlite_source(
    tmp_path: Path,
    *,
    name: str,
    setup_sql: str,
    profiling: bool = True,
    mask_columns: str = "",
) -> str:
    db_file = tmp_path / f"{name}_{uuid.uuid4().hex[:6]}.db"
    conn = sqlite3.connect(db_file)
    try:
        conn.executescript(setup_sql)
        conn.commit()
    finally:
        conn.close()

    options: dict[str, Any] = {CATALOG_PROFILE_OPTION: {"enabled": profiling}}
    if mask_columns:
        options[MASK_COLUMNS_OPTION] = mask_columns

    return await sqlite_source(name, db_file, options=options, schema=True)


@pytest.fixture
async def multi_source_env(tmp_path: Path):
    mes_name = f"mes_ag_{uuid.uuid4().hex[:6]}"
    erp_name = f"erp_ag_{uuid.uuid4().hex[:6]}"
    wf_id = f"wf_ag_{uuid.uuid4().hex[:6]}"

    mes_id = await _make_sqlite_source(
        tmp_path,
        name=mes_name,
        setup_sql="""
            CREATE TABLE dwd_workorder (
                line_code TEXT,
                line_name TEXT,
                operator_phone TEXT,
                good_qty INTEGER
            );
            INSERT INTO dwd_workorder VALUES
                ('L1', '一号产线', '13900000011', 100),
                ('L2', '二号产线', '13900000022', 95);
        """,
        profiling=True,
        mask_columns="operator_phone",
    )
    erp_id = await _make_sqlite_source(
        tmp_path,
        name=erp_name,
        setup_sql="""
            CREATE TABLE dwd_settlement (
                workshop_line TEXT,
                settled_qty INTEGER
            );
            INSERT INTO dwd_settlement VALUES
                ('LINE_01', 98),
                ('LINE_02', 95);
        """,
        profiling=True,
    )

    async with SessionLocal() as session:
        wf = Workflow(
            id=wf_id,
            name=f"良率核算_{wf_id}",
            version=1,
            graph={
                "nodes": [
                    {
                        "id": "in_1",
                        "type": "input",
                        "data": {"config": {"fields": [{"name": "p_line", "label": "产线编码", "type": "string"}]}},
                    },
                    {
                        "id": "card_yield",
                        "type": "metrics",
                        "data": {"config": {"metrics": [{"id": "fpy", "name": "一次良率"}]}},
                    },
                ],
                "edges": [],
            },
        )
        session.add(wf)
        await session.commit()

    created_doc_ids: list[str] = []
    try:
        yield {
            "mes_id": mes_id,
            "mes_name": mes_name,
            "erp_id": erp_id,
            "erp_name": erp_name,
            "wf_id": wf_id,
            "doc_ids": created_doc_ids,
        }
    finally:
        await forget_source(mes_id)
        await forget_source(erp_id)
        async with SessionLocal() as session:
            await session.execute(delete(CanonicalEntity))
            await session.execute(delete(EntityDiscoveryJob))
            await session.execute(delete(Workflow).where(Workflow.id == wf_id))
            if created_doc_ids:
                await session.execute(delete(Chunk).where(Chunk.document_id.in_(created_doc_ids)))
                await session.execute(delete(Document).where(Document.id.in_(created_doc_ids)))
            await session.commit()


async def test_attachment_upload_and_optional_save_to_kb(multi_source_env):
    csv_bytes = "标准编码,产线名称,MES编码,ERP编码\nL1,一号产线,L1,LINE_01\nL2,二号产线,L2,LINE_02\n".encode("utf-8")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1) 不勾选存入知识库：提取表格内容但不创建 Document
        r1 = await client.post(
            "/api/ontology/entities/attachments",
            files={"file": ("line_mapping.csv", csv_bytes, "text/csv")},
            data={"save_to_kb": "false"},
        )
        assert r1.status_code == 201, r1.text
        d1 = r1.json()
        assert d1["saved_to_kb"] is False
        assert d1["document_id"] is None
        assert d1["chunk_count"] >= 1

        # 2) 勾选存入知识库：同步创建 Document 并返回 document_id
        r2 = await client.post(
            "/api/ontology/entities/attachments",
            files={"file": ("line_mapping_kb.csv", csv_bytes, "text/csv")},
            data={"save_to_kb": "true", "collection": "default"},
        )
        assert r2.status_code == 201, r2.text
        d2 = r2.json()
        assert d2["saved_to_kb"] is True
        assert d2["document_id"]
        multi_source_env["doc_ids"].append(d2["document_id"])

    async with SessionLocal() as session:
        doc_row = await session.get(Document, d2["document_id"])
        assert doc_row is not None
        assert "LINE_01" in doc_row.content

        # 同时传 attachment_id 与对应 document_id 时自动去重为 1 份
        bundles = await entity_docs.load_doc_bundles(
            session,
            attachment_ids=[d2["attachment_id"]],
            document_ids=[d2["document_id"]],
        )
        assert len(bundles) == 1
        hits = entity_docs.search_doc_bundles(bundles, query="LINE_01")
        assert len(hits) == 1
        assert "文档《line_mapping_kb.csv》" in hits[0]["citation"]


async def test_entity_patch_guardrails_masked_columns_and_cas(multi_source_env):
    mes = multi_source_env["mes_name"]
    erp = multi_source_env["erp_name"]
    wf_id = multi_source_env["wf_id"]

    async with SessionLocal() as session:
        # 1) 校验不存在的列、不存在的参数与遮罩列枚举阻断
        bad_plan = await entity_patch.plan_entity_patch(
            session,
            [
                {"op": "upsert_entity", "entity_key": "phone_ent", "label": "作业员号码"},
                {
                    "op": "upsert_binding",
                    "entity_key": "phone_ent",
                    "source_name": mes,
                    "table_name": "dwd_workorder",
                    "column_name": "operator_phone",
                },
                {
                    "op": "upsert_member",
                    "entity_key": "phone_ent",
                    "value": "P1",
                    "label": "张三",
                    "source_codes": {mes: "13900000011"},
                },
                {
                    "op": "upsert_binding",
                    "entity_key": "phone_ent",
                    "source_name": mes,
                    "table_name": "dwd_workorder",
                    "column_name": "no_such_col",
                },
                {
                    "op": "upsert_caliber_param",
                    "entity_key": "phone_ent",
                    "workflow_id": wf_id,
                    "param_name": "wrong_param",
                },
            ],
            mode="confirmed",
        )
        assert len(bad_plan.problems) == 3
        assert any("遮罩列" in p for p in bad_plan.problems)
        assert any("no_such_col" in p for p in bad_plan.problems)
        assert any("wrong_param" in p for p in bad_plan.problems)

        # 2) mode="confirmed" 正常落库，随后测试 expected_versions CAS 乐观锁与 mode="proposed" 隔离
        res = await entity_patch.apply_entity_patch(
            session,
            [
                {
                    "op": "upsert_entity",
                    "entity_key": "prod_line_t",
                    "label": "产线",
                    "status": "proposed",  # 模型自报状态将被忽略，confirmed 模式下按 confirmed 落库
                },
                {
                    "op": "upsert_binding",
                    "entity_key": "prod_line_t",
                    "source_name": mes,
                    "table_name": "dwd_workorder",
                    "column_name": "line_code",
                    "reason": "使用者说：工单产线列是 line_code",
                },
            ],
            mode="confirmed",
            actor="builder_a",
        )
        await session.commit()
        assert res["created_entities"] == 1
        ent = res["entities"][0]
        assert ent["bindings"][0]["status"] == "confirmed"

        # 将一条 ERP 绑定预先设为 rejected，验证 mode="proposed" 绝不覆盖 confirmed 或复活 rejected
        ent_row = await entities.get_entity_by_key(session, "prod_line_t")
        assert ent_row is not None
        await entities.update_entity(
            session,
            ent_row.id,
            {
                "version": ent_row.version,
                "bindings": [
                    *ent_row.bindings,
                    {
                        "source_name": erp,
                        "table_name": "dwd_settlement",
                        "column_name": "settled_qty",
                        "status": "rejected",
                        "source": "human",
                    },
                ],
            },
        )
        await session.commit()

        # 3) mode="proposed"：尝试重提 rejected 列 + 新增 workshop_line + 自封 status="confirmed"
        prop_res = await entity_patch.apply_entity_patch(
            session,
            [
                {
                    "op": "upsert_binding",
                    "entity_key": "prod_line_t",
                    "source_name": erp,
                    "table_name": "dwd_settlement",
                    "column_name": "settled_qty",
                    "status": "confirmed",
                },
                {
                    "op": "upsert_binding",
                    "entity_key": "prod_line_t",
                    "source_name": erp,
                    "table_name": "dwd_settlement",
                    "column_name": "workshop_line",
                    "status": "confirmed",
                    "reason": "跨源目录对齐：结算产线",
                },
                {
                    "op": "upsert_caliber_param",
                    "entity_key": "prod_line_t",
                    "workflow_id": wf_id,
                    "card_id": "card_yield",
                    "param_name": "p_line",
                    "status": "confirmed",
                },
            ],
            mode="proposed",
        )
        await session.commit()
        assert prop_res["bindings_changed"] == 1
        assert prop_res["caliber_params_changed"] == 1
        updated_ent = prop_res["entities"][0]
        by_col = {b["column_name"]: b["status"] for b in updated_ent["bindings"]}
        assert by_col["line_code"] == "confirmed"
        assert by_col["settled_qty"] == "rejected"
        assert by_col["workshop_line"] == "proposed"
        assert updated_ent["caliber_params"][0]["status"] == "proposed"

        # 4) CAS 版本冲突应抛 EntityConflict
        with pytest.raises(entities.EntityConflict):
            await entity_patch.apply_entity_patch(
                session,
                [{"op": "upsert_entity", "entity_key": "prod_line_t", "label": "新产线名"}],
                mode="confirmed",
                expected_versions={"prod_line_t": 1},
            )


async def test_entity_assistant_sse_and_apply_patch(multi_source_env, monkeypatch):
    mes = multi_source_env["mes_name"]
    erp = multi_source_env["erp_name"]
    wf_id = multi_source_env["wf_id"]

    scripted = _ScriptedModel(
        turns=[
            _Turn(
                calls=[
                    ("inspect_sources", {"keyword": "line"}),
                    ("read_document", {"query": "LINE_01"}),
                    ("run_query", {"source_name": mes, "sql": "SELECT line_code, operator_phone FROM dwd_workorder"}),
                ]
            ),
            _Turn(
                calls=[
                    (
                        "propose_entity_changes",
                        {
                            "operations": [
                                {
                                    "op": "upsert_entity",
                                    "entity_key": "workshop_line_ent",
                                    "label": "车间产线",
                                    "aliases": ["产线"],
                                    "reason": "使用者说：建立车间产线标准实体",
                                },
                                {
                                    "op": "upsert_binding",
                                    "entity_key": "workshop_line_ent",
                                    "source_name": mes,
                                    "table_name": "dwd_workorder",
                                    "column_name": "line_code",
                                    "reason": "数据探查显示：工单表产线列为 line_code",
                                },
                                {
                                    "op": "upsert_binding",
                                    "entity_key": "workshop_line_ent",
                                    "source_name": erp,
                                    "table_name": "dwd_settlement",
                                    "column_name": "workshop_line",
                                    "reason": "文档《对照表.md》写着：ERP 结算表对应 workshop_line",
                                },
                                {
                                    "op": "upsert_member",
                                    "entity_key": "workshop_line_ent",
                                    "value": "L1",
                                    "label": "一号产线",
                                    "source_codes": {mes: "L1", erp: "LINE_01"},
                                    "reason": "文档《对照表.md》写着：L1 对应 LINE_01",
                                },
                                {
                                    "op": "upsert_caliber_param",
                                    "entity_key": "workshop_line_ent",
                                    "workflow_id": wf_id,
                                    "card_id": "card_yield",
                                    "param_name": "p_line",
                                    "reason": "口径入口对齐",
                                },
                            ]
                        },
                    )
                ]
            ),
            _Turn(text=["已核对实表与对照文档，生成车间产线标准实体及跨源编码提案，请确认保存。"]),
        ]
    )

    async def _fake_resolve(_session):
        return scripted, "mock-draft-model"

    monkeypatch.setattr(entity_assistant, "resolve_model", _fake_resolve)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        att_res = await client.post(
            "/api/ontology/entities/attachments",
            files={"file": ("对照表.md", "一号产线：MES 为 L1，ERP dwd_settlement.workshop_line 为 LINE_01".encode("utf-8"), "text/markdown")},
            data={"save_to_kb": "false"},
        )
        assert att_res.status_code == 201
        att_id = att_res.json()["attachment_id"]

        sse_res = await client.post(
            "/api/ontology/entities/assistant",
            json={
                "messages": [{"role": "user", "content": "请根据对照表帮我关联车间产线实体"}],
                "attachment_ids": [att_id],
            },
        )
        assert sse_res.status_code == 200
        events = [
            json.loads(line[5:].strip())
            for line in sse_res.text.splitlines()
            if line.startswith("data:")
        ]
        types = [ev["type"] for ev in events]
        assert "query" in types
        assert "proposal" in types
        assert types[-1] == "done"

        # 确认只读查询中的敏感列已被遮罩为「已遮罩」
        q_ev = next(ev for ev in events if ev["type"] == "query")
        assert q_ev["source"] == mes
        assert all(row[1] == "已遮罩" for row in q_ev["rows"])

        # 提案尚未落库
        list_before = await client.get("/api/ontology/entities")
        assert all(e["key"] != "workshop_line_ent" for e in list_before.json()["items"])

        # 人工确认并应用提案
        prop_ev = next(ev for ev in events if ev["type"] == "proposal")
        ops = [c["op_payload"] for c in prop_ev["changes"]]
        apply_res = await client.post(
            "/api/ontology/entities/apply-patch",
            json={"operations": ops, "expected_versions": prop_ev["expected_versions"]},
            headers={"X-Actor": "builder_one"},
        )
        assert apply_res.status_code == 200, apply_res.text
        applied = apply_res.json()
        assert applied["created_entities"] == 1
        saved = applied["entities"][0]
        assert saved["key"] == "workshop_line_ent"
        assert all(b["status"] == "confirmed" for b in saved["bindings"])
        assert saved["members"][0]["source_codes"] == {mes: "L1", erp: "LINE_01"}


async def test_entity_discovery_job_lifecycle_and_proposed_isolation(multi_source_env, monkeypatch):
    mes = multi_source_env["mes_name"]
    erp = multi_source_env["erp_name"]

    discovery_json = json.dumps(
        {
            "operations": [
                {
                    "op": "upsert_entity",
                    "entity_key": "discovered_line",
                    "label": "自动发现产线",
                    "description": "跨 MES 与 ERP 的产线对象",
                    "aliases": ["产线"],
                    "reason": "跨源目录对齐：工单与结算产线",
                },
                {
                    "op": "upsert_binding",
                    "entity_key": "discovered_line",
                    "entity_label": "自动发现产线",
                    "source_name": mes,
                    "table_name": "dwd_workorder",
                    "column_name": "line_code",
                    "status": "confirmed",  # 后台发现必须强制降为 proposed
                    "reason": "跨源目录对齐：MES 工单产线",
                },
                {
                    "op": "upsert_binding",
                    "entity_key": "discovered_line",
                    "entity_label": "自动发现产线",
                    "source_name": erp,
                    "table_name": "dwd_settlement",
                    "column_name": "workshop_line",
                    "reason": "跨源目录对齐：ERP 结算产线",
                },
                {
                    "op": "upsert_member",
                    "entity_key": "discovered_line",
                    "entity_label": "自动发现产线",
                    "value": "L1",
                    "label": "一号产线",
                    "source_codes": {mes: "L1", erp: "LINE_01"},
                    "reason": "文档对照",
                },
            ]
        },
        ensure_ascii=False,
    )
    fake_model = _ScriptedModel(invoke_reply=f"```json\n{discovery_json}\n```")

    async def _fake_resolve(_session):
        return fake_model, "mock-discovery-model"

    monkeypatch.setattr(catalog_assistant, "resolve_model", _fake_resolve)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post(
            "/api/ontology/discovery-jobs",
            json={
                "source_ids": [multi_source_env["mes_id"], multi_source_env["erp_id"]],
                "note": "重点扫描产线",
            },
            headers={"X-Actor": "builder_scan"},
        )
        assert r.status_code == 202, r.text
        job_id = r.json()["id"]

        # 轮询等待完成
        final_job = None
        for _ in range(40):
            jr = await client.get(f"/api/ontology/discovery-jobs/{job_id}")
            assert jr.status_code == 200
            body = jr.json()
            if body["status"] in ("succeeded", "failed", "cancelled"):
                final_job = body
                break
            await asyncio.sleep(0.05)

        assert final_job is not None
        assert final_job["status"] == "succeeded", final_job
        assert final_job["summary"]["created_entities"] == 1
        assert final_job["summary"]["proposed_bindings"] == 2
        assert final_job["summary"]["added_members"] == 1

        ents = (await client.get("/api/ontology/entities")).json()["items"]
        disc = next(e for e in ents if e["key"] == "discovered_line")
        assert all(b["status"] == "proposed" and b["source"] == "ai" for b in disc["bindings"])

        # 运行期 confirmed_bindings 必须为 0（状态门禁隔离）
        assert entities.confirmed_bindings(disc) == []


async def test_entity_discovery_job_busy_and_cancel(multi_source_env, monkeypatch):
    entered = asyncio.Event()

    class _SlowModel:
        async def ainvoke(self, _messages):
            entered.set()
            await asyncio.sleep(30)
            return AIMessage(content='{"operations": []}')

    async def _fake_resolve(_session):
        return _SlowModel(), "mock-slow-model"

    monkeypatch.setattr(catalog_assistant, "resolve_model", _fake_resolve)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r1 = await client.post(
            "/api/ontology/discovery-jobs",
            json={"source_ids": [multi_source_env["mes_id"]]},
        )
        assert r1.status_code == 202
        job_id = r1.json()["id"]

        await asyncio.wait_for(entered.wait(), timeout=3.0)

        # 并发再次发起应返回 409 discovery_job_busy
        r2 = await client.post(
            "/api/ontology/discovery-jobs",
            json={"source_ids": [multi_source_env["mes_id"]]},
        )
        assert r2.status_code == 409
        assert r2.json().get("code") == "discovery_job_busy"

        # 主动停止扫描
        rc = await client.post(f"/api/ontology/discovery-jobs/{job_id}/cancel")
        assert rc.status_code == 200
        assert rc.json()["status"] == "cancelled"


async def test_entity_assistant_source_scope_isolates_unrelated_domains(multi_source_env):
    """进入单个数据源目录页时，传入 source_ids 仅暴露所选数据源的表、查询权限与关联工作流，隔离其他业务域。"""
    mes_id = multi_source_env["mes_id"]
    mes_name = multi_source_env["mes_name"]
    erp_id = multi_source_env["erp_id"]
    erp_name = multi_source_env["erp_name"]
    wf_mes_id = f"wf_mes_only_{uuid.uuid4().hex[:6]}"
    wf_erp_id = f"wf_erp_only_{uuid.uuid4().hex[:6]}"

    async with SessionLocal() as session:
        # 构造一个仅归属 MES 域的实体与两个分别绑定不同数据源工具的工作流
        await entities.create_entity(
            session,
            {
                "key": "mes_only_line",
                "label": "车间专属产线",
                "bindings": [
                    {
                        "source_id": mes_id,
                        "source_name": mes_name,
                        "table_name": "dwd_workorder",
                        "column_name": "line_code",
                        "status": "confirmed",
                        "source": "human",
                    }
                ],
            },
            actor="tester",
        )
        session.add_all(
            [
                Workflow(
                    id=wf_mes_id,
                    name="车间工单良率",
                    version=1,
                    graph={
                        "nodes": [
                            {
                                "id": "in_m",
                                "type": "input",
                                "data": {"config": {"fields": [{"name": "workshop_code", "label": "产线", "type": "string"}]}},
                            },
                            {
                                "id": "q_m",
                                "type": "tool",
                                "data": {"config": {"tool": f"db_query__{mes_name}", "args": {"sql": "SELECT 1"}}},
                            },
                            {
                                "id": "c_m",
                                "type": "metrics",
                                "data": {"config": {"metrics": [{"id": "m1", "name": "良率"}]}},
                            },
                        ],
                        "edges": [],
                    },
                ),
                Workflow(
                    id=wf_erp_id,
                    name="结算金额口径",
                    version=1,
                    graph={
                        "nodes": [
                            {
                                "id": "in_e",
                                "type": "input",
                                "data": {"config": {"fields": [{"name": "store_id", "label": "门店", "type": "string"}]}},
                            },
                            {
                                "id": "q_e",
                                "type": "tool",
                                "data": {"config": {"tool": f"db_query__{erp_name}", "args": {"sql": "SELECT 1"}}},
                            },
                            {
                                "id": "c_e",
                                "type": "metrics",
                                "data": {"config": {"metrics": [{"id": "e1", "name": "结算量"}]}},
                            },
                        ],
                        "edges": [],
                    },
                ),
            ]
        )
        await session.commit()

    try:
        async with SessionLocal() as session:
            ctx = await entity_assistant.prepare(session, source_ids=[erp_id])

        # 1. 只加载 erp_name，不包含 mes_name
        assert list(ctx.sources.keys()) == [erp_name.lower()]
        assert ctx.queryable_sources == [erp_name]

        # 2. 系统提示词不泄露 mes_name 的物理表名 dwd_workorder
        assert "dwd_workorder" not in ctx.system
        assert erp_name in ctx.system

        # 3. inspect_sources 只返回 erp 的表与 erp 关联的口径工作流，不包含车间工单良率工作流
        tools = entity_assistant._EntityTools(ctx)
        inspected = tools.inspect("", "")
        assert "dwd_settlement" in inspected
        assert "dwd_workorder" not in inspected
        assert wf_erp_id in inspected
        assert wf_mes_id not in inspected

        # 4. 对未纳入范围的 mes_name 发起只读查询会被立即拒绝，不尝试连接该库
        ev, msg = await tools.query(mes_name, "SELECT * FROM dwd_workorder LIMIT 5")
        assert ev is None
        assert erp_name in msg
    finally:
        async with SessionLocal() as session:
            await session.execute(delete(Workflow).where(Workflow.id.in_([wf_mes_id, wf_erp_id])))
            await session.commit()


async def test_entity_discovery_streaming_progress_and_summary(multi_source_env, monkeypatch):
    """扫描发现任务支持 astream 流式输出并在 summary 中记录量化扫描统计与新增实体清单。"""
    mes_id = multi_source_env["mes_id"]
    mes_name = multi_source_env["mes_name"]
    stream_payload = json.dumps(
        {
            "operations": [
                {
                    "op": "upsert_entity",
                    "entity_key": "stream_line",
                    "label": "流式识别产线",
                    "reason": "目录语义对齐：产线编码",
                },
                {
                    "op": "upsert_binding",
                    "entity_key": "stream_line",
                    "entity_label": "流式识别产线",
                    "source_name": mes_name,
                    "table_name": "dwd_workorder",
                    "column_name": "line_code",
                    "reason": "目录语义对齐：产线字段",
                },
            ]
        },
        ensure_ascii=False,
    )

    class _StreamingDiscoveryModel:
        async def astream(self, _messages):
            mid = len(stream_payload) // 2
            yield AIMessageChunk(content=stream_payload[:mid])
            await asyncio.sleep(0.02)
            yield AIMessageChunk(content=stream_payload[mid:])

    async def _fake_resolve(_session):
        return _StreamingDiscoveryModel(), "mock-stream-model"

    monkeypatch.setattr(catalog_assistant, "resolve_model", _fake_resolve)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post(
            "/api/ontology/discovery-jobs",
            json={"source_ids": [mes_id]},
        )
        assert r.status_code == 202
        job_id = r.json()["id"]

        final_job = None
        for _ in range(40):
            jr = await client.get(f"/api/ontology/discovery-jobs/{job_id}")
            body = jr.json()
            if body["status"] in ("succeeded", "failed", "cancelled"):
                final_job = body
                break
            await asyncio.sleep(0.05)

        assert final_job is not None
        assert final_job["status"] == "succeeded", final_job
        assert isinstance(final_job["started_at"], str) and (
            final_job["started_at"].endswith("+00:00") or final_job["started_at"].endswith("Z")
        )
        assert isinstance(final_job["finished_at"], str) and (
            final_job["finished_at"].endswith("+00:00") or final_job["finished_at"].endswith("Z")
        )
        summary = final_job["summary"]
        assert summary["progress_pct"] == 100
        assert summary["sources_scanned"] == 1
        assert summary["source_names"] == [mes_name]
        assert summary["tables_scanned"] >= 1
        assert summary["ops_detected"] == 2
        assert any(t["key"] == "stream_line" for t in summary["touched_entities"])



