"""倒排索引：把候选从"整个集合"收窄到"含有查询词的那些"。

在此之前每次检索都把集合里所有片段载入内存，再重建一遍 BM25 倒排。这不只是
扫表——是对每一段重新分词、重新统计词频，每次查询都来一遍。

这里把倒排落到库里（chunk_terms），查询时只捞含有查询词的片段。BM25 的公式
和参数与原来完全一致（k1=1.5, b=0.75，同一个分词器），所以排序结果不变——
test_retrieval_quality 里有一条专门比对两条路径给出的名次是否一样。
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Chunk, ChunkTerm
from app.memory.embeddings import CJK_STOPWORDS, _tokens, filter_query_tokens

_CJK_STOPWORDS = CJK_STOPWORDS
_K1 = 1.5
_B = 0.75
_IN_BATCH = 500
_REBUILD_BATCH = 256


def term_freqs(text: str) -> dict[str, int]:
    """一段文本的词频。和 BM25 用同一个分词器，否则倒排和打分会对不上。"""
    freqs: dict[str, int] = defaultdict(int)
    for tok in _tokens(text):
        # 超长 token 存不进 String(64)，而它们几乎都是噪音（长串 id、base64）
        if len(tok) <= 64:
            freqs[tok] += 1
    return dict(freqs)


async def index_chunk(session: AsyncSession, chunk: Chunk) -> None:
    """给一个片段建倒排。调用方负责 commit。"""
    freqs = term_freqs(chunk.content)
    chunk.token_len = sum(freqs.values())
    for term, tf in freqs.items():
        session.add(ChunkTerm(
            chunk_id=chunk.id, collection=chunk.collection, term=term, tf=tf))


async def drop_chunk_terms(session: AsyncSession, chunk_ids: list[str]) -> None:
    """删片段时一起删倒排。

    外键写了 ondelete=CASCADE，但 SQLite 的级联要连接级 PRAGMA 打开才生效
    （base.py 里是打开的）——索引一致性不该依赖一个开关，显式按批删，避免超过
    SQLite / PostgreSQL 单条语句绑定参数上限（32,766）。
    """
    for i in range(0, len(chunk_ids), _IN_BATCH):
        part = chunk_ids[i:i + _IN_BATCH]
        if part:
            await session.execute(
                delete(ChunkTerm).where(ChunkTerm.chunk_id.in_(part)))


async def candidates_with_scores(
    session: AsyncSession, *, collection: str | None, query: str, limit: int,
) -> tuple[list[tuple[str, float]], dict[str, float]]:
    """按 BM25 取候选，返回 (前 limit 条 [(chunk_id, 分数)], 全部命中片段的精确 BM25 分数字典)。

    直接通过 ChunkTerm JOIN Chunk 一次性取回 token_len，彻底消除大集合高频词命中数万条时
    `Chunk.id.in_(...)` 超出数据库 32,767 参数上限崩溃的问题。
    """
    # 按查询词的出现次数加权，不能去重。原来的 BM25.score 遍历的是带重复的
    # 词表：「导出订单最多多少行」里「多」出现两次，它的贡献就加两次。去重会让
    # 同一个查询在倒排和全表扫两条路上给出不同的分——实测顶部那条差了 2.32
    raw_qtokens = [t for t in _tokens(query) if len(t) <= 64]
    qcounts = Counter(filter_query_tokens(raw_qtokens))
    terms = list(qcounts)
    if not terms:
        return [], {}

    rows: list[Any] = []
    for i in range(0, len(terms), _IN_BATCH):
        part = terms[i:i + _IN_BATCH]
        stmt = (
            select(ChunkTerm.chunk_id, ChunkTerm.term, ChunkTerm.tf, Chunk.token_len)
            .join(Chunk, Chunk.id == ChunkTerm.chunk_id)
            .where(ChunkTerm.term.in_(part))
        )
        if collection:
            stmt = stmt.where(ChunkTerm.collection == collection)
        rows.extend((await session.execute(stmt)).all())
    if not rows:
        return [], {}

    # df 直接从命中里数：含有该词的片段数，正是 BM25 要的那个 df
    df: dict[str, int] = defaultdict(int)
    for _chunk_id, term, _tf, _tlen in rows:
        df[term] += 1

    stat = select(func.count(Chunk.id), func.sum(Chunk.token_len))
    if collection:
        stat = stat.where(Chunk.collection == collection)
    total, total_len = (await session.execute(stat)).one()
    total = int(total or 0)
    if not total:
        return [], {}
    avg_len = float(total_len or 0) / total or 1.0

    scores: dict[str, float] = defaultdict(float)
    for chunk_id, term, tf, token_len in rows:
        idf = math.log(1 + (total - df[term] + 0.5) / (df[term] + 0.5))
        length = int(token_len or 0) or 1
        denom = tf + _K1 * (1 - _B + _B * length / avg_len)
        scores[chunk_id] += qcounts[term] * idf * (tf * (_K1 + 1)) / denom

    ranked = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
    return ranked[:limit], dict(scores)


async def candidates(
    session: AsyncSession, *, collection: str | None, query: str, limit: int,
) -> list[tuple[str, float]]:
    """按 BM25 取候选，返回 [(chunk_id, 分数)]，已按分数降序。

    没有任何查询词命中时返回空——调用方据此决定是不是要退回全表。
    """
    top, _ = await candidates_with_scores(session, collection=collection, query=query, limit=limit)
    return top


async def has_terms(session: AsyncSession, collection: str | None = None) -> bool:
    """集合里是否已建有倒排词项（区分「真没命中查询词」与「存量升级尚未建倒排」）。"""
    stmt = select(ChunkTerm.id).limit(1)
    if collection:
        stmt = stmt.where(ChunkTerm.collection == collection)
    return (await session.execute(stmt)).first() is not None


async def missing_count(session: AsyncSession, collection: str | None = None) -> int:
    """有多少片段还没建倒排。存量数据升级上来时就是全部。"""
    stmt = select(func.count(Chunk.id)).where(Chunk.token_len == 0)
    if collection:
        stmt = stmt.where(Chunk.collection == collection)
    return int((await session.execute(stmt)).scalar_one() or 0)


async def rebuild(session: AsyncSession, collection: str | None = None) -> int:
    """按游标分批重建倒排，避免一次性把全库片段加载进内存。"""
    total = 0
    last_id = ""
    while True:
        stmt = select(Chunk).where(Chunk.id > last_id).order_by(Chunk.id.asc()).limit(_REBUILD_BATCH)
        if collection:
            stmt = stmt.where(Chunk.collection == collection)
        batch = list((await session.execute(stmt)).scalars())
        if not batch:
            break
        last_id = batch[-1].id
        await drop_chunk_terms(session, [r.id for r in batch])
        for row in batch:
            await index_chunk(session, row)
        await session.commit()
        total += len(batch)
    return total
