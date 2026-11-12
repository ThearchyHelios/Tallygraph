"""起草任务的持久化生命周期；重启后标中断，用户重试才继续调用模型。"""
from __future__ import annotations

from typing import Any, Awaitable, Callable

from sqlalchemy import select, update

from app.core import job_runtime as runtime
from app.db.base import SessionLocal, current_owner, utcnow
from app.db.models import CaliberDraft, CaliberDraftJob, DataSource

executor: Callable[[str], Awaitable[None]] | None = None


async def finish(session: Any, job_id: str, status: str, error: str | None = None) -> None:
    changed = await session.execute(update(CaliberDraftJob).where(
        CaliberDraftJob.id == job_id, CaliberDraftJob.status.in_(runtime.ACTIVE)
    ).values(status=status, error=error, finished_at=utcnow()).execution_options(synchronize_session=False))
    if changed.rowcount and status != "succeeded":
        await session.execute(update(CaliberDraft).where(CaliberDraft.id == job_id, CaliberDraft.status == "drafting")
                              .values(status="failed", error=error).execution_options(synchronize_session=False))
    await session.commit()


class DraftJob(runtime.JobRun):
    model = CaliberDraftJob
    kind = "caliber_draft"

    def lane_of(self, job: Any) -> None:
        return None

    async def _run(self) -> None:
        if not await self._write(runtime.claim, CaliberDraftJob, self.job_id):
            return
        if executor is None:
            raise runtime.JobFailed("起草服务尚未初始化")
        await executor(self.job_id)
        async with SessionLocal() as session:
            draft = await session.get(CaliberDraft, self.job_id)
            error = draft.error if draft else "草稿已删除"
            status = "succeeded" if draft is not None and draft.status not in ("failed", "drafting", "rejected") else "failed"
        await self._write(finish, self.job_id, status, error)

    async def _stopped(self) -> None:
        interrupted = self.stop_reason != "user"
        await self._write(finish, self.job_id, "interrupted" if interrupted else "cancelled",
                          "服务中断，已保留起草进度，可重试" if interrupted else "起草已停止，已保留进度")

    async def _failed(self, reason: str) -> None:
        await self._write(finish, self.job_id, "failed", reason)

    async def _crashed(self, exc: Exception) -> None:
        await self._failed(f"起草任务失败：{exc}")


manager = runtime.JobManager(DraftJob, label="口径起草任务", name="caliber_draft")


async def start(job_id: str) -> None:
    # 重试复用草稿 id；上一尝试可能刚写完终态、还在释放资源，先等它真正退出。
    await manager.wait(job_id)
    manager.start(job_id)
    await manager.wait(job_id)


async def startup(*, live: Any = None) -> int:
    async def close(session: Any, job_id: str) -> None:
        await finish(session, job_id, "interrupted", "服务中断，已保留起草进度，可重试")

    count = await runtime.interrupt_on_startup(CaliberDraftJob, close, running=manager.running, live=live)
    # 升级前由 BackgroundTasks 启动、没有任务行的草稿也不能永远停在起草中。
    async with SessionLocal() as session:
        result = await session.execute(update(CaliberDraft).where(
            CaliberDraft.status == "drafting", ~CaliberDraft.id.in_(select(CaliberDraftJob.id))
        ).values(status="failed", error="历史起草任务已中断，可重试").execution_options(synchronize_session=False))
        await session.commit()
        return count + result.rowcount


async def retry(session: Any, draft: CaliberDraft) -> None:
    from app.ledger.authoring import require_authoring_source

    require_authoring_source(await session.get(DataSource, draft.datasource_id) if draft.datasource_id else None)
    recovering = bool((draft.payload or {}).get("finalization_pending"))
    if draft.status != "failed" or (draft.workflow_id and not recovering):
        raise ValueError("只有尚未生成工作流的失败草稿可以重试")
    claimed = await session.execute(update(CaliberDraft).where(
        CaliberDraft.id == draft.id, CaliberDraft.status == "failed", CaliberDraft.workflow_id == draft.workflow_id,
        CaliberDraft.updated_at == draft.updated_at
    ).values(status="drafting", error=None).execution_options(synchronize_session=False))
    if claimed.rowcount != 1:
        raise ValueError("草稿状态已经变化，请刷新后再重试")
    job = await session.get(CaliberDraftJob, draft.id)
    if job is None:
        session.add(CaliberDraftJob(id=draft.id))
    else:
        result = await session.execute(update(CaliberDraftJob).where(
            CaliberDraftJob.id == draft.id, CaliberDraftJob.status.not_in(runtime.ACTIVE)
        ).values(status="queued", owner=current_owner(), attempt=CaliberDraftJob.attempt + 1,
                 error=None, started_at=None, finished_at=None).execution_options(synchronize_session=False))
        if result.rowcount != 1:
            raise ValueError("这份草稿已经在起草，请等待当前任务完成")
    draft.status, draft.error = "drafting", None
    await session.commit()
