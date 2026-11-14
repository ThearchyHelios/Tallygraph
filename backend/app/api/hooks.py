"""入站钩子（BE-2）：POST /api/hooks/{hook_id}，只认签名、不走会话。设计文档 §2.4。

签名、时间窗、幂等键、start / signal 的规则见 app/process/hooks.py。钩子的增删在 /api/apps/{id}/hooks（应用包）。
开了登录时，身份中间件要放过这条路径（外部系统没有会话，也不带 X-AgentLab-Request）。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from app.process import hooks
from app.process.tasks import TaskRefused

router = APIRouter(prefix="/api/hooks", tags=["hooks"])

_MAX_HOOK_BODY_BYTES = 2 * 1024 * 1024


async def _read_bounded_body(request: Request) -> bytes:
    cl = request.headers.get("content-length")
    if cl is not None:
        try:
            declared = int(cl)
        except ValueError as e:
            raise HTTPException(400, "Content-Length 请求头无效") from e
        if declared < 0:
            raise HTTPException(400, "Content-Length 请求头无效")
        if declared > _MAX_HOOK_BODY_BYTES:
            raise HTTPException(413, "请求体过大")

    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        if not chunk:
            continue
        total += len(chunk)
        if total > _MAX_HOOK_BODY_BYTES:
            raise HTTPException(413, "请求体过大")
        chunks.append(chunk)
    return b"".join(chunks)


@router.post("/{hook_id}", response_model=None)
async def receive(hook_id: str, request: Request) -> Any:
    from app.apps.entries import FormRejected

    body = await _read_bounded_body(request)
    try:
        return await hooks.receive(hook_id, request.headers, body)
    except hooks.HookRejected as e:
        raise HTTPException(e.status_code, str(e)) from e
    except FormRejected as e:
        return JSONResponse({"detail": str(e), "code": "form_invalid", "errors": e.errors}, status_code=422)
    except TaskRefused as e:
        raise HTTPException(e.status_code, str(e)) from e
