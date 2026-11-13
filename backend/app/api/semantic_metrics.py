"""独立语义指标库（SemanticMetric）与后台口径文档一键拆解工作流 REST 接口（搭建者及以上权限）。

- GET /api/semantic-metrics：列出语义指标卡片（支持按 source_id、status、q 过滤）
- POST /api/semantic-metrics：创建单条语义指标卡片（支持单表 expression 与多表参数化 sql_template，保存即刻生效）
- POST /api/semantic-metrics/batch：批量保存勾选的候选语义指标卡片
- PUT /api/semantic-metrics/{metric_id}：编辑语义指标卡片（支持乐观锁 expected_version）
- DELETE /api/semantic-metrics/{metric_id}：删除语义指标卡片
- POST /api/semantic-metrics/preview：在线试算（支持已落库 metric_id 或草稿 definition 实时预览 SQL 与结果表）
- POST /api/semantic-metrics/suggest：同步提取候选指标卡片（兼容旧调用）
- POST /api/semantic-metrics/attachments：上传业务口径文档或编码对照表附件（.md/.txt/.pdf/.docx/.xlsx/.csv）
- POST /api/semantic-metrics/decompose-jobs：启动后台文档一键拆解 / 智能提取指标任务（实时阶段进度，关闭弹窗继续执行）
- GET /api/semantic-metrics/decompose-jobs/active：查询正在运行与最近一次拆解任务（供口径台账页顶部横幅轮询）
- GET /api/semantic-metrics/decompose-jobs/{job_id}：查询单个拆解任务的实时进度、分步耗时与待审阅结果
- POST /api/semantic-metrics/decompose-jobs/{job_id}/cancel：取消排队或执行中的拆解任务
- POST /api/semantic-metrics/decompose-jobs/{job_id}/apply：一键应用审阅勾选的「字段目录补丁 + 指标口径 + 标准业务实体」
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, Header, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.coded import CodedHTTPException
from app.api.deps import actor_of
from app.data import entity_docs
from app.data.semantic_decompose import (
    DECOMPOSE_BUSY_CODE,
    DECOMPOSE_FINISHED_CODE,
    DECOMPOSE_NOT_FOUND_CODE,
    DecomposeBusy,
    DecomposeFinished,
    DecomposeNotFound,
    apply_decompose_job,
    cancel_decompose_job,
    create_decompose_job,
    get_active_or_latest_job,
    get_job,
    job_to_dict,
    list_jobs,
)
from app.data.semantic_metrics import (
    MetricConflict,
    MetricInvalid,
    MetricNotFound,
    create_metric,
    delete_metric,
    get_metric,
    list_metrics,
    metric_to_dict,
    preview_metric,
    suggest_metrics_from_source,
    update_metric,
)
from app.db.base import get_session
from app.db.models import DataSource

router = APIRouter(prefix="/api/semantic-metrics", tags=["semantic-metrics"])


class MetricDimensionIn(BaseModel):
    key: str = ""
    label: str = ""
    column: str = Field(min_length=1)
    entity_key: str = ""


class MetricCreateIn(BaseModel):
    key: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=200)
    aliases: list[str] = Field(default_factory=list)
    description: str = ""
    unit: str = ""
    source_id: str = Field(min_length=1)
    calc_mode: str = "expression"
    table_name: str = ""
    sql_expression: str = ""
    sql_template: str = ""
    date_column: str = ""
    filter_sql: str = ""
    dimensions: list[MetricDimensionIn] = Field(default_factory=list)
    default_time_grain: str = "day"
    status: str = "enabled"
    source: str = "human"


class MetricUpdateIn(BaseModel):
    key: str | None = None
    name: str | None = None
    aliases: list[str] | None = None
    description: str | None = None
    unit: str | None = None
    source_id: str | None = None
    calc_mode: str | None = None
    table_name: str | None = None
    sql_expression: str | None = None
    sql_template: str | None = None
    date_column: str | None = None
    filter_sql: str | None = None
    dimensions: list[MetricDimensionIn] | None = None
    default_time_grain: str | None = None
    status: str | None = None
    expected_version: int | None = None


class MetricBatchCreateIn(BaseModel):
    items: list[MetricCreateIn] = Field(min_length=1, max_length=50)


class MetricPreviewIn(BaseModel):
    metric_id: str | None = None
    definition: dict[str, Any] | None = None
    date_from: str | None = None
    date_to: str | None = None
    group_by_date: bool = False
    time_grain: str | None = None
    group_by: list[str] = Field(default_factory=list)
    filters: dict[str, Any] = Field(default_factory=dict)
    limit: int = Field(default=50, ge=1, le=200)


class MetricSuggestIn(BaseModel):
    source_id: str = Field(min_length=1)
    table_names: list[str] | None = None
    document_ids: list[str] | None = None
    note: str = ""


class DecomposeJobCreateIn(BaseModel):
    source_id: str = Field(min_length=1)
    scopes: list[str] | None = None
    table_names: list[str] | None = None
    document_ids: list[str] = Field(default_factory=list)
    attachment_ids: list[str] = Field(default_factory=list)
    doc_title: str = ""
    doc_text: str = ""
    save_to_kb: bool = True
    note: str = ""


class DecomposeJobApplyIn(BaseModel):
    catalog_patches: list[dict[str, Any]] | None = None
    metrics: list[dict[str, Any]] | None = None
    entity_operations: list[dict[str, Any]] | None = None
    overwrite_existing_metrics: bool = True


def _raise_metric_error(exc: Exception) -> HTTPException:
    if isinstance(exc, DecomposeNotFound):
        return CodedHTTPException(404, str(exc), DECOMPOSE_NOT_FOUND_CODE)
    if isinstance(exc, DecomposeBusy):
        return CodedHTTPException(409, str(exc), DECOMPOSE_BUSY_CODE)
    if isinstance(exc, DecomposeFinished):
        return CodedHTTPException(409, str(exc), DECOMPOSE_FINISHED_CODE)
    if isinstance(exc, MetricNotFound):
        return CodedHTTPException(404, str(exc), "metric_not_found")
    if isinstance(exc, MetricConflict):
        return CodedHTTPException(409, str(exc), "metric_conflict")
    if isinstance(exc, MetricInvalid):
        return CodedHTTPException(422, str(exc), "metric_invalid")
    return HTTPException(400, str(exc))


async def _source_names_map(session: AsyncSession, source_ids: set[str]) -> dict[str, str]:
    if not source_ids:
        return {}
    rows = (await session.execute(select(DataSource.id, DataSource.name).where(DataSource.id.in_(source_ids)))).all()
    return {str(sid): str(sname) for sid, sname in rows}


@router.get("")
async def api_list_metrics(
    source_id: str | None = Query(default=None),
    status: str | None = Query(default=None),
    q: str | None = Query(default=None),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    rows = await list_metrics(session, source_id=source_id, status=status, q=q)
    src_names = await _source_names_map(session, {r.source_id for r in rows if r.source_id})
    return {
        "items": [metric_to_dict(r, source_name=src_names.get(r.source_id, "")) for r in rows],
        "total": len(rows),
    }


@router.post("/attachments")
async def api_upload_decompose_attachment(
    background: BackgroundTasks,
    file: UploadFile = File(...),
    save_to_kb: bool = Form(default=False),
    collection: str = Form(default="default"),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    try:
        raw_bytes = await entity_docs.read_upload_limited(file)
        res = await entity_docs.ingest_entity_attachment(
            session,
            raw_bytes=raw_bytes,
            filename=file.filename or "上传文档",
            mime=file.content_type or "",
            save_to_kb=save_to_kb,
            collection=collection,
            background=background,
        )
        await session.commit()
        return res
    except entity_docs.AttachmentTooLarge as exc:
        raise CodedHTTPException(413, str(exc), "attachment_too_large") from exc
    except entity_docs.AttachmentInvalid as exc:
        raise CodedHTTPException(422, str(exc), "attachment_invalid") from exc


@router.get("/decompose-jobs/active")
async def api_active_decompose_job(
    source_id: str | None = Query(default=None),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    return await get_active_or_latest_job(session, source_id=source_id)


@router.get("/decompose-jobs")
async def api_list_decompose_jobs(
    source_id: str | None = Query(default=None),
    limit: int = Query(default=10, ge=1, le=30),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    items = await list_jobs(session, source_id=source_id, limit=limit)
    return {"items": items}


@router.post("/decompose-jobs", status_code=202)
async def api_create_decompose_job(
    payload: DecomposeJobCreateIn,
    session: AsyncSession = Depends(get_session),
    x_actor: str | None = Header(default=None),
) -> dict[str, Any]:
    try:
        job = await create_decompose_job(
            session,
            source_id=payload.source_id,
            scopes=payload.scopes,
            table_names=payload.table_names,
            document_ids=payload.document_ids,
            attachment_ids=payload.attachment_ids,
            doc_title=payload.doc_title,
            doc_text=payload.doc_text,
            save_to_kb=payload.save_to_kb,
            note=payload.note,
            actor=actor_of(x_actor),
        )
        return {"job": job_to_dict(job, include_result=False)}
    except Exception as exc:  # noqa: BLE001
        await session.rollback()
        raise _raise_metric_error(exc) from exc


@router.get("/decompose-jobs/{job_id}")
async def api_get_decompose_job(
    job_id: str,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    try:
        job = await get_job(session, job_id)
        return {"job": job_to_dict(job, include_result=True)}
    except Exception as exc:  # noqa: BLE001
        raise _raise_metric_error(exc) from exc


@router.post("/decompose-jobs/{job_id}/cancel")
async def api_cancel_decompose_job(
    job_id: str,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    try:
        job = await cancel_decompose_job(session, job_id)
        return {"job": job_to_dict(job, include_result=False)}
    except Exception as exc:  # noqa: BLE001
        await session.rollback()
        raise _raise_metric_error(exc) from exc


@router.post("/decompose-jobs/{job_id}/apply")
async def api_apply_decompose_job(
    job_id: str,
    payload: DecomposeJobApplyIn,
    session: AsyncSession = Depends(get_session),
    x_actor: str | None = Header(default=None),
) -> dict[str, Any]:
    try:
        return await apply_decompose_job(
            session,
            job_id,
            catalog_patches=payload.catalog_patches,
            metrics=payload.metrics,
            entity_operations=payload.entity_operations,
            overwrite_existing_metrics=payload.overwrite_existing_metrics,
            actor=actor_of(x_actor),
        )
    except Exception as exc:  # noqa: BLE001
        await session.rollback()
        raise _raise_metric_error(exc) from exc


@router.get("/{metric_id}")
async def api_get_metric(metric_id: str, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    try:
        row = await get_metric(session, metric_id)
    except Exception as exc:  # noqa: BLE001
        raise _raise_metric_error(exc) from exc
    src_names = await _source_names_map(session, {row.source_id} if row.source_id else set())
    return {"metric": metric_to_dict(row, source_name=src_names.get(row.source_id, ""))}


@router.post("", status_code=201)
async def api_create_metric(
    payload: MetricCreateIn,
    session: AsyncSession = Depends(get_session),
    x_actor: str | None = Header(default=None),
) -> dict[str, Any]:
    try:
        row, ds = await create_metric(session, payload.model_dump(), actor=actor_of(x_actor))
        await session.commit()
        await session.refresh(row)
    except Exception as exc:  # noqa: BLE001
        await session.rollback()
        raise _raise_metric_error(exc) from exc
    return {"metric": metric_to_dict(row, source_name=ds.name)}


@router.post("/batch", status_code=201)
async def api_batch_create_metrics(
    payload: MetricBatchCreateIn,
    session: AsyncSession = Depends(get_session),
    x_actor: str | None = Header(default=None),
) -> dict[str, Any]:
    actor = actor_of(x_actor)
    created: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    for item in payload.items:
        data = item.model_dump()
        try:
            async with session.begin_nested():
                row, ds = await create_metric(session, data, actor=actor)
                created.append(metric_to_dict(row, source_name=ds.name))
        except Exception as exc:  # noqa: BLE001
            skipped.append({"key": item.key, "reason": str(exc)})
    await session.commit()
    return {"created": created, "skipped": skipped}


@router.put("/{metric_id}")
async def api_update_metric(
    metric_id: str,
    payload: MetricUpdateIn,
    session: AsyncSession = Depends(get_session),
    x_actor: str | None = Header(default=None),
) -> dict[str, Any]:
    data = {k: v for k, v in payload.model_dump().items() if v is not None}
    try:
        row, ds = await update_metric(session, metric_id, data, actor=actor_of(x_actor))
        await session.commit()
        await session.refresh(row)
    except Exception as exc:  # noqa: BLE001
        await session.rollback()
        raise _raise_metric_error(exc) from exc
    return {"metric": metric_to_dict(row, source_name=ds.name)}


@router.delete("/{metric_id}")
async def api_delete_metric(metric_id: str, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    try:
        row = await delete_metric(session, metric_id)
        await session.commit()
    except Exception as exc:  # noqa: BLE001
        await session.rollback()
        raise _raise_metric_error(exc) from exc
    return {"ok": True, "deleted_id": row.id, "deleted_key": row.key}


@router.post("/preview")
async def api_preview_metric(
    payload: MetricPreviewIn,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    try:
        return await preview_metric(
            session,
            metric_id=payload.metric_id,
            definition=payload.definition,
            date_from=payload.date_from,
            date_to=payload.date_to,
            group_by_date=payload.group_by_date,
            time_grain=payload.time_grain,
            group_by=payload.group_by,
            filters=payload.filters,
            limit=payload.limit,
        )
    except Exception as exc:  # noqa: BLE001
        raise _raise_metric_error(exc) from exc


@router.post("/suggest")
async def api_suggest_metrics(
    payload: MetricSuggestIn,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    try:
        return await suggest_metrics_from_source(
            session,
            source_id=payload.source_id,
            table_names=payload.table_names,
            document_ids=payload.document_ids,
            note=payload.note,
        )
    except Exception as exc:  # noqa: BLE001
        raise _raise_metric_error(exc) from exc
