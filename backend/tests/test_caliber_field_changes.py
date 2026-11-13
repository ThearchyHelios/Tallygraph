"""K03 字段变更闭环：按稳定 metric_id + 字段路径改一处，整批原子、图是原图的深拷贝、回执不吹验证。

- 打补丁打在**原图的深拷贝**上：没提到的字段（definition 的 citations/owner、contract、扩展字段）、别的指标、
  别人加的图级扩展字段和其他节点的配置都原样保留（E02）；
- 白名单：真有的业务字段和 definition 的展示字段能改，id / citations / 负责人 / 生效日期 / 指标类型 / 契约 /
  任意路径 / 按数组位置定位一律拒，且各自有一句具体的话（不是笼统的「不认识」）；
- 省略、set 空串（数组是 []）、set null、remove 四种写法语义不同；数组字段整份设置；
- 一批要么全成、要么全不成：一条不合规就一条都不落地，也不新增版本；整批最后没改动内容（先设后删）同样不落版本；
- 回执带变更分类、影响指标、定义指纹前后值、失效说明和绑定状态——没有绑定已对齐、也没有验证通过这种话；
- 同一语义变化经画布（整图保存）和经 operations 得到同一张执行图，旧试跑都判过期（E08）；
- K02 的规矩一条不少：要带 expected_revision、旧写 409 stale_revision、同一 request_id 幂等（指纹包含 operations）、
  同键不同内容 409 idempotency_conflict、并发同键只落一版；整份换指标那条老路照旧（回执不带 change 段）。

模型一律注入假模型（test_ledger_docdraft_api.fake_model），试跑换成写一行 succeeded 的运行（fake_trial），不发真实请求。
"""
from __future__ import annotations

import asyncio
import copy
import json
import uuid

import pytest
from sqlalchemy import select

from app.api import ledger as api_ledger
from app.core.artifact_store import execution_hash, graph_hash
from app.db.base import SessionLocal
from app.db.models import CaliberDraft, CaliberWriteRequest, Workflow, WorkflowVersion
from app.ledger import changes
from app.ledger.repository import CaliberGone, GraphBackedCaliberRepository, MetricGone
from tests.test_caliber_write_guard import (  # noqa: F401 - 这几个 fixture / 助手和 K02 的用例共用
    _graph_of, _versions, cfg, client, datasource, documents, fake_trial, ready_draft,
)


def patch_url(draft_id: str) -> str:
    return f"/api/ledger/drafts/{draft_id}"


def ops_set(metric_id: str, path: str, value) -> dict:
    return {"op": "set", "metric_id": metric_id, "path": path, "value": value}


def ops_remove(metric_id: str, path: str) -> dict:
    return {"op": "remove", "metric_id": metric_id, "path": path}


async def change(client, body: dict, operations: list[dict], *, request_id: str | None = None,
                 expected_revision: int | None = None, **extra):
    """按字段改一次（默认带上读草稿时给的基准版本）。"""
    payload = {"operations": operations,
               "expected_revision": body["revision"] if expected_revision is None else expected_revision, **extra}
    if request_id is not None:
        payload["request_id"] = request_id
    return await client.patch(patch_url(body["id"]), json=payload)


def metric_of(graph: dict, metric_id: str = "m1", card: str = "card") -> dict:
    metrics = cfg(graph, card)["metrics"]
    return next(metric for metric in metrics if metric["id"] == metric_id)


async def bare_draft(client, metrics: list[dict]) -> dict:
    """一份手建的已接管草稿（docattach 那种：tags 有 doc_draft、origin 里没有 kind），图上只有一张口径卡。

    起草出来的指标总是带整份 definition，画布上却什么形状都可能：边角形状（没有 definition、definition 是
    null、被人写成字符串）走它来试。
    """
    workflow_id = f"wf-{uuid.uuid4().hex[:12]}"
    draft_id = uuid.uuid4().hex[:32]
    graph = {"nodes": [{"id": "card", "type": "metrics", "position": {"x": 0, "y": 0},
                        "data": {"label": "口径卡", "config": {"metrics": copy.deepcopy(metrics)}}}], "edges": []}
    async with SessionLocal() as session:
        session.add(Workflow(id=workflow_id, name=f"{OWN_PREFIX}手建 {uuid.uuid4().hex[:6]}", status="draft",
                             graph=graph, version=1, tags=["doc_draft"],
                             origin={"run_id": "run-1", "sources": [{"run_id": "run-1"}]}))
        session.add(WorkflowVersion(workflow_id=workflow_id, version=1, graph=graph, graph_hash=graph_hash(graph)))
        await session.flush()          # 草稿的外键指着工作流：先落工作流那一行
        session.add(CaliberDraft(id=draft_id, title="手建口径", status="ready", workflow_id=workflow_id,
                                 payload={"metrics": copy.deepcopy(metrics), "version": 1,
                                          "workflow_id": workflow_id}))
        await session.commit()
    return {"id": draft_id, "workflow_id": workflow_id, "revision": 1, "card_id": "card"}


#: 这个文件自己建的工作流（连同版本、幂等记录、草稿）跑完删掉，不给后面的用例留行
OWN_PREFIX = "K03 "


@pytest.fixture(autouse=True)
async def cleanup_own_rows():
    yield
    async with SessionLocal() as session:
        workflow_ids = list((await session.execute(
            select(Workflow.id).where(Workflow.name.like(f"{OWN_PREFIX}%")))).scalars())
        if workflow_ids:
            for model in (CaliberWriteRequest, WorkflowVersion, CaliberDraft):
                for row in (await session.execute(
                        select(model).where(model.workflow_id.in_(workflow_ids)))).scalars():
                    await session.delete(row)
            for workflow in (await session.execute(
                    select(Workflow).where(Workflow.id.in_(workflow_ids)))).scalars():
                await session.delete(workflow)
        await session.commit()


# ==========================================================================
# 原图上的字段补丁：改一处，别的都留着
# ==========================================================================

async def test_a_field_change_lands_on_the_graph_and_leaves_everything_else_alone(
        client, documents, datasource, monkeypatch, fake_trial):
    """改时间字段不动 citations / contract / 口径说明 / 扩展字段，也不动其他节点和其他指标（K03 的反例）。"""
    body = await ready_draft(client, documents, datasource, monkeypatch)
    wid = body["workflow_id"]
    # 图上先有别人加的扩展字段和自定义指标键（前端不认识的那些），还有第二条指标和第二条边
    graph = await _graph_of(wid)
    graph["extensions"] = {"unknown": True}
    card_metrics = cfg(graph, "card")["metrics"]
    card_metrics[0]["x_custom"] = {"keep": [1, 2]}
    card_metrics.append({"id": "m2", "name": "订单数", "expression": "cell(nodes.fetch, 0, 'orders')",
                         "contract": {"shape": "scalar"}})
    saved = await client.patch(f"/api/workflows/{wid}", json={"graph": graph, "expected_revision": body["revision"]})
    assert saved.status_code == 200, saved.text
    before = await _graph_of(wid)
    before_m1 = copy.deepcopy(metric_of(before))

    res = await change(client, {**body, "revision": saved.json()["version"]},
                       [ops_set("m1", "/time_field", "paid_at"),
                        ops_set("m1", "/definition/exclusions", ["全额退款"]),
                        ops_set("m1", "/grain", "order")])
    assert res.status_code == 200, res.text
    made = res.json()
    assert made["version"] == saved.json()["version"] + 1
    assert await _versions(wid) == [1, 2, 3]

    after = await _graph_of(wid)
    changed = metric_of(after)
    assert changed["time_field"] == "paid_at" and changed["grain"] == "order"
    assert changed["definition"]["exclusions"] == ["全额退款"]
    # 没碰的：口径说明、负责人、引用原文、取值契约、表达式、小数位、自定义键原样
    assert changed["definition"]["text"] == before_m1["definition"]["text"]
    assert changed["definition"]["owner"] == before_m1["definition"]["owner"]
    assert changed["definition"]["citations"] == before_m1["definition"]["citations"]
    assert changed["contract"] == before_m1["contract"]
    assert changed["expression"] == before_m1["expression"]
    assert changed["decimals"] == before_m1["decimals"]
    assert changed["x_custom"] == {"keep": [1, 2]}
    # 别的指标、别的节点、图级扩展字段都没动
    assert metric_of(after, "m2") == metric_of(before, "m2")
    for node_id in ("start", "fetch", "write", "out"):
        assert cfg(after, node_id) == cfg(before, node_id)
    assert after["extensions"] == {"unknown": True} and after["edges"] == before["edges"]

    # 草稿页从图读：新定义立刻可见，旧试跑按执行指纹判过期
    view = (await client.get(patch_url(body["id"]))).json()
    assert view["metrics"][0]["time_field"] == "paid_at"
    assert view["trial"]["status"] == "stale"

    # 回执：变更分类、影响指标、定义指纹前后值、失效说明、绑定状态（没声明业务字段的旧资产：unbound，不装对齐）
    block = made["change"]
    assert block["applied"] is True and block["classification"] == "definition"
    assert block["metrics"] == ["m1"] and block["card"] == "card" and block["revision"] == made["version"]
    assert [op["path"] for op in block["operations"]] == ["/time_field", "/definition/exclusions", "/grain"]
    assert block["definition_hash"]["changed"] is True
    assert block["definition_hash"]["before"] != block["definition_hash"]["after"]
    assert block["validation"]["invalidated"] is True and block["validation"]["scope"] == "card"
    assert block["validation"]["metrics"] == ["m1"] and "重新试运行" in block["validation"]["next"]
    assert block["binding"]["status"] == "unbound" and block["binding"]["checked"] is True
    assert block["binding"]["synced"] is False and block["binding"]["fix"] == "none"
    assert "aligned" not in json.dumps(block, ensure_ascii=False)


async def test_a_batch_can_touch_several_metrics(client, documents, datasource, monkeypatch, fake_trial):
    """一批操作可以落在不同指标上：影响摘要按卡上的先后列出每一条动过的指标。"""
    body = await ready_draft(client, documents, datasource, monkeypatch)
    second = {**copy.deepcopy(body["metrics"][0]), "id": "m2", "name": "订单数",
              "expression": "cell(nodes.fetch, 0, 'orders')"}
    seeded = await client.patch(patch_url(body["id"]),
                                json={"metrics": [copy.deepcopy(body["metrics"][0]), second],
                                      "expected_revision": body["revision"]})
    assert seeded.status_code == 200, seeded.text

    res = await change(client, seeded.json(), [ops_set("m2", "/unit", "笔"), ops_set("m1", "/unit", "元")])
    assert res.status_code == 200, res.text
    assert res.json()["change"]["metrics"] == ["m1", "m2"]           # 按卡上的先后，不按提交顺序
    after = await _graph_of(body["workflow_id"])
    assert metric_of(after, "m1")["unit"] == "元" and metric_of(after, "m2")["unit"] == "笔"
    assert metric_of(after, "m2")["expression"] == second["expression"]


async def test_a_display_only_change_is_classified_as_display(client, documents, datasource, monkeypatch, fake_trial):
    """同义词这类展示字段改完，定义指纹不动、分类是 display；页面据此能说「数字怎么算没变」。"""
    body = await ready_draft(client, documents, datasource, monkeypatch)
    res = await change(client, body, [ops_set("m1", "/synonyms", ["退款率", "退单率"])])
    assert res.status_code == 200, res.text
    block = res.json()["change"]
    assert block["classification"] == "display" and block["definition_hash"]["changed"] is False
    assert block["validation"]["invalidated"] is True          # 图还是变了：旧试跑照样过期
    assert metric_of(await _graph_of(body["workflow_id"]))["synonyms"] == ["退款率", "退单率"]
    # 定义类字段（时间字段）改完就是 definition
    again = await change(client, res.json(), [ops_set("m1", "/time_field", "paid_at")])
    assert again.json()["change"]["classification"] == "definition"


async def test_untouched_metrics_and_cards_stay_untouched(client, documents, datasource, monkeypatch, fake_trial):
    """同一张卡上的别的指标、以及图上另一张可写的卡：没点名就一个字段不动。"""
    body = await ready_draft(client, documents, datasource, monkeypatch)
    wid = body["workflow_id"]
    graph = await _graph_of(wid)
    metrics = cfg(graph, "card")["metrics"]
    cfg(graph, "card")["metrics"] = [*metrics, {**copy.deepcopy(metrics[0]), "id": "m2", "name": "另一个指标"}]
    graph["nodes"].append({"id": "card_2", "type": "metrics", "position": {"x": 0, "y": 200},
                           "data": {"label": "另一张卡", "config": {"metrics": [
                               {**copy.deepcopy(metrics[0]), "id": "m9", "name": "第二张卡上的指标"}]}}})
    saved = await client.patch(f"/api/workflows/{wid}", json={"graph": graph, "expected_revision": body["revision"]})
    assert saved.status_code == 200, saved.text
    before = await _graph_of(wid)

    fresh = (await client.get(patch_url(body["id"]))).json()
    # 多张可写的卡、又没指明目标：拒（不做「取第一张」）
    refused = await change(client, fresh, [ops_set("m1", "/unit", "元")])
    assert refused.status_code == 409 and refused.json()["code"] == "card_required", refused.text
    # 指明第二张卡：改的是它，card 上一条不动
    written = await change(client, fresh, [ops_set("m9", "/unit", "元")], target={"card_id": "card_2"})
    assert written.status_code == 200, written.text
    after = await _graph_of(wid)
    assert metric_of(after, "m9", card="card_2")["unit"] == "元"
    assert cfg(after, "card")["metrics"] == cfg(before, "card")["metrics"]     # 第一张卡整份不动


# ==========================================================================
# 四种写法：省略 / 空串 / null / remove
# ==========================================================================

async def test_omit_empty_string_null_and_remove_are_four_different_things(
        client, documents, datasource, monkeypatch, fake_trial):
    body = await ready_draft(client, documents, datasource, monkeypatch)
    before = metric_of(await _graph_of(body["workflow_id"]))
    assert before["unit"] == "%" and "time_field" not in before

    # 省略：只提 unit，time_field / grain 一个字都没提，保持原样（「没有这个键」还是「没有这个键」）
    first = await change(client, body, [ops_remove("m1", "/unit")])
    assert first.status_code == 200, first.text
    metric = metric_of(await _graph_of(body["workflow_id"]))
    assert "unit" not in metric and "time_field" not in metric and "grain" not in metric

    # set 空串：键在、值是空的——和 remove 的「没声明」不是一回事，存储上分得出来
    second = await change(client, first.json(), [ops_set("m1", "/unit", "")])
    assert second.status_code == 200, second.text
    metric = metric_of(await _graph_of(body["workflow_id"]))
    assert metric["unit"] == "" and "unit" in metric

    # set null：拒（「清空」有 remove、「声明为空」有 ''，不替客户端猜）
    null = await change(client, second.json(), [ops_set("m1", "/unit", None)])
    assert null.status_code == 422, null.text
    assert null.json()["code"] == "operation_value" and "null" in null.json()["detail"]
    # set 不带 value：拒
    missing = await change(client, second.json(), [{"op": "set", "metric_id": "m1", "path": "/unit"}])
    assert missing.status_code == 422 and missing.json()["code"] == "operation_value"
    # remove 带 value：拒
    extra = await change(client, second.json(), [{"op": "remove", "metric_id": "m1", "path": "/unit", "value": ""}])
    assert extra.status_code == 422 and extra.json()["code"] == "operation_value"
    # 三种错法都没落地：还是第二次那一份（键在、值为空串）
    assert metric_of(await _graph_of(body["workflow_id"]))["unit"] == ""
    assert await _versions(body["workflow_id"]) == [1, 2, 3]


async def test_array_fields_are_set_as_a_whole_and_never_by_position(
        client, documents, datasource, monkeypatch, fake_trial):
    """数组字段整份设置；想按位置改（/synonyms/0）直接拒——位置不是身份，重排会让它指到别人。"""
    body = await ready_draft(client, documents, datasource, monkeypatch)
    res = await change(client, body, [ops_set("m1", "/synonyms", ["退款率"]),
                                      ops_set("m1", "/dimensions", ["门店", "渠道"])])
    assert res.status_code == 200, res.text
    metric = metric_of(await _graph_of(body["workflow_id"]))
    assert metric["synonyms"] == ["退款率"] and metric["dimensions"] == ["门店", "渠道"]

    by_position = await change(client, res.json(), [ops_set("m1", "/synonyms/0", "新的同义词")])
    assert by_position.status_code == 422, by_position.text
    assert by_position.json()["code"] == "operation_path"
    assert "位置" in by_position.json()["detail"]
    assert metric_of(await _graph_of(body["workflow_id"]))["synonyms"] == ["退款率"]

    # 值里的空串不算「声明成空」：数组元素得有内容
    empty_item = await change(client, res.json(), [ops_set("m1", "/synonyms", ["", "x"])])
    assert empty_item.status_code == 422 and empty_item.json()["code"] == "operation_value"


# ==========================================================================
# 非法操作：拒得具体，一个字段都不落
# ==========================================================================

@pytest.mark.parametrize("operation,code", [
    ({"op": "set", "metric_id": "m1", "path": "/id", "value": "m9"}, "operation_path"),
    ({"op": "set", "metric_id": "m1", "path": "/definition/citations", "value": []}, "operation_path"),
    ({"op": "set", "metric_id": "m1", "path": "/definition/owner", "value": "别人"}, "operation_path"),
    ({"op": "set", "metric_id": "m1", "path": "/definition/effective_from", "value": "2026-01-01"}, "operation_path"),
    ({"op": "set", "metric_id": "m1", "path": "/contract", "value": {"shape": "scalar"}}, "operation_path"),
    ({"op": "set", "metric_id": "m1", "path": "/kind", "value": "rule"}, "operation_path"),
    ({"op": "set", "metric_id": "m1", "path": "/open_questions", "value": ["x"]}, "operation_path"),
    ({"op": "set", "metric_id": "m1", "path": "unit", "value": "元"}, "operation_path"),
    ({"op": "set", "metric_id": "m1", "path": "/definition", "value": {"text": "整份换"}}, "operation_path"),
    ({"op": "patch", "metric_id": "m1", "path": "/unit", "value": "元"}, "operation_invalid"),
    ({"op": "set", "metric_id": "m9", "path": "/unit", "value": "元"}, "operation_metric"),
    ({"op": "set", "metric_id": "m1", "path": "/decimals", "value": "2"}, "operation_value"),
    ({"op": "set", "metric_id": "m1", "path": "/name", "value": "  "}, "operation_value"),
    ({"op": "set", "metric_id": "m1", "path": "/name", "value": 3}, "operation_value"),
])
async def test_illegal_operations_are_refused_with_a_specific_reason(
        client, documents, datasource, monkeypatch, fake_trial, operation, code):
    body = await ready_draft(client, documents, datasource, monkeypatch)
    before = await _graph_of(body["workflow_id"])
    res = await change(client, body, [operation])
    assert res.status_code == 422, res.text
    assert res.json()["code"] == code, res.text
    assert res.json()["field"] == "operations" and res.json()["operation"] == 0
    assert res.json()["detail"]                                           # 一句具体的话，不是空壳
    assert await _versions(body["workflow_id"]) == [1]
    assert await _graph_of(body["workflow_id"]) == before


async def test_the_whole_batch_is_atomic(client, documents, datasource, monkeypatch, fake_trial):
    """一条不合规，整批都不落地：前面那条合规的也不能半落进去（4.2 的原子性）。"""
    body = await ready_draft(client, documents, datasource, monkeypatch)
    before = await _graph_of(body["workflow_id"])
    res = await change(client, body, [ops_set("m1", "/unit", "元"), ops_set("m1", "/definition/nope", "x")])
    assert res.status_code == 422, res.text
    assert res.json()["operation"] == 1
    assert await _versions(body["workflow_id"]) == [1]
    assert await _graph_of(body["workflow_id"]) == before

    # 换一条好的一起提交：两条都落
    ok = await change(client, body, [ops_set("m1", "/unit", "元"), ops_set("m1", "/grain", "order")])
    assert ok.status_code == 200, ok.text
    metric = metric_of(await _graph_of(body["workflow_id"]))
    assert metric["unit"] == "元" and metric["grain"] == "order"
    assert await _versions(body["workflow_id"]) == [1, 2]


async def test_a_batch_that_ends_up_changing_nothing_creates_no_version(
        client, documents, datasource, monkeypatch, fake_trial):
    """先设后删（或者设成原来的值）：内容没变，不新增版本，回执照实说没改动。"""
    body = await ready_draft(client, documents, datasource, monkeypatch)
    same = await change(client, body, [ops_set("m1", "/unit", metric_of(await _graph_of(body["workflow_id"]))["unit"])])
    assert same.status_code == 200, same.text
    assert same.json()["change"]["applied"] is False and same.json()["change"]["classification"] == "none"
    assert same.json()["change"]["validation"]["invalidated"] is False
    assert await _versions(body["workflow_id"]) == [1]

    churn = await change(client, same.json(), [ops_set("m1", "/grain", "order"), ops_remove("m1", "/grain")])
    assert churn.status_code == 200, churn.text
    assert churn.json()["change"]["applied"] is False
    assert [op["changed"] for op in churn.json()["change"]["operations"]] == [True, True]
    assert await _versions(body["workflow_id"]) == [1]


async def test_a_missing_definition_object_is_created_by_set_and_ignored_by_remove(client):
    """指标上还没有 definition 对象：set /definition/… 把它建出来；remove 是「本来就没有」——无变化，不是路径非法。"""
    body = await bare_draft(client, [
        {"id": "m1", "name": "退款率", "unit": "%", "expression": "cell(nodes.fetch, 0, 'refund_rate')"},
        {"id": "m2", "name": "订单数", "definition": None, "expression": "cell(nodes.fetch, 0, 'orders')"},
    ])
    before = metric_of(await _graph_of(body["workflow_id"]), "m1")
    res = await change(client, body, [ops_set("m1", "/definition/text", "只算已支付的订单"),
                                      ops_set("m2", "/definition/exclusions", ["全额退款"])])
    assert res.status_code == 200, res.text
    after = await _graph_of(body["workflow_id"])
    # 字段落在新建出来的对象里；这条指标上别的键一个不动
    assert metric_of(after, "m1")["definition"] == {"text": "只算已支付的订单"}
    assert metric_of(after, "m2")["definition"] == {"exclusions": ["全额退款"]}
    assert metric_of(after, "m1")["unit"] == before["unit"]
    assert metric_of(after, "m1")["expression"] == before["expression"]

    # remove 一个本来就不存在的子字段：不新建对象、不新增版本，也不报路径非法
    none = await change(client, res.json(), [ops_remove("m2", "/definition/scope")])
    assert none.status_code == 200, none.text
    assert none.json()["change"]["applied"] is False and none.json()["change"]["classification"] == "none"
    assert metric_of(await _graph_of(body["workflow_id"]), "m2")["definition"] == {"exclusions": ["全额退款"]}
    assert await _versions(body["workflow_id"]) == [1, 2]


async def test_a_definition_that_is_not_an_object_is_still_refused(client):
    """definition 在、但不是对象（被人写成了字符串或数组）：照旧拒——数据坏了就说清楚，不把原来的值丢掉。"""
    body = await bare_draft(client, [
        {"id": "m1", "name": "退款率", "definition": "说明写在字符串里"},
        {"id": "m2", "name": "订单数", "definition": ["不是对象"]},
    ])
    before = await _graph_of(body["workflow_id"])
    for metric_id in ("m1", "m2"):
        for operation in (ops_set(metric_id, "/definition/text", "改成别的"), ops_remove(metric_id, "/definition/text")):
            res = await change(client, body, [operation])
            assert res.status_code == 422, res.text
            assert res.json()["code"] == "operation_path" and "不是对象" in res.json()["detail"]
    assert await _versions(body["workflow_id"]) == [1]
    assert await _graph_of(body["workflow_id"]) == before
    assert metric_of(await _graph_of(body["workflow_id"]), "m1")["definition"] == "说明写在字符串里"


# ==========================================================================
# 和整份换指标互斥；老路照旧
# ==========================================================================

async def test_field_operations_and_a_full_metrics_replacement_are_mutually_exclusive(
        client, documents, datasource, monkeypatch, fake_trial):
    body = await ready_draft(client, documents, datasource, monkeypatch)
    metrics = copy.deepcopy(body["metrics"])
    res = await client.patch(patch_url(body["id"]),
                             json={"metrics": metrics, "operations": [ops_set("m1", "/unit", "元")],
                                   "expected_revision": body["revision"]})
    assert res.status_code == 422, res.text
    assert res.json()["code"] == "change_conflict"
    everything = await client.patch(patch_url(body["id"]),
                                    json={"resolved": [], "operations": [ops_set("m1", "/unit", "元")],
                                          "expected_revision": body["revision"]})
    assert everything.status_code == 422 and everything.json()["code"] == "change_conflict"
    assert await _versions(body["workflow_id"]) == [1]


async def test_a_target_naming_another_workflow_is_refused_even_without_a_card(
        client, documents, datasource, monkeypatch, fake_trial):
    """target.workflow_id 只要给了就必须是这份草稿自己的：没给 card_id 也一样拒（两条写路径同一套定位）。"""
    body = await ready_draft(client, documents, datasource, monkeypatch)
    elsewhere = await change(client, body, [ops_set("m1", "/unit", "元")], target={"workflow_id": "wf-elsewhere"})
    assert elsewhere.status_code == 409, elsewhere.text
    assert elsewhere.json()["code"] == "target_mismatch" and "target" == elsewhere.json()["field"]

    metrics = copy.deepcopy(body["metrics"])
    metrics[0]["unit"] = "元"
    whole = await client.patch(patch_url(body["id"]),
                               json={"metrics": metrics, "expected_revision": body["revision"],
                                     "target": {"workflow_id": "wf-elsewhere"}})
    assert whole.status_code == 409, whole.text
    assert whole.json()["code"] == "target_mismatch"
    assert await _versions(body["workflow_id"]) == [1]

    # 指对工作流、不指卡：图上只有一张可写的卡，照常改
    ok = await change(client, body, [ops_set("m1", "/unit", "元")], target={"workflow_id": body["workflow_id"]})
    assert ok.status_code == 200, ok.text
    assert metric_of(await _graph_of(body["workflow_id"]))["unit"] == "元"


async def test_the_old_metrics_path_is_unchanged(client, documents, datasource, monkeypatch, fake_trial):
    """整份换指标照旧（回执不带 change 段）；没接管到工作流上的老草稿照旧只改 payload，字段操作落不到图上。"""
    body = await ready_draft(client, documents, datasource, monkeypatch)
    metrics = copy.deepcopy(body["metrics"])
    metrics[0]["name"] = "整份换过的"
    res = await client.patch(patch_url(body["id"]),
                             json={"metrics": metrics, "expected_revision": body["revision"]})
    assert res.status_code == 200, res.text
    assert res.json()["metrics"][0]["name"] == "整份换过的" and res.json()["version"] == 2
    assert "change" not in res.json()

    legacy_id = uuid.uuid4().hex[:32]
    async with SessionLocal() as session:
        session.add(CaliberDraft(id=legacy_id, title="老草稿", status="ready",
                                 payload={"metrics": [{"id": "m1", "name": "旧指标"}], "open_questions": []}))
        await session.commit()
    try:
        refused = await client.patch(patch_url(legacy_id),
                                     json={"operations": [ops_set("m1", "/unit", "元")]})
        assert refused.status_code == 409, refused.text
        assert refused.json()["code"] == "change_unavailable"
        ok = await client.patch(patch_url(legacy_id), json={"metrics": [{"id": "m1", "name": "改过的"}]})
        assert ok.status_code == 200 and ok.json()["metrics"][0]["name"] == "改过的"
    finally:
        async with SessionLocal() as session:
            row = await session.get(CaliberDraft, legacy_id)
            if row is not None:
                await session.delete(row)
                await session.commit()


# ==========================================================================
# 同一语义变化：画布和 operations 一致，旧试跑都过期
# ==========================================================================

async def test_the_same_change_via_the_canvas_and_via_operations_agree(
        client, documents, datasource, monkeypatch, fake_trial):
    """E08：同一个语义改动，从画布（整图保存）和从工作台（operations）提交得到同一张执行图，旧试跑都判过期。"""
    body = await ready_draft(client, documents, datasource, monkeypatch)
    wid = body["workflow_id"]
    assert (await client.get(patch_url(body["id"]))).json()["trial"]["status"] == "succeeded"
    v1 = await _graph_of(wid)

    # 画布那条路：拿 v1 的图，人工把同一个字段改成同一个值，整图保存
    canvas_graph = copy.deepcopy(v1)
    metric_of(canvas_graph)["time_field"] = "paid_at"
    saved = await client.patch(f"/api/workflows/{wid}",
                               json={"graph": canvas_graph, "expected_revision": body["revision"]})
    assert saved.status_code == 200 and saved.json()["version"] == 2, saved.text
    canvas_after = await _graph_of(wid)
    assert (await client.get(patch_url(body["id"]))).json()["trial"]["status"] == "stale"

    # operations 那条路：回到 v1 的图上改同一个字段（回滚出一版，再按字段改）
    rolled = await client.post(f"/api/workflows/{wid}/versions/1/restore", json={"expected_revision": 2})
    assert rolled.status_code == 200 and rolled.json()["version"] == 3, rolled.text
    assert await _graph_of(wid) == v1
    changed = await change(client, {**body, "revision": 3}, [ops_set("m1", "/time_field", "paid_at")])
    assert changed.status_code == 200, changed.text
    ops_after = await _graph_of(wid)
    assert changed.json()["change"]["applied"] is True
    assert (await client.get(patch_url(body["id"]))).json()["trial"]["status"] == "stale"

    # 两条路得到的是同一张执行图：图哈希可以不同（版本快照元数据），执行指纹一样
    assert execution_hash(ops_after) == execution_hash(canvas_after)
    assert metric_of(ops_after) == metric_of(canvas_after)


# ==========================================================================
# K02 的写入保护：字段变更一条不少
# ==========================================================================

async def test_field_operations_need_a_base_revision_and_refuse_a_stale_page(
        client, documents, datasource, monkeypatch, fake_trial):
    """E01 的字段操作版：没带基准版本 409 revision_required；旧页面 409 stale_revision，先写进去的不被盖掉。"""
    body = await ready_draft(client, documents, datasource, monkeypatch)
    missing = await client.patch(patch_url(body["id"]), json={"operations": [ops_set("m1", "/unit", "元")]})
    assert missing.status_code == 409, missing.text
    assert missing.json()["code"] == "revision_required" and missing.json()["current_version"] == 1
    assert await _versions(body["workflow_id"]) == [1]

    first = await change(client, body, [ops_set("m1", "/unit", "元")])
    assert first.status_code == 200 and first.json()["version"] == 2, first.text
    stale = await change(client, body, [ops_set("m1", "/unit", "万元")])       # 还拿着 v1
    assert stale.status_code == 409, stale.text
    assert stale.json()["code"] == "stale_revision" and stale.json()["current_version"] == 2
    assert await _versions(body["workflow_id"]) == [1, 2]
    assert metric_of(await _graph_of(body["workflow_id"]))["unit"] == "元"
    # 指到没有的卡：拒（K02 的卡级定位一起管着这条路）
    missing_card = await change(client, body, [ops_set("m1", "/unit", "元")], target={"card_id": "card-nope"})
    assert missing_card.status_code == 409 and missing_card.json()["code"] == "card_not_found"


async def test_the_same_request_id_replays_and_different_operations_conflict(
        client, documents, datasource, monkeypatch, fake_trial):
    """指纹包含 operations：同键同内容重放不新增版本；同键换了字段或换了值都算另一次请求（409）。"""
    body = await ready_draft(client, documents, datasource, monkeypatch)
    request = [ops_set("m1", "/unit", "元")]
    first = await change(client, body, request, request_id="k03-0001")
    assert first.status_code == 200 and first.json()["version"] == 2, first.text
    assert first.json().get("replayed") is None

    # 同键同内容：基准版本这时已经过期，但幂等在版本检查之前判，照旧成功，不再新增版本
    again = await change(client, body, request, request_id="k03-0001")
    assert again.status_code == 200, again.text
    assert again.json()["replayed"] is True and again.json()["version"] == 2
    assert await _versions(body["workflow_id"]) == [1, 2]

    # 同键、换了路径：内容不同 → 冲突
    other_path = await change(client, body, [ops_set("m1", "/grain", "元")], request_id="k03-0001")
    assert other_path.status_code == 409, other_path.text
    assert other_path.json()["code"] == "idempotency_conflict"
    # 同键、同一个字段换了值：内容不同 → 冲突（值算了进去）
    other_value = await change(client, body, [ops_set("m1", "/unit", "万元")], request_id="k03-0001")
    assert other_value.status_code == 409 and other_value.json()["code"] == "idempotency_conflict"
    # 同键、同一批换顺序：顺序是内容的一部分（同一条指标上的操作有先后）→ 冲突
    reordered = await change(client, body, [ops_set("m1", "/grain", "order"), ops_set("m1", "/unit", "元")],
                             request_id="k03-0001")
    assert reordered.status_code == 409 and reordered.json()["code"] == "idempotency_conflict"
    # 省略 value 和写 null 是两件事，指纹也分得开
    assert api_ledger_fingerprint(body, request) != api_ledger_fingerprint(
        body, [ops_set("m1", "/unit", None)])
    assert await _versions(body["workflow_id"]) == [1, 2]


def api_ledger_fingerprint(body: dict, operations: list[dict]) -> str:
    """同一个 request_id 的两次请求，指纹按内容算（把 pydantic 的排除未设置行为也一起算进去）。"""
    payload = api_ledger.DraftPatchIn(operations=operations, expected_revision=body["revision"])
    return api_ledger._patch_fingerprint(payload, body["card_id"])


async def test_old_request_ids_still_replay_after_the_operations_field_landed(
        client, documents, datasource, monkeypatch, fake_trial):
    """不带 operations 的老请求算出来的还是原来那一份指纹：升级前记下的 request_id 不会被判成「同键不同内容」。"""
    from app.apps.versions import write_fingerprint

    body = await ready_draft(client, documents, datasource, monkeypatch)
    metrics = copy.deepcopy(body["metrics"])
    metrics[0]["name"] = "老请求"
    payload = api_ledger.DraftPatchIn(metrics=metrics, expected_revision=body["revision"])
    before_k03 = write_fingerprint({"title": None, "metrics": metrics, "resolved": None, "card": body["card_id"]})
    assert api_ledger._patch_fingerprint(payload, body["card_id"]) == before_k03


async def test_two_operations_with_the_same_key_at_once_write_once(
        client, documents, datasource, monkeypatch, fake_trial):
    """同一个 request_id 的两路字段变更同时发：只落一版，两边都是 200。"""
    body = await ready_draft(client, documents, datasource, monkeypatch)
    payload = {"operations": [ops_set("m1", "/unit", "元")], "expected_revision": body["revision"],
               "request_id": "k03-race"}
    url = patch_url(body["id"])
    first, second = await asyncio.gather(client.patch(url, json=payload), client.patch(url, json=payload))
    assert sorted(res.status_code for res in (first, second)) == [200, 200], (first.text, second.text)
    assert await _versions(body["workflow_id"]) == [1, 2]
    assert [res.json().get("replayed") for res in (first, second)].count(True) <= 1
    assert metric_of(await _graph_of(body["workflow_id"]))["unit"] == "元"


async def test_two_field_changes_at_once_do_not_lose_an_update(
        client, documents, datasource, monkeypatch, fake_trial):
    """两路各自改一处、都基于同一版同时发：CAS 只让一路落成，另一路 409 stale_revision（不静默盖掉）。"""
    body = await ready_draft(client, documents, datasource, monkeypatch)
    wid = body["workflow_id"]
    url = patch_url(body["id"])
    first, second = await asyncio.gather(
        client.patch(url, json={"operations": [ops_set("m1", "/unit", "元")],
                                "expected_revision": body["revision"], "request_id": "k03-a"}),
        client.patch(url, json={"operations": [ops_set("m1", "/unit", "万元")],
                                "expected_revision": body["revision"], "request_id": "k03-b"}))
    assert sorted(res.status_code for res in (first, second)) == [200, 409], (first.text, second.text)
    refused = first if first.status_code == 409 else second
    assert refused.json()["code"] in ("stale_revision", "idempotency_conflict")
    assert refused.json()["current_version"] == 2
    assert await _versions(wid) == [1, 2]
    assert metric_of(await _graph_of(wid))["unit"] in ("元", "万元")


# ==========================================================================
# 仓储：薄边界，图上改
# ==========================================================================

def test_the_repository_hides_the_graph_and_refuses_what_is_not_a_source_card():
    graph = {"nodes": [
        {"id": "card", "type": "metrics", "data": {"label": "口径卡", "config": {"metrics": [
            {"id": "m1", "name": "退款率", "unit": "%"}, {"id": "m2", "name": "订单数"}]}}},
        {"id": "chained", "type": "metrics", "data": {"label": "转抄", "config": {
            "caliber_from": {"workflow_id": "w", "workflow_version": 1}, "metrics": []}}},
        {"id": "fetch", "type": "tool", "data": {"label": "取数", "config": {"tool": "db_query__shop"}}},
    ], "edges": []}
    repo = GraphBackedCaliberRepository(graph, card_id="card")
    assert repo.card.node_id == "card" and repo.card.title == "口径卡"
    assert repo.ids() == ["m1", "m2"]

    # 读出来的是深拷贝：改它不影响图
    metric = repo.metric("m1")
    metric["unit"] = "改过的"
    assert repo.metric("m1")["unit"] == "%"
    with pytest.raises(MetricGone):
        repo.metric("m9")

    # 换指标交回的是整张图的深拷贝，别的节点带着走，指标顺序不变
    new_graph = repo.replace_metrics({"m1": {**repo.metric("m1"), "unit": "元"}})
    assert [node["id"] for node in new_graph["nodes"]] == ["card", "chained", "fetch"]
    assert new_graph["nodes"][2] == graph["nodes"][2] and new_graph is not graph
    assert [m["id"] for m in new_graph["nodes"][0]["data"]["config"]["metrics"]] == ["m1", "m2"]
    assert new_graph["nodes"][0]["data"]["config"]["metrics"][0]["unit"] == "元"
    assert graph["nodes"][0]["data"]["config"]["metrics"][0]["unit"] == "%"
    with pytest.raises(MetricGone):
        repo.replace_metrics({"m9": {"id": "m9"}})

    with pytest.raises(CaliberGone):
        GraphBackedCaliberRepository(graph, card_id="chained")
    with pytest.raises(CaliberGone):
        GraphBackedCaliberRepository(graph, card_id="fetch")
    with pytest.raises(CaliberGone):
        GraphBackedCaliberRepository(graph, card_id="nope")


def test_the_operation_whitelist_covers_the_fields_that_really_exist():
    """白名单落在指标真有的字段上：顶层路径按 data/caliber_identity.SEMANTIC_FIELDS 对齐（同义词是展示字段）。"""
    from app.data.caliber_identity import SEMANTIC_FIELDS

    top = {path[1:] for path in changes.FIELDS if path.count("/") == 1}
    assert top <= set(SEMANTIC_FIELDS) | {"synonyms"}
    # 起草阶段真正会写进指标的字段（app/ledger/draftplan.METRIC_FIELDS）都要能按字段改
    assert {"id", "name", "synonyms", "unit", "decimals", "expression", "grain", "time_field",
            "numerator", "denominator", "dimensions", "filters"} - {"id"} <= top
    assert {path for path in changes.FIELDS if path.startswith("/definition/")} == {
        "/definition/text", "/definition/scope", "/definition/exclusions"}
    # 身份、证据归属和治理归属都在「不能改」那一栏里
    assert {"/id", "/definition/citations", "/definition/owner", "/definition/effective_from"} <= set(changes.FORBIDDEN)
