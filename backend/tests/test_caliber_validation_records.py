"""K05 的验证记录与最小审阅：追加存储、绑定定义/绑定依据、独立期望、覆盖判断、认证审阅。

这份文件盯住任务书 4.8 与 E05/E07/E15/R04/R05/R06/R07 的硬要求：

1. **没有期望只能记 observed，不能 pass。** 试跑成功不等于验证通过：没有独立期望时状态是 observed，证据不足是
   unverified，只有「有期望、逐条对得上」才是 matched——而 matched 也只是「这一次对得上」，不等于批准。
2. **独立期望能做全指标、全约定形状的比较。** 期望要逐条给全卡指标（漏一条拒）；声明成标量的指标必须恰好一行，
   空结果与多行**不能只看第一行**（R04/R05）。比较是原值比较、不做隐式舍入（R06）。
3. **覆盖是读的时候按指纹现算的。** 定义、绑定或取数语义（含 SQL）变了，旧记录不再覆盖当前版本（covers_current
   false）；把一段没验证过的业务描述删掉不会让旧的验证/审阅升级成通过（E15/R07）。
4. **交错结束的两次试跑各留一条记录。** 晚到的旧试跑替换不了当前 trial 指针，但它自己那一行一定要落下（E07）。
5. **审阅绑定认证主体。** 审批人只来自服务端身份：请求体里写 actor 回 422，没有可记的主体回 409；决定绑定所审
   记录的依据，样例匹配不会自动变成批准。

模型一律注入假模型、试跑换成假的（结果证据用桩替代）；走的是真实 HTTP 接口，不连真实业务库。
"""
from __future__ import annotations

import copy
import json
import re
import uuid

import pytest
from sqlalchemy import delete, select

from app.api import ledger as api_ledger
from app.db.base import SessionLocal
from app.db.models import CaliberDraft, CaliberReviewDecision, CaliberValidationRecord, Workflow
from app.ledger import validation
from tests.test_caliber_field_baseline import accepted_draft  # noqa: F401 - 起草帮手和夹具共用
from tests.test_ledger_docdraft_api import (  # noqa: F401 - fixture 和起草用例共用
    client, datasource, documents, extract, fake_model, fake_trial, groups_reply, plan_reply,
)


def plant_evidence(monkeypatch, *, value=2, values=None, rows=1, masked=False, status="ok", semantic=False,
                   metric_ids=("m1",), node="fetch", nodes=None):
    """把这次试运行交回的证据换成桩：指标 + 每个取数节点一条查询快照。

    值、行数从这里给，用来构造空结果、多行、遮罩这些反例；`values` 可以逐条指标给不同的值，`nodes` 给
    metric_id → 取数节点（多指标卡的两个节点各查一次）。semantic=True 模拟引擎判出的
    「业务描述和取数条件的关系还没被证明」（semantic_unverified）。"""
    per_metric = dict(values or {})
    per_node = dict(nodes or {})

    async def metrics(run_id: str) -> list[dict]:
        out: list[dict] = []
        for metric_id in metric_ids:
            reported = None if (masked or rows == 0) else per_metric.get(metric_id, value)
            item = {"id": metric_id, "name": f"指标 {metric_id}", "unit": "%", "value": reported,
                    "status": status, "rendered": reported, "node_id": per_node.get(metric_id, node)}
            if masked:
                item.update(masked=True, quality={"status": "unknown", "checks": [
                    {"code": "masked", "status": "unknown", "detail": "指标值已隐藏，不能在此验证"}]})
            else:
                checks = []
                if semantic:
                    checks.append({"code": "semantic_definition", "status": "unknown",
                                   "detail": "业务描述与取数条件的一致性仍需审阅"})
                item["quality"] = {"status": "unknown" if checks else "pass", "checks": checks}
            out.append(item)
        return out

    async def queries(run_id: str) -> list[dict]:
        wanted = list(dict.fromkeys(per_node.get(mid, node) for mid in metric_ids))
        return [{"node_id": node_id, "tool": "db_query", "source": "shop", "columns": ["rate"],
                 "rows": [], "total_rows": rows, "truncated": False, "masked": [],
                 "source_truncated": False, "preview_truncated": False} for node_id in wanted]

    async def links(run_id: str) -> tuple[dict[str, str], list[dict]]:
        """指标 → 取数快照的关联（真实运行里由 _trial_metric_links 从封存口径卡认出来）：桩按 nodes 给。"""
        wanted = list(dict.fromkeys(per_node.get(mid, node) for mid in metric_ids))
        return ({mid: per_node.get(mid, node) for mid in metric_ids},
                [{"key": node_id, "node_id": node_id, "total_rows": rows, "truncated": False}
                 for node_id in wanted])

    monkeypatch.setattr(api_ledger, "_trial_metrics", metrics)
    monkeypatch.setattr(api_ledger, "_trial_queries", queries)
    monkeypatch.setattr(api_ledger, "_trial_metric_links", links)


def nodes_of(metrics: list[dict]) -> dict[str, str]:
    """每条指标读哪个取数节点（从表达式里取）：桩证据按它对到查询上，多指标卡两个节点各一条快照。"""
    out: dict[str, str] = {}
    for metric in metrics if isinstance(metrics, list) else []:
        found = re.search(r"nodes\.([\w-]+)", str((metric or {}).get("expression") or ""))
        if isinstance(metric, dict) and metric.get("id") and found:
            out[str(metric["id"])] = found.group(1)
    return out


async def run_trial(client, draft_id: str, *, expect=None, inputs=None) -> dict:
    body: dict = {"inputs": inputs or {}}
    if expect is not None:
        body["expect"] = expect
    return (await client.post(f"/api/ledger/drafts/{draft_id}/trial", json=body)).json()


@pytest.fixture(autouse=True)
def _fake_trial(fake_trial):
    """本模块的草稿都走接口起草：试跑必须换成假的，不然会去打夹具里不存在的库。"""
    return fake_trial


async def validations(client, draft_id: str) -> dict:
    return (await client.get(f"/api/ledger/drafts/{draft_id}/validations")).json()


def baseline_metrics(one: dict, *, name: str = "订单金额", mid: str = "m2", column: str = "gmv") -> dict:
    """第二组指标：改 id / 名字 / 表达式 / SQL，用来构造「一张卡两条指标」的全指标比较。"""
    two = copy.deepcopy(one)
    two["metrics"][0].update(id=mid, name=name, expression=f"cell(nodes.fetch, 0, '{column}')")
    two["sql"] = f"SELECT SUM(amount) AS {column} FROM orders WHERE store = '{{{{ input.store }}}}'"
    return two


# ==========================================================================
# 1. 没有期望只记 observed
# ==========================================================================


async def test_a_trial_without_an_expectation_is_only_observed(client, documents, datasource, monkeypatch):
    """自动试跑没有独立期望：记录照落，但状态是 observed——跑了、取了数，一个字也不能说「通过」。"""
    made = await accepted_draft(client, documents, datasource, monkeypatch)
    listed = await validations(client, made["id"])
    assert len(listed["records"]) == 1                      # 同一次试跑保存两次也只落一行
    [record] = listed["records"]
    assert record["status"] == "observed" and record["expected"] is None
    assert record["covers_current"] is True
    assert record["definition_hash"] and record["semantic_hash"]
    assert record["method"]["version"] == validation.METHOD_VERSION
    assert any("没有独立期望" in problem for problem in record["problems"])
    assert listed["summary"]["verdict"] == "observed" and listed["summary"]["review"] is None
    assert "不能算验证通过" in listed["summary"]["reason"]

    # 试跑接口里也带上这条记录，并且同样不说通过
    trial = (await client.get(f"/api/ledger/drafts/{made['id']}/trial")).json()
    assert trial["validation"]["record"]["id"] == record["id"]
    assert trial["validation"]["covers_current"] is True
    detail = (await client.get(f"/api/ledger/drafts/{made['id']}")).json()
    assert detail["validation"]["verdict"] == "observed"


# ==========================================================================
# 2. 固定期望：对得上 / 对不上 / 空结果 / 多行
# ==========================================================================


async def test_a_fixed_fixture_expectation_decides_matched_or_mismatched(client, documents, datasource,
                                                                        monkeypatch):
    """显式固定 fixture 期望：对得上记 matched，对不上记 mismatched；但它只是「这一次对得上」，不是批准。"""
    made = await accepted_draft(client, documents, datasource, monkeypatch)
    plant_evidence(monkeypatch, value=2, rows=1)

    ok = await run_trial(client, made["id"], expect={"metrics": {"m1": 2}, "source": "fixture",
                                                     "sample_id": "oct-2026", "sample_version": "1",
                                                     "note": "独立算出来的十月预期"})
    assert ok["status"] == "succeeded", ok
    record = ok["validation"]["record"]
    assert record["status"] == "matched" and record["problems"] == []
    assert record["expected"]["source"] == "fixture"
    assert record["expected"]["metrics"] == {"m1": 2}
    assert record["actual"]["m1"]["value"] == 2 and record["actual"]["m1"]["ok"] is True
    assert record["actual"]["m1"]["rows"] == 1 and record["actual"]["m1"]["shape"] == "scalar"
    assert record["sample"] == {"id": "oct-2026", "version": "1"}
    assert record["covers_current"] is True and record["inputs_hash"]

    # 对得上也不等于批准：摘要仍是 unverified，并且这么说
    listed = await validations(client, made["id"])
    assert listed["summary"]["status"] == "matched"
    assert listed["summary"]["verdict"] == "unverified"
    assert "不等于正式批准" in listed["summary"]["reason"]

    bad = await run_trial(client, made["id"], expect={"metrics": {"m1": 3}, "source": "fixture"})
    record = bad["validation"]["record"]
    assert record["status"] == "mismatched"
    assert record["actual"]["m1"]["ok"] is False
    assert any("对不上" in problem and "3" in problem for problem in record["problems"])
    assert (await validations(client, made["id"]))["summary"]["verdict"] == "mismatched"


async def test_an_empty_result_is_not_a_pass_and_multi_row_does_not_read_the_first_row(
        client, documents, datasource, monkeypatch):
    """空结果与多行都不是「取第一行就对」：声明成标量的指标要恰好一行（R04/R05）。"""
    made = await accepted_draft(client, documents, datasource, monkeypatch)

    plant_evidence(monkeypatch, value=None, rows=0)
    empty = await run_trial(client, made["id"], expect={"metrics": {"m1": 2}})
    record = empty["validation"]["record"]
    assert record["status"] == "mismatched" and record["actual"]["m1"]["rows"] == 0
    assert any("空结果" in problem or "0 行" in problem for problem in record["problems"])

    # 第一行正好等于期望，但查询回来 3 行：标量取数有歧义，不能算通过
    plant_evidence(monkeypatch, value=2, rows=3)
    multi = await run_trial(client, made["id"], expect={"metrics": {"m1": 2}})
    record = multi["validation"]["record"]
    assert record["status"] == "mismatched" and record["actual"]["m1"]["value"] == 2
    assert record["actual"]["m1"]["rows"] == 3
    assert any("恰好一行" in problem for problem in record["problems"])

    # 值被遮罩：判不了，记 unverified（不是 matched、也不是 mismatched）
    plant_evidence(monkeypatch, value=2, rows=1, masked=True)
    hidden = await run_trial(client, made["id"], expect={"metrics": {"m1": 2}})
    record = hidden["validation"]["record"]
    assert record["status"] == "unverified" and record["actual"]["m1"]["masked"] is True
    assert any("遮罩" in problem for problem in record["problems"])


async def test_a_semantic_gap_is_recorded_and_never_becomes_a_pass_by_itself(client, documents, datasource,
                                                                           monkeypatch):
    """R07：语义说明没被证明是一处待确认的缺口，照实记在记录里；它自己不会让任何东西算「通过」。"""
    made = await accepted_draft(client, documents, datasource, monkeypatch)
    plant_evidence(monkeypatch, value=2, rows=1, semantic=True)
    trial = await run_trial(client, made["id"], expect={"metrics": {"m1": 2}, "source": "fixture"})
    record = trial["validation"]["record"]
    assert record["status"] == "matched" and record["actual"]["m1"]["ok"] is True
    assert record["actual"]["m1"]["semantic_unverified"] is True
    assert not any("语义" in problem for problem in record["problems"])   # 不是比较结论，不并进 problems

    listed = await validations(client, made["id"])
    assert listed["summary"]["semantic_gaps"] == ["m1"]
    assert listed["summary"]["status"] == "matched"
    assert listed["summary"]["verdict"] == "unverified"        # 没有认证审阅就还是不算通过

    # 人工确认之后才算 verified，但语义缺口仍旧写在结论里（「已确认」不等于「这段描述被证明」）
    confirmed = await client.post(f"/api/ledger/drafts/{made['id']}/validations/{record['id']}/review",
                                  json={"decision": "confirmed"}, headers={"X-Actor": "alice"})
    assert confirmed.status_code == 201, confirmed.text
    listed = await validations(client, made["id"])
    assert listed["summary"]["verdict"] == "verified"
    assert listed["summary"]["semantic_gaps"] == ["m1"]
    assert "语义说明仍待人工确认" in listed["summary"]["reason"]


async def test_a_trial_that_never_ran_is_recorded_as_unverified(client, documents, datasource, monkeypatch):
    """停在 needs_input 的试跑（一条 SQL 都没发）：记录照落，状态是 unverified，并明说没有可比较的实际结果。"""
    made = await accepted_draft(client, documents, datasource, monkeypatch)

    async def waiting(*, draft_id, workflow_id, graph, values, started_by, entry_fields=(), version=1):
        return {**api_ledger._trial_binding(version, graph, values), "trial_id": "wait-1", "run_id": "",
                "status": "needs_input", "error": "缺少必填输入：start"}

    monkeypatch.setattr(api_ledger, "_run_trial", waiting)
    await run_trial(client, made["id"], expect={"metrics": {"m1": 2}})
    listed = await validations(client, made["id"])
    newest = listed["records"][0]
    assert newest["status"] == "unverified" and newest["run_id"] == "" and newest["trial_id"] == "wait-1"
    assert newest["expected"]["metrics"] == {"m1": 2}
    assert any("没有成功" in problem for problem in newest["problems"])
    assert listed["summary"]["verdict"] == "unverified"


async def test_the_expectation_must_cover_the_whole_card_and_use_a_known_source(client, documents, datasource,
                                                                               monkeypatch):
    """全指标比较：期望要逐条给全卡指标；来源只能是三种之一。不合规在发起运行之前回 422。"""
    doc = await documents("val-multi-" + uuid.uuid4().hex[:8])
    source = await datasource()
    one = json.loads(plan_reply(f"{doc}-2"))
    fake_model(monkeypatch, groups_reply(one, baseline_metrics(one)))
    body = await extract(client, doc, source)
    draft_id = body["draft_id"]
    made = (await client.get(f"/api/ledger/drafts/{draft_id}")).json()
    assert sorted(m["id"] for m in made["metrics"]) == ["m1", "m2"]

    partial = await client.post(f"/api/ledger/drafts/{draft_id}/trial",
                                json={"inputs": {}, "expect": {"metrics": {"m1": 2}}})
    assert partial.status_code == 422 and "m2" in partial.text

    stranger = await client.post(f"/api/ledger/drafts/{draft_id}/trial",
                                 json={"inputs": {}, "expect": {"metrics": {"nope": 2}}})
    assert stranger.status_code == 422 and "nope" in stranger.text

    invented = await client.post(f"/api/ledger/drafts/{draft_id}/trial",
                                 json={"inputs": {}, "expect": {"metrics": {"m1": 2, "m2": 3},
                                                                "source": "model_said_so"}})
    assert invented.status_code == 422 and "human_confirmed" in invented.text

    plant_evidence(monkeypatch, value=2, rows=1)
    whole = await client.post(f"/api/ledger/drafts/{draft_id}/trial",
                              json={"inputs": {}, "expect": {"metrics": {"m1": 2, "m2": 3},
                                                             "source": "independent_computation"}})
    assert whole.status_code == 200, whole.text
    record = whole.json()["validation"]["record"]
    assert record["metric_ids"] == ["m1", "m2"]
    assert record["actual"]["m1"]["ok"] is True
    # m2 这一次没有交回结果（桩只造了 m1）：判不了，不能算通过
    assert record["actual"]["m2"]["status"] == "missing" and record["actual"]["m2"]["ok"] is None
    assert record["status"] == "unverified"


# ==========================================================================
# 3. 覆盖判断与 E15/R07：删掉没验证过的描述不会升级成通过
# ==========================================================================


async def test_deleting_a_semantic_field_does_not_keep_an_old_verification_verified(
        client, documents, datasource, monkeypatch):
    """确认过的一次验证，在定义被改（删掉一段业务描述）之后不再覆盖当前版本，也不能拿它当新定义的批准。"""
    made = await accepted_draft(client, documents, datasource, monkeypatch)
    draft_id = made["id"]
    plant_evidence(monkeypatch, value=2, rows=1)
    trial = await run_trial(client, draft_id, expect={"metrics": {"m1": 2}, "source": "fixture"})
    record_id = trial["validation"]["record"]["id"]

    confirmed = await client.post(f"/api/ledger/drafts/{draft_id}/validations/{record_id}/review",
                                  json={"decision": "confirmed", "note": "看过独立样例"},
                                  headers={"X-Actor": "alice"})
    assert confirmed.status_code == 201, confirmed.text
    listed = await validations(client, draft_id)
    assert listed["summary"]["verdict"] == "verified"

    # 删掉「口径说明」这段没被取数证明过的业务描述：定义指纹变了
    revision = (await client.get(f"/api/ledger/drafts/{draft_id}")).json()["revision"]
    removed = await client.patch(f"/api/ledger/drafts/{draft_id}", json={
        "operations": [{"op": "remove", "metric_id": "m1", "path": "/definition/text"}],
        "expected_revision": revision, "request_id": "drop-semantic-1"})
    assert removed.status_code == 200, removed.text

    after = await validations(client, draft_id)
    assert after["summary"]["covers_current"] is False and after["summary"]["verdict"] == "stale"
    # 旧记录和旧审阅都还在（历史可追），但都明说不再覆盖当前版本
    old = next(item for item in after["records"] if item["id"] == record_id)
    assert old["status"] == "matched" and old["covers_current"] is False
    assert old["review"]["decision"] == "confirmed" and old["review"]["covers_current"] is False
    assert old["review"]["actor"] == "alice"
    # 摘要里带上的是「覆盖不了当前版本」的结论，不是拿旧的 confirmed 当通过
    assert after["summary"]["verdict"] != "verified"
    assert after["summary"]["covers_current"] is False
    assert (await client.get(f"/api/ledger/drafts/{draft_id}")).json()["validation"]["verdict"] == "stale"


async def test_a_sql_change_alone_stops_the_old_record_from_covering(client, documents, datasource, monkeypatch):
    """取数 SQL 改了（定义没动）也算对新版本不覆盖：覆盖看的不只是定义指纹；画布编辑也不推进 B。"""
    made = await accepted_draft(client, documents, datasource, monkeypatch)
    listed = await validations(client, made["id"])
    assert listed["summary"]["covers_current"] is True
    before_baseline = listed["field_baseline"]
    assert before_baseline["status"] == "known"
    async with SessionLocal() as session:
        workflow = await session.get(Workflow, made["workflow_id"])
        graph = copy.deepcopy(workflow.graph)
        revision = workflow.version
    fetch = next(node for node in graph["nodes"] if node["id"] == "fetch")
    fetch["data"]["config"]["args"]["sql"] = "SELECT COUNT(*) * 2.0 AS refund_rate FROM orders " \
                                             "WHERE status = 'paid' AND store = '{{ input.store }}'"
    saved = await client.patch(f"/api/workflows/{made['workflow_id']}",
                               json={"graph": graph, "expected_revision": revision})
    assert saved.status_code == 200, saved.text
    after = await validations(client, made["id"])
    assert after["summary"]["covers_current"] is False and after["summary"]["verdict"] == "stale"
    # 画布上的改动只动 L：字段基线 B 的引用和值一个字没变
    assert after["field_baseline"] == before_baseline


# ==========================================================================
# 4. 交错结束的两次试跑：两条记录都留下（E07）
# ==========================================================================


async def test_interleaved_trials_each_leave_their_own_record(client, documents, datasource, monkeypatch):
    """晚到的旧试跑替换不了当前 trial 指针，但它自己那一行一定要落下：新旧两条都可追踪。"""
    made = await accepted_draft(client, documents, datasource, monkeypatch)
    draft_id = made["id"]
    async with SessionLocal() as session:
        workflow = await session.get(Workflow, made["workflow_id"])
        graph_v1 = copy.deepcopy(workflow.graph)
    metrics = copy.deepcopy(made["metrics"])
    metrics[0]["unit"] = "％"                                     # 定义变了一处 → 新版本
    changed = await client.patch(f"/api/ledger/drafts/{draft_id}",
                                 json={"metrics": metrics, "expected_revision": made["revision"]})
    assert changed.status_code == 200, changed.text

    plant_evidence(monkeypatch, value=2, rows=1)
    newest = await run_trial(client, draft_id)
    # 旧那一次（还对着 v1 的图）晚到：指针不换，记录照落
    late = {**api_ledger._trial_binding(1, graph_v1, {}), "trial_id": "late-1", "run_id": "",
            "status": "succeeded", "error": ""}
    assert await api_ledger._save_trial(draft_id, late) is False

    detail = (await client.get(f"/api/ledger/drafts/{draft_id}")).json()
    assert detail["trial"]["trial_id"] == newest["trial_id"]      # 当前指针还是新的那一次
    listed = await validations(client, draft_id)
    by_trial = {record["trial_id"]: record for record in listed["records"]}
    assert {"late-1", newest["trial_id"]} <= set(by_trial)
    late_record, new_record = by_trial["late-1"], by_trial[newest["trial_id"]]
    assert late_record["definition_hash"] != new_record["definition_hash"]
    assert late_record["covers_current"] is False and new_record["covers_current"] is True
    assert listed["summary"]["covers_current"] is True and listed["summary"]["record_id"] == new_record["id"]
    # 晚到那一条照实说它不对着当前版本跑
    assert any("找不到对应的定义版本" in problem for problem in late_record["problems"]) is False


# ==========================================================================
# 5. 审阅：认证主体、只追加、样例匹配不是批准
# ==========================================================================


async def test_a_review_binds_the_authenticated_subject_and_the_record_basis(client, documents, datasource,
                                                                            monkeypatch):
    """审阅的审批人只来自服务端身份：请求体写 actor 回 422，没有主体回 409；决定绑定所审记录的依据。"""
    made = await accepted_draft(client, documents, datasource, monkeypatch)
    draft_id = made["id"]
    plant_evidence(monkeypatch, value=2, rows=1)
    trial = await run_trial(client, draft_id, expect={"metrics": {"m1": 2}, "source": "fixture"})
    record = trial["validation"]["record"]
    record_id = record["id"]

    forged = await client.post(f"/api/ledger/drafts/{draft_id}/validations/{record_id}/review",
                               json={"decision": "confirmed", "actor": "模型自己写的审批人"},
                               headers={"X-Actor": "alice"})
    assert forged.status_code == 422

    anonymous = await client.post(f"/api/ledger/drafts/{draft_id}/validations/{record_id}/review",
                                  json={"decision": "confirmed"})
    assert anonymous.status_code == 409 and anonymous.json()["code"] == "review_actor_required"

    unknown = await client.post(f"/api/ledger/drafts/{draft_id}/validations/{record_id}/review",
                                json={"decision": "approved"}, headers={"X-Actor": "alice"})
    assert unknown.status_code == 422 and unknown.json()["code"] == "review_decision"

    outside = await client.post(f"/api/ledger/drafts/{draft_id}/validations/{record_id}/review",
                               json={"decision": "confirmed", "metrics": ["m9"]},
                               headers={"X-Actor": "alice"})
    assert outside.status_code == 422 and outside.json()["code"] == "review_scope"

    made_review = await client.post(f"/api/ledger/drafts/{draft_id}/validations/{record_id}/review",
                                    json={"decision": "confirmed", "note": "看过独立样例"},
                                    headers={"X-Actor": "alice"})
    assert made_review.status_code == 201, made_review.text
    review = made_review.json()["record"]["review"]
    assert review["decision"] == "confirmed" and review["actor"] == "alice" and review["verified"] is False
    assert review["scope"] == ["m1"] and review["covers_current"] is True
    assert review["basis"]["definition_hash"] == record["definition_hash"]
    assert review["basis"]["method_version"] == validation.METHOD_VERSION

    listed = await validations(client, draft_id)
    assert listed["summary"]["verdict"] == "verified"
    assert listed["summary"]["review"]["actor"] == "alice"

    # 再审一次（另一名主体驳回）：追加一条，显示用最新的一条，旧的一条还在
    again = await client.post(f"/api/ledger/drafts/{draft_id}/validations/{record_id}/review",
                              json={"decision": "rejected", "note": "样例覆盖不全"},
                              headers={"X-Actor": "bob"})
    assert again.status_code == 201
    listed = await validations(client, draft_id)
    assert listed["summary"]["verdict"] == "rejected"
    assert listed["records"][0]["review"]["actor"] == "bob"
    async with SessionLocal() as session:
        rows = list((await session.execute(select(CaliberReviewDecision).where(
            CaliberReviewDecision.record_id == record_id)
            .order_by(CaliberReviewDecision.created_at.asc()))).scalars())
    assert [row.decision for row in rows] == ["confirmed", "rejected"]
    assert [row.actor for row in rows] == ["alice", "bob"]

    # 记录不存在时 404；跨草稿也读不到
    missing = await client.post(f"/api/ledger/drafts/{draft_id}/validations/nope/review",
                                json={"decision": "confirmed"}, headers={"X-Actor": "alice"})
    assert missing.status_code == 404


# ==========================================================================
# 6. 比较方法与覆盖判断本身（纯函数，R06 的原值口径）
# ==========================================================================


def fake_record(**over) -> CaliberValidationRecord:
    """一个不落库的记录对象：只用来算覆盖、摘要和读形状。"""
    fields = {"id": "rec-1", "draft_id": "d-1", "workflow_id": "wf-1", "version": 1, "card_id": "card",
              "metric_ids": ["m1"], "definition_hash": "a" * 64, "binding_fingerprints": {"fetch": "b" * 64},
              "semantic_hash": "c" * 64, "execution_hash": "d" * 64, "expectation": None,
              "expectation_hash": "", "expectation_source": "", "sample_id": "", "sample_version": "",
              "inputs": {}, "inputs_hash": "", "method_version": validation.METHOD_VERSION,
              "actual": {}, "status": validation.MATCHED, "problems": [], "trial_id": "t-1", "run_id": "r-1",
              "actor": "alice", "origin": "trial"}
    fields.update(over)
    return CaliberValidationRecord(**fields)


CARD = [{"id": "m1", "name": "退款率", "contract": {"shape": "scalar"}}]


def actual_of_one(*, value=2, rows=1, masked=False, status="ok") -> dict:
    return validation.actual_of(
        metrics=[{"id": "m1", "value": None if (masked or rows == 0) else value, "status": status,
                  "node_id": "fetch", "masked": masked,
                  "quality": {"status": "pass", "checks": []}}],
        queries=[{"node_id": "fetch", "total_rows": rows, "truncated": False}],
        card_metrics_=CARD, metric_ids=["m1"])


def test_the_method_compares_raw_values_over_the_whole_card_and_every_declared_shape():
    """方法版本 1：原值比较（不做隐式舍入）、全指标、全约定形状；没有期望只记 observed。"""
    # 原值：2 与 2.0 同，字符串 "2" 与数字 2 不同（不做隐式转换）
    assert validation.same_value(2, 2.0) is True
    assert validation.same_value(2, "2") is False
    assert validation.same_value(True, 1) is False
    assert validation.same_value(2.25, 2.2500001) is False

    observed = validation.compare(expected=None, actual=actual_of_one(), metric_ids=["m1"],
                                  card_metrics_=CARD)
    assert observed["status"] == validation.OBSERVED
    assert any("没有独立期望" in problem for problem in observed["problems"])

    matched = validation.compare(expected={"m1": 2.0}, actual=actual_of_one(value=2), metric_ids=["m1"],
                                 card_metrics_=CARD)
    assert matched["status"] == validation.MATCHED and matched["actual"]["m1"]["ok"] is True

    # 期望漏了一条：全指标比较，算 mismatched 而不是「只比了几条就通过」
    partial = validation.compare(expected={"other": 1}, actual=actual_of_one(),
                                 metric_ids=["m1", "other"], card_metrics_=CARD, problems=())
    assert partial["status"] == validation.MISMATCHED
    assert any("期望没有覆盖指标" in problem for problem in partial["problems"])

    # 卡上没有可比对的指标：不能是 matched（缺证据不得升级）
    empty = validation.compare(expected={}, actual={}, metric_ids=[], card_metrics_=[])
    assert empty["status"] == validation.UNVERIFIED

    # 运行没成功：failed，且不问期望
    failed = validation.compare(expected={"m1": 2}, actual=actual_of_one(),
                                metric_ids=["m1"], card_metrics_=CARD, run_status="failed")
    assert failed["status"] == validation.FAILED


def test_coverage_tracks_definition_binding_and_semantics_not_layout():
    """覆盖看得是定义/绑定/取数语义三样指纹：布局变更（版本号变了）不作废，删语义字段作废。"""
    record = fake_record()
    current = {"workflow_id": "wf-1", "card_id": "card", "definition_hash": "a" * 64,
               "binding_fingerprints": {"fetch": "b" * 64}, "semantic_hash": "c" * 64}
    assert validation.covers_current(record, current=current) is True
    # 版本号（布局变更带来的）不在判断里
    assert validation.covers_current(fake_record(version=9), current=current) is True
    assert validation.covers_current(record, current={**current, "definition_hash": "z" * 64}) is False
    assert validation.covers_current(record, current={**current, "binding_fingerprints": {}}) is False
    assert validation.covers_current(record, current={**current, "semantic_hash": "z" * 64}) is False
    assert validation.covers_current(fake_record(definition_hash=""), current=current) is False
    # 取数语义指纹**任一侧缺失都算不覆盖**（不得跳过这一项）
    assert validation.covers_current(fake_record(semantic_hash=""), current=current) is False
    assert validation.covers_current(record, current={**current, "semantic_hash": ""}) is False

    # 摘要：只有「覆盖 + matched + 认证确认**全部指标**」才是 verified；missing/stale/部分确认一律不算
    assert validation.summarize([record], current=current)["verdict"] == "unverified"
    confirmed = {"decision": "confirmed", "covers_current": True, "actor": "alice", "scope": ["m1"]}
    assert validation.summarize([record], current=current,
                                reviews={record.id: confirmed})["verdict"] == "verified"
    # 空范围 / 只确认一部分：不产生卡级 verified，未确认的指标要暴露出来
    partial = validation.summarize([record], current=current,
                                   reviews={record.id: {**confirmed, "scope": []}})
    assert partial["verdict"] == "unverified" and partial["confirmed"] == []
    assert partial["unconfirmed"] == ["m1"] and "没被确认" in partial["reason"]
    assert validation.summarize([record], current=current,
                                reviews={record.id: {**confirmed, "scope": ["m2"]}})["verdict"] == "unverified"
    assert validation.summarize([record], current={**current, "definition_hash": "z" * 64},
                                reviews={record.id: {**confirmed, "covers_current": False}})["verdict"] == "stale"
    assert validation.summarize([], current=current)["verdict"] == "none"
    observed = validation.summarize([fake_record(status=validation.OBSERVED)], current=current)
    assert observed["verdict"] == "observed" and observed["unconfirmed"] == ["m1"]
    rejected = validation.summarize([record], current=current,
                                    reviews={record.id: {"decision": "rejected", "covers_current": True,
                                                         "scope": ["m1"]}})
    assert rejected["verdict"] == "rejected"


def test_a_review_decision_needs_its_own_semantic_fingerprint_to_cover_current():
    """审阅决定同样按定义/绑定/取数语义三样指纹判覆盖：缺取数语义（任一侧）都算不覆盖。"""
    from app.db.models import CaliberReviewDecision

    from app.ledger import review

    current = {"workflow_id": "wf-1", "card_id": "card", "definition_hash": "a" * 64,
               "binding_fingerprints": {"fetch": "b" * 64}, "semantic_hash": "c" * 64}
    basis = {"version": 1, "definition_hash": "a" * 64, "binding_fingerprints": {"fetch": "b" * 64},
             "semantic_hash": "c" * 64, "method_version": 1}
    row = CaliberReviewDecision(id="rev-1", draft_id="d-1", record_id="rec-1", workflow_id="wf-1",
                                decision="confirmed", actor="alice", verified=False, scope=["m1"],
                                basis=dict(basis))
    assert review.covers_current(row, current=current) is True
    assert review.covers_current(row, current={**current, "semantic_hash": ""}) is False
    assert review.covers_current(row, current={**current, "semantic_hash": "z" * 64}) is False
    missing = CaliberReviewDecision(id="rev-2", draft_id="d-1", record_id="rec-1", workflow_id="wf-1",
                                    decision="confirmed", scope=["m1"],
                                    basis={**basis, "semantic_hash": ""})
    assert review.covers_current(missing, current=current) is False


async def test_a_failed_trial_keeps_a_stable_identity_and_reading_never_grows_the_ledger(
        client, documents, datasource, monkeypatch):
    """反例（K05 复核）：试运行没起来那一条没有身份时，每读一次就多插一行。

    新试运行（含起草时试运行出错这一条）必须带稳定 trial_id；老草稿里没有身份的试运行，读入口不补记。"""
    async def boom(**kwargs):
        raise RuntimeError("试运行没有起来")

    monkeypatch.setattr(api_ledger, "_run_trial", boom)
    doc = await documents("val-stable-" + uuid.uuid4().hex[:8])
    source_id = await datasource()
    fake_model(monkeypatch, plan_reply(f"{doc}-2"))
    body = await extract(client, doc, source_id)
    draft_id = body["draft_id"]

    detail = (await client.get(f"/api/ledger/drafts/{draft_id}")).json()
    assert detail["trial"]["status"] == "failed" and detail["trial"]["trial_id"]      # 稳定身份
    assert detail["trial"]["error"].startswith("试运行出错")
    first = await validations(client, draft_id)
    assert len(first["records"]) == 1
    [record] = first["records"]
    assert record["trial_id"] == detail["trial"]["trial_id"] and record["status"] == "failed"

    # 反复读：草稿详情 / 试运行 / 验证列表各读几遍，行数不增长（以前每次生成一个随机身份 → 每次多一行）
    for _ in range(3):
        await client.get(f"/api/ledger/drafts/{draft_id}")
        await client.get(f"/api/ledger/drafts/{draft_id}/trial")
        await validations(client, draft_id)
    assert len((await validations(client, draft_id))["records"]) == 1


async def test_a_trial_without_a_stable_identity_is_never_appended_by_reads(client):
    """反例（K05 复核）：老草稿 payload 里那一条没有 trial_id / run_id 的试运行，读入口不得补记。"""
    draft_id = "legacy-trial-" + uuid.uuid4().hex[:8]
    stale_trial = {"version": 1, "graph_hash": "g" * 64, "execution_hash": "e" * 64, "inputs_hash": "i" * 64,
                   "run_id": "", "status": "needs_input", "error": "缺少必填输入：start"}
    async with SessionLocal() as session:
        session.add(CaliberDraft(id=draft_id, title="老草稿的试运行", status="questions_open",
                                 payload={"metrics": [{"id": "m1", "name": "旧指标"}], "trial": stale_trial}))
        await session.commit()
    try:
        first = (await client.get(f"/api/ledger/drafts/{draft_id}")).json()
        assert first["trial"] == stale_trial
        # 三个读入口各读两遍：一条都不补（没有身份就认不出「是不是同一次」）
        for _ in range(2):
            assert (await client.get(f"/api/ledger/drafts/{draft_id}/trial")).status_code == 200
            await validations(client, draft_id)
        listed = await validations(client, draft_id)
        assert listed["records"] == [] and listed["summary"]["verdict"] == "none"
        # 试运行响应里这一段形状照旧，只是没有本次记录：covers_current 是 null（判不了），不是 false，也不顶别人的记录
        trial = (await client.get(f"/api/ledger/drafts/{draft_id}/trial")).json()
        assert trial["validation"]["record"] is None and trial["validation"]["covers_current"] is None
        assert "不能算验证通过" in trial["validation"]["note"]
    finally:
        async with SessionLocal() as session:
            row = await session.get(CaliberDraft, draft_id)
            if row is not None:
                await session.delete(row)
            await session.commit()


async def test_the_trial_validation_block_has_one_shape_and_never_borrows_another_record(
        client, documents, datasource, monkeypatch):
    """反例（K05 复核）：GET 试运行的 validation 各分支形状不一，还会拿别的最新记录冒充本次。

    每个分支都给同一组键；本次没有记录时 record 是 null、covers_current 是 null，绝不退回最新一条。"""
    made = await accepted_draft(client, documents, datasource, monkeypatch)
    draft_id = made["id"]
    keys = {"schema_version", "execution", "status", "checks", "record", "covers_current", "note"}

    ok = (await client.get(f"/api/ledger/drafts/{draft_id}/trial")).json()
    assert set(ok["validation"]) == keys and ok["validation"]["record"]["trial_id"]
    assert ok["validation"]["covers_current"] is True

    # 把 payload 里的试运行换成一条**没有稳定身份**的老写法（run_id 也空）：本次没有记录，
    # 库里明明还有别的记录，这一段也不能拿别的顶上
    async with SessionLocal() as session:
        row = await session.get(CaliberDraft, draft_id)
        keyless = {k: v for k, v in row.payload["trial"].items() if k != "trial_id"}
        keyless.update(run_id="", status="needs_input", error="缺少必填输入：start")
        row.payload = {**row.payload, "trial": keyless}
        await session.commit()
    before = (await validations(client, draft_id))["records"]
    assert before                                              # 库里确实有别的记录
    alone = (await client.get(f"/api/ledger/drafts/{draft_id}/trial")).json()
    assert set(alone["validation"]) == keys
    assert alone["validation"]["record"] is None and alone["validation"]["covers_current"] is None
    assert alone["validation"]["status"] == "unknown"
    assert len((await validations(client, draft_id))["records"]) == len(before)   # 没有补记

    # 换成一个这次还没记过、但有稳定身份的身份：本请求里补记一条，读回来就是它的记录
    async with SessionLocal() as session:
        row = await session.get(CaliberDraft, draft_id)
        row.payload = {**row.payload, "trial": {**keyless, "trial_id": "another-trial"}}
        await session.commit()
    after = (await client.get(f"/api/ledger/drafts/{draft_id}/trial")).json()
    assert set(after["validation"]) == keys
    assert after["validation"]["record"]["trial_id"] == "another-trial"
    assert after["validation"]["record"]["status"] == "unverified"      # 没发起运行，没有可比较的结果
    # 它对着的还是当前这张图，所以覆盖判断是 true——记录里的依据说话，不看它是哪一次
    assert after["validation"]["covers_current"] is True


async def test_a_partial_review_never_makes_the_whole_card_verified(client, documents, datasource, monkeypatch):
    """反例（K05 复核）：空范围 / 只确认一部分时不能给卡级 verified，未确认的指标要说出来。"""
    doc = await documents("val-scope-" + uuid.uuid4().hex[:8])
    source = await datasource()
    one = json.loads(plan_reply(f"{doc}-2"))
    fake_model(monkeypatch, groups_reply(one, baseline_metrics(one)))
    body = await extract(client, doc, source)
    draft_id = body["draft_id"]
    metrics = (await client.get(f"/api/ledger/drafts/{draft_id}")).json()["metrics"]
    plant_evidence(monkeypatch, values={"m1": 2, "m2": 3}, metric_ids=[m["id"] for m in metrics],
                   nodes=nodes_of(metrics))
    trial = await run_trial(client, draft_id, expect={"metrics": {"m1": 2, "m2": 3}, "source": "fixture"})
    record = trial["validation"]["record"]
    assert record["status"] == "matched" and record["metric_ids"] == ["m1", "m2"]

    empty = await client.post(f"/api/ledger/drafts/{draft_id}/validations/{record['id']}/review",
                              json={"decision": "confirmed", "metrics": []}, headers={"X-Actor": "alice"})
    assert empty.status_code == 422 and empty.json()["code"] == "review_scope"

    partial = await client.post(f"/api/ledger/drafts/{draft_id}/validations/{record['id']}/review",
                                json={"decision": "confirmed", "metrics": ["m1"]}, headers={"X-Actor": "alice"})
    assert partial.status_code == 201 and partial.json()["record"]["review"]["scope"] == ["m1"]
    listed = await validations(client, draft_id)
    assert listed["summary"]["verdict"] == "unverified"
    assert listed["summary"]["confirmed"] == ["m1"] and listed["summary"]["unconfirmed"] == ["m2"]
    assert "没被确认" in listed["summary"]["reason"]

    whole = await client.post(f"/api/ledger/drafts/{draft_id}/validations/{record['id']}/review",
                              json={"decision": "confirmed"}, headers={"X-Actor": "bob"})
    assert whole.status_code == 201 and sorted(whole.json()["record"]["review"]["scope"]) == ["m1", "m2"]
    listed = await validations(client, draft_id)
    assert listed["summary"]["verdict"] == "verified" and listed["summary"]["unconfirmed"] == []


async def test_an_unreadable_baseline_artifact_is_never_reported_as_known(client, documents, datasource,
                                                                         monkeypatch):
    """反例（K05 复核）：基线工件被删掉或改过时，读草稿不能报 known，也不能抛出去把页面弄崩。"""
    from app.core.config import settings

    made = await accepted_draft(client, documents, datasource, monkeypatch)
    baseline = made["field_baseline"]
    assert baseline["status"] == "known" and baseline["readable"] is True
    artifact = baseline["artifact_id"]
    path = settings.data_dir / "artifacts" / artifact[:2] / f"{artifact}.json"

    original = path.read_bytes()
    try:
        path.write_text('{"kind": "caliber_field_baseline", "tampered": true}', encoding="utf-8")
        tampered = (await client.get(f"/api/ledger/drafts/{made['id']}")).json()
        assert tampered["field_baseline"]["status"] == "baseline_unknown"
        assert tampered["field_baseline"]["readable"] is False
        assert tampered["field_baseline"]["artifact_id"] == artifact
        assert "读不回来" in tampered["field_baseline"]["reason"]
        # 验证列表和试运行接口照常给（读页面不崩）
        listed = (await client.get(f"/api/ledger/drafts/{made['id']}/validations")).json()
        assert listed["field_baseline"]["status"] == "baseline_unknown"
        assert (await client.get(f"/api/ledger/drafts/{made['id']}/trial")).status_code == 200

        path.unlink()
        missing = (await client.get(f"/api/ledger/drafts/{made['id']}")).json()
        assert missing["field_baseline"]["status"] == "baseline_unknown"
        assert missing["field_baseline"]["readable"] is False
    finally:
        path.write_bytes(original)
    assert (await client.get(f"/api/ledger/drafts/{made['id']}")).json()["field_baseline"]["status"] == "known"


# ==========================================================================
# 收尾：本模块造的行（草稿由夹具清，记录/审阅跟着草稿级联，这里兜一下）
# ==========================================================================


@pytest.fixture(autouse=True)
async def _cleanup_records():
    yield
    async with SessionLocal() as session:
        drafts = list((await session.execute(select(CaliberDraft.id))).scalars())
        if drafts:
            await session.execute(delete(CaliberReviewDecision).where(
                CaliberReviewDecision.draft_id.in_(drafts)))
            await session.execute(delete(CaliberValidationRecord).where(
                CaliberValidationRecord.draft_id.in_(drafts)))
        await session.commit()
