"""第四批（Batch 4 / P0）：安全边界闭环与运行正确性止血回归测试。

覆盖：
1. SSRF 全地址段封堵（RFC 6598 `100.64.0.0/10` 含阿里云元数据 `100.100.100.200`、NAT64/6to4/Teredo/IPv6 Zone ID）、重定向上限死代码修复；
2. `/api/auth/bootstrap` 在无密码 OIDC 管理员存在或已初始化后不可重开、拒绝 `svc-` 保留前缀与并发互斥；
3. 掩码密钥绑定目标 Host 防篡改外泄 + 外部拒绝直传 `enc::` 密文防解密预言机；
4. SQL 只读守卫补扫顶层 `VALUES` / Oracle `UTL_HTTP` / `DELETE (SELECT ...)`、驱动连接参数白名单防 `local_infile`、SQL 模板运行时字面量与 `semantic_metrics` 正则反斜杠防崩；
5. `isolation="strict"` 沙箱失败闭合（拒绝静默降级 `LocalSandbox`）及内置 `file_write` 与代码沙箱工作区对齐；
6. `EventBus.close()` 满队列必达 `None` 哨兵与瞬态 `llm.token` 不挤出结构性节点事件。
"""
from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from app.api import auth as auth_api
from app.capabilities import secrets
from app.core import crypto
from app.core.bus import EventBus, _QUEUE_MAXSIZE
from app.core.config import settings
from app.data import engine as data_engine
from app.data import guard as sql_guard
from app.data import semantic_metrics
from app.db.base import SessionLocal
from app.db.models import DataSource, User
from app.engine.expressions import ExpressionError, eval_expression
from app.ledger import params
from app.main import app
from app.sandbox import manager as sandbox_mgr
from app.sandbox.local_sandbox import LocalSandbox
from app.tools import net
from app.tools.builtin import files as builtin_files
from app.tools.registry import ToolContext


async def test_ssrf_blocks_rfc6598_aliyun_metadata_ipv6_transitions_and_redirect_cap(monkeypatch: pytest.MonkeyPatch):
    # 1. RFC 6598 100.64.0.0/10（Python ipaddress.is_private 为 False）与阿里云元数据 IP 必须拦截
    assert net._is_private("100.100.100.200") is True
    assert net._is_private("100.64.0.1") is True
    assert net._is_metadata("100.100.100.200") is True

    # 2. NAT64 / 6to4 / Zone ID 包装的元数据地址必须识别拦截
    assert net._is_metadata("64:ff9b::a9fe:a9fe") is True
    assert net._is_metadata("2002:a9fe:a9fe::1") is True
    assert net._is_metadata("fd00:ec2::254%eth0") is True
    assert net._is_private("64:ff9b::6464:64c8") is True  # NAT64 -> 100.100.100.200

    # 3. 即使 allow_intranet=True，解析到云元数据 IP 的域名也必须拦截
    monkeypatch.setattr(
        net.socket,
        "getaddrinfo",
        lambda host, port, *a, **kw: [(net.socket.AF_INET, net.socket.SOCK_STREAM, 6, "", ("100.100.100.200", port))],
    )
    with pytest.raises(net.UnsafeUrlError, match="元数据"):
        await net.assert_safe_url("http://metadata-alias.internal/latest/meta-data", allow_intranet=True)

    # 4. safe_request 连续重定向超过上限必须抛 UnsafeUrlError（原第 6 跳落出循环死代码已修复）
    monkeypatch.setattr(
        net.socket,
        "getaddrinfo",
        lambda host, port, *a, **kw: [(net.socket.AF_INET, net.socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))],
    )

    def redirect_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"Location": "http://example.com/loop"}, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(redirect_handler)) as mock_client:
        with pytest.raises(net.UnsafeUrlError, match="重定向次数过多"):
            await net.safe_request("GET", "http://example.com/start", client=mock_client)


async def test_bootstrap_cannot_reopen_with_oidc_admin_and_rejects_svc_prefix(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AGENTLAB_AUTH_MODE", "local")
    admin_id = uuid.uuid4().hex
    async with SessionLocal() as session:
        # 模拟仅存在无密码的 OIDC 管理员（password_hash=None）
        oidc_admin = User(
            id=admin_id,
            username=f"oidc-admin-{admin_id[:6]}",
            display_name="OIDC Admin",
            role="admin",
            password_hash=None,
            disabled=False,
        )
        session.add(oidc_admin)
        await session.commit()

    try:
        async with SessionLocal() as session:
            assert await auth_api._can_bootstrap(session) is False
            with pytest.raises(Exception, match="svc-"):
                await auth_api._create_admin(
                    session,
                    auth_api.BootstrapIn(username="svc-internal", password="StrongPassword!123"),
                )

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                "/api/auth/bootstrap",
                json={"username": "attacker-admin", "password": "StrongPassword!123"},
            )
            assert resp.status_code in (403, 409), resp.text
    finally:
        async with SessionLocal() as session:
            row = await session.get(User, admin_id)
            if row is not None:
                await session.delete(row)
                await session.commit()


def test_secrets_seal_blocks_ciphertext_oracle_and_host_change_exfiltration():
    saved_enc = crypto.encrypt("sk-real-production-secret-token")
    assert saved_enc is not None and saved_enc.startswith("enc::")
    masked = crypto.mask(saved_enc)

    # 1. 未修改目标地址时，回传掩码保持原密文
    kept = secrets.seal({"Authorization": masked}, {"Authorization": saved_enc}, target_changed=False)
    assert crypto.decrypt(kept["Authorization"]) == "sk-real-production-secret-token"

    # 2. 修改目标主机地址后回传旧掩码，必须拒绝（防 Confused Deputy 窃取密钥）
    with pytest.raises(ValueError, match="修改目标地址"):
        secrets.seal({"Authorization": masked}, {"Authorization": saved_enc}, target_changed=True)

    # 3. 外部直接提交偷来的其它 enc:: 密文，必须拒绝（防解密预言机）
    other_cipher = crypto.encrypt("other-datasource-db-password")
    with pytest.raises(ValueError, match="密文"):
        secrets.seal({"X-Leak": other_cipher}, {"Authorization": saved_enc}, target_changed=False)


def test_sql_guard_values_oracle_and_driver_options_hardening():
    # 1. 顶层 VALUES 调用 set_config 关闭只读会话必须被只读守卫拦截
    with pytest.raises(sql_guard.SqlRejected):
        sql_guard.check(
            "VALUES (set_config('default_transaction_read_only', 'off', false))",
            readonly=True,
            dialect="postgresql",
        )

    # 2. Oracle 自治事务与 UTL_HTTP 网络外联必须被拦截
    with pytest.raises(sql_guard.SqlRejected):
        sql_guard.check(
            "SELECT utl_http.request('http://169.254.169.254/latest/meta-data') FROM dual",
            readonly=True,
            dialect="oracle",
        )

    # 3. DELETE (SELECT ...) 不得因 `(?!\\s*\\()` 漏网，而 MySQL SELECT INSERT('abcd', 2, 1, 'x') 同名函数照常放行
    with pytest.raises(sql_guard.SqlRejected):
        sql_guard.check("WITH t AS (SELECT 1) DELETE (SELECT * FROM orders)", readonly=True, dialect="mysql")
    sql_guard.check("SELECT INSERT('abcd', 2, 1, 'X'), TRUNCATE(12.345, 1)", readonly=True, dialect="mysql")

    # 4. DataSource.options 禁止注入 local_infile / read_default_file / init_command 等高危客户端驱动参数
    bad_ds = DataSource(
        id=uuid.uuid4().hex,
        name="bad-mysql",
        kind="mysql",
        host="127.0.0.1",
        port=3306,
        username="u",
        database="d",
        options={"local_infile": "1"},
    )
    with pytest.raises(ValueError, match="local_infile"):
        data_engine.build_url(bad_ds)


def test_sql_template_runtime_literal_and_semantic_metric_regex_backslash_safety():
    # 1. PostgreSQL $$...$$ 美元引号内的模板槽位不得将含 $ 的文本当作安全单引号字面量放行
    probs = params.runtime_problems(
        "SELECT $$hello {{ input.tag }}$$ AS msg",
        declared={"tag": {"name": "tag", "type": "text"}},
        values={"tag": "x$$; DROP TABLE users; --"},
        dialect="postgresql",
    )
    assert probs, "美元引号内含 $ 的注入载荷必须在运行时被拦截"

    # 2. semantic_metrics: 过滤值含正则反向引用 `\\1` / `\\g<0>` 或末尾反斜杠时，compile_metric_sql 不得抛 re.error
    ds = DataSource(id="ds-1", name="sales", kind="mysql", database="shop", readonly=True)
    metric_def = {
        "key": "gmv",
        "name": "销售额",
        "table_name": "orders",
        "sql_expression": "SUM(amount)",
        "sql_template": "SELECT {{select_dims}} SUM(amount) AS gmv FROM orders {{where}} {{group_by}} {{order_by}} LIMIT {{limit}}",
        "date_column": "created_at",
        "dimensions": [{"key": "note", "label": "备注", "column": "note"}],
    }
    compiled_sql, _ = semantic_metrics.compile_metric_sql(
        metric_def,
        source=ds,
        filters={"note": r"foo\1\g<0>bar\baz"},
    )
    assert "orders" in compiled_sql
    assert r"\\" in compiled_sql  # MySQL 方言下反斜杠已转义

    with pytest.raises(ValueError, match="NaN|Infinity"):
        semantic_metrics._sql_literal(float("nan"), dialect="postgresql")

    # 3. 表达式幂运算位数上限保护（防止超过 Python 4300 位 int_max_str_digits 击穿 JSON 序列化）
    with pytest.raises(ExpressionError, match="位数上限"):
        eval_expression("(10 ** 14) ** 1024", {})


async def test_strict_sandbox_fails_closed_and_builtin_files_share_workspace(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True)
    monkeypatch.setattr(settings, "data_dir", data_dir)

    # 1. 当 microVM 不可用时，节点请求 strict 硬件隔离必须失败闭合，绝不能静默退化到裸跑 LocalSandbox
    monkeypatch.setattr(sandbox_mgr.MicroVMSandbox, "available", staticmethod(lambda: False))
    monkeypatch.setattr(sandbox_mgr.MicroVMSandbox, "unavailable_reason", staticmethod(lambda: "缺少 /dev/kvm"))
    monkeypatch.setattr(settings, "sandbox_backend", "local")
    mgr = sandbox_mgr.SandboxManager()
    strict_box = await mgr.get("strict")
    assert isinstance(strict_box, sandbox_mgr.UnavailableSandbox)
    res = await strict_box.run("print('should not run on host')")
    assert res.ok is False and "拒绝降级" in (res.error or "")

    # 2. 内置 file_write 与代码执行沙箱共享同一会话目录，写入后 python 子进程立即可读，cleanup 一并回收
    await mgr.get()
    monkeypatch.setattr(builtin_files, "sandbox_manager", mgr)
    sid = f"sess-{uuid.uuid4().hex[:8]}"
    write_out = await builtin_files.file_write(
        ToolContext(sandbox_session=sid),
        path="hello.txt",
        content="agentlab-unified",
    )
    assert write_out.get("ok") is True
    exec_res = await LocalSandbox().run(
        "from pathlib import Path\nprint(Path('hello.txt').read_text())",
        session_id=sid,
    )
    assert exec_res.ok is True
    assert exec_res.stdout.strip() == "agentlab-unified"
    ws_dir = await mgr.workspace_for(sid)
    assert ws_dir.exists()
    await mgr.cleanup(sid)
    assert not ws_dir.exists()


async def test_event_bus_close_delivers_sentinel_when_full_and_protects_structural_events():
    bus = EventBus()
    run_id = f"run-{uuid.uuid4().hex[:8]}"
    sub = bus.attach(run_id)

    # 先放入一条关键结构性事件，再用超量瞬态 llm.token 灌满队列
    await bus.publish({"run_id": run_id, "seq": 1, "type": "node.started", "node": "n1"})
    for idx in range(2, _QUEUE_MAXSIZE + 80):
        await bus.publish({"run_id": run_id, "seq": idx, "type": "llm.token", "ephemeral": True, "text": "x"})
    await bus.publish({"run_id": run_id, "seq": _QUEUE_MAXSIZE + 90, "type": "run.completed"})

    # 队列满载时调用 close()，结束哨兵 None 绝不能被丢弃，stream() 必须在有限时间内正常收敛退出
    await bus.close(run_id)

    received_types: list[str] = []
    with sub:
        async with asyncio.timeout(2.0):
            async for ev in sub.stream():
                received_types.append(str(ev.get("type")))

    assert "node.started" in received_types
    assert "run.completed" in received_types
    assert sub.dropped > 0
