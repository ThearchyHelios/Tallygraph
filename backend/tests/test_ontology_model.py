"""Wave 1-a 单元测试：标准实体 CanonicalEntity、指标本体 links、DAG 无环门禁、指纹/发布清单后向兼容与 /api/ontology 接口。"""
from __future__ import annotations

from datetime import date

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete

from app.api.workflows import commit_publish
from app.apps import publications
from app.apps.validation import gate_issues
from app.core import artifact_store
from app.data import caliber_identity
from app.data.entities import (
    EntityConflict,
    EntityInvalid,
    confirmed_bindings,
    create_entity,
    delete_entity,
    entities_bridging_sources,
    entity_to_dict,
    resolve_caliber_param,
    review_entity_item,
    translate_entity_value,
    update_entity,
)
from app.db.base import SessionLocal
from app.db.models import Workflow, WorkflowPublication, WorkflowVersion
from app.db.models_ontology import CanonicalEntity
from app.ledger.docdraft import normalize_metrics
from app.ledger.ontology import (
    ONTOLOGY_CYCLE_CODE,
    OntologyCycleError,
    active_links,
    build_published_ontology_graph,
    card_metric_lineage,
    check_factor_dag,
    find_factor_cycles,
    ontology_manifest_section,
)
from app.main import app
from tests.fixtures.acceptance import multi_source_ontology as mso


@pytest.fixture(autouse=True)
async def _clean_entities():
    yield
    async with SessionLocal() as session:
        await session.execute(delete(CanonicalEntity))
        await session.commit()


async def test_canonical_entity_crud_optimistic_lock_and_proposed_gating():
    async with SessionLocal() as session:
        row = await create_entity(
            session,
            {
                "key": "work_line_test",
                "label": "示范产线",
                "aliases": ["产线编码", "工位线"],
                "bindings": [
                    {
                        "source_name": "ops_shift",
                        "table_name": "shift_output",
                        "column_name": "line_code",
                        "status": "confirmed",
                    },
                    {
                        "source_name": "equip_master",
                        "table_name": "line_specs",
                        "column_name": "equip_line_id",
                        "status": "confirmed",
                    },
                    {
                        "source_name": "quality_scrap",
                        "table_name": "scrap_ledger",
                        "column_name": "workcenter_code",
                        "source": "ai",
                    },
                ],
                "caliber_params": [
                    {"workflow_id": "wf_eq", "param_name": "equip_line", "status": "confirmed"},
                    {"workflow_id": "wf_q", "param_name": "wc_code", "status": "proposed"},
                ],
                "members": [
                    {
                        "value": "L1",
                        "label": "星梭一线",
                        "aliases": ["一线"],
                        "source_codes": {"ops_shift": "L1", "equip_master": "EQ-L1", "quality_scrap": "WC-01"},
                    }
                ],
            },
            actor="builder_a",
        )
        await session.commit()
        entity_id = row.id
        assert row.version == 1

        # AI 来源未显式指定 status 时自动落为 proposed，不进 confirmed_bindings
        active = confirmed_bindings(row)
        assert {b["source_name"] for b in active} == {"ops_shift", "equip_master"}

        # 确定性跨源值翻译：精确命中 L1 -> EQ-L1，子串或未知编码返回 None（绝不模糊猜测）
        translated = translate_entity_value(row, "L1", from_source="ops_shift", to_source="equip_master")
        assert translated == {"canonical": "L1", "label": "星梭一线", "target_value": "EQ-L1"}
        assert translate_entity_value(row, "L10", from_source="ops_shift", to_source="equip_master") is None

        # 口径参数解析：confirmed 可见，proposed 被拦下
        assert resolve_caliber_param(row, workflow_id="wf_eq") == "equip_line"
        assert resolve_caliber_param(row, workflow_id="wf_q") is None

        # 跨源桥接：只列出 confirmed 绑定的对侧数据源
        bridges = entities_bridging_sources([row], source_name="ops_shift", tables=["shift_output"])
        assert len(bridges) == 1 and bridges[0]["peer_sources"] == ["equip_master"]

        # 受限视角（restricted=True）：隐去物理绑定、物理表列名与底层 source_codes
        restricted_view = entity_to_dict(row, restricted=True)
        assert "bindings" not in restricted_view
        assert "shift_output" not in str(restricted_view) and "line_code" not in str(restricted_view)
        for m in restricted_view["members"]:
            assert "source_codes" not in m

        # 审阅确认 AI 候选绑定后，quality_scrap 进入生效绑定，版本号自增
        proposed_item = next(b for b in (row.bindings or []) if b["source_name"] == "quality_scrap")
        updated = await review_entity_item(
            session,
            entity_id,
            target_kind="binding",
            item_id=proposed_item["id"],
            status="confirmed",
            actor="reviewer_b",
            expected_version=1,
        )
        await session.commit()
        assert updated.version == 2
        assert {b["source_name"] for b in confirmed_bindings(updated)} == {
            "ops_shift",
            "equip_master",
            "quality_scrap",
        }

        # 乐观锁冲突：携带旧版本号 v1 更新或审阅必须抛 EntityConflict
        with pytest.raises(EntityConflict):
            await update_entity(session, entity_id, {"label": "过期写入", "version": 1}, actor="builder_a")

        # 唯一键冲突与非法 key 校验
        with pytest.raises(EntityConflict):
            await create_entity(session, {"key": "work_line_test", "label": "重复"})
        with pytest.raises(EntityInvalid):
            await create_entity(session, {"key": "Bad-Key!", "label": "非法"})

        assert await delete_entity(session, entity_id) is True
        await session.commit()


def test_factor_cycle_detection_and_docdraft_proposed_links():
    # 无环 factor 链放行
    check_factor_dag([("wf1:c1:oee", "wf1:c1:avail"), ("wf1:c1:avail", "wf2:c2:run_time")])

    # 二跳环与三跳环均精准检出
    cycles_2 = find_factor_cycles([("a", "b"), ("b", "a")])
    assert len(cycles_2) == 1
    with pytest.raises(OntologyCycleError) as exc_info:
        check_factor_dag([("m1", "m2"), ("m2", "m3"), ("m3", "m1")])
    assert exc_info.value.code == ONTOLOGY_CYCLE_CODE

    # 文档起草（docdraft）即使模型在草稿里写了 status="confirmed"，落库前一律强制降为 proposed 且带 source="docdraft"
    draft_metrics, dropped = normalize_metrics(
        [
            {
                "id": "load_rate",
                "name": "产线负荷率",
                "unit": "%",
                "citations": [{"chunk": "c0", "quote": "负荷率等于实际每小时产量除以额定每小时产量"}],
                "links": [
                    {
                        "kind": "factor",
                        "target": {"workflow_id": "wf_eq", "card_id": "m_eq", "metric_id": "rated_qty"},
                        "role": "denominator",
                        "status": "confirmed",
                    }
                ],
            }
        ],
        chunks={"c0": {"content": "负荷率等于实际每小时产量除以额定每小时产量。", "document_id": "doc_1"}},
    )
    assert dropped == []
    assert len(draft_metrics) == 1
    links = draft_metrics[0]["links"]
    assert len(links) == 1
    assert links[0]["status"] == "proposed" and links[0]["source"] == "docdraft"
    assert active_links(links) == []


async def test_definition_hash_and_publication_manifest_backward_compatibility(tmp_path, scratch_source):
    await scratch_source("demo_ops", mso.build(tmp_path).ops_path)

    base_metric = {
        "id": "good_qty",
        "name": "合格产量",
        "expression": "{{ nodes.q1.rows[0].good_qty }}",
        "unit": "件",
    }
    h_plain = caliber_identity.definition_hash("日产率", "", [base_metric])
    h_empty = caliber_identity.definition_hash("日产率", "", [{**base_metric, "links": []}])
    assert h_plain == h_empty

    with_link = {
        **base_metric,
        "links": [
            {
                "kind": "upstream_driver",
                "target": {"workflow_id": "wf_up", "card_id": "c_up", "metric_id": "down_min"},
                "status": "confirmed",
            }
        ],
    }
    assert caliber_identity.definition_hash("日产率", "", [with_link]) != h_plain

    def make_graph(metrics_list):
        return {
            "nodes": [
                {"id": "in1", "type": "input", "position": {"x": 0, "y": 0}, "data": {"label": "入口", "config": {}}},
                {
                    "id": "q1",
                    "type": "tool",
                    "position": {"x": 200, "y": 0},
                    "data": {
                        "label": "取数",
                        "config": {
                            "tool": "db_query__demo_ops",
                            "args": {"sql": "SELECT good_qty FROM shift_output WHERE operator_phone <> 'SECRET-PHONE'"},
                        },
                    },
                },
                {
                    "id": "m1",
                    "type": "metrics",
                    "position": {"x": 420, "y": 0},
                    "data": {"label": "指标卡", "config": {"caliber": "日产率", "metrics": metrics_list}},
                },
                {"id": "out1", "type": "output", "position": {"x": 640, "y": 0}, "data": {"label": "出口", "config": {}}},
            ],
            "edges": [
                {"source": "in1", "target": "q1"},
                {"source": "q1", "target": "m1"},
                {"source": "m1", "target": "out1"},
            ],
        }

    # 1) 无 links 时：ontology_manifest_section 为 None，发布清单完全不含 "ontology" 键（存量哈希零变动）
    plain_graph = make_graph([base_metric])
    assert ontology_manifest_section(workflow_id="wf_plain", version=1, graph=plain_graph) is None

    # 自动反向血缘只记 (source, tables)，绝不泄漏 SQL 字面量里的敏感字符串
    lineage = card_metric_lineage(plain_graph, "m1")
    assert lineage["tables_by_source"] == {"demo_ops": ["shift_output"]}
    assert "SECRET-PHONE" not in str(lineage)

    async with SessionLocal() as session:
        wf1 = Workflow(name="无本体边工作流测试", description="", graph=plain_graph, version=1)
        session.add(wf1)
        await session.flush()
        snap1 = WorkflowVersion(workflow_id=wf1.id, version=1, graph=plain_graph, note="v1")
        session.add(snap1)
        await session.commit()
        await session.refresh(wf1)
        await session.refresh(snap1)
        pub1 = await commit_publish(session, wf1, snap1, "published", "tester")
        manifest1 = artifact_store.load(pub1["manifest"])
        assert "ontology" not in manifest1

        # 2) 含自环 factor 边的工作流在发布预检 gate_issues 阶段被直接拦下
        cyclic_graph = make_graph(
            [
                {
                    **base_metric,
                    "links": [
                        {
                            "kind": "factor",
                            "target": {"workflow_id": wf1.id, "card_id": "m1", "metric_id": "good_qty"},
                            "status": "confirmed",
                        }
                    ],
                }
            ]
        )
        issues = await gate_issues(session, cyclic_graph, "published", workflow_id=wf1.id)
        assert issues is not None
        assert any(i["level"] == "error" and i.get("code") == ONTOLOGY_CYCLE_CODE for i in issues)

        # 3) 合法本体边发布后物化进清单，且 publications.rebuild 重建结果逐字节一致
        linked_graph = make_graph([with_link])
        wf2 = Workflow(name="含本体边工作流测试", description="", graph=linked_graph, version=1)
        session.add(wf2)
        await session.flush()
        snap2 = WorkflowVersion(workflow_id=wf2.id, version=1, graph=linked_graph, note="v1")
        session.add(snap2)
        await session.commit()
        await session.refresh(wf2)
        await session.refresh(snap2)
        pub2 = await commit_publish(session, wf2, snap2, "published", "tester")
        manifest2 = artifact_store.load(pub2["manifest"])
        assert "ontology" in manifest2
        assert "SECRET-PHONE" not in str(manifest2["ontology"])
        pub_row = await session.get(WorkflowPublication, pub2["publication"]["id"])
        assert pub_row is not None
        rebuilt = await publications.rebuild(session, pub_row)
        assert rebuilt["manifest"] == pub2["manifest"]
        assert rebuilt["matches"] is True


async def test_ontology_http_endpoints_and_published_graph(tmp_path, scratch_source):
    seeded = await mso.seed_ontology_fixture(tmp_path, scratch_source, today=date(2026, 10, 22))
    ops_wf = seeded["workflow_ids"]["ops"]

    async with SessionLocal() as session:
        active_graph = await build_published_ontology_graph(session, include_proposed=False)
        all_graph = await build_published_ontology_graph(session, include_proposed=True)
        # ops 工作流里有 3 条 confirmed 边 + 1 条 proposed 边
        ops_active = [e for e in active_graph["edges"] if e["from"]["workflow_id"] == ops_wf]
        ops_all = [e for e in all_graph["edges"] if e["from"]["workflow_id"] == ops_wf]
        assert len(ops_active) == 3
        assert len(ops_all) == 4

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res_list = await client.get("/api/ontology/entities", params={"source_name": "ops_shift"})
        assert res_list.status_code == 200
        keys = [e["key"] for e in res_list.json()["items"]]
        assert "production_line" in keys

        res_graph = await client.get("/api/ontology/graph")
        assert res_graph.status_code == 200
        payload = res_graph.json()
        assert any(e["key"] == "production_line" for e in payload["entities"])
        assert any(edge["kind"] == "factor" for edge in payload["edges"])

        res_wf = await client.get(f"/api/ontology/workflows/{ops_wf}")
        assert res_wf.status_code == 200
        wf_index = res_wf.json()
        assert wf_index["workflow_id"] == ops_wf
        assert wf_index["cards"][0]["tables_by_source"] == {"ops_shift": ["shift_output"]}

        # 乐观锁 409 验证
        entity_id = seeded["entity_id"]
        res_conflict = await client.patch(
            f"/api/ontology/entities/{entity_id}",
            json={"label": "冲突测试", "version": 999},
        )
        assert res_conflict.status_code == 409
