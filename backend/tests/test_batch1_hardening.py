"""第一批 P0 止血修复、安全加固与多模型适配回归测试。"""
from __future__ import annotations

import asyncio
import types
import uuid

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from sqlalchemy import select

from app.core import artifact_store
from app.core.config import settings
from app.data import engine as data_engine
from app.db.base import SessionLocal
from app.db.models import Artifact, DataSource, Provider, Run
from app.db.models_blueprints import StoredFile
from app.engine import checkpoints
from app.engine.context import NodeContext, NodeError, RunContext
from app.engine.expressions import ExpressionError, eval_expression, resolve_path
from app.engine.nodes.llm import _invoke_streaming, run_llm
from app.engine.runner import run_manager
from app.engine.schema import GraphNode, GraphSpec
from app.files import store as files_store
from app.main import app
from app.providers import catalog, factory
from app.providers.factory import ModelSpec, _ReasoningAwareChatOpenAI, build_chat_model
from app.sandbox.local_sandbox import LocalSandbox
from app.sandbox.seatbelt_sandbox import SeatbeltSandbox, build_policy
from app.tools import net


async def test_memory_sqlite_engine_cache_and_epoch_invalidation(tmp_path):
    cache = data_engine.EngineCache()
    try:
        mem_source = DataSource(
            id=uuid.uuid4().hex,
            name="mem-src",
            kind="sqlite",
            database=":memory:",
            readonly=False,
        )
        eng = await cache.get(mem_source)
        async with eng.connect() as conn:
            val = await conn.scalar(data_engine.text("SELECT 42"))
        assert val == 42

        # 模拟 get() 在锁外核对期间并发触发 invalidate()，旧世代不得写回缓存
        db_file = tmp_path / "snap.db"
        conn0 = data_engine.open_checked_sqlite(str(db_file), readonly=False)
        try:
            conn0.execute("CREATE TABLE t (x INT)")
            conn0.commit()
        finally:
            conn0.close()
        digest = data_engine._file_sha256(str(db_file))
        snap_view = types.SimpleNamespace(
            id="src-epoch",
            snapshot_id="s1",
            kind="sqlite",
            database=str(db_file),
            readonly=True,
            immutable=True,
            expected_sha256=digest,
            options={},
        )
        orig_verify = cache._verify_once
        invalidated_once = False

        async def slow_verify(key: str, source: object, expected: str):
            nonlocal invalidated_once
            fp = await orig_verify(key, source, expected)
            if not invalidated_once:
                invalidated_once = True
                await cache.invalidate("src-epoch", broadcast=False)
            return fp

        cache._verify_once = slow_verify  # type: ignore[method-assign]
        eng2 = await cache.get(snap_view)
        assert eng2 is not None
        assert cache._epochs.get("src-epoch", 0) == 1
    finally:
        await cache.close()


async def test_sqlite_cancelled_query_interrupts_immediately(tmp_path):
    db_file = tmp_path / "slow.db"
    conn0 = data_engine.open_checked_sqlite(str(db_file), readonly=False)
    try:
        conn0.execute("CREATE TABLE t (x INT)")
        conn0.commit()
    finally:
        conn0.close()

    src = DataSource(
        id=uuid.uuid4().hex,
        name="slow-sqlite",
        kind="sqlite",
        database=str(db_file),
        readonly=True,
    )
    infinite_cte = (
        "WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM r WHERE n < 100000000) "
        "SELECT SUM(n) FROM r"
    )
    task = asyncio.create_task(
        data_engine.run_query(src, infinite_cte, limits=data_engine.QueryLimits(timeout_seconds=30))
    )
    await asyncio.sleep(0.08)
    task.cancel()
    # 修复前 finally 先把 deadline.over 置 True，这里会挂满整条递归 CTE 甚至永久卡死
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=2.0)
    await data_engine.engines.invalidate(src.id, broadcast=False)


async def test_delete_run_cleans_artifacts_files_and_checkpoints():
    await run_manager.setup()
    try:
        graph = {
            "nodes": [
                {"id": "in", "type": "input", "data": {"config": {}}},
                {"id": "out", "type": "output", "data": {"config": {"fields": [{"name": "ok", "value": "1"}]}}},
            ],
            "edges": [{"source": "in", "target": "out"}],
        }
        started = await run_manager.start(graph=graph, input_payload={})
        for _ in range(100):
            await asyncio.sleep(0.03)
            async with SessionLocal() as session:
                row = await session.get(Run, started.id)
            if row and row.status in ("succeeded", "failed"):
                break
        assert row is not None and row.status == "succeeded"

        art_id = await artifact_store.put_json({"unique_for_delete_run": started.id}, kind="test", run_id=started.id)
        async with SessionLocal() as session:
            saved_file = await files_store.save(
                session,
                f"run-bound-{started.id}".encode(),
                "note.txt",
                uploaded_by="tester",
                context={"blueprint_id": "b1"},
            )
            saved_file.bound_run_id = started.id
            await session.commit()
            file_id, file_sha = saved_file.id, saved_file.sha256

        ckpt_before, _ = await checkpoints.count(run_manager.checkpointer, started.id)
        assert ckpt_before >= 1
        assert artifact_store.load(art_id) is not None
        assert files_store.blob_path(file_sha).exists()

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.delete(f"/api/runs/{started.id}")
            assert resp.status_code == 204, resp.text

        assert artifact_store.load(art_id) is None
        assert not files_store.blob_path(file_sha).exists()
        async with SessionLocal() as session:
            assert await session.get(StoredFile, file_id) is None
            rem_arts = list((await session.execute(select(Artifact).where(Artifact.run_id == started.id))).scalars())
            assert rem_arts == []
        ckpt_after, _ = await checkpoints.count(run_manager.checkpointer, started.id)
        assert ckpt_after == 0
    finally:
        await run_manager.shutdown()


async def test_sandbox_path_prefix_and_ephemeral_cleanup():
    box = LocalSandbox()
    # workdir 同前缀兄弟目录逃逸（如 tmp-xxx -> ../tmp-xxx2/evil.txt）必须被 is_relative_to 拦下
    escaped = await box.run("print(1)", session_id="sess1", files={"../sess1_sibling/evil.txt": "bad"})
    assert not escaped.ok and "非法文件路径" in (escaped.error or "")
    await box.cleanup("sess1")

    before_dirs = {p.name for p in box._root.iterdir()} if box._root.exists() else set()
    ok = await box.run("print('hi')")
    assert ok.ok and "hi" in ok.stdout
    after_dirs = {p.name for p in box._root.iterdir()} if box._root.exists() else set()
    assert after_dirs - before_dirs == set(), "无 session_id 的临时工作目录执行完必须回收"


def test_seatbelt_policy_denies_data_dir_outside_home(tmp_path, monkeypatch):
    ext_data = tmp_path / "external_agentlab_data"
    work = ext_data / "workspace" / "seatbelt" / "s1"
    work.mkdir(parents=True)
    monkeypatch.setattr(settings, "data_dir", ext_data)
    policy = build_policy(work, language="python", network=False)
    real_data = str(ext_data.resolve())
    real_work = str(work.resolve())
    assert f'(deny file-read* (subpath "{real_data}"))' in policy
    assert policy.index(f'(deny file-read* (subpath "{real_data}"))') < policy.index(
        f'(allow file-read* (subpath "{real_work}"))'
    )
    assert SeatbeltSandbox is not None


async def test_net_ipv4_mapped_metadata_and_streaming_redirect_sanitization(monkeypatch):
    assert net._is_metadata("::ffff:169.254.169.254") is True
    assert net._is_metadata("::ffff:100.100.100.200") is True
    assert net._is_private("::ffff:127.0.0.1") is True

    async def fake_assert_safe(url: str, *, allow_intranet: bool = False) -> str:
        return url

    monkeypatch.setattr(net, "assert_safe_url", fake_assert_safe)
    seen_requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen_requests.append(request)
        if request.url.host == "origin-a.example":
            return httpx.Response(302, headers={"location": "https://origin-b.example/final?x=1"})
        return httpx.Response(200, content=b"0123456789abcdef", headers={"content-type": "text/plain"})

    orig_client = httpx.AsyncClient

    def patched_client(**kwargs):
        return orig_client(transport=httpx.MockTransport(handler), **{k: v for k, v in kwargs.items() if k != "trust_env"})

    monkeypatch.setattr(net.httpx, "AsyncClient", patched_client)
    res = await net.safe_request(
        "POST",
        "https://origin-a.example/start",
        headers={"Authorization": "Bearer secret", "Cookie": "sid=1", "X-Custom": "keep"},
        params={"init": "1"},
        json_body={"hello": "world"},
        max_bytes=10,
    )
    assert res["status"] == 200
    assert res["body"] == "0123456789"
    assert res["truncated"] is True
    assert len(seen_requests) == 2
    hop2 = seen_requests[1]
    assert hop2.method == "GET"
    assert "authorization" not in hop2.headers
    assert "cookie" not in hop2.headers
    assert hop2.headers.get("x-custom") == "keep"
    assert str(hop2.url) == "https://origin-b.example/final?x=1"


def test_expressions_private_attr_and_seq_repeat_cap():
    class Dummy:
        public = "ok"
        _secret = "nope"

    assert resolve_path({"obj": Dummy()}, "obj.public") == "ok"
    assert resolve_path({"obj": Dummy()}, "obj._secret") is None

    assert eval_expression("'ab' * 3", {}) == "ababab"
    with pytest.raises(ExpressionError, match="序列重复长度超过上限"):
        eval_expression("'x' * 2000000", {})
    with pytest.raises(ExpressionError, match="序列重复长度超过上限"):
        eval_expression("[1, 2] * 600000", {})


async def test_provider_reasoning_roundtrip_bearer_catalog_and_structured_usage(monkeypatch):
    # 1. o1 / o3 / o4 禁采样 + 大小写不敏感匹配 + 自定义模型价格 + 缓存折扣
    assert catalog.supports_sampling("o1") is False
    assert catalog.supports_sampling("openai/o3-mini") is False
    assert catalog.supports_sampling("O4-mini") is False
    assert catalog.supports_sampling("ollama") is True
    assert catalog.find_pricing("deepseek-ai/deepseek-v3") is not None

    base_cost = catalog.estimate_cost("claude-sonnet-5", 10_000, 1_000)
    cached_cost = catalog.estimate_cost("claude-sonnet-5", 10_000, 1_000, cache_read_tokens=9_000)
    assert cached_cost < base_cost

    # 2. Anthropic bearer 保留 timeout 与 default_headers
    p_bearer = Provider(
        name="anth-gw",
        kind="anthropic",
        base_url="https://gw.example.com",
        api_key="tok-123",
        default_model="claude-sonnet-5",
        enabled=True,
        extra={"auth_style": "bearer", "headers": {"X-Tenant": "t1"}},
    )
    chat_anth = build_chat_model(p_bearer, ModelSpec(timeout=42))
    assert chat_anth._async_client.timeout == 42
    assert chat_anth._async_client.default_headers.get("X-Tenant") == "t1"

    # 3. ReasoningAwareChatOpenAI 在带 tool_calls 的多轮消息里回传 reasoning_content
    llm = _ReasoningAwareChatOpenAI(model="deepseek-reasoner", api_key="k", base_url="https://api.deepseek.com/v1")
    msgs = [
        HumanMessage(content="查销售额"),
        AIMessage(
            content="",
            tool_calls=[{"name": "sql_query", "args": {"sql": "SELECT 1"}, "id": "call_1", "type": "tool_call"}],
            additional_kwargs={"reasoning_content": "先查表确认销售额"},
        ),
        ToolMessage(content="100", tool_call_id="call_1"),
    ]
    payload = llm._get_request_payload(msgs)
    assistant_msg = next(m for m in payload["messages"] if m["role"] == "assistant")
    assert assistant_msg["reasoning_content"] == "先查表确认销售额"

    # 4. _NO_STREAM_USAGE 按 (endpoint, model) 粒度隔离
    monkeypatch.setattr(factory, "_NO_STREAM_USAGE", {("https://gw.example.com/v1", "legacy-model")})
    assert ("https://gw.example.com/v1", "modern-model") not in factory._NO_STREAM_USAGE

    # 5. run_llm 结构化输出通过 include_raw=True 记录真实 token 与费用
    class FakeStructuredModel:
        def with_structured_output(self, schema, include_raw: bool = False):
            assert include_raw is True

            class _Runner:
                async def ainvoke(self_inner, _messages):
                    return {
                        "raw": AIMessage(
                            content='{"answer": 42}',
                            usage_metadata={"input_tokens": 500, "output_tokens": 100, "total_tokens": 600},
                        ),
                        "parsed": {"answer": 42},
                        "parsing_error": None,
                    }

            return _Runner()

    async def fake_get_chat_model(_session, _spec):
        return FakeStructuredModel(), "gpt-5.2"

    monkeypatch.setattr("app.engine.nodes.llm.get_chat_model", fake_get_chat_model)
    node = GraphNode(
        id="s1",
        type="llm",
        data={
            "label": "结构化",
            "config": {
                "prompt": "回答",
                "output_schema": {"type": "object", "properties": {"answer": {"type": "integer"}}},
            },
        },
    )
    events_seen: list[dict] = []
    ctx = NodeContext(
        node=node,
        run=RunContext(
            run_id="r-struct",
            thread_id="t-struct",
            spec=GraphSpec(nodes=[node], edges=[]),
            extra={"stream_writer": lambda ev: events_seen.append(ev)},
        ),
    )
    out = await run_llm({"input": {}, "vars": {}, "nodes": {}, "messages": []}, ctx)
    assert out["nodes"]["s1"]["data"] == {"answer": 42}
    assert out["usage"]["input_tokens"] == 500 and out["usage"]["output_tokens"] == 100
    assert out["usage"]["cost_usd"] > 0

    # 6. _invoke_streaming 遇到 401/429 等不可重试错误直接抛出，不触发二次非流式请求
    ainvoke_called = 0

    class AuthFailModel:
        async def astream(self, _messages):
            if False:
                yield None
            err = RuntimeError("401 invalid api key")
            err.status_code = 401  # type: ignore[attr-defined]
            raise err

        async def ainvoke(self, _messages):
            nonlocal ainvoke_called
            ainvoke_called += 1
            return AIMessage(content="should not happen")

    with pytest.raises(NodeError):
        await _invoke_streaming(AuthFailModel(), [HumanMessage(content="hi")], ctx, "gpt-5.2")
    assert ainvoke_called == 0
