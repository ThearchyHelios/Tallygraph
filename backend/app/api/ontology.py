"""标准业务实体与指标本体图谱接口：/api/ontology。

- GET    /api/ontology/entities                   列出标准业务实体（可按数据源过滤）
- POST   /api/ontology/entities                   新建标准业务实体
- GET    /api/ontology/entities/{entity_id}       读取单个标准业务实体
- PATCH  /api/ontology/entities/{entity_id}       更新标准业务实体（支持 version 乐观锁）
- DELETE /api/ontology/entities/{entity_id}       删除标准业务实体
- POST   /api/ontology/entities/{entity_id}/review 审阅单条跨源表列绑定或口径参数映射（确认 / 驳回）
- GET    /api/ontology/graph                      汇总已发布口径的指标关系网、反向表血缘与标准实体跨源桥
- GET    /api/ontology/workflows/{workflow_id}    读取单个工作流（当前草稿或指定版本）的指标关系与反向表血缘
"""
from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, BackgroundTasks, Body, Depends, File, Form, Header, HTTPException, Query, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.coded import CATALOG_DRAFT_MODEL_UNAVAILABLE, CodedHTTPException
from app.api.deps import actor_of
from app.data import catalog, entity_assistant, entity_discovery, entity_docs, entity_patch
from app.data.entities import (
    EntityConflict,
    EntityInvalid,
    EntityNotFound,
    create_entity,
    delete_entity,
    entity_to_dict,
    get_entity,
    list_entities,
    review_entity_item,
    update_entity,
)
from app.db.base import get_session
from app.db.models import Workflow, WorkflowVersion
from app.engine.schema import GraphSpec
from app.ledger.ontology import build_published_ontology_graph, ontology_publish_issues, workflow_ontology_index

router = APIRouter(prefix="/api/ontology", tags=["ontology"])


class EntityReviewIn(BaseModel):
    target_kind: Literal["binding", "caliber_param"]
    item_id: str
    status: Literal["proposed", "verified", "confirmed", "rejected"]
    reason: str = ""
    version: int | None = None


class EntityAssistantMessageIn(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(max_length=entity_assistant.MESSAGE_MAX)


class EntityAssistantIn(BaseModel):
    messages: list[EntityAssistantMessageIn] = Field(default_factory=list, max_length=entity_assistant.MAX_MESSAGES)
    source_ids: list[str] | None = Field(default=None, max_length=50)
    focus_entity_key: str | None = None
    attachment_ids: list[str] = Field(default_factory=list, max_length=20)
    document_ids: list[str] = Field(default_factory=list, max_length=30)
    pending_ops: list[dict[str, Any]] = Field(default_factory=list)


class EntityApplyPatchIn(BaseModel):
    operations: list[dict[str, Any]] = Field(min_length=1, max_length=200)
    expected_versions: dict[str, int] | None = None


class EntityDiscoveryJobIn(BaseModel):
    source_ids: list[str] | None = None
    document_ids: list[str] | None = None
    attachment_ids: list[str] | None = None
    note: str | None = None


@router.get("/entities")
async def api_list_entities(
    source_name: str | None = Query(default=None),
    only_active: bool = Query(default=False),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    rows = await list_entities(session, source_name=source_name, only_active_bindings=only_active)
    return {"items": [entity_to_dict(r) for r in rows]}


@router.post("/entities", status_code=201)
async def api_create_entity(
    payload: dict[str, Any] = Body(...),
    x_actor: str | None = Header(default=None, alias="X-Actor"),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    try:
        row = await create_entity(session, payload, actor=actor_of(x_actor))
        await session.commit()
    except EntityInvalid as exc:
        raise HTTPException(status_code=422, detail={"message": exc.message, "field": exc.field}) from None
    except EntityConflict as exc:
        raise HTTPException(status_code=409, detail={"message": exc.message, "field": exc.field}) from None
    return entity_to_dict(row)


@router.post("/entities/attachments", status_code=201)
async def api_upload_entity_attachment(
    background: BackgroundTasks,
    file: UploadFile = File(...),
    save_to_kb: bool = Form(default=False),
    collection: str = Form(default="default"),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """解析业务文档或主数据编码对照表附件，支持勾选同步存入知识库复用。"""
    try:
        raw_bytes = await entity_docs.read_upload_limited(file)
        return await entity_docs.ingest_entity_attachment(
            session,
            raw_bytes=raw_bytes,
            filename=file.filename or "上传文档",
            mime=file.content_type or "text/plain",
            save_to_kb=save_to_kb,
            collection=collection,
            background=background,
        )
    except entity_docs.AttachmentTooLarge as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from None
    except entity_docs.AttachmentInvalid as exc:
        raise HTTPException(status_code=415, detail=str(exc)) from None


@router.post("/entities/assistant")
async def api_entity_assistant(
    payload: EntityAssistantIn,
    session: AsyncSession = Depends(get_session),
) -> Any:
    """交互式实体助手（SSE）：跨数据源与业务文档对话对齐业务实体，只产出提案不直接写库。"""
    messages = [m.model_dump() for m in payload.messages]
    if problem := entity_assistant.conversation_problem(messages):
        raise HTTPException(status_code=422, detail=problem)
    try:
        model, model_id = await entity_assistant.resolve_model(session)
    except catalog.CatalogModelUnavailable as exc:
        raise CodedHTTPException(409, str(exc), CATALOG_DRAFT_MODEL_UNAVAILABLE) from exc
    ctx = await entity_assistant.prepare(
        session,
        source_ids=payload.source_ids,
        focus_entity_key=payload.focus_entity_key,
        attachment_ids=payload.attachment_ids,
        document_ids=payload.document_ids,
        pending_ops=payload.pending_ops,
    )
    return StreamingResponse(
        entity_assistant.stream(model, model_id, ctx, messages),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/entities/apply-patch")
async def api_apply_entity_patch(
    body: EntityApplyPatchIn,
    x_actor: str | None = Header(default=None, alias="X-Actor"),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """批量应用人工在实体助手审阅确认的修改提案（校验乐观锁版本）。"""
    try:
        result = await entity_patch.apply_entity_patch(
            session,
            body.operations,
            mode="confirmed",
            actor=actor_of(x_actor),
            expected_versions=body.expected_versions,
        )
        await session.commit()
        return result
    except EntityInvalid as exc:
        raise HTTPException(status_code=422, detail={"message": exc.message, "field": exc.field}) from None
    except EntityConflict as exc:
        raise HTTPException(status_code=409, detail={"message": exc.message, "field": exc.field}) from None


@router.post("/discovery-jobs", status_code=202)
async def api_create_discovery_job(
    body: EntityDiscoveryJobIn,
    x_actor: str | None = Header(default=None, alias="X-Actor"),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """创建后台扫描发现候选标准业务实体的任务（结果一律落为 proposed）。"""
    try:
        job = await entity_discovery.create_job(
            session,
            source_ids=body.source_ids,
            document_ids=body.document_ids,
            attachment_ids=body.attachment_ids,
            note=body.note,
            actor=actor_of(x_actor),
        )
        return entity_discovery.job_to_dict(job)
    except catalog.CatalogModelUnavailable as exc:
        raise CodedHTTPException(409, str(exc), CATALOG_DRAFT_MODEL_UNAVAILABLE) from exc
    except entity_discovery.DiscoveryBusy as exc:
        raise CodedHTTPException(409, str(exc), entity_discovery.DISCOVERY_BUSY_CODE) from exc
    except (entity_discovery.DiscoveryEmptyScope, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


@router.get("/discovery-jobs/active")
async def api_get_active_discovery_job(
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    return await entity_discovery.get_active_or_latest_job(session)


@router.get("/discovery-jobs/{job_id}")
async def api_get_discovery_job(
    job_id: str,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    row = await entity_discovery.get_job(session, job_id)
    if row is None:
        raise HTTPException(status_code=404, detail="候选实体扫描任务不存在")
    return entity_discovery.job_to_dict(row)


@router.post("/discovery-jobs/{job_id}/cancel")
async def api_cancel_discovery_job(
    job_id: str,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    try:
        row = await entity_discovery.cancel_job(session, job_id)
        return entity_discovery.job_to_dict(row)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    except entity_discovery.DiscoveryFinished as exc:
        raise CodedHTTPException(409, str(exc), entity_discovery.DISCOVERY_FINISHED_CODE) from exc


@router.get("/entities/{entity_id}")
async def api_get_entity(
    entity_id: str,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    row = await get_entity(session, entity_id)
    if row is None:
        raise HTTPException(status_code=404, detail="标准业务实体不存在")
    return entity_to_dict(row)


@router.patch("/entities/{entity_id}")
async def api_update_entity(
    entity_id: str,
    payload: dict[str, Any] = Body(...),
    x_actor: str | None = Header(default=None, alias="X-Actor"),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    try:
        row = await update_entity(session, entity_id, payload, actor=actor_of(x_actor))
        await session.commit()
    except EntityNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    except EntityInvalid as exc:
        raise HTTPException(status_code=422, detail={"message": exc.message, "field": exc.field}) from None
    except EntityConflict as exc:
        raise HTTPException(status_code=409, detail={"message": exc.message, "field": exc.field}) from None
    return entity_to_dict(row)


@router.post("/entities/{entity_id}/review")
async def api_review_entity_item(
    entity_id: str,
    body: EntityReviewIn,
    x_actor: str | None = Header(default=None, alias="X-Actor"),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    try:
        row = await review_entity_item(
            session,
            entity_id,
            target_kind=body.target_kind,
            item_id=body.item_id,
            status=body.status,
            reason=body.reason,
            actor=actor_of(x_actor),
            expected_version=body.version,
        )
        await session.commit()
    except EntityNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    except EntityInvalid as exc:
        raise HTTPException(status_code=422, detail={"message": exc.message, "field": exc.field}) from None
    except EntityConflict as exc:
        raise HTTPException(status_code=409, detail={"message": exc.message, "field": exc.field}) from None
    return entity_to_dict(row)


@router.delete("/entities/{entity_id}")
async def api_delete_entity(
    entity_id: str,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    deleted = await delete_entity(session, entity_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="标准业务实体不存在")
    await session.commit()
    return {"ok": True}


@router.get("/graph")
async def api_get_ontology_graph(
    include_proposed: bool = Query(default=True),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    return await build_published_ontology_graph(session, include_proposed=include_proposed)


@router.get("/workflows/{workflow_id}")
async def api_get_workflow_ontology(
    workflow_id: str,
    version: int | None = Query(default=None),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    workflow = await session.get(Workflow, workflow_id)
    if workflow is None:
        raise HTTPException(status_code=404, detail="工作流不存在")
    if version is not None:
        snap = (
            await session.execute(
                select(WorkflowVersion).where(
                    WorkflowVersion.workflow_id == workflow_id,
                    WorkflowVersion.version == version,
                )
            )
        ).scalar_one_or_none()
        if snap is None:
            raise HTTPException(status_code=404, detail=f"工作流不存在 v{version} 版本")
        graph = snap.graph or {}
        ver = int(snap.version)
    else:
        graph = workflow.graph or {}
        ver = int(workflow.version or 1)
    index = workflow_ontology_index(
        workflow_id=workflow.id,
        version=ver,
        graph=graph,
        workflow_name=workflow.name or workflow.id,
    )
    issues: list[dict[str, Any]] = []
    try:
        spec = GraphSpec.model_validate(graph)
        issues = await ontology_publish_issues(session, spec, workflow_id=workflow.id)
    except Exception:  # noqa: BLE001 - 草稿图结构未成形时照常返回可提取的指标清单
        issues = []
    return {**index, "issues": issues}
