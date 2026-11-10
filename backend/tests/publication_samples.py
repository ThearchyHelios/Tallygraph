"""K07 发布依据与用途判定测试共用的帮手：建一份「已接管」的口径资产、放下验证记录与审阅、按入口发布。

资产的形状就是文档起草建出来的那种（tags=["doc_draft"]，app/apps/versions.taken_over）：一张源头口径卡 `card`、
一个取数节点 `fetch`（查虚构店铺库）、一张口径「周销售」。验证记录和审阅走 K05 的真实落库入口
（app/ledger/validation.append、app/ledger/review.decide），发布走真实的发布入口（HTTP 发布接口 / commit_publish），
不手写行、不伪造历史。
"""
from __future__ import annotations

import asyncio
import uuid
from typing import Any

from httpx import AsyncClient
from sqlalchemy import select

from app.core.artifact_store import graph_hash
from app.db.base import SessionLocal
from app.db.models import CaliberDraft, Run, Workflow, WorkflowPublication, WorkflowVersion
from app.ledger import review as ledger_review
from app.ledger import validation
from tests import ledger_samples as S

CARD = "card"
#: 人工确认过的独立期望（K05 的 expectation：来源 + 逐条给全卡指标）
EXPECTATION = {"source": "human_confirmed", "metrics": {"gmv": 2}, "note": "独立核算：十月两单"}


async def make_workflow(*, graph: dict | None = None, tags: list[str] | None = None, version: int = 1) -> dict:
    """建一份口径资产（工作流 + v1 快照 + 一份草稿行），还没发布。tags 缺省是文档起草的产物（已接管）。"""
    graph = graph or S.caliber_graph()
    async with SessionLocal() as session:
        workflow = Workflow(name=f"周销售-{uuid.uuid4().hex[:6]}", graph=graph, status="draft",
                            tags=tags if tags is not None else ["doc_draft"], version=version)
        session.add(workflow)
        await session.flush()
        session.add(WorkflowVersion(workflow_id=workflow.id, version=version, graph=graph,
                                    graph_hash=graph_hash(graph), note="从业务文档分组生成"))
        draft = CaliberDraft(title="周销售口径草稿", workflow_id=workflow.id, status="ready")
        session.add(draft)
        await session.commit()
        return {"workflow_id": workflow.id, "draft_id": draft.id, "graph": graph, "version": version}


async def add_version(asset: dict, graph: dict, *, version: int, note: str = "改过") -> dict:
    """加一版（图上改了东西之后的保存结果），并把工作流当前版本推上去。"""
    async with SessionLocal() as session:
        workflow = await session.get(Workflow, asset["workflow_id"])
        workflow.graph = graph
        workflow.version = version
        session.add(WorkflowVersion(workflow_id=workflow.id, version=version, graph=graph,
                                    graph_hash=graph_hash(graph), note=note))
        await session.commit()
    return {**asset, "graph": graph, "version": version}


async def add_record(asset: dict, *, graph: dict | None = None, card_id: str = CARD, status: str = "matched",
                     metric_ids: tuple[str, ...] = ("gmv",), version: int | None = None) -> str:
    """按 K05 的落库入口追加一条验证记录。返回记录 id。"""
    graph = graph or asset["graph"]
    materials = validation.materials(graph, card_id, workflow_id=asset["workflow_id"])
    record_id = await validation.append(
        draft_id=asset["draft_id"], workflow_id=asset["workflow_id"], version=version or asset["version"],
        card_id=card_id, metric_ids=list(metric_ids), basis=materials, inputs={"start": "2026-10-01"},
        inputs_hash="inputs-1", trial_id=uuid.uuid4().hex, run_id="", actor="试跑人", status=status, problems=[],
        expectation=EXPECTATION if status == "matched" else None,
        actual={mid: {"value": 2, "status": "ok"} for mid in metric_ids})
    assert record_id, "验证记录没落下"
    return record_id


async def confirm(asset: dict, record_id: str, *, scope: list[str] | None = None, verified: bool = True,
                  decision: str = "confirmed") -> str:
    """按 K05 的审阅入口追加一条决定（主体、范围、所审依据）。返回决定 id。"""
    async with SessionLocal() as session:
        record = await validation.get_record(session, asset["draft_id"], record_id)
        row = await ledger_review.decide(session, draft_id=asset["draft_id"], record=record, decision=decision,
                                         actor="审核人", verified=verified, scope=scope, note="看过了")
        await session.commit()
        return row.id


async def publish(client: AsyncClient, asset: dict, *, version: int | None = None, level: str = "published") -> dict:
    """走 HTTP 发布接口（唯一的发布入口最终都到这里）。"""
    body = {"level": level, "version": version or asset["version"]}
    res = await client.post(f"/api/workflows/{asset['workflow_id']}/publish", json=body,
                            headers={"X-Actor": "%E5%B0%8F%E7%8E%8B"})
    assert res.status_code == 200, res.text
    return res.json()


async def events_of(workflow_id: str, version: int | None = None) -> list[WorkflowPublication]:
    from app.apps import publications

    async with SessionLocal() as session:
        return await publications.events(session, workflow_id, version)


async def latest_event(workflow_id: str, version: int) -> WorkflowPublication:
    from app.apps import publications

    async with SessionLocal() as session:
        row = await publications.latest(session, workflow_id, version)
        assert row is not None, f"「{workflow_id}」v{version} 没有发布事件"
        return row


async def persisted(workflow_id: str) -> Workflow:
    async with SessionLocal() as session:
        return await session.get(Workflow, workflow_id)


async def version_row(workflow_id: str, version: int) -> WorkflowVersion:
    async with SessionLocal() as session:
        return (await session.execute(select(WorkflowVersion).where(
            WorkflowVersion.workflow_id == workflow_id, WorkflowVersion.version == version))).scalar_one()


def report_model(monkeypatch) -> None:
    """假模型：图里那张报告节点（指令「写周销售」）引用口径卡的指标，别的照旧。正式运行才跑得完。"""
    from langchain_core.messages import AIMessage

    from app.providers import mock_model

    def decide(self, messages):
        text = "\n".join(str(getattr(m, "content", "")) for m in messages)
        return AIMessage(content="周销售 [[m:gmv]]。" if "写周销售" in text else "这周的两单")

    monkeypatch.setattr(mock_model.MockChatModel, "_decide", decide)


async def launch_formal(asset: dict, *, version: int | None = None, actor: str = "发布人",
                        input_payload: dict[str, Any] | None = None, origin_kind: str | None = "launcher") -> Run:
    """从已发布的版本发起一次正式运行（engine/launch.launch_run：正式身份的唯一入口）。"""
    from app.engine.launch import launch_run

    async with SessionLocal() as session:
        return await launch_run(session, workflow_id=asset["workflow_id"], run_class="formal",
                                version=version or asset["version"], actor=actor,
                                input_payload=input_payload or {"start": "2026-09-01", "end": "2026-09-08"},
                                origin_kind=origin_kind)


async def wait_run(run_id: str, timeout: float = 20.0) -> Run:
    """等一次运行跑到终态（测试收尾不留挂着的任务）。"""
    for _ in range(int(timeout / 0.05)):
        async with SessionLocal() as session:
            row = await session.get(Run, run_id)
        if row.status in ("succeeded", "failed", "cancelled") and row.manifest_seq is not None:
            return row
        await asyncio.sleep(0.05)
    raise AssertionError(f"运行 {run_id} 没有结束：{row.status}")


def publication_of(run: Run) -> dict[str, Any]:
    """运行来源里记下的发布依据（K07：正式运行固定了哪一版、哪份清单、什么身份）。"""
    origin = run.origin if isinstance(run.origin, dict) else {}
    return origin.get("publication") or {}


async def issue_plan(session: Any, asset: dict, *, version: int | None = None) -> Any:
    """按口径出具这一次的打算（走 K07 那条预检 ledger/issue.check_target，builder 身份）。"""
    from app.api.deps import Principal
    from app.ledger import issue as ledger_issue
    from app.ledger.labels import caliber_key

    number = version or asset["version"]
    workflow = await session.get(Workflow, asset["workflow_id"])
    plan = ledger_issue.Plan(run_id="source-run", workflow_id=asset["workflow_id"], version=number,
                             caliber=caliber_key(asset["workflow_id"], number))
    await ledger_issue.check_target(session, plan, workflow,
                                    principal=Principal(username="boss", role="builder", mode="off", via="off"),
                                    source_run="source-run")
    return plan
