from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
import heapq
import re
from typing import Any

import numpy as np
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Chunk, Document
from app.memory import inverted
from app.memory.embeddings import (
    default_alpha, embed_text, embed_texts, embedder_dim, embedder_id, from_blob,
    hybrid_contrib, to_blob, usable,
)

_TARGET = 800
_OVERLAP = 120
#: 每批送多少段去算向量。远端接口单次批量有上限（OpenAI 是 2048），而且
#: 一次把上万段全发出去，要么超时要么被限流，中途失败还没有断点
_EMBED_BATCH = 64
_ID_FETCH_BATCH = 500
_VEC_SCAN_BATCH = 512
_VEC_CACHE_MAX_COLLECTIONS = 16
_VEC_CACHE_MAX_CHUNKS = 200_000


@dataclass
class _VectorCacheEntry:
    stamp: tuple[int, str, str, int]
    chunk_ids: list[str]
    matrix: np.ndarray


_VEC_CACHE: OrderedDict[str, _VectorCacheEntry] = OrderedDict()


def invalidate_vector_cache(collection: str | None = None) -> None:
    """文档增删或重建索引后失效进程内归一化向量矩阵缓存。"""
    if collection is None:
        _VEC_CACHE.clear()
        return
    _VEC_CACHE.pop(collection, None)
    _VEC_CACHE.pop("__all__", None)


def chunk_text(text: str, target: int = _TARGET, overlap: int = _OVERLAP) -> list[str]:
    """按语义边界切块。

    先按空行分段，段落塞不下再按句子切，实在超长才硬切。
    这样切出来的块不会把一句话拦腰截断，检索命中后读起来是完整的。
    """
    text = text.replace("\r\n", "\n").strip()
    if not text:
        return []

    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    blocks: list[str] = []
    for para in paragraphs:
        if len(para) <= target:
            blocks.append(para)
            continue
        sentences = re.split(r"(?<=[。！？.!?\n])\s*", para)
        current = ""
        for sentence in sentences:
            if not sentence:
                continue
            if len(current) + len(sentence) <= target:
                current += sentence
            else:
                if current:
                    blocks.append(current)
                while len(sentence) > target:
                    blocks.append(sentence[:target])
                    sentence = sentence[target:]
                current = sentence
        if current:
            blocks.append(current)

    # 把相邻小块合并到接近目标长度，并留一点重叠保住上下文。
    #
    # 表格要特殊照顾：一张几十行的表会横跨好几块，而除了第一块，其余都只有
    # 数据行没有列名——「上海 | 172151 | 13%」这样的片段召回出来，模型没法
    # 知道这几个数是什么。所以开新块时把表头补在前面。
    merged: list[str] = []
    buf = ""
    header = ""          # 当前这张表的表头
    prev_is_row = False

    for block in blocks:
        is_row = bool(_TABLE_ROW.match(block))
        if is_row and not prev_is_row:
            header = block.strip()      # 一段表格的第一行就是表头
        elif not is_row:
            header = ""                 # 离开表格了
        prev_is_row = is_row

        if len(buf) + len(block) + 1 <= target:
            buf = f"{buf}\n{block}" if buf else block
            continue

        if buf:
            merged.append(buf)
            tail = _overlap_tail(buf, overlap) if overlap else ""
            # 新块要从表格中间开始，就先补一份表头。它才几十个字，
            # 换来的是这块单独拿出来也读得懂
            lead = ""
            if is_row and header and header not in tail:
                lead = f"{header}\n"
            # 表头必须在最前面。写成 tail+lead 的话它会夹在重叠的那几行后面，
            # 变成表格中间的一行——块的开头仍然是一串没有主语的数
            buf = f"{lead}{tail}{block}"
        else:
            merged.append(block)
            buf = ""
    if buf:
        merged.append(buf)
    return [m.strip() for m in merged if m.strip()]


def _overlap_tail(buf: str, overlap: int) -> str:
    """取重叠部分，但只在行边界上取。

    原来是 buf[-overlap:] 按字符切，于是表格会从行中间断开——实测一份 Word
    文档切出来的第二块开头是「| 上海 | 172151 | 13%」，门店名被切没了。
    一列没有主语的数字，召回到了也读不懂。
    """
    if not buf or overlap <= 0:
        return ""
    tail = buf[-overlap:]
    cut = tail.find("\n")
    # 整段都没换行就整个丢掉：宁可不要重叠，也不要半行
    return tail[cut + 1:] + "\n" if cut >= 0 else ""


#: 看起来像表格行：含有单元格分隔符。docx 抽出来用 " | " 拼，Markdown 表格同形
_TABLE_ROW = re.compile(r"^\s*\|?[^|\n]*\|")


async def ingest_document(
    session: AsyncSession,
    *,
    collection: str,
    title: str,
    content: str,
    source: str = "",
    mime: str = "text/plain",
    meta: dict[str, Any] | None = None,
    on_progress: Any = None,
) -> Document:
    doc = await create_document(
        session, collection=collection, title=title, content=content,
        source=source, mime=mime, meta=meta, status="ready",
    )
    await process_document(session, doc, on_progress=on_progress)
    return doc


async def create_document(
    session: AsyncSession,
    *,
    collection: str,
    title: str,
    content: str,
    source: str = "",
    mime: str = "text/plain",
    meta: dict[str, Any] | None = None,
    status: str = "ready",
) -> Document:
    """只建文档行，不切块不算向量。

    拆出来是为了让上传能立刻返回：切块 + 算向量在后台跑，一个 10MB 文档配上
    远端 embedding 要几十分钟，压在 HTTP 请求里必然超时。
    """
    doc = Document(
        collection=collection,
        title=title or source or "未命名文档",
        source=source,
        mime=mime,
        content=content,
        meta=meta or {},
        status=status,
    )
    session.add(doc)
    await session.commit()
    await session.refresh(doc)
    return doc


async def process_document(
    session: AsyncSession, doc: Document, *, on_progress: Any = None
) -> Document:
    """切块、算向量、建倒排。可以对着一个已存在的文档行反复跑（会先清掉旧的）。"""
    from sqlalchemy import delete as sa_delete

    old_ids = list((await session.execute(
        select(Chunk.id).where(Chunk.document_id == doc.id))).scalars())
    if old_ids:
        await inverted.drop_chunk_terms(session, old_ids)
        await session.execute(sa_delete(Chunk).where(Chunk.document_id == doc.id))

    collection, content = doc.collection, doc.content
    pieces = chunk_text(content)
    model_id, dim = embedder_id(), embedder_dim()

    # 一批算完就落盘。分批本身是必须的——原来 embed_texts(pieces) 一次全发，
    # 本地哈希只是慢，换成远端模型就是必挂（一个 10MB 文档近八千段，塞不下）。
    #
    # 每批都 commit 还有第二个好处：进度能被别人看见。试过用另一个 session
    # 单独写进度，但 SQLite 只允许一个写者，主事务开着的时候那次写会被堵住，
    # 界面上整个过程都是空的、只在结束时闪一下。
    for i in range(0, len(pieces), _EMBED_BATCH):
        part = pieces[i:i + _EMBED_BATCH]
        vectors = await embed_texts(part)
        made: list[Chunk] = []
        for offset, (piece, vec) in enumerate(zip(part, vectors)):
            chunk = Chunk(
                document_id=doc.id,
                collection=collection,
                ordinal=i + offset,
                content=piece,
                embedding=to_blob(vec),
                embed_model=model_id,
                embed_dim=dim,
                meta={"title": doc.title},
            )
            session.add(chunk)
            made.append(chunk)
        await session.flush()          # 先拿到 chunk.id，倒排行要引用它
        for chunk in made:
            await inverted.index_chunk(session, chunk)

        done = min(i + _EMBED_BATCH, len(pieces))
        doc.meta = {**(doc.meta or {}), "progress": {"done": done, "total": len(pieces)}}
        await session.commit()
        if on_progress:
            on_progress(done, len(pieces))

    doc.chunk_count = len(pieces)
    doc.status = "ready"
    doc.error = ""
    await session.commit()
    invalidate_vector_cache(collection)
    await session.refresh(doc)
    return doc


async def _fetch_chunks_by_ids(session: AsyncSession, chunk_ids: list[str]) -> dict[str, Chunk]:
    found: dict[str, Chunk] = {}
    for i in range(0, len(chunk_ids), _ID_FETCH_BATCH):
        part = chunk_ids[i:i + _ID_FETCH_BATCH]
        if not part:
            continue
        for row in (await session.execute(select(Chunk).where(Chunk.id.in_(part)))).scalars():
            found[row.id] = row
    return found


def _unit_vector(vec: np.ndarray) -> np.ndarray:
    arr = np.asarray(vec, dtype=np.float32)
    norm = float(np.linalg.norm(arr))
    return arr / norm if norm > 0 else np.zeros_like(arr, dtype=np.float32)


async def _vector_top_ids(
    session: AsyncSession,
    *,
    collection: str | None,
    query_vec: np.ndarray,
    limit: int,
) -> list[str]:
    """全集合语义向量路独立 Top-K 召回（不被 BM25 Top-50 前置截断）。

    中小型集合缓存 L2 归一化 `float32` 矩阵，单次检索仅需一次 BLAS 矩阵乘法；
    超大集合（> 20 万块）自动退化为游标分批扫描 + 小顶堆，保证常驻内存有界。
    """
    if limit <= 0:
        return []
    model_id, dim = embedder_id(), embedder_dim()
    stat_stmt = select(func.count(Chunk.id), func.max(Chunk.id))
    if collection:
        stat_stmt = stat_stmt.where(Chunk.collection == collection)
    total_raw, max_id_raw = (await session.execute(stat_stmt)).one()
    total = int(total_raw or 0)
    if total == 0:
        return []
    q_unit = _unit_vector(query_vec)
    if q_unit.shape[0] != dim:
        return []

    cache_key = collection or "__all__"
    stamp = (total, str(max_id_raw or ""), model_id, dim)

    if total <= _VEC_CACHE_MAX_CHUNKS:
        entry = _VEC_CACHE.get(cache_key)
        if entry is None or entry.stamp != stamp:
            ids: list[str] = []
            vecs: list[np.ndarray] = []
            last_id = ""
            while True:
                stmt = (
                    select(Chunk.id, Chunk.embedding)
                    .where(Chunk.id > last_id)
                    .order_by(Chunk.id.asc())
                    .limit(_VEC_SCAN_BATCH)
                )
                if collection:
                    stmt = stmt.where(Chunk.collection == collection)
                batch = (await session.execute(stmt)).all()
                if not batch:
                    break
                last_id = str(batch[-1][0])
                for cid, blob in batch:
                    v = from_blob(blob, dim)
                    ids.append(str(cid))
                    vecs.append(_unit_vector(v) if v is not None else np.zeros(dim, dtype=np.float32))
            matrix = np.vstack(vecs).astype(np.float32, copy=False) if vecs else np.zeros((0, dim), dtype=np.float32)
            entry = _VectorCacheEntry(stamp=stamp, chunk_ids=ids, matrix=matrix)
            _VEC_CACHE[cache_key] = entry
            _VEC_CACHE.move_to_end(cache_key)
            while len(_VEC_CACHE) > _VEC_CACHE_MAX_COLLECTIONS:
                _VEC_CACHE.popitem(last=False)
        else:
            _VEC_CACHE.move_to_end(cache_key)

        if entry.matrix.shape[0] == 0:
            return []
        scores = entry.matrix @ q_unit
        n = scores.shape[0]
        if n <= limit:
            order = sorted(range(n), key=lambda idx: (-float(scores[idx]), entry.chunk_ids[idx]))
            return [entry.chunk_ids[idx] for idx in order]
        top_idx = np.argpartition(-scores, limit - 1)[:limit]
        ordered = sorted((int(idx) for idx in top_idx), key=lambda idx: (-float(scores[idx]), entry.chunk_ids[idx]))
        return [entry.chunk_ids[idx] for idx in ordered]

    # 超大集合流式扫描兜底
    heap: list[tuple[float, str]] = []
    last_id = ""
    while True:
        stmt = (
            select(Chunk.id, Chunk.embedding)
            .where(Chunk.id > last_id)
            .order_by(Chunk.id.asc())
            .limit(_VEC_SCAN_BATCH)
        )
        if collection:
            stmt = stmt.where(Chunk.collection == collection)
        batch = (await session.execute(stmt)).all()
        if not batch:
            break
        last_id = str(batch[-1][0])
        for cid, blob in batch:
            v = from_blob(blob, dim)
            sc = float(np.dot(_unit_vector(v), q_unit)) if v is not None else 0.0
            if len(heap) < limit:
                heapq.heappush(heap, (sc, str(cid)))
            elif sc > heap[0][0]:
                heapq.heapreplace(heap, (sc, str(cid)))
    heap.sort(key=lambda item: (-item[0], item[1]))
    return [cid for _, cid in heap]


async def _stale_model_names(session: AsyncSession, collection: str | None) -> list[str]:
    stmt = select(Chunk.embed_model).distinct().where(
        or_(Chunk.embed_model != embedder_id(), Chunk.embed_dim != embedder_dim())
    )
    if collection:
        stmt = stmt.where(Chunk.collection == collection)
    names = [str(m or "未知模型") for m in (await session.execute(stmt)).scalars()]
    return sorted(set(names)) or ["未知模型"]


async def search(
    session: AsyncSession,
    *,
    collection: str | None,
    query: str,
    limit: int = 5,
    alpha: float | None = None,
    on_degrade: Any = None,
) -> list[dict[str, Any]]:
    """真·双路混合检索（词面 BM25 独立召回 + 全集合向量矩阵独立召回）。

    存量向量和当前 embedder 对不上时**整个查询退回纯关键词**，并通过
    on_degrade 把原因交出去。不把对不上的那些当 0 分：那会让它们静默沉底，
    用户看到的是结果莫名其妙变差，而不是"这里有一批索引该重建了"。
    """
    if alpha is None:
        alpha = default_alpha()

    total_stale = await stale_count(session, collection)
    if total_stale > 0:
        if on_degrade:
            names = await _stale_model_names(session, collection)
            on_degrade(
                f"{collection or '全部集合'}里有 {total_stale} 条向量由"
                f"「{'、'.join(names)}」建立，与当前的「{embedder_id()}」不一致，"
                f"本次已退回纯关键词检索。重建索引后恢复。"
            )
        alpha = 0.0

    query_vec = await embed_text(query)
    cand_limit = max(limit * 8, 50)

    # 第一路：倒排索引词面 BM25 Top-K（返回精确全集合 BM25 得分字典）
    lex_picked, all_kw_scores = await inverted.candidates_with_scores(
        session, collection=collection, query=query, limit=cand_limit,
    )
    lex_ids = [cid for cid, _ in lex_picked]

    # 第二路：当启用语义权重时，独立从全集合归一化向量索引召回语义 Top-K，不再被 BM25 Top-50 卡死
    vec_ids = (
        await _vector_top_ids(session, collection=collection, query_vec=query_vec, limit=cand_limit)
        if alpha > 0.0
        else []
    )

    merged_ids = sorted(set(lex_ids) | set(vec_ids))
    rows: list[Chunk] = []
    kw_scores: list[float] | None = None

    if merged_ids:
        found = await _fetch_chunks_by_ids(session, merged_ids)
        rows = [found[cid] for cid in merged_ids if cid in found]
        if lex_picked or await inverted.has_terms(session, collection):
            kw_scores = [all_kw_scores.get(r.id, 0.0) for r in rows]
        else:
            kw_scores = None
    else:
        if await inverted.has_terms(session, collection):
            # 倒排已就绪但没有任何查询词命中且 alpha == 0：所有片段词面分均为 0，只需按 id 定序取前 limit 条
            stmt = select(Chunk).order_by(Chunk.id.asc()).limit(limit)
            if collection:
                stmt = stmt.where(Chunk.collection == collection)
            rows = list((await session.execute(stmt)).scalars())
            kw_scores = [0.0] * len(rows)
        else:
            # 存量数据升级尚未建倒排：退回全表扫现算 BM25，保证不漏检
            stmt = select(Chunk).order_by(Chunk.id.asc())
            if collection:
                stmt = stmt.where(Chunk.collection == collection)
            rows = list((await session.execute(stmt)).scalars())
            kw_scores = None

    if not rows:
        return []

    if total_stale > 0 or alpha == 0.0:
        vectors: list[Any] = (
            [None] * len(rows)
            if total_stale > 0
            else [from_blob(r.embedding, embedder_dim()) for r in rows]
        )
    else:
        vectors = [from_blob(r.embedding, embedder_dim()) for r in rows]

    ranked = hybrid_contrib(
        query,
        [r.content for r in rows],
        vectors,
        query_vec,
        alpha=alpha,
        keyword_scores=kw_scores,
    )
    out: list[dict[str, Any]] = []
    for hit in ranked[:limit]:
        row = rows[hit.index]
        score = round(hit.score, 4)
        by_vec = min(round(hit.contrib["vector"], 4), score)
        out.append(
            {
                "chunk_id": row.id,
                "document_id": row.document_id,
                "title": (row.meta or {}).get("title", ""),
                "ordinal": row.ordinal,
                "content": row.content,
                "score": score,
                "signals": {k: round(v, 4) for k, v in hit.signals.items()},
                "contrib": {"vector": by_vec, "keyword": round(score - by_vec, 4)},
            }
        )
    return out


async def reindex(
    session: AsyncSession, collection: str | None = None, *, on_progress: Any = None,
) -> dict[str, Any]:
    """用当前 embedder 按游标分批重算向量并重建倒排，避免全表一次性载入内存。"""
    count_stmt = select(func.count(Chunk.id))
    if collection:
        count_stmt = count_stmt.where(Chunk.collection == collection)
    total = int((await session.execute(count_stmt)).scalar_one() or 0)
    model_id, dim = embedder_id(), embedder_dim()
    if total == 0:
        invalidate_vector_cache(collection)
        return {"collection": collection, "reindexed": 0, "embedder": model_id}

    done = 0
    last_id = ""
    while True:
        stmt = select(Chunk).where(Chunk.id > last_id).order_by(Chunk.id.asc()).limit(_EMBED_BATCH)
        if collection:
            stmt = stmt.where(Chunk.collection == collection)
        part = list((await session.execute(stmt)).scalars())
        if not part:
            break
        last_id = part[-1].id
        vectors = await embed_texts([r.content for r in part])
        for row, vec in zip(part, vectors):
            row.embedding = to_blob(vec)
            row.embed_model = model_id
            row.embed_dim = dim
        await session.commit()
        done += len(part)
        if on_progress:
            on_progress(min(done, total), total)
    await inverted.rebuild(session, collection)
    invalidate_vector_cache(collection)
    return {"collection": collection, "reindexed": done, "embedder": model_id}


async def stale_count(session: AsyncSession, collection: str | None = None) -> int:
    """有多少条向量和当前 embedder 对不上。知识库页据此提示要不要重建。

    用 SQL 数而不是把片段全载进来再在 Python 里过一遍——这个数在检索降级时
    也要取，那条路径上不该再来一次全表扫。
    """
    stmt = select(func.count(Chunk.id)).where(
        or_(Chunk.embed_model != embedder_id(), Chunk.embed_dim != embedder_dim()))
    if collection:
        stmt = stmt.where(Chunk.collection == collection)
    return int((await session.execute(stmt)).scalar_one() or 0)


async def delete_document(session: AsyncSession, doc_id: str) -> bool:
    doc = await session.get(Document, doc_id)
    if not doc:
        return False
    collection = doc.collection
    chunk_ids = list((await session.execute(
        select(Chunk.id).where(Chunk.document_id == doc_id))).scalars())
    await inverted.drop_chunk_terms(session, chunk_ids)
    await session.delete(doc)
    await session.commit()
    invalidate_vector_cache(collection)
    return True


async def list_collections(session: AsyncSession) -> list[dict[str, Any]]:
    rows = await session.execute(
        select(
            Document.collection,
            func.count(Document.id),
            func.sum(Document.chunk_count),
        ).group_by(Document.collection)
    )
    # sum 在 PostgreSQL 上给的是 numeric（驱动交回 Decimal），SQLite 给整数：一律转成 int
    return [
        {"collection": name, "documents": int(docs), "chunks": int(chunks or 0)}
        for name, docs, chunks in rows
    ]
