"""第五批（Batch 5 / P1）：并发一致性、多进程缓存同步、连接池收尾与计算精度回归测试。

覆盖：
1. `EventBus` 并行分支乱序提交补齐窗口（`seen`）与无订阅后台活跃运行防 `_trim_seq` 驱逐；
2. Provider 流式计费异常区分（上下文超长/内容安全 400 不重试不污染，`_NO_STREAM_USAGE` TTL 过期自愈）与 `judge` 缓存 Token 计价；
3. 知识库向量缓存跨进程 `caches` 广播失效、失败文档残留脏块清理、中文高频虚词过滤与标准向量模型免同步探测；
4. SQLite 连接池 `max_rows` 提前截断自动关闭游标防 `DETACH` 锁库、`temp."records"` 用后即删、归档历史版本纳入 `default_view`；
5. `nlchange` 多日期防静默截断为单日、`validation.same_value` 精确 `Decimal` 字符串对齐、`metrics`/`recall` `ROUND_HALF_UP` 一致性与对账 `NULL` 键及负基数修正。
"""
from __future__ import annotations

import sqlite3
import time
import uuid
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from app.cluster import caches
from app.core.bus import EventBus, _MAX_SEQ_ENTRIES
from app.core.config import settings
from app.data import engine as data_engine
from app.db.base import SessionLocal
from app.db.models import Chunk, DataSource, Document
from app.db.models_apps import App
from app.engine.expressions import eval_expression
from app.engine.nodes.metrics import render_metric_value, round_metric
from app.ledger import nlchange, reconcile, validation
from app.memory import embeddings, inverted, kb
from app.providers import catalog, factory
from app.records import store as records_store
from app.records.tables import columns_of, ensure_table, path_of
from app.records.views import default_view, resolve


async def test_event_bus_out_of_order_seen_window_and_active_background_run_protection():
    bus = EventBus()
    run_id = "run-ooo"
    sub = bus.attach(run_id)

    # 模拟 DB 回放时 seq=11 先提交（after=11, seen={1..9, 11}），而 seq=10 后到达内存总线
    await bus.publish({"run_id": run_id, "seq": 10, "type": "node.finished", "node": "branch_a"})
    await bus.publish({"run_id": run_id, "seq": 11, "type": "node.finished", "node": "branch_b"})
    await bus.publish({"run_id": run_id, "seq": 12, "type": "run.completed"})
    await bus.close(run_id)

    got_seqs: list[int] = []
    with sub:
        async for ev in sub.stream(after=11, seen={*range(1, 10), 11}):
            got_seqs.append(int(ev["seq"]))
    assert got_seqs == [10, 12], "后提交的乱序缺口 seq=10 应被补发，已回放的 seq=11 不重复投递"

    # 模拟后台无 WebSocket 订阅的定时运行跨越 >4096 次新运行，活跃期间序号不得被重置，且头部活跃项不阻塞后续旧项淘汰
    bg_run = "bg-active-run"
    bus.mark_active(bg_run)
    assert bus.next_seq(bg_run) == 1
    assert bus.next_seq(bg_run) == 2

    for idx in range(_MAX_SEQ_ENTRIES + 50):
        bus.next_seq(f"ephemeral-{idx}")

    assert len(bus._seq) <= _MAX_SEQ_ENTRIES
    assert bus.next_seq(bg_run) == 3, "活跃后台运行的序号计数器绝不能被中途淘汰归零"
    bus.mark_inactive(bg_run)
    await bus.close(bg_run)


def test_provider_error_discrimination_ttl_healing_and_prompt_cache_pricing(monkeypatch: pytest.MonkeyPatch):
    class FakeBadRequest(Exception):
        def __init__(self, status_code: int, message: str) -> None:
            super().__init__(message)
            self.status_code = status_code

    # 上下文超长 / 内容审查 / 工具调用错误不得误触发思考参数或 stream_options 重试
    ctx_err = FakeBadRequest(400, "This model's maximum context length is 128000 tokens")
    mod_err = FakeBadRequest(400, "Output blocked by content_filter policy")
    assert factory._rejects_reasoning_hint(ctx_err) is False
    assert factory._rejects_stream_options(ctx_err) is False
    assert factory._rejects_stream_options(mod_err) is False

    # 明确不认思考参数时照常识别剥离
    hint_err = FakeBadRequest(400, "Unrecognized request argument supplied: reasoning_effort")
    assert factory._rejects_reasoning_hint(hint_err) is True

    # _NO_STREAM_USAGE 具备 TTL 自动过期自愈能力，不会因偶发网关异常永久失明
    saved_map = dict(factory._NO_STREAM_USAGE) if isinstance(factory._NO_STREAM_USAGE, dict) else {}
    monkeypatch.setattr(factory, "_NO_STREAM_USAGE", {})
    key = ("http://gw.example.test/v1", "gpt-5.2-mini")
    factory._record_no_stream_usage(key)
    assert factory._has_no_stream_usage(key) is True
    factory._NO_STREAM_USAGE[key] = time.monotonic() - (factory._NO_STREAM_USAGE_TTL_S + 5.0)
    assert factory._has_no_stream_usage(key) is False
    monkeypatch.setattr(factory, "_NO_STREAM_USAGE", saved_map)

    # Prompt Caching 价格核算：带 cache_read 命中时成本应显著低于全量未缓存输入
    catalog.sync_provider_models(
        [
            SimpleNamespace(
                name="test-cache-prov",
                models=[
                    {
                        "id": "cached-pro-v1",
                        "pricing": {"input": 3.0, "output": 15.0, "cache_read": 0.3, "cache_creation": 3.75},
                    }
                ],
            )
        ],
        broadcast=False,
    )
    try:
        base_cost = catalog.estimate_cost("cached-pro-v1", 100_000, 1_000)
        cached_cost = catalog.estimate_cost("cached-pro-v1", 100_000, 1_000, cache_read=90_000, cache_write=0)
        assert base_cost > 0 and cached_cost > 0
        assert cached_cost < base_cost * 0.5
    finally:
        catalog.sync_provider_models([], broadcast=False)


async def test_kb_cross_worker_invalidation_failed_doc_cleanup_cjk_stopwords_and_known_dims():
    # 1. OpenAIEmbedder 标准模型名无需在事件循环发起同步网络探测即可获知维度
    emb_inst = embeddings.OpenAIEmbedder(model="text-embedding-3-small", api_key="sk-no-network")
    assert emb_inst.dim == 1536

    # 2. CJK 高频单字停用词在存在实词双字切分时自动剔除，纯虚词查询保留兜底
    filtered = inverted.filter_query_tokens(["的", "是", "销售", "售额", "在"])
    assert "销售" in filtered and "的" not in filtered and "是" not in filtered
    assert inverted.filter_query_tokens(["的", "了"]) == ["的", "了"]

    # 3. caches 收到 kb_vectors 事件时清理本地向量缓存
    kb._VEC_CACHE["kb-sync-test"] = ("stamp", None, [])  # type: ignore[assignment]
    handler = caches._handlers.get("kb_vectors")
    assert handler is not None
    handler("kb-sync-test")
    assert "kb-sync-test" not in kb._VEC_CACHE

    # 4. 失败文档残留 Chunk 清理：状态为 error 的半截文档块被彻底清除且不污染检索矩阵
    collection = f"kb-{uuid.uuid4().hex[:8]}"
    async with SessionLocal() as session:
        doc = await kb.create_document(
            session,
            collection=collection,
            title="broken.md",
            content="半截脏数据",
            status="processing",
        )
        chunk = Chunk(
            document_id=doc.id,
            collection=collection,
            ordinal=0,
            content="半截脏数据",
            embedding=embeddings.to_blob(np.array([0.5, 0.5], dtype=np.float32)),
            embed_model="mock",
            embed_dim=2,
            meta={},
        )
        session.add(chunk)
        await session.flush()
        await inverted.index_chunk(session, chunk)
        await session.commit()

        await kb.cleanup_failed_document_chunks(session, doc.id)
        doc.status = "error"
        await session.commit()

        total, stamp = await kb._kb_stamp(session, collection=collection, model_id="mock", dim=2)
        assert total == 0
        entry = await kb._load_kb_matrix(session, collection=collection, stamp=stamp, dim=2)
        assert entry.chunk_ids == [] and entry.matrix.size == 0


async def test_sqlite_pool_early_break_detach_temp_view_cleanup_and_archived_default_view(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    data_dir = tmp_path / "data"
    (data_dir / "uploads").mkdir(parents=True)
    monkeypatch.setattr(settings, "data_dir", data_dir)

    async with SessionLocal() as session:
        owner = App(name="连接池归档测试", created_by="admin")
        session.add(owner)
        await session.commit()
        app_id = owner.id

    # 连续演进 12 个版本触发归档（超出 MAX_DIRECT_ATTACH=7）
    latest = None
    for idx in range(1, 13):
        cols = [
            {"name": "item", "label": "品名", "type": "string", "required": True},
            {"name": f"vcol_{idx}", "label": f"列_{idx}", "type": "integer", "required": True},
        ]
        async with SessionLocal() as session:
            owner = await session.get(App, app_id)
            latest = await ensure_table(session, owner, cols)
            await session.commit()
        for r in range(3):
            records_store.append(
                path_of(latest),
                columns_of(latest),
                {"item": f"sku-{idx}-{r}", f"vcol_{idx}": r},
                version=latest.version,
                run_id=None,
                actor="alice",
                action_key=uuid.uuid4().hex,
            )

    assert latest is not None
    async with SessionLocal() as session:
        source = await session.get(DataSource, latest.source_id)
        resolved = await resolve(session, source)
        # default_view 必须纳入已归档的早期版本，总行数应为 12 * 3 = 36
        def_view = default_view(source)
        assert "records_archive" in " ".join(def_view.prelude)

    # 触发 max_rows=2 提前截断游标：修复前未关闭 cursor 会锁住 ATTACH 的 archive.db 导致 DETACH 报错！
    res1 = await data_engine.run_query(
        resolved,
        'SELECT * FROM "records"',
        limits=data_engine.QueryLimits(max_rows=2),
    )
    assert len(res1.rows) == 2 and res1.truncated is True

    # 复用同一连接池分别执行 resolved 与 default_view 全量计数（无 database archive is locked，且 36 行完整可见）
    res2 = await data_engine.run_query(resolved, 'SELECT COUNT(*) FROM "records"')
    assert res2.rows == [[36]]
    res_def = await data_engine.run_query(def_view, 'SELECT COUNT(*) FROM "records"')
    assert res_def.rows == [[36]]

    # 验证直接裸借底层连接池连接时 temp."records" 已在归还池前清理干净
    eng = await data_engine.engines.get(resolved)
    async with eng.connect() as conn:
        temp_rows = (
            await conn.exec_driver_sql("SELECT name FROM sqlite_temp_master WHERE type='view' AND name='records'")
        ).fetchall()
    assert temp_rows == []
    await data_engine.engines.invalidate(latest.source_id, broadcast=False)


def test_nlchange_multi_date_guard_decimal_rounding_and_reconcile_null_negative_base():
    # 1. 含起止两个日期的问句绝不能被静默截断为第 1 天单日区间
    assert nlchange._auto_date_interval("2026-10-01~2026-10-07的销售额") is None
    assert nlchange._auto_date_interval("2026年10月1日到2026年10月7日的销售额") is None
    assert nlchange._auto_date_interval("2026-10-01的销售额") == ("2026-10-01", "2026-10-02")

    # 2. validation.same_value 对 SQL DECIMAL 字符串与数值精确等价，同时保持布尔与纯文本边界
    assert validation.same_value(Decimal("12.50"), 12.5) is True
    assert validation.same_value(12.5, "12.50") is True
    assert validation.same_value(True, 1) is False
    assert validation.same_value(2, "2") is False

    # 3. 口径卡数值舍入统一使用 ROUND_HALF_UP（2.5 -> 3，1.25 保留 1 位 -> 1.3，避免银行家舍入与回溯校验冲突）
    assert round_metric(Decimal("2.5"), 0) == 3
    assert round_metric(Decimal("1.25"), 1) == 1.3
    assert render_metric_value(Decimal("1.25"), decimals=1) == "1.3"

    # 4. 对账 SQL 支持 NULL 维度桶对齐，且负基数（亏损收窄：-100 -> -50，diff=+50）相对变化率为 +0.5 而非 -0.5
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("CREATE TABLE o (region TEXT, profit REAL)")
        conn.execute("CREATE TABLE c (region TEXT, profit REAL)")
        conn.executemany("INSERT INTO o VALUES (?, ?)", [(None, -50.0), ("East", 120.0)])
        conn.executemany("INSERT INTO c VALUES (?, ?)", [(None, -100.0), ("East", 100.0)])
        pair = reconcile.Pair(ours="profit", column="profit")
        sql = reconcile.align_sql([("region", "region")], [pair], [(None,), ("East",)])
        rows = conn.execute(sql).fetchall()
        assert rows[0] == (None, -50.0, -100.0)

        four = reconcile._four("0", "利润", "ours_v", "theirs_v", pair)
        rel_expr = next(m["expression"] for m in four if m["id"] == "rel_0")
        rel_val = eval_expression(rel_expr, {"ours_v": -50.0, "theirs_v": -100.0})
        assert rel_val == pytest.approx(0.5)
    finally:
        conn.close()
