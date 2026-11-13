"""第三批架构级演进回归测试（Step 3.1 – 3.4）。

覆盖：
1. 记录库 `records` 多版本单库归档演进（突破 SQLite 10 库 `ATTACH` 上限、增量同步、WAL 与 `records_archive` 越权拦截）；
2. 知识库真·双路混合检索（零词面重合高语义命中不被 BM25 前置截断、向量缓存失效、>1000 块倒排分批更新）；
3. 方言感知 SQL 模板词法分析（MySQL `\\'`/`#`、PostgreSQL `E'...'`/`$$...$$`/嵌套注释/数组下标、Oracle `q'[...]'`、SQLite `[id]`）与 `Decimal`/`bool`/`NaN` 参数完整性；
4. 分层依赖守卫（`app/ledger/*` 零依赖 `app.api.settings` / `app.api.ledger` / `app.api.evidence`）与前端组件拆分门禁。
"""
from __future__ import annotations

import ast
import os
import re
import sqlite3
import subprocess
import uuid
from decimal import Decimal
from pathlib import Path

import numpy as np
import pytest
from sqlalchemy import select

from app.core.config import settings
from app.data.engine import run_query
from app.data.guard import SqlRejected
from app.data.sql_semantics import _template_slots
from app.db.base import SessionLocal
from app.db.models import Chunk, DataSource, Document
from app.db.models_apps import App
from app.db.models_blueprints import RecordsTable
from app.engine.expressions import render_template
from app.ledger import params
from app.memory import embeddings as emb
from app.memory import inverted, kb
from app.records import store
from app.records.tables import columns_of, ensure_table, path_of
from app.records.views import ARCHIVE_SCHEMA, resolve


@pytest.fixture
def isolated_data_dir(monkeypatch, tmp_path):
    data = tmp_path / "data"
    (data / "uploads").mkdir(parents=True)
    monkeypatch.setattr(settings, "data_dir", data)
    kb.invalidate_vector_cache()
    yield data
    kb.invalidate_vector_cache()


# ---------------------------------------------------------------------------
# Step 3.1：记录库 >10 版本单库归档、增量同步与安全边界
# ---------------------------------------------------------------------------


async def test_records_scale_beyond_sqlite_attach_limit_with_incremental_archive(isolated_data_dir):
    async with SessionLocal() as session:
        owner = App(name="多版本报损台账", created_by="admin")
        session.add(owner)
        await session.commit()
        app_id = owner.id

    versions: list[RecordsTable] = []
    inserted_row_ids: dict[int, str] = {}

    # 连续演进 14 个不兼容版本（每次新增一个必填列），远超 SQLite 默认 SQLITE_MAX_ATTACHED=10 上限
    for idx in range(1, 15):
        cols = [
            {"name": "门店", "label": "门店", "type": "string", "required": True},
            {"name": "数量", "label": "数量", "type": "integer", "required": True},
            {"name": f"必填项_{idx}", "label": f"必填项_{idx}", "type": "string", "required": True},
        ]
        async with SessionLocal() as session:
            owner = await session.get(App, app_id)
            tbl = await ensure_table(session, owner, cols)
            await session.commit()
        assert tbl.version == idx
        rec = store.append(
            path_of(tbl),
            columns_of(tbl),
            {"门店": f"门店_{idx:02d}", "数量": idx * 10, f"必填项_{idx}": f"v{idx}"},
            version=tbl.version,
            run_id=None,
            actor="amy",
            action_key=uuid.uuid4().hex,
        )
        inserted_row_ids[idx] = rec.row_id
        versions.append(tbl)

    # 验证单表库开启了 WAL 模式
    with sqlite3.connect(path_of(versions[-1])) as conn:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert str(mode).lower() == "wal"

    # 在将被归档的第 2 版中做一次更正（supersedes），确保归档表保留 _supersedes 演进语义
    store.append(
        path_of(versions[1]),
        columns_of(versions[1]),
        {"门店": "门店_02", "数量": 25, "必填项_2": "v2_fixed"},
        version=2,
        run_id=None,
        actor="amy",
        action_key=uuid.uuid4().hex,
        supersedes=inserted_row_ids[2],
    )

    latest = versions[-1]
    async with SessionLocal() as session:
        source = await session.get(DataSource, latest.source_id)
        resolved = await resolve(session, source)

    # 13 个历史版本若逐库 ATTACH 会触发 OperationalError: too many attached databases (max 10)
    # 归档收敛后挂接数 <= 8（7 个近期直挂库 + 1 个归档库）
    assert len(resolved.attach) <= 8
    assert ARCHIVE_SCHEMA in dict(resolved.attach)
    assert store.archive_path(latest.table_key).exists()

    dbs = await run_query(resolved, "PRAGMA database_list")
    assert len(dbs.rows) <= 10

    summary = await run_query(resolved, 'SELECT count(*) AS n, sum("数量") AS total FROM records')
    expected_total = sum(i * 10 for i in range(1, 15)) - 20 + 25
    assert summary.rows == [[14, expected_total]]

    # 模拟停用前已在途的动作向第 1 版追加一行，验证 sync_archive 按 _seq > since_seq 增量同步
    store.append(
        path_of(versions[0]),
        columns_of(versions[0]),
        {"门店": "门店_01_补单", "数量": 5, "必填项_1": "v1_late"},
        version=1,
        run_id=None,
        actor="bob",
        action_key=uuid.uuid4().hex,
    )
    async with SessionLocal() as session:
        refreshed = await resolve(session, source)
    summary_after = await run_query(refreshed, 'SELECT count(*) AS n, sum("数量") AS total FROM records')
    assert summary_after.rows == [[15, expected_total + 5]]

    # 直接点名归档 schema 必须被 SQL 守卫拦截
    for forbidden_sql in (
        f"SELECT count(*) FROM {ARCHIVE_SCHEMA}.rows_v1",
        f'SELECT count(*) FROM "{ARCHIVE_SCHEMA}"."rows_v1"',
        f"SELECT count(*) FROM [{ARCHIVE_SCHEMA.upper()}].rows_v1",
    ):
        with pytest.raises(SqlRejected):
            await run_query(refreshed, forbidden_sql)


# ---------------------------------------------------------------------------
# Step 3.2：真·双路混合检索、缓存失效与超千块倒排分批处理
# ---------------------------------------------------------------------------


async def test_kb_true_dual_path_hybrid_search_finds_zero_lexical_overlap_hit(isolated_data_dir, monkeypatch):
    target_text = "超级管理员可调整他人角色与访问范围。"
    distractors = [f"审批规范第 {i} 条：普通报销单需部门主管签字归档。" for i in range(65)]

    async def fake_embed_texts(texts: list[str]) -> list[np.ndarray]:
        vecs: list[np.ndarray] = []
        for t in texts:
            v = np.zeros(emb.embedder_dim(), dtype=np.float32)
            if "超级管理员" in t:
                v[0] = 1.0
            else:
                v[1] = 1.0
            vecs.append(v)
        return vecs

    async def fake_embed_text(query: str) -> np.ndarray:
        v = np.zeros(emb.embedder_dim(), dtype=np.float32)
        v[0] = 1.0
        return v

    monkeypatch.setattr(kb, "embed_texts", fake_embed_texts)
    monkeypatch.setattr(kb, "embed_text", fake_embed_text)

    collection = f"dual_path_{uuid.uuid4().hex[:8]}"
    async with SessionLocal() as session:
        # 65 条词面强命中「审批规范」的干扰块（超过原 BM25 单路候选池上限 50）
        for idx, text in enumerate(distractors):
            await kb.ingest_document(session, collection=collection, title=f"干扰_{idx}", content=text)
        # 1 条零词面重合但语义完全一致的目标块
        target_doc = await kb.ingest_document(session, collection=collection, title="权限说明", content=target_text)

    degrade_reasons: list[str] = []
    async with SessionLocal() as session:
        hits = await kb.search(
            session,
            collection=collection,
            query="审批规范里谁能改权限",
            limit=5,
            alpha=0.6,
            on_degrade=degrade_reasons.append,
        )

    assert degrade_reasons == []
    assert hits, "双路召回应当返回结果"
    assert hits[0]["content"] == target_text, "零词面重合但余弦相似度为 1.0 的目标块在双路混合检索下应排第一"

    # 删除目标文档后，向量矩阵缓存自动失效，不再返回已删块
    async with SessionLocal() as session:
        deleted = await kb.delete_document(session, target_doc.id)
        assert deleted is True
        after_delete = await kb.search(
            session,
            collection=collection,
            query="审批规范里谁能改权限",
            limit=5,
            alpha=0.6,
        )
    assert all(h["content"] != target_text for h in after_delete)


async def test_inverted_index_handles_over_thousand_chunks_without_bind_limit(isolated_data_dir):
    collection = f"batch_inv_{uuid.uuid4().hex[:8]}"
    doc_id = f"doc_{uuid.uuid4().hex[:12]}"
    chunk_ids: list[str] = []
    made: list[Chunk] = []
    async with SessionLocal() as session:
        session.add(Document(id=doc_id, collection=collection, title="bulk", content="bulk"))
        await session.flush()
        for i in range(1100):
            cid = uuid.uuid4().hex
            chunk_ids.append(cid)
            chunk = Chunk(
                id=cid,
                document_id=doc_id,
                collection=collection,
                ordinal=i,
                content=f"统一结算批次规范条目 {i}",
                embedding=b"",
                embed_model="",
                embed_dim=0,
                meta={"title": "bulk"},
            )
            session.add(chunk)
            made.append(chunk)
        await session.flush()
        for chunk in made:
            await inverted.index_chunk(session, chunk)
        await session.commit()

    async with SessionLocal() as session:
        scored, all_scores = await inverted.candidates_with_scores(
            session, collection=collection, query="统一结算批次规范", limit=25,
        )
        assert len(scored) == 25
        assert len(all_scores) == 1100
        assert scored[0][1] > 0

        # 超 1000 个 chunk_id 删除时自动分批，不触发 SQLite 999 / PG 32767 绑定参数上限
        await inverted.drop_chunk_terms(session, chunk_ids)
        await session.commit()
        after, after_all = await inverted.candidates_with_scores(
            session, collection=collection, query="统一结算批次规范", limit=25,
        )
        assert after == []
        assert after_all == {}


# ---------------------------------------------------------------------------
# Step 3.3：方言感知 SQL 模板词法分析与 Decimal / bool / NaN 参数完整性
# ---------------------------------------------------------------------------


def test_dialect_aware_sql_template_slots():
    # MySQL 反斜杠转义单引号与 # 行注释：注释里的模板识别为 comment，真参数仍在 WHERE 后作为 bare
    mysql_sql = r"SELECT 'it\'s fine' AS tag # 忽略 {{ input.ignored }}" + "\nFROM orders WHERE id = {{ input.order_id }}"
    mysql_spans, _ = _template_slots(mysql_sql, dialect="mysql")
    assert [(s.field, s.context) for _, _, s in mysql_spans] == [
        ("ignored", "comment"),
        ("order_id", "bare"),
    ]

    # PostgreSQL $$...$$ 美元引号、E'...' 转义串、嵌套块注释与数组下标 arr[{{ input.idx }}]
    pg_sql = (
        "/* 外层 /* 内层 {{ input.in_comment }} */ 结束 */\n"
        "SELECT $$raw {{ input.in_dollar }}$$ AS d, E'escaped \\' {{ input.in_estr }}' AS e, "
        "tags[{{ input.idx }}] AS picked FROM items"
    )
    pg_spans, _ = _template_slots(pg_sql, dialect="postgres")
    assert [(s.field, s.context) for _, _, s in pg_spans] == [
        ("in_comment", "comment"),
        ("in_dollar", "quoted"),
        ("in_estr", "quoted"),
        ("idx", "bare"),
    ]

    # Oracle q'[...]' 替代引号字面量识别为 quoted 上下文
    oracle_sql = "SELECT q'[ Department's {{ input.dept }} ]' FROM dual"
    oracle_spans, _ = _template_slots(oracle_sql, dialect="oracle")
    assert [(s.field, s.context) for _, _, s in oracle_spans] == [("dept", "quoted")]

    # SQLite 方括号标识符里的占位符属于不支持的标识符注入位，必须拒绝
    sqlite_sql = "SELECT * FROM [{{ input.table_name }}]"
    with pytest.raises(params.ParameterRefused) as exc_info:
        params.slots_of(sqlite_sql, dialect="sqlite")
    assert exc_info.value.code == params.UNSUPPORTED


def test_decimal_boolean_and_non_finite_parameter_integrity():
    # 极小 Decimal 在表达式求值与参数写出时不退化为科学计数法 1E-7
    rendered = render_template("{{ input.rate }}", {"input": {"rate": Decimal("1E-7")}})
    assert rendered == "0.0000001"

    fields = [
        {"name": "amount", "type": "decimal", "required": True},
        {"name": "active", "type": "boolean", "required": True},
    ]
    template = "SELECT * FROM ledger_tx WHERE amount >= {{ input.amount }} AND is_active = {{ input.active }}"
    assert params.check_template(template, fields=params.declared(fields), dialect="postgres") == []

    stmt = params.plan(
        template,
        fields=fields,
        values={"amount": Decimal("19.95"), "active": True},
        dialect="postgres",
    )
    assert "amount >= 19.95" in stmt.statement
    assert "is_active = True" in stmt.statement
    by_name = {entry["name"]: entry for entry in stmt.material["parameters"]}
    assert by_name["amount"]["kind"] == "number" and by_name["amount"]["declared"] == "number"
    assert by_name["active"]["kind"] == "boolean" and by_name["active"]["declared"] == "boolean"

    # NaN / Inf 绝不能当作合法裸数拼入 SQL（否则会变成列名 NaN / Inf）
    for bad_number in (float("nan"), float("inf"), float("-inf"), Decimal("NaN"), Decimal("Infinity")):
        with pytest.raises(params.ParameterRefused) as exc_info:
            params.plan(
                "SELECT * FROM ledger_tx WHERE amount >= {{ input.amount }}",
                fields=[{"name": "amount", "type": "decimal", "required": True}],
                values={"amount": bad_number},
                dialect="postgres",
            )
        assert exc_info.value.code == params.LITERAL


# ---------------------------------------------------------------------------
# Step 3.3 & 3.4：分层依赖边界与前端超大文件拆分门禁
# ---------------------------------------------------------------------------


def test_ledger_layer_has_no_upward_api_domain_imports():
    repo_root = Path(__file__).resolve().parents[2]
    ledger_dir = repo_root / "backend" / "app" / "ledger"
    forbidden_modules = {"app.api.settings", "app.api.ledger", "app.api.evidence"}

    violations: list[str] = []
    for py_file in sorted(ledger_dir.rglob("*.py")):
        tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        rel = py_file.relative_to(repo_root)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name in forbidden_modules:
                        violations.append(f"{rel}:{node.lineno} import {alias.name}")
            elif isinstance(node, ast.ImportFrom) and node.module:
                if node.module in forbidden_modules:
                    violations.append(f"{rel}:{node.lineno} from {node.module} import ...")
                elif node.module == "app.api":
                    for alias in node.names:
                        full = f"app.api.{alias.name}"
                        if full in forbidden_modules:
                            violations.append(f"{rel}:{node.lineno} from app.api import {alias.name}")

    assert violations == [], f"ledger 领域层出现了对 api 路由层的反向依赖：{violations}"

    listing_py = repo_root / "backend" / "app" / "records" / "listing.py"
    listing_src = listing_py.read_text(encoding="utf-8")
    assert "app.api.evidence" not in listing_src
    assert "app.engine.evidence_reader" in listing_src


def test_caliber_drafts_page_is_modularized():
    repo_root = Path(__file__).resolve().parents[2]
    drafts_page = repo_root / "frontend" / "src" / "pages" / "ledger" / "CaliberDrafts.tsx"
    drafts_dir = repo_root / "frontend" / "src" / "pages" / "ledger" / "drafts"

    line_count = len(drafts_page.read_text(encoding="utf-8").splitlines())
    assert line_count < 900, f"CaliberDrafts.tsx 仍超过 900 行（当前 {line_count} 行）"
    expected_modules = {
        "helpers.ts",
        "DraftSharedBlocks.tsx",
        "DraftTrialSection.tsx",
        "DraftRulesSection.tsx",
        "DraftImportsSection.tsx",
        "DraftNlSection.tsx",
        "DraftValidationSection.tsx",
    }
    actual_modules = {p.name for p in drafts_dir.glob("*.ts*")}
    assert expected_modules.issubset(actual_modules)


def test_shell_scripts_brace_variables_before_utf8_and_retention_cleanup(tmp_path):
    """macOS 自带 Bash 3.2 在 UTF-8 全角标点（如 0xEF 开头的「（」「，」「：」）紧跟未加花括号的 $VAR 时，
    会把首字节 0xEF（Latin-1 字母 ï）吞入变量名，在 set -u 下触发 `VAR\\xef: unbound variable`。"""
    repo_root = Path(__file__).resolve().parents[2]
    unbraced_utf8_var = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)([^\x00-\x7f])")

    offenders: list[str] = []
    for sh_file in sorted(repo_root.rglob("*.sh")):
        if ".git" in sh_file.parts or "node_modules" in sh_file.parts:
            continue
        for lineno, line in enumerate(sh_file.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.lstrip()
            if stripped.startswith("#"):
                continue
            for m in unbraced_utf8_var.finditer(line):
                offenders.append(f"{sh_file.relative_to(repo_root)}:{lineno} ${m.group(1)}{m.group(2)}")
    assert offenders == [], f"Shell 脚本存在未加花括号且紧邻非 ASCII 字符的变量展开：{offenders}"

    # 提取 package-docker.sh 第 4 节清理逻辑，在真实 bash -euo pipefail 下验证含空格路径与保留份数
    pkg_text = (repo_root / "scripts" / "package-docker.sh").read_text(encoding="utf-8")
    start = pkg_text.index("# ---- 4. 清理旧离线包 ----")
    end = pkg_text.index("size() {", start)
    cleanup_snippet = pkg_text[start:end]

    out_dir = tmp_path / "release with space"
    out_dir.mkdir()
    stems = [f"agentlab-v{i}-linux-arm64" for i in range(1, 6)]
    for idx, stem in enumerate(stems, 1):
        tar_file = out_dir / f"{stem}.tar"
        tar_file.write_bytes(b"x")
        ts = 1_700_000_000 + idx * 60
        os.utime(tar_file, (ts, ts))

    proc = subprocess.run(
        ["bash", "-e", "-u", "-o", "pipefail", "-c", cleanup_snippet],
        env={
            **os.environ,
            "KEEP_RELEASES": "3",
            "NAME": stems[-1],
            "OUT": str(out_dir),
            "ARCH": "arm64",
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    remaining = sorted(p.name for p in out_dir.glob("*.tar"))
    assert remaining == [
        "agentlab-v3-linux-arm64.tar",
        "agentlab-v4-linux-arm64.tar",
        "agentlab-v5-linux-arm64.tar",
    ]
