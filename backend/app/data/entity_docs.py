"""标准业务实体维护的业务文档与主数据对照表附件服务（E1）。

支持两类文档来源：
1. 临时上传附件（POST /api/ontology/entities/attachments）：复用 memory/parsing.extract(..., tables=True)
   提取 .md / .txt / .docx / .pdf / .pptx / .html 以及 .xlsx / .csv / .tsv 编码对照表正文，
   按 kb.chunk_text 切块（跨块自动保留 Markdown 表头）并存入内容寻址工件（entity_doc_attachment）；
   若勾选 save_to_kb=True，同步创建知识库 Document 并在后台完成切块向量索引，返回 document_id 供后续直接复用。
2. 已有知识库文档（Document + Chunk）：若文档仍在后台切块中，自动回退按 doc.content 即时切块，无需等待向量计算。
"""
from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from fastapi import BackgroundTasks, UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import artifact_store
from app.core.config import settings
from app.core.errors import explain, raw as raw_err
from app.db.base import SessionLocal
from app.db.models import Chunk, Document
from app.memory import kb
from app.memory.parsing import UnsupportedDocument, extract

logger = logging.getLogger(__name__)

ATTACHMENT_KIND = "entity_doc_attachment"
MAX_DOC_SEARCH_LIMIT = 15

#: 持有后台切块任务引用，防止事件循环提前回收
_KB_TASKS: set[asyncio.Task[Any]] = set()

_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]{2,}")


class AttachmentTooLarge(ValueError):
    """上传附件超过系统大小上限。"""


class AttachmentInvalid(ValueError):
    """上传附件格式不支持或无有效文本。"""


@dataclass(slots=True)
class EntityDocBundle:
    ref_id: str
    kind: str  # "attachment" | "kb"
    title: str
    mime: str
    char_count: int
    chunks: list[str]
    document_id: str | None = None


async def _index_kb_document(doc_id: str) -> None:
    """后台对新建的知识库文档切块并建立向量与倒排索引。"""
    async with SessionLocal() as session:
        doc = await session.get(Document, doc_id)
        if not doc:
            return
        try:
            await kb.process_document(session, doc)
        except Exception as exc:  # noqa: BLE001
            logger.warning("实体文档存入知识库索引失败（%s）：%s", doc_id, raw_err(exc))
            await session.rollback()
            doc = await session.get(Document, doc_id)
            if doc:
                reason, hint = explain(exc)
                doc.status = "failed"
                doc.error = (f"文档分块或生成向量时出错：{reason}" + (f"。{hint}" if hint else ""))[:500]
                await session.commit()


async def read_upload_limited(file: UploadFile) -> bytes:
    limit = settings.max_upload_mb * 1024 * 1024
    pieces: list[bytes] = []
    total = 0
    while chunk := await file.read(1024 * 1024):
        total += len(chunk)
        if total > limit:
            raise AttachmentTooLarge(f"文件超过大小上限（{settings.max_upload_mb} MB）")
        pieces.append(chunk)
    return b"".join(pieces)


async def ingest_entity_attachment(
    session: AsyncSession,
    *,
    raw_bytes: bytes,
    filename: str,
    mime: str = "",
    save_to_kb: bool = False,
    collection: str = "default",
    background: BackgroundTasks | None = None,
) -> dict[str, Any]:
    """解析上传的业务文档或主数据表格，生成附件工件并按需存入知识库。"""
    clean_name = (filename or "上传文档").strip() or "上传文档"
    clean_mime = (mime or "text/plain").strip() or "text/plain"
    if not raw_bytes:
        raise AttachmentInvalid("上传文件为空，请选择包含有效内容的文件")
    try:
        content = extract(raw_bytes, clean_name, clean_mime, tables=True)
    except UnsupportedDocument as exc:
        raise AttachmentInvalid(str(exc)) from None

    content = (content or "").strip()
    if not content:
        raise AttachmentInvalid("未能从文件中提取出有效文字或表格内容")

    chunks = kb.chunk_text(content)
    if not chunks:
        chunks = [content[:4000]]

    doc_id: str | None = None
    clean_collection = (collection or "default").strip() or "default"
    if save_to_kb:
        doc = await kb.create_document(
            session,
            collection=clean_collection,
            title=clean_name,
            content=content,
            source=clean_name,
            mime=clean_mime,
            meta={"origin": "entity_agent"},
            status="processing",
        )
        doc_id = doc.id
        if background is not None:
            background.add_task(_index_kb_document, doc.id)
        else:
            task = asyncio.create_task(_index_kb_document(doc.id))
            _KB_TASKS.add(task)
            task.add_done_callback(_KB_TASKS.discard)

    payload = {
        "title": clean_name,
        "filename": clean_name,
        "mime": clean_mime,
        "char_count": len(content),
        "chunks": chunks,
        "document_id": doc_id,
    }
    attachment_id = await artifact_store.put_json(
        payload,
        kind=ATTACHMENT_KIND,
        meta={"title": clean_name, "document_id": doc_id},
    )
    return {
        "attachment_id": attachment_id,
        "title": clean_name,
        "mime": clean_mime,
        "char_count": len(content),
        "chunk_count": len(chunks),
        "saved_to_kb": bool(doc_id),
        "document_id": doc_id,
        "collection": clean_collection if doc_id else None,
    }


async def load_doc_bundles(
    session: AsyncSession,
    *,
    attachment_ids: Sequence[str] = (),
    document_ids: Sequence[str] = (),
) -> list[EntityDocBundle]:
    """统一加载临时附件与知识库文档的切块，按引用去重。"""
    bundles: list[EntityDocBundle] = []
    seen_doc_ids: set[str] = set()
    seen_att_ids: set[str] = set()

    for raw_att in attachment_ids or ():
        att_id = str(raw_att or "").strip()
        if not att_id or att_id in seen_att_ids:
            continue
        seen_att_ids.add(att_id)
        try:
            loaded = artifact_store.load(att_id)
        except ValueError:
            loaded = None
        if not isinstance(loaded, dict) or not isinstance(loaded.get("chunks"), list):
            continue
        chunks = [str(c) for c in loaded["chunks"] if isinstance(c, str) and c.strip()]
        if not chunks:
            continue
        linked_doc_id = str(loaded.get("document_id") or "").strip() or None
        if linked_doc_id:
            seen_doc_ids.add(linked_doc_id)
        bundles.append(
            EntityDocBundle(
                ref_id=att_id,
                kind="attachment",
                title=str(loaded.get("title") or "上传附件"),
                mime=str(loaded.get("mime") or "text/plain"),
                char_count=int(loaded.get("char_count") or sum(len(c) for c in chunks)),
                chunks=chunks,
                document_id=linked_doc_id,
            )
        )

    clean_doc_ids = [
        str(d).strip()
        for d in (document_ids or ())
        if str(d or "").strip() and str(d).strip() not in seen_doc_ids
    ]
    if clean_doc_ids:
        seen_doc_ids.update(clean_doc_ids)
        docs = {
            row.id: row
            for row in (await session.execute(select(Document).where(Document.id.in_(clean_doc_ids)))).scalars()
        }
        chunk_rows = list(
            (
                await session.execute(
                    select(Chunk)
                    .where(Chunk.document_id.in_(clean_doc_ids))
                    .order_by(Chunk.document_id.asc(), Chunk.ordinal.asc())
                )
            ).scalars()
        )
        by_doc: dict[str, list[str]] = {}
        for crow in chunk_rows:
            if crow.content and crow.content.strip():
                by_doc.setdefault(crow.document_id, []).append(crow.content)

        for doc_id in clean_doc_ids:
            doc = docs.get(doc_id)
            if doc is None:
                continue
            pieces = by_doc.get(doc_id) or kb.chunk_text(doc.content or "")
            if not pieces and (doc.content or "").strip():
                pieces = [(doc.content or "").strip()[:4000]]
            if not pieces:
                continue
            bundles.append(
                EntityDocBundle(
                    ref_id=doc.id,
                    kind="kb",
                    title=doc.title or doc.source or "知识库文档",
                    mime=doc.mime or "text/plain",
                    char_count=len(doc.content or ""),
                    chunks=pieces,
                    document_id=doc.id,
                )
            )
    return bundles


def _keywords(query: str) -> list[str]:
    q = (query or "").strip().lower()
    if not q:
        return []
    tokens = [m.group(0) for m in _TOKEN_RE.finditer(q)]
    out: list[str] = []
    seen: set[str] = set()
    for tok in [q, *tokens]:
        if tok and tok not in seen:
            seen.add(tok)
            out.append(tok)
    return out


def search_doc_bundles(
    bundles: Sequence[EntityDocBundle],
    *,
    query: str = "",
    doc_ref: str = "",
    start_chunk: int = 0,
    limit: int = 6,
) -> list[dict[str, Any]]:
    """按关键词或分段序号检索已挂载文档片段，返回带文档标题与段号的原文片段。"""
    cap = max(1, min(int(limit or 6), MAX_DOC_SEARCH_LIMIT))
    offset = max(0, int(start_chunk or 0))
    ref_norm = (doc_ref or "").strip().lower()

    scoped = [
        b
        for b in bundles
        if not ref_norm
        or b.ref_id.lower() == ref_norm
        or (b.document_id and b.document_id.lower() == ref_norm)
        or ref_norm in b.title.lower()
    ]
    if not scoped:
        scoped = list(bundles)

    terms = _keywords(query)
    if not terms:
        out: list[dict[str, Any]] = []
        for bundle in scoped:
            for idx in range(offset, len(bundle.chunks)):
                out.append({
                    "doc_ref": bundle.ref_id,
                    "title": bundle.title,
                    "chunk_index": idx,
                    "total_chunks": len(bundle.chunks),
                    "citation": f"文档《{bundle.title}》第 {idx + 1} 段",
                    "content": bundle.chunks[idx],
                })
                if len(out) >= cap:
                    return out
        return out

    scored: list[tuple[int, int, int, EntityDocBundle, str]] = []
    for b_idx, bundle in enumerate(scoped):
        for c_idx, chunk in enumerate(bundle.chunks):
            if c_idx < offset:
                continue
            low = chunk.lower()
            score = 0
            for t_idx, term in enumerate(terms):
                hits = low.count(term)
                if hits:
                    weight = 5 if t_idx == 0 else (3 if len(term) >= 3 else 1)
                    score += hits * weight
            if score > 0:
                scored.append((-score, b_idx, c_idx, bundle, chunk))
    scored.sort(key=lambda item: (item[0], item[1], item[2]))
    return [
        {
            "doc_ref": bundle.ref_id,
            "title": bundle.title,
            "chunk_index": c_idx,
            "total_chunks": len(bundle.chunks),
            "citation": f"文档《{bundle.title}》第 {c_idx + 1} 段",
            "content": chunk,
        }
        for _neg, _b, c_idx, bundle, chunk in scored[:cap]
    ]
