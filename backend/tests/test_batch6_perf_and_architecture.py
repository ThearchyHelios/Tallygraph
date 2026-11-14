"""第六批（Batch 6 / P2-P3）：语义指标收敛、流式与 I/O 性能、分层架构解耦与工程门禁回归测试。

覆盖：
1. `SemanticMetric -> Caliber Card` 统一桥接转换（`to_caliber_card_spec`）与受限 `user` 角色 `AppGrant` 资产级 ACL + 提示词物理表/SQL 脱敏；
2. 大 CSV 工作簿惰性流式扫描（`Workbooks.csv` 不再前置调用全量 `csv_grid.parse` 驻留内存）；
3. `core/json_extract.py` `O(N)` JSON 提取与 Copilot `_with_heartbeat` 客户端断连即时取消后台生成任务；
4. `files/store.py` 分桶锁防并发 GC 误删与 `cluster/signal.py` 未连库期间保留待发信号；
5. 后端分层依赖门禁（`engine/{evidence_reader,judge,review}`、`tools/mcp_manager`、`core/{coded,json_extract}` 零依赖 `app.api.*`）；
6. 前端增量流式解码器（`createRunDecoder`）、首屏 `authSession.ts` 按需分包、CSS `@layer components` 与跨平台脚本/CI 门禁静态核验。
"""
from __future__ import annotations

import ast
import asyncio
import re
import time
import uuid
from pathlib import Path

import pytest

from app.api import copilot as copilot_api
from app.api import deps
from app.chat import grounding
from app.cluster.signal import Signals
from app.core.config import settings
from app.core.json_extract import _extract_json
from app.data import csv_grid, semantic_metrics, xlsx_cells
from app.db.base import SessionLocal
from app.db.models import DataSource
from app.db.models_apps import App, AppGrant, AppVersion
from app.db.models_metrics import SemanticMetric
from app.files import store as files_store
from app.tools import metrics as metrics_tool
from app.tools.registry import ToolContext


def test_semantic_metric_to_caliber_card_bridge_spec():
    spec = semantic_metrics.to_caliber_card_spec(
        {
            "id": "sm-001",
            "key": "net_revenue",
            "name": "净营收",
            "aliases": ["净收入", "Net Revenue"],
            "description": "扣除退款后的实收金额",
            "source_id": "ds-fin",
            "table_name": "dwd_orders",
            "sql_expression": "SUM(pay_amount - refund_amount)",
            "date_column": "pay_date",
            "unit": "元",
            "dimensions": [{"key": "channel", "label": "渠道", "column": "channel"}],
        }
    )
    assert spec["kind"] == "caliber_card_spec"
    assert spec["bridge_source"] == "semantic_metric"
    assert spec["metric_id"] == "net_revenue"
    assert spec["card"]["caliber"] == "净营收"
    assert {f["name"] for f in spec["entry_fields"]} == {"date_from", "date_to", "channel"}
    assert spec["metrics"][0]["id"] == "net_revenue"


async def test_restricted_user_semantic_metric_acl_and_grounding_redaction(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AGENTLAB_AUTH_MODE", "local")
    allowed_ds_id = f"ds-allow-{uuid.uuid4().hex[:6]}"
    secret_ds_id = f"ds-secret-{uuid.uuid4().hex[:6]}"
    allowed_metric_key = f"m_allowed_{uuid.uuid4().hex[:6]}"
    secret_metric_key = f"m_secret_{uuid.uuid4().hex[:6]}"
    username = f"viewer_{uuid.uuid4().hex[:6]}"

    async with SessionLocal() as session:
        ds_allow = DataSource(id=allowed_ds_id, name=f"公开销售库_{allowed_ds_id}", kind="sqlite", database=":memory:", enabled=True)
        ds_secret = DataSource(id=secret_ds_id, name=f"核心薪酬库_{secret_ds_id}", kind="sqlite", database=":memory:", enabled=True)
        m_allow = SemanticMetric(
            id=uuid.uuid4().hex,
            key=allowed_metric_key,
            name="门店订单数",
            description="已授权门店订单计数",
            source_id=allowed_ds_id,
            table_name="public_orders",
            sql_expression="COUNT(order_id)",
            status="enabled",
        )
        m_secret = SemanticMetric(
            id=uuid.uuid4().hex,
            key=secret_metric_key,
            name="高管年薪总额",
            description="未授权敏感薪酬指标",
            source_id=secret_ds_id,
            table_name="secret_payroll_table",
            sql_expression="SUM(ceo_bonus_secret_col)",
            status="enabled",
        )
        app_row = App(id=uuid.uuid4().hex, name="门店看板", published_version=1, status="active", created_by="admin")
        app_ver = AppVersion(
            app_id=app_row.id,
            version=1,
            spec={},
            spec_hash="hash-v1",
            pins=[],
            bindings={"slots": {"main": {"source_id": allowed_ds_id}}},
        )
        grant = AppGrant(app_id=app_row.id, principal_kind="user", principal=username, role="use")
        session.add_all([ds_allow, ds_secret, m_allow, m_secret, app_row, app_ver, grant])
        await session.commit()

        permitted_ids = await semantic_metrics.authorized_metric_source_ids(session, username)
        assert allowed_ds_id in permitted_ids
        assert secret_ds_id not in permitted_ids

    token = deps.set_principal(deps.Principal(username=username, role="user", verified=True, mode="local"))
    try:
        # 1. 受限普通用户强行指定未授权指标 key 调用 run_metric_query 必须被拒绝
        tctx = ToolContext(started_by=username)
        denied = await metrics_tool.run_metric_query(tctx, metric=secret_metric_key)
        assert "没有权限" in denied

        # 2. Grounding 注入的系统提示词同样隔离未授权指标且不泄露底层物理表名/SQL 表达式
        async with SessionLocal() as session:
            bundle = await grounding.ground_question(session, "门店订单数是多少", restricted=True)
        assert allowed_metric_key in bundle.system_context
        assert secret_metric_key not in bundle.system_context
        assert "secret_payroll_table" not in bundle.system_context
        assert "public_orders" not in bundle.system_context
    finally:
        deps.reset_principal(token)


def test_csv_workbook_streams_lazily_without_full_parse(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    csv_bytes = ("\n".join(["id,amount"] + [f"{i},{i * 10}" for i in range(1, 2001)])).encode("utf-8")

    # 若 Workbooks.csv 仍调用全量 csv_grid.parse，此处桩函数会直接抛错
    def forbid_full_parse(*_args, **_kwargs):
        raise AssertionError("Workbooks.csv 应当走流式扫描，不应调用全量 csv_grid.parse")

    monkeypatch.setattr(csv_grid, "parse", forbid_full_parse)
    with xlsx_cells.Workbooks(csv_bytes) as wb:
        stream = wb.csv()
        assert isinstance(stream, csv_grid.CsvStream)
        assert stream._records_cache is None
        it = stream.iter_records()
        assert next(it) == ["id", "amount"]
        assert next(it) == ["1", "10"]
        assert stream._records_cache is None, "流式迭代前若干行不得把整张 CSV 缓存进内存"


async def test_linear_json_extract_and_copilot_heartbeat_disconnect_cancellation(
    monkeypatch: pytest.MonkeyPatch,
):
    # 1. O(N) _extract_json 在长前缀噪声 + 嵌套 JSON + 尾部文本下瞬间提取且不退化为 O(N^2)
    noisy = "思考过程：" + ("无关前导文本 { 伪括号 " * 3000) + '\n{"ops": [{"op": "add_node", "id": "n1"}], "ok": true}\n补充说明'
    t0 = time.perf_counter()
    parsed = _extract_json(noisy)
    elapsed = time.perf_counter() - t0
    assert parsed == {"ops": [{"op": "add_node", "id": "n1"}], "ok": True}
    assert elapsed < 0.25, f"_extract_json 耗时过长（{elapsed:.3f}s），疑似退化"

    # 2. _with_heartbeat 在 SSE 客户端断开（aclose）时立即取消在途（pending）的慢 LLM 迭代任务
    monkeypatch.setattr(copilot_api, "_HEARTBEAT_SECONDS", 0.02)
    cancelled_flag = False

    async def slow_llm_stream():
        nonlocal cancelled_flag
        yield {"op": "plan", "text": "first"}
        try:
            await asyncio.sleep(60)
            yield {"op": "done"}
        except asyncio.CancelledError:
            cancelled_flag = True
            raise

    gen = copilot_api._with_heartbeat(slow_llm_stream())
    first_item = await anext(gen)
    assert first_item == {"op": "plan", "text": "first"}
    # 触发一次保活心跳，确保后台 slow_llm_stream 已处于在途 pending 阻塞态
    hb_item = await anext(gen)
    assert hb_item["op"] == "heartbeat"
    await gen.aclose()
    await asyncio.sleep(0)
    assert cancelled_flag is True, "SSE 客户端断开后未取消后台慢生成协程"


async def test_files_store_bucket_lock_and_cluster_signal_pending_retention(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True)
    monkeypatch.setattr(settings, "data_dir", data_dir)

    async def _save_one(name: str):
        async with SessionLocal() as session:
            return await files_store.save(
                session,
                b"same-content-concurrent",
                name,
                uploaded_by="alice",
                context={},
            )

    row1, row2 = await asyncio.gather(_save_one("a.txt"), _save_one("b.txt"))
    assert row1.sha256 == row2.sha256

    # cluster/signal.py 在尚未建立 _conn 连接时不得在 _flush_loop 轮转中清空丢弃 _pending 信号
    sig = Signals()
    sig._pending[("cache", "kb_vectors")] = "cache|owner|kb_vectors"
    sig._conn = None
    sig._stopping = False
    sig._wake = asyncio.Event()
    sig._wake.set()
    flusher = asyncio.create_task(sig._flush_loop())
    await asyncio.sleep(0.06)
    sig._stopping = True
    sig._wake.set()
    await flusher
    assert ("cache", "kb_vectors") in sig._pending


def test_backend_decoupling_and_frontend_devops_architecture_gates():
    repo_root = Path(__file__).resolve().parents[2]

    # 1. 验证解耦后的核心引擎/工具/基础模块零向上依赖 app.api.*
    decoupled_files = [
        repo_root / "backend" / "app" / "core" / "coded.py",
        repo_root / "backend" / "app" / "core" / "json_extract.py",
        repo_root / "backend" / "app" / "capabilities" / "check_state.py",
        repo_root / "backend" / "app" / "engine" / "evidence_reader.py",
        repo_root / "backend" / "app" / "engine" / "judge.py",
        repo_root / "backend" / "app" / "engine" / "review.py",
        repo_root / "backend" / "app" / "tools" / "mcp_manager.py",
    ]
    violations: list[str] = []
    for py_file in decoupled_files:
        tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        rel = py_file.relative_to(repo_root)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("app.api"):
                        violations.append(f"{rel}:{node.lineno} import {alias.name}")
            elif isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("app.api"):
                violations.append(f"{rel}:{node.lineno} from {node.module} import ...")
    assert violations == [], f"核心领域模块仍存在向 app.api 层的反向依赖：{violations}"

    # 2. 验证前端首屏 App.tsx 已移除对重型 useStudio / useChat 的顶层运行时静态导入（仅保留编译期擦除的 import type）
    app_tsx = (repo_root / "frontend" / "src" / "App.tsx").read_text(encoding="utf-8")
    runtime_store_imports = re.findall(
        r"^\s*import\s+(?!type\b).*from\s+['\"]\./store/(?:studio|chat)['\"]",
        app_tsx,
        flags=re.MULTILINE,
    )
    assert runtime_store_imports == [], f"App.tsx 仍存在顶层运行时 Store 静态导入：{runtime_store_imports}"
    assert (repo_root / "frontend" / "src" / "store" / "authSession.ts").exists()

    decode_ts = (repo_root / "frontend" / "src" / "run" / "decode.ts").read_text(encoding="utf-8")
    assert "export function createRunDecoder" in decode_ts

    surface_css = (repo_root / "frontend" / "src" / "canvas" / "surface.css").read_text(encoding="utf-8")
    index_css = (repo_root / "frontend" / "src" / "index.css").read_text(encoding="utf-8")
    assert "@layer components" in surface_css
    assert "@layer components" in index_css

    # 3. 验证检查脚本不再硬编码 macOS Chrome 路径，且 CI 与 .dockerignore 已加固
    for script_name in ("check-guards.mjs", "check-auth.mjs", "check-canvas-layout.mjs"):
        script_src = (repo_root / "scripts" / script_name).read_text(encoding="utf-8")
        assert "/Applications/Google Chrome.app" not in script_src
        assert "resolveChrome" in script_src

    dockerignore = (repo_root / ".dockerignore").read_text(encoding="utf-8")
    assert ".claude" in dockerignore and "release" in dockerignore

    ci_yml = (repo_root / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "--no-deps -e backend" in ci_yml
    assert "backend-postgres:" in ci_yml
    assert "check-docs.py" in ci_yml
