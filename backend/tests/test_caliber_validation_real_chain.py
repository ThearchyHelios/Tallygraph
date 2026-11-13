"""K05 的真实链路：真引擎、真 SQLite 数据源、真 HTTP —— 试运行 → matched → 认证审阅 → verified → 发布。

**这份文件不换 `_trial_metrics` / `_trial_queries` 的桩**：走的是「起草出来的那种口径卡 + 真跑一次探针图」的完整
路径，考的就是「真实运行的证据能不能被验证记录认出来」。以前的阻断点正是这里：界面那份指标投影里没有取数节点，
而验证记录按节点把指标对到查询快照上——对不上就一律判不了形状，matched / verified / 发布永远到不了。

覆盖的形状（都是真跑出来的）：恰好一行（matched）、多行（标量取数有歧义）、空结果（没有可取的数）。

发布那一步证明的是：**认证审阅过的这条记录，绑的是被发布的那一版定义与绑定**（发布不改图，所以记录照旧覆盖）。
"""
from __future__ import annotations

import copy
import uuid

import pytest
from sqlalchemy import delete, select

from app.db.base import SessionLocal
from app.db.models import CaliberDraft, Run, Workflow, WorkflowVersion
from app.engine.runner import run_manager
from app.ledger import validation
from tests import ledger_samples as S
from tests.test_caliber_draft_reads import attached_draft  # noqa: F401 - 帮手共用
from tests.test_ledger_docdraft_api import client  # noqa: F401 - fixture 和别的口径用例共用

#: 这次起草的口径：九月一号到八号，契约数据集里 SUM(amount) = 170.5（独立期望照这个写，不照这次运行的结果抄）
EXPECTED = 170.5
WINDOW = {"start": "2026-09-01", "end": "2026-09-08"}

#: 这一份文件自己建的工作流（收尾时连草稿、版本、记录一起清掉）
OWN_PREFIX = "K05 真实链路 "


def real_graph(sql: str, *, fields: list[dict] | None = None) -> dict:
    """一份像**起草产物**那样已接管的图：入口 → 取数 → 口径卡（标量契约）。

    契约是照着 app/ledger/draftplan 建图时写的来（`contract: {"shape": "scalar"}`）：起草出来的指标都声明标量，
    多行和空结果才会被引擎和验证记录判成形状不符。
    """
    return S.chain(
        S.node("start", "input", fields=fields if fields is not None else [
            {"name": "start", "required": True}, {"name": "end", "required": True}]),
        S.node("fetch", "tool", tool=S.tool(), args={"sql": sql}),
        S.node("card", "metrics", caliber="周销售", caliber_version="v1",
               metrics=[{"id": "gmv", "name": "销售额", "unit": "元", "decimals": 2,
                         "expression": "cell(nodes.fetch, 0, 'gmv')", "contract": {"shape": "scalar"}}]))


@pytest.fixture(autouse=True)
async def engine_and_source(tmp_path):
    await run_manager.setup()
    await S.fresh_source(tmp_path / "shop.db")
    yield
    await run_manager.shutdown()
    async with SessionLocal() as session:
        ids = list((await session.execute(
            select(Workflow.id).where(Workflow.name.like(f"{OWN_PREFIX}%")))).scalars())
        # 试运行留下的运行行：工作流删掉之后 workflow_id 会被置空，按当时记下的工作流名清掉
        await session.execute(delete(Run).where(Run.workflow_name.like(f"{OWN_PREFIX}%")))
        if ids:
            await session.execute(delete(CaliberDraft).where(CaliberDraft.workflow_id.in_(ids)))
            await session.execute(delete(WorkflowVersion).where(WorkflowVersion.workflow_id.in_(ids)))
            await session.execute(delete(Workflow).where(Workflow.id.in_(ids)))
        await session.commit()


async def real_draft(*, sql: str | None = None) -> dict:
    """一份像起草产物那样已接管、指向真实数据源的草稿（图里就一张口径卡 + 一次取数）。"""
    made = await attached_draft(graph=real_graph(sql or S.TEMPLATE),
                                payload={"sql": S.TEMPLATE, "entry_fields": [{"name": "start"}, {"name": "end"}]})
    async with SessionLocal() as session:
        workflow = await session.get(Workflow, made["workflow_id"])
        workflow.name = f"{OWN_PREFIX}{uuid.uuid4().hex[:6]}"
        await session.commit()
    return made


async def validations(client, draft_id: str) -> dict:
    return (await client.get(f"/api/ledger/drafts/{draft_id}/validations")).json()


async def repoint(client, body: dict, sql: str) -> int:
    """把这份草稿的取数换成另一句（画布那种改法），返回新版本号。"""
    saved = await client.patch(f"/api/workflows/{body['workflow_id']}",
                               json={"graph": real_graph(sql), "expected_revision": body["revision"]})
    assert saved.status_code == 200, saved.text
    return saved.json()["version"]


async def test_a_real_trial_matches_confirms_verifies_and_survives_publication(client):
    """真跑一次：独立期望对得上 → matched；认证审阅（全指标）→ verified；发布的那一版和记录绑的是同一份依据。"""
    body = await real_draft()
    done = await client.post(f"/api/ledger/drafts/{body['id']}/trial",
                             json={"inputs": WINDOW,
                                   "expect": {"metrics": {"gmv": EXPECTED}, "source": "fixture",
                                              "sample_id": "sept-2026", "sample_version": "1"}})
    assert done.status_code == 200, done.text
    trial = done.json()
    assert trial["status"] == "succeeded", trial.get("error")
    # 真实运行交回的指标值就是契约数据集里那一份（不是桩）
    assert float(trial["metrics"][0]["value"]) == EXPECTED
    assert "node_id" not in trial["metrics"][0]          # 关联网不放给用户看的响应

    record = trial["validation"]["record"]
    assert record["status"] == "matched", record["problems"]
    assert record["problems"] == []
    assert record["metric_ids"] == ["gmv"]
    assert record["expected"]["metrics"] == {"gmv": EXPECTED}
    assert record["actual"]["gmv"]["value"] == EXPECTED
    assert record["actual"]["gmv"]["rows"] == 1 and record["actual"]["gmv"]["shape_ok"] is True
    assert record["actual"]["gmv"]["ok"] is True
    assert record["covers_current"] is True

    # 对得上不等于批准：还没有审阅
    listed = await validations(client, body["id"])
    assert listed["summary"]["status"] == "matched" and listed["summary"]["verdict"] == "unverified"

    confirmed = await client.post(
        f"/api/ledger/drafts/{body['id']}/validations/{record['id']}/review",
        json={"decision": "confirmed", "note": "按独立算出来的九月预期核过"}, headers={"X-Actor": "alice"})
    assert confirmed.status_code == 201, confirmed.text
    review = confirmed.json()["record"]["review"]
    assert review["decision"] == "confirmed" and review["scope"] == ["gmv"]
    listed = await validations(client, body["id"])
    assert listed["summary"]["verdict"] == "verified" and listed["summary"]["unconfirmed"] == []

    # 发布：这一版就是被审阅过的那一份定义与绑定；发布不改图，所以记录照旧覆盖
    published = await client.post(f"/api/workflows/{body['workflow_id']}/publish",
                                  json={"level": "published"})
    assert published.status_code == 200 and published.json()["ok"], published.text
    detail = (await client.get(f"/api/ledger/drafts/{body['id']}")).json()
    assert detail["validation"]["verdict"] == "verified"
    assert detail["validation"]["covers_current"] is True
    async with SessionLocal() as session:
        row = (await session.execute(select(WorkflowVersion).where(
            WorkflowVersion.workflow_id == body["workflow_id"],
            WorkflowVersion.version == body["revision"]))).scalar_one()
    assert row.level == "published"
    # 被发布的这份图和记录里的定义依据是同一份（发布依据和验证依据对得上）
    assert validation.definition_hash(row.graph, "card") == record["definition_hash"]
    assert (await validations(client, body["id"]))["records"][0]["id"] == record["id"]


async def test_a_real_multi_row_result_does_not_pass_on_the_first_row(client):
    """多行形状（真跑）：标量指标取回两行时不能只看第一行，记 mismatched 并把「恰好一行」说清楚。"""
    body = await real_draft()
    sql = ("SELECT o.region AS gmv FROM orders o WHERE o.status = 'paid' "
           "AND o.paid_at >= '{{ input.start }}' AND o.paid_at < '{{ input.end }}'")
    version = await repoint(client, body, sql)
    done = await client.post(f"/api/ledger/drafts/{body['id']}/trial",
                             json={"inputs": WINDOW, "expect": {"metrics": {"gmv": EXPECTED}}})
    assert done.status_code == 200, done.text
    trial = done.json()
    assert trial["status"] == "succeeded", trial.get("error")
    [query] = trial["queries"]
    assert query["total_rows"] > 1
    record = trial["validation"]["record"]
    assert record["version"] == version
    assert record["status"] == "mismatched", record["problems"]
    assert record["actual"]["gmv"]["rows"] > 1 and record["actual"]["gmv"]["shape_ok"] is False
    assert any("恰好一行" in problem for problem in record["problems"])


async def test_a_real_empty_result_never_counts_as_a_pass(client):
    """空结果形状（真跑）：查询一行都没有时（0 行）不能当作取到了值，记录不会是 matched。"""
    body = await real_draft()
    # 非聚合取数：窗口里没有行时结果就是 0 行（聚合在 0 行上仍交回一行 NULL，另按「没有值」判）
    await repoint(client, body, "SELECT o.region AS gmv FROM orders o WHERE 1 = 0 "
                                "AND o.paid_at >= '{{ input.start }}' AND o.paid_at < '{{ input.end }}'")
    done = await client.post(f"/api/ledger/drafts/{body['id']}/trial",
                             json={"inputs": WINDOW, "expect": {"metrics": {"gmv": EXPECTED}}})
    assert done.status_code == 200, done.text
    trial = done.json()
    record = trial["validation"]["record"]
    assert record is not None
    assert record["status"] != "matched"
    actual = record["actual"].get("gmv") or {}
    if record["status"] == "mismatched":
        assert actual.get("rows") == 0 and actual.get("shape_ok") is False
        assert any("空结果" in problem or "0 行" in problem for problem in record["problems"])
    else:
        # 这条取数让口径卡算不出值（取不到第一格）时，整个试运行就是 failed：同样不能算通过
        assert record["status"] == "failed"
        assert any("没有成功" in problem or "没有结果" in problem for problem in record["problems"])


async def test_a_real_aggregate_over_no_rows_is_not_a_pass(client):
    """聚合查询在空窗口上仍交回一行 NULL：有行、没有值，也不算通过（不能拿 NULL 当 0）。"""
    body = await real_draft()
    await repoint(client, body, "SELECT SUM(amount) AS gmv FROM orders WHERE 1 = 0 "
                                "AND paid_at >= '{{ input.start }}' AND paid_at < '{{ input.end }}'")
    done = await client.post(f"/api/ledger/drafts/{body['id']}/trial",
                             json={"inputs": WINDOW, "expect": {"metrics": {"gmv": EXPECTED}}})
    assert done.status_code == 200, done.text
    record = done.json()["validation"]["record"]
    assert record["status"] == "mismatched", record["problems"]
    assert record["actual"]["gmv"]["value"] is None
    assert any("没有取到值" in problem for problem in record["problems"])
