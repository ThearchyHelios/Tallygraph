from __future__ import annotations

import asyncio
import copy
import json

import pytest
from sqlalchemy import select

from app.db.base import SessionLocal
from app.db.models import CaliberDraftJob, Workflow
from app.ledger import draftplan, jobs
from tests.test_ledger_docdraft_api import (client, datasource, documents, extract, fake_model, fake_trial, plan_reply)
from tests.test_caliber_regressions import draft


@pytest.mark.sqlite_only
async def test_concurrent_draft_edit_is_not_overwritten_by_trial_completion(
        client, documents, datasource, monkeypatch):
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.api.ledger import _save_trial
    from app.db.models import CaliberDraft

    body = await draft(client, documents, datasource, monkeypatch)
    execute = AsyncSession.execute
    raced = False

    async def concurrent(session, statement, *args, **kwargs):
        nonlocal raced
        if (not raced and getattr(statement, "is_update", False)
                and statement.table.name == CaliberDraft.__tablename__):
            raced = True
            async with SessionLocal() as other:
                row = await other.get(CaliberDraft, body["id"])
                row.payload = {**row.payload, "guidance": "刚提交的审阅意见"}
                await other.commit()
        return await execute(session, statement, *args, **kwargs)

    monkeypatch.setattr(AsyncSession, "execute", concurrent)
    assert not await _save_trial(body["id"], {**body["trial"], "error": "较早试跑的结果"})
    assert raced
    async with SessionLocal() as session:
        current = await session.get(CaliberDraft, body["id"])
        assert current.payload["guidance"] == "刚提交的审阅意见"
        assert current.payload["trial"] == body["trial"]


async def test_layout_save_keeps_trial_evidence_but_sql_change_invalidates(client, documents, datasource, monkeypatch):
    body = await draft(client, documents, datasource, monkeypatch)
    published = await client.post(f"/api/workflows/{body['workflow_id']}/publish", json={"level": "published"})
    assert published.status_code == 200 and published.json()["ok"]
    async with SessionLocal() as session:
        workflow = await session.get(Workflow, body["workflow_id"])
        graph = copy.deepcopy(workflow.graph)
    graph["nodes"][0]["position"] = {"x": 100, "y": 200}
    saved = await client.patch(f"/api/workflows/{body['workflow_id']}", json={"graph": graph})
    assert saved.status_code == 200
    assert saved.json()["same_as_published"] is True
    result = (await client.get(f"/api/ledger/drafts/{body['id']}/trial")).json()
    assert result["status"] == "succeeded" and result["version"] == 1
    fetch = next(n for n in graph["nodes"] if n["id"] == "fetch")
    fetch["data"]["config"]["args"]["sql"] = "SELECT 9 AS refund_rate FROM orders"
    changed = await client.patch(f"/api/workflows/{body['workflow_id']}", json={"graph": graph})
    assert changed.json()["same_as_published"] is False
    result = (await client.get(f"/api/ledger/drafts/{body['id']}/trial")).json()
    assert result["status"] == "stale" and result["metrics"] == []


async def test_interrupted_authoring_resumes_completed_groups_only_for_same_material(
        client, documents, datasource, monkeypatch):
    doc = await documents("architecture-resume")
    source = await datasource()
    group = json.loads(plan_reply(f"{doc}-2"))
    calls = []

    async def interrupted(_session, **kwargs):
        calls.append(list(kwargs["covered"]))
        if len(calls) == 1:
            return {"groups": [group], "uncovered": ["第二个指标"]}
        raise asyncio.CancelledError

    monkeypatch.setattr(draftplan, "plan_groups", interrupted)
    made = await extract(client, doc, source)
    draft_id = made["draft_id"]
    body = (await client.get(f"/api/ledger/drafts/{draft_id}")).json()
    assert body["status"] == "failed" and body["job"]["status"] == "interrupted"
    assert body["groups"] and body["material_hash"]
    second = copy.deepcopy(group)
    second["metrics"][0].update(id="m2", name="第二个指标")

    async def resume(_session, **kwargs):
        assert kwargs["covered"] == [group["metrics"][0]["name"]]
        return {"groups": [second], "uncovered": []}

    monkeypatch.setattr(draftplan, "plan_groups", resume)
    reply = await client.post(f"/api/ledger/drafts/{draft_id}/retry", json={})
    assert reply.status_code == 200, reply.text
    body = (await client.get(f"/api/ledger/drafts/{draft_id}")).json()
    assert body["status"] == "ready" and len(body["metrics"]) == 2
    assert body["job"]["attempt"] == 2 and body["job"]["status"] == "succeeded"
    assert (await client.post(f"/api/ledger/drafts/{draft_id}/retry", json={})).status_code == 409


async def test_parallel_group_repairs_have_independent_sessions(client, documents, datasource, monkeypatch):
    doc = await documents("architecture-sessions")
    source = await datasource()
    group = json.loads(plan_reply(f"{doc}-2"))
    broken = [{**copy.deepcopy(group), "sql": "SELECT * FROM unknown_table"} for _ in range(2)]
    broken[1]["metrics"][0].update(id="m2", name="另一个指标")
    sessions = []

    async def plan(_session, **kwargs):
        return {"groups": broken, "uncovered": []}

    async def repair(session, **kwargs):
        sessions.append(session)
        await session.execute(select(Workflow.id).limit(1))
        await asyncio.sleep(.01)
        return {**kwargs["group"], "sql": group["sql"]}

    monkeypatch.setattr(draftplan, "plan_groups", plan)
    monkeypatch.setattr(draftplan, "repair_group", repair)
    made = await extract(client, doc, source)
    body = (await client.get(f"/api/ledger/drafts/{made['draft_id']}")).json()
    assert len(sessions) == 2 and sessions[0] is not sessions[1]
    assert body["status"] == "ready" and len(body["metrics"]) == 2


async def test_startup_marks_orphaned_job_interrupted_and_preserves_progress(client, documents, datasource, monkeypatch):
    from app.db.models import CaliberDraft

    async with SessionLocal() as session:
        row = CaliberDraft(title="中断任务", payload={"progress": {"round": 1}}, status="drafting")
        session.add(row)
        await session.flush()
        session.add(CaliberDraftJob(id=row.id, status="running"))
        await session.commit()
        draft_id = row.id
    await jobs.startup()
    body = (await client.get(f"/api/ledger/drafts/{draft_id}")).json()
    assert body["status"] == "failed" and body["job"]["status"] == "interrupted"
    assert body["progress"]["round"] == 1 and body["job"]["retryable"]


async def test_stop_api_cancels_the_worker_and_preserves_checkpoint(client, monkeypatch):
    from app.db.models import CaliberDraft

    checkpoint = {"groups": [{"metrics": [{"id": "m1", "name": "已完成指标"}]}]}
    async with SessionLocal() as session:
        row = CaliberDraft(title="停止任务", payload={"checkpoint": checkpoint}, status="drafting")
        session.add(row)
        await session.flush()
        session.add(CaliberDraftJob(id=row.id))
        await session.commit()
        draft_id = row.id
    entered = asyncio.Event()

    async def generating(_draft_id):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(jobs, "executor", generating)
    jobs.manager.start(draft_id)
    try:
        await asyncio.wait_for(entered.wait(), 3)
        edit = await client.patch(f"/api/ledger/drafts/{draft_id}", json={"metrics": []})
        assert edit.status_code == 409, edit.text
        result = await client.post(f"/api/ledger/drafts/{draft_id}/stop", json={})
        assert result.status_code == 200, result.text
        body = result.json()
        assert body["status"] == "failed" and body["job"]["status"] == "cancelled"
        assert body["checkpoint"] == checkpoint and body["job"]["retryable"]
        assert not jobs.manager.running(draft_id)
    finally:
        await jobs.manager.stop(draft_id, "user")
