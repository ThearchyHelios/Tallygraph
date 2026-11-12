"""后台口径文档一键拆解与智能指标提取任务引擎（SemanticDecomposeJob）。

一次性打通业务文档（粘贴 Markdown、上传附件、选择知识库文档）与数据源目录的四层语义资产：
1. 知识库归档与判异红线留存（KB Document + guardrail_notes）：自动将粘贴或上传的口径总表存入知识库，保留物理红线、异常阈值与「暂无数据源」说明供问数检索。
2. 字段目录补丁（catalog_patches）：提取表中文名、粒度、业务日期列、有效过滤条件，以及列级符号映射（如 EXT_VAL_01 → 计划开班时长）、单位、流量或存量角色、存储陷阱备注；经 catalog.plan_patch 严格核验，并在内存里即时注入下一阶段！
3. 单表与多表参数化指标口径（metric_candidates）：结合最新字段符号映射，同时提取单表聚合表达式（calc_mode="expression"）与跨表 JOIN / CTE / 动态周期天数参数化模板（calc_mode="sql_template"），逐条通过 AST 与只读守卫预编译核验。
4. 标准业务实体与物理列编码映射（entity_changes）：提取产线、车间、模具、不良代码等共用维度实体及表列绑定，经 entity_patch.plan_entity_patch 核验。
"""
from __future__ import annotations

import asyncio
import contextlib
import copy
import json
import logging
import re
import time
from collections.abc import Mapping, Sequence
from datetime import timezone
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import describe_exception, first_line
from app.data import catalog, catalog_assistant, entity_docs, entity_patch
from app.data.engine import masked_columns
from app.data.entities import confirmed_bindings, list_entities
from app.data.names import name_key
from app.data.semantic_metrics import (
    SUGGEST_SYSTEM_PROMPT,
    MetricConflict,
    MetricInvalid,
    MetricNotFound,
    _normalize_metric_payload,
    _parse_suggested_metrics,
    _resolve_source,
    create_metric,
    list_metrics,
    metric_to_dict,
    update_metric,
)
from app.db.base import SessionLocal, utcnow
from app.db.models import DataSource, Document
from app.db.models_metrics import SemanticDecomposeJob, SemanticMetric
from app.engine.state import message_text
from app.memory import kb

logger = logging.getLogger(__name__)

OPEN_STATUSES = ("queued", "running")
VALID_SCOPES = ("catalog", "metrics", "entities")
MAX_DOC_CHARS = 32_000
MAX_NOTE_CHARS = 1500

DECOMPOSE_BUSY_CODE = "decompose_job_busy"
DECOMPOSE_FINISHED_CODE = "decompose_job_finished"
DECOMPOSE_NOT_FOUND_CODE = "decompose_job_not_found"

INTERRUPTED_ERROR = "服务重启，后台口径拆解任务中断，请重新发起"
JOB_FAILED_TEXT = "口径拆解任务执行失败：{reason}"

_FENCE_RE = re.compile(r"```(?:json)?\s*([\s\S]*?)```", re.IGNORECASE)
_ACTIVE_TASKS: dict[str, asyncio.Task[Any]] = {}
_CREATE_LOCK = asyncio.Lock()
_KB_BG_TASKS: set[asyncio.Task[Any]] = set()


class DecomposeBusy(Exception):
    """当前数据源已有正在执行的口径拆解任务。"""


class DecomposeFinished(Exception):
    """要取消的口径拆解任务已经结束。"""


class DecomposeNotFound(LookupError):
    """找不到指定的口径拆解任务。"""


CATALOG_DECOMPOSE_SYSTEM_PROMPT = """你是 AgentLab 的「数据目录与字段字典拆解专家」。请阅读数据源物理表结构、现有数据目录以及业务口径文档（如《指标公式总表》），提取需要补充或修正的「表目录与字段字典补丁」，同时摘录文档中的「物理判异红线与缺源提醒」。

硬性纪律：
1. 只输出一个合法 JSON 对象，格式为：
   {
     "tables": [
       {
         "table": "物理表名（逐字取自输入表结构）",
         "changes": [
           {"path": "路径", "value": "取值", "reason": "简明依据"}
         ]
       }
     ],
     "guardrail_notes": [
       {"title": "规则标题", "rule": "判异红线、存储陷阱或暂无数据源说明", "category": "redline | storage | missing_source"}
     ]
   }
   不要输出任何额外解释文字。
2. 表名与列名必须 100% 逐字存在于输入提供的表结构中，严禁臆造不存在的表或列；标有「[遮罩列]」的敏感列严禁输出补丁。
3. changes[].path 仅支持以下规范路径：
   - 表级：
     · "label"：表中文名称（纯名词短语，勿写公式）
     · "grain"：一行代表什么业务粒度（如 "每条产线每个班次每天一行"）
     · "business_date"：{"column": "物理日期列名", "rule": "统计归属说明（可空串）"}
     · "valid_filter"：默认有效记录 SQL 过滤片段（如 "is_deleted = 0"，不含 WHERE）
   - 列级（把 <col> 替换为真实物理列名，如 EXT_VAL_01）：
     · "columns.<col>.label"：字段中文名或符号含义（纯名称，如 "计划开班时长"，严禁包含 =、/、*、SUM( 等计算公式符号！）
     · "columns.<col>.unit"：单位（如 "秒"、"%"、"件"、"人次"）
     · "columns.<col>.measure"：仅限 "flow"（流量，可跨日加总，如时长、产量）、"stock"（存量，不可跨期直接求和）、"ratio"（比率，如良品率）、"identifier"（编码标识）、"status"（状态码）、"attribute"（属性）
     · "columns.<col>.meaning"：关键业务含义与存储说明（如 "底表直接存百分数如 99.65，引用时须除以 100，切勿重复乘 100"；注意文字中不要写等号公式，用自然中文表述）
     · "columns.<col>.codes"：枚举码值字典，如 {"1": "正常", "0": "停用"}
4. 特别留意工业宽表中的盲列（如 EXT_VAL_01～ATTRIBUTE55）：凡文档给出了字段对照或符号说明的，务必逐列提取 label、unit、measure 与 meaning！
"""

ENTITY_DECOMPOSE_SYSTEM_PROMPT = """你是 AgentLab 的「标准业务实体拆解专家」。请结合数据源物理表结构（含最新字段说明）与业务口径文档，识别多张表或业务提问中共用的核心维度实体（如产线、车间、机台、产品型号、不良原因、区域等），输出增量操作 JSON。

硬性纪律：
1. 只输出一个合法 JSON 对象，格式为 {"operations": [...]}，不要输出任何额外解释文字。
2. 表名、列名、数据源名必须 100% 逐字存在于输入材料中，严禁编造；纯自增流水主键或纯度量数值列不要建为业务实体；标有「[遮罩列]」的敏感列严禁绑定或提取成员。
3. 若没有明确可建的标准业务实体，返回 {"operations": []}。
4. operations 支持以下结构（新建实体时请先输出一条 upsert_entity，再输出其 upsert_binding / upsert_member）：
   - {"op": "upsert_entity", "entity_key": "小写英文下划线标识，如 production_line", "label": "产线", "description": "制造产线标准维度", "aliases": ["生产线", "线体"], "reason": "依据"}
   - {"op": "upsert_binding", "entity_key": "production_line", "entity_label": "产线", "source_name": "数据源名称", "table_name": "物理表名", "column_name": "物理列名", "note": "产线编码列", "reason": "依据"}
   - {"op": "upsert_member", "entity_key": "production_line", "entity_label": "产线", "value": "L1", "label": "一号产线", "aliases": ["1线"], "source_codes": {"数据源名称": "L1"}, "reason": "依据"}
"""


def _iso(val: Any) -> str | None:
    if val is None or not hasattr(val, "isoformat"):
        return None
    if getattr(val, "tzinfo", None) is None:
        val = val.replace(tzinfo=timezone.utc)
    return val.isoformat()


def _parse_json_obj(raw_text: str) -> dict[str, Any]:
    text = (raw_text or "").strip()
    if not text:
        return {}
    m = _FENCE_RE.search(text)
    candidate = m.group(1).strip() if m else text
    for snippet in (candidate, text):
        start = snippet.find("{")
        end = snippet.rfind("}")
        if start >= 0 and end > start:
            try:
                obj = json.loads(snippet[start : end + 1])
                if isinstance(obj, Mapping):
                    return dict(obj)
            except ValueError:
                continue
    return {}


def _normalize_scopes(raw_scopes: Sequence[str] | None, *, has_docs: bool) -> list[str]:
    if not raw_scopes:
        return ["catalog", "metrics", "entities"] if has_docs else ["metrics"]
    out: list[str] = []
    for s in raw_scopes:
        clean = str(s or "").strip().lower()
        if clean in VALID_SCOPES and clean not in out:
            out.append(clean)
    return out or (["catalog", "metrics", "entities"] if has_docs else ["metrics"])


def _initial_steps(scopes: Sequence[str], *, has_docs: bool) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = [
        {
            "key": "prepare",
            "label": "准备表结构并归档口径文档" if has_docs else "读取数据源表结构与目录上下文",
            "status": "pending",
            "detail": "等待执行",
            "count": 0,
            "elapsed_ms": 0,
        }
    ]
    if "catalog" in scopes:
        steps.append(
            {
                "key": "catalog",
                "label": "拆解字段符号映射与表目录补丁",
                "status": "pending",
                "detail": "等待执行",
                "count": 0,
                "elapsed_ms": 0,
            }
        )
    if "metrics" in scopes:
        steps.append(
            {
                "key": "metrics",
                "label": "生成单表表达式与多表模板指标口径",
                "status": "pending",
                "detail": "等待执行",
                "count": 0,
                "elapsed_ms": 0,
            }
        )
    if "entities" in scopes:
        steps.append(
            {
                "key": "entities",
                "label": "识别标准业务实体与物理列映射",
                "status": "pending",
                "detail": "等待执行",
                "count": 0,
                "elapsed_ms": 0,
            }
        )
    return steps


def job_to_dict(row: SemanticDecomposeJob, *, include_result: bool = True) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": row.id,
        "status": row.status,
        "source_id": row.source_id,
        "source_name": row.source_name or "",
        "scopes": list(row.scopes or []),
        "document_ids": list(row.document_ids or []),
        "attachment_ids": list(row.attachment_ids or []),
        "doc_title": row.doc_title or "",
        "has_doc_text": bool((row.doc_text or "").strip()),
        "save_to_kb": bool(row.save_to_kb),
        "note": row.note or "",
        "phase": row.phase or "",
        "phase_label": row.phase_label or "",
        "progress": int(row.progress or 0),
        "steps": list(row.steps or []),
        "summary": dict(row.summary or {}),
        "error": row.error,
        "started_by": row.started_by,
        "started_at": _iso(row.started_at),
        "finished_at": _iso(row.finished_at),
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
    }
    if include_result:
        data["result"] = dict(row.result or {})
    return data


async def get_job(session: AsyncSession, job_id: str) -> SemanticDecomposeJob:
    clean = str(job_id or "").strip()
    if not clean:
        raise DecomposeNotFound("未指定口径拆解任务编号")
    row = await session.get(SemanticDecomposeJob, clean)
    if row is None:
        raise DecomposeNotFound(f"找不到口径拆解任务「{clean}」")
    return row


async def get_active_or_latest_job(
    session: AsyncSession,
    *,
    source_id: str | None = None,
) -> dict[str, Any]:
    """返回正在排队或运行中的任务，以及最近一次已完成任务（供口径台账页顶部横幅轮询与恢复审阅）。"""
    active_stmt = select(SemanticDecomposeJob).where(SemanticDecomposeJob.status.in_(OPEN_STATUSES))
    latest_stmt = select(SemanticDecomposeJob)
    if source_id and source_id.strip():
        sid = source_id.strip()
        active_stmt = active_stmt.where(SemanticDecomposeJob.source_id == sid)
        latest_stmt = latest_stmt.where(SemanticDecomposeJob.source_id == sid)
    active_row = (
        await session.execute(active_stmt.order_by(SemanticDecomposeJob.created_at.desc()).limit(1))
    ).scalars().first()
    latest_row = (
        await session.execute(latest_stmt.order_by(SemanticDecomposeJob.created_at.desc()).limit(1))
    ).scalars().first()
    return {
        "active": job_to_dict(active_row, include_result=False) if active_row else None,
        "latest": job_to_dict(latest_row, include_result=True) if latest_row else None,
    }


async def list_jobs(
    session: AsyncSession,
    *,
    source_id: str | None = None,
    limit: int = 10,
) -> list[dict[str, Any]]:
    stmt = select(SemanticDecomposeJob).order_by(SemanticDecomposeJob.created_at.desc()).limit(max(1, min(limit, 30)))
    if source_id and source_id.strip():
        stmt = stmt.where(SemanticDecomposeJob.source_id == source_id.strip())
    rows = list((await session.execute(stmt)).scalars().all())
    return [job_to_dict(r, include_result=False) for r in rows]


async def create_decompose_job(
    session: AsyncSession,
    *,
    source_id: str,
    scopes: Sequence[str] | None = None,
    table_names: Sequence[str] | None = None,
    document_ids: Sequence[str] | None = None,
    attachment_ids: Sequence[str] | None = None,
    doc_title: str = "",
    doc_text: str = "",
    save_to_kb: bool = True,
    note: str = "",
    actor: str | None = None,
) -> SemanticDecomposeJob:
    """创建并启动后台口径文档一键拆解或智能提取指标任务。"""
    ds = await _resolve_source(session, source_id)
    cache = ds.schema_cache if isinstance(ds.schema_cache, Mapping) else {}
    tables = cache.get("tables") if isinstance(cache, Mapping) else None
    if not isinstance(tables, Mapping) or not tables:
        raise MetricInvalid(f"数据源「{ds.name}」尚未探查表结构，请先在数据源设置中完成探查", field="source_id")

    clean_docs = [str(d).strip() for d in (document_ids or []) if str(d or "").strip()]
    clean_atts = [str(a).strip() for a in (attachment_ids or []) if str(a or "").strip()]
    clean_text = (doc_text or "").strip()
    clean_title = (doc_title or "").strip()[:240]
    has_docs = bool(clean_docs or clean_atts or clean_text)
    norm_scopes = _normalize_scopes(scopes, has_docs=has_docs)
    clean_tables = [str(t).strip() for t in (table_names or []) if str(t or "").strip()]

    async with _CREATE_LOCK:
        existing_open = (
            await session.execute(
                select(SemanticDecomposeJob).where(
                    SemanticDecomposeJob.source_id == ds.id,
                    SemanticDecomposeJob.status.in_(OPEN_STATUSES),
                )
            )
        ).scalars().first()
        if existing_open is not None:
            raise DecomposeBusy(f"数据源「{ds.name}」已有正在后台执行的拆解任务，请等待完成或先取消当前任务")

        job = SemanticDecomposeJob(
            status="queued",
            source_id=ds.id,
            source_name=ds.name,
            scopes=norm_scopes,
            document_ids=clean_docs,
            attachment_ids=clean_atts,
            doc_title=clean_title,
            doc_text=clean_text,
            save_to_kb=bool(save_to_kb),
            note=(note or "").strip()[:MAX_NOTE_CHARS],
            phase="queued",
            phase_label="已进入后台队列，准备读取数据源与文档",
            progress=4,
            steps=_initial_steps(norm_scopes, has_docs=has_docs),
            result={"table_names": clean_tables} if clean_tables else {},
            summary={},
            started_by=actor,
            started_at=utcnow(),
        )
        session.add(job)
        await session.commit()
        await session.refresh(job)

        task = asyncio.create_task(_execute_job(job.id), name=f"semantic-decompose-{job.id}")
        _ACTIVE_TASKS[job.id] = task
        task.add_done_callback(lambda _t, jid=job.id: _ACTIVE_TASKS.pop(jid, None))
        return job


def _update_step_list(
    steps: list[dict[str, Any]],
    step_key: str,
    *,
    status: str,
    detail: str | None = None,
    count: int | None = None,
    elapsed_ms: int | None = None,
) -> list[dict[str, Any]]:
    updated = [copy.deepcopy(s) for s in steps]
    for s in updated:
        if s.get("key") == step_key:
            s["status"] = status
            if detail is not None:
                s["detail"] = detail
            if count is not None:
                s["count"] = count
            if elapsed_ms is not None:
                s["elapsed_ms"] = elapsed_ms
    return updated


async def _commit_progress(
    session: AsyncSession,
    job: SemanticDecomposeJob,
    *,
    phase: str,
    phase_label: str,
    progress: int,
    steps: list[dict[str, Any]],
) -> None:
    job.status = "running"
    job.phase = phase
    job.phase_label = phase_label
    job.progress = max(0, min(99, int(progress)))
    job.steps = steps
    await session.commit()


def _infer_doc_title(doc_title: str, doc_text: str, source_name: str) -> str:
    if doc_title.strip():
        return doc_title.strip()[:200]
    for line in doc_text.splitlines():
        clean = line.strip().lstrip("#").strip()
        if clean:
            return clean[:120]
    return f"{source_name} 业务指标口径文档"


def _build_schema_prompt_block(
    ds: DataSource,
    tables: Mapping[str, Any],
    selected_tables: Sequence[str],
    entries: Mapping[str, Any],
    masked_set: set[str],
    col_entity: Mapping[tuple[str, str], str],
    overlay_notes: Mapping[str, Mapping[str, Any]] | None = None,
) -> str:
    lines: list[str] = [f"数据源：{ds.name}（方言：{ds.kind}）"]
    if (ds.kind or "").strip().lower() == "oracle":
        lines.append("方言注意：Oracle 严禁写 LIMIT、严禁写 CAST(x AS TEXT)、表或子查询别名不写 AS；动态周期天数请直接用 {{period_days}} 槽位。")
    lines.append("\n【物理表结构与数据目录】")
    for tname in selected_tables:
        tinfo = tables.get(tname) or {}
        base_notes = catalog.visible_notes(entries[tname].notes) if tname in entries else {}
        notes = copy.deepcopy(dict(base_notes))
        if overlay_notes and tname in overlay_notes:
            ov = overlay_notes[tname]
            for k, v in ov.items():
                if k == "columns" and isinstance(v, Mapping):
                    cols_dict = dict(notes.get("columns") or {}) if isinstance(notes.get("columns"), Mapping) else {}
                    for ck, cv in v.items():
                        prev_c = dict(cols_dict.get(ck) or {}) if isinstance(cols_dict.get(ck), Mapping) else {}
                        if isinstance(cv, Mapping):
                            prev_c.update(cv)
                        cols_dict[ck] = prev_c
                    notes["columns"] = cols_dict
                else:
                    notes[k] = v

        t_label = (notes.get("label") or {}).get("value") if isinstance(notes.get("label"), Mapping) else ""
        t_grain = (notes.get("grain") or {}).get("value") if isinstance(notes.get("grain"), Mapping) else ""
        b_date_raw = (notes.get("business_date") or {}).get("value") if isinstance(notes.get("business_date"), Mapping) else ""
        if isinstance(b_date_raw, Mapping):
            b_col = str(b_date_raw.get("column") or "").strip()
            b_rule = str(b_date_raw.get("rule") or "").strip()
            b_date = f"{b_col}（{b_rule}）" if (b_col and b_rule) else (b_col or b_rule)
        else:
            b_date = str(b_date_raw or "").strip()
        v_filt = (notes.get("valid_filter") or {}).get("value") if isinstance(notes.get("valid_filter"), Mapping) else ""

        head_bits = [f"表 {tname}"]
        qualified_t = str(tinfo.get("qualified") or "").strip()
        if qualified_t and qualified_t != tname:
            head_bits.append(f"SQL 全名：{qualified_t}")
        if t_label:
            head_bits.append(f"中文名：{t_label}")
        if t_grain:
            head_bits.append(f"粒度：{t_grain}")
        if b_date:
            head_bits.append(f"业务日期列：{b_date}")
        if v_filt:
            head_bits.append(f"有效过滤：{v_filt}")
        lines.append("- " + " ｜ ".join(head_bits))

        col_notes = notes.get("columns") if isinstance(notes.get("columns"), Mapping) else {}
        t_short = name_key(tname.rsplit(".", 1)[-1])
        for col in (tinfo.get("columns") or [])[:75]:
            if not isinstance(col, Mapping) or not col.get("name"):
                continue
            cname = str(col["name"])
            ctype = str(col.get("type") or "")
            if name_key(cname) in masked_set:
                lines.append(f"  · {cname} ({ctype}) [遮罩列]")
                continue
            cn = col_notes.get(cname) if isinstance(col_notes, Mapping) else None
            c_label = (cn.get("label") or {}).get("value") if isinstance(cn, Mapping) and isinstance(cn.get("label"), Mapping) else ""
            c_unit = (cn.get("unit") or {}).get("value") if isinstance(cn, Mapping) and isinstance(cn.get("unit"), Mapping) else ""
            c_measure = (cn.get("measure") or {}).get("value") if isinstance(cn, Mapping) and isinstance(cn.get("measure"), Mapping) else ""
            c_meaning = (
                (cn.get("meaning") or {}).get("value")
                if isinstance(cn, Mapping) and isinstance(cn.get("meaning"), Mapping)
                else ((cn.get("note") or {}).get("value") if isinstance(cn, Mapping) and isinstance(cn.get("note"), Mapping) else "")
            )
            ent_key = col_entity.get((t_short, name_key(cname)), "")
            extra_bits = [
                b
                for b in (
                    c_label,
                    f"单位：{c_unit}" if c_unit else "",
                    f"度量：{c_measure}" if c_measure else "",
                    f"含义：{c_meaning}" if c_meaning else "",
                    f"已绑定标准实体：{ent_key}" if ent_key else "",
                )
                if b
            ]
            suffix = f" — {'，'.join(extra_bits)}" if extra_bits else ""
            lines.append(f"  · {cname} ({ctype}){suffix}")
    return "\n".join(lines)


def _align_catalog_changes_for_table(
    raw_changes: Sequence[Any],
    *,
    tinfo: Mapping[str, Any],
    masked_set: set[str],
) -> list[dict[str, Any]]:
    """对齐列路径大小写，过滤敏感遮罩列，避免模型把 EXT_VAL_01 写成 ext_val_01 导致落空。"""
    col_exact_by_key: dict[str, str] = {}
    for c in tinfo.get("columns") or []:
        if isinstance(c, Mapping) and c.get("name"):
            cname = str(c["name"])
            col_exact_by_key[name_key(cname)] = cname

    aligned: list[dict[str, Any]] = []
    for item in raw_changes or []:
        if not isinstance(item, Mapping):
            continue
        raw_path = str(item.get("path") or "").strip()
        if not raw_path or "value" not in item:
            continue
        val = copy.deepcopy(item["value"])
        reason = str(item.get("reason") or "").strip()

        if raw_path.startswith("columns."):
            parts = raw_path.split(".")
            if len(parts) == 3:
                raw_col, slot_field = parts[1], parts[2]
                if name_key(raw_col) in masked_set:
                    continue
                if slot_field in ("note", "description"):
                    slot_field = "meaning"
                exact_col = col_exact_by_key.get(name_key(raw_col), raw_col)
                raw_path = f"columns.{exact_col}.{slot_field}"
                # 若模型不小心在 label 里写了等号，自动清洗掉等号右侧以免被 _formula_like 整条丢弃
                if slot_field == "label" and isinstance(val, str) and "=" in val:
                    val = val.split("=", 1)[0].strip()
        elif raw_path == "business_date" and isinstance(val, Mapping) and val.get("column"):
            bcol = str(val["column"])
            if name_key(bcol) in masked_set:
                continue
            val = {**dict(val), "column": col_exact_by_key.get(name_key(bcol), bcol)}
        elif raw_path == "business_date" and isinstance(val, str) and val.strip():
            bcol = val.strip()
            if name_key(bcol) in masked_set:
                continue
            val = {"column": col_exact_by_key.get(name_key(bcol), bcol), "rule": ""}

        aligned.append({"path": raw_path, "value": val, "reason": reason})
    return aligned


async def _execute_job(job_id: str) -> None:
    try:
        async with SessionLocal() as session:
            job = await session.get(SemanticDecomposeJob, job_id)
            if job is None or job.status not in OPEN_STATUSES:
                return

            steps = list(job.steps or [])
            scopes = list(job.scopes or ["metrics"])
            requested_tables = list((job.result or {}).get("table_names") or [])

            # ---------------- 阶段 1：准备表结构与归档知识库 ----------------
            t_step = time.perf_counter()
            steps = _update_step_list(steps, "prepare", status="running", detail="正在加载数据源表结构与口径文档")
            await _commit_progress(
                session,
                job,
                phase="preparing",
                phase_label="正在加载数据源结构并整理业务口径文档…",
                progress=12,
                steps=steps,
            )

            ds = await _resolve_source(session, job.source_id)
            cache = ds.schema_cache if isinstance(ds.schema_cache, Mapping) else {}
            tables = cache.get("tables") if isinstance(cache, Mapping) else {}
            if not isinstance(tables, Mapping) or not tables:
                raise MetricInvalid(f"数据源「{ds.name}」尚未探查表结构")

            entries = await catalog.read_catalog(session, ds.id)
            masked_set = {name_key(c) for c in masked_columns(ds.options)}
            entities_list = await list_entities(session)
            existing_metrics = await list_metrics(session, source_id=ds.id)
            existing_metric_keys = {m.key for m in existing_metrics}

            col_entity: dict[tuple[str, str], str] = {}
            for ent in entities_list:
                for b in confirmed_bindings(ent):
                    if name_key(str(b.get("source_name") or "")) == name_key(ds.name):
                        t_short = name_key(str(b.get("table_name") or "").rsplit(".", 1)[-1])
                        c_key = name_key(str(b.get("column_name") or ""))
                        col_entity[(t_short, c_key)] = ent.key

            selected_tables: list[str] = []
            if requested_tables:
                for raw_t in requested_tables:
                    resolved = catalog.resolve_table_name(cache, str(raw_t))
                    if resolved and resolved not in selected_tables:
                        selected_tables.append(resolved)
            if not selected_tables:
                def _table_priority(tname: str) -> tuple[int, str]:
                    has_cat = 0 if (tname in entries and catalog.visible_notes(entries[tname].notes)) else 1
                    return (has_cat, tname)

                selected_tables = sorted(tables.keys(), key=_table_priority)[:32]

            # 组装文档内容，并按需把粘贴文本归档入知识库
            kb_documents: list[dict[str, Any]] = []
            doc_sections: list[str] = []
            doc_budget = MAX_DOC_CHARS

            pasted_text = (job.doc_text or "").strip()
            if pasted_text:
                inferred_title = _infer_doc_title(job.doc_title or "", pasted_text, ds.name)
                created_doc_id: str | None = None
                if job.save_to_kb:
                    existing_doc = (
                        await session.execute(
                            select(Document).where(Document.title == inferred_title).order_by(Document.created_at.desc()).limit(1)
                        )
                    ).scalars().first()
                    if existing_doc is not None and (existing_doc.content or "").strip() == pasted_text:
                        created_doc_id = existing_doc.id
                        kb_documents.append({"id": existing_doc.id, "title": inferred_title, "created_now": False})
                    else:
                        new_doc = await kb.create_document(
                            session,
                            collection="default",
                            title=inferred_title,
                            content=pasted_text,
                            source=inferred_title,
                            mime="text/markdown",
                            meta={"origin": "semantic_decompose", "source_id": ds.id},
                            status="processing",
                        )
                        await session.flush()
                        try:
                            await kb.process_document(session, new_doc)
                        except Exception as kb_exc:  # noqa: BLE001
                            logger.warning("知识库文档切块索引降级（%s）：%s", new_doc.id, kb_exc)
                            new_doc.status = "ready"
                        created_doc_id = new_doc.id
                        kb_documents.append({"id": new_doc.id, "title": inferred_title, "created_now": True})

                snippet = pasted_text[: min(len(pasted_text), doc_budget)]
                doc_budget -= len(snippet)
                doc_sections.append(f"--- 文档《{inferred_title}》 ---\n{snippet}")
                if created_doc_id and created_doc_id not in (job.document_ids or []):
                    job.document_ids = [*(job.document_ids or []), created_doc_id]

            bundles = await entity_docs.load_doc_bundles(
                session,
                attachment_ids=job.attachment_ids or [],
                document_ids=job.document_ids or [],
            )
            seen_kb_ids = {d["id"] for d in kb_documents}
            for bundle in bundles:
                if bundle.document_id and bundle.document_id in seen_kb_ids:
                    continue
                joined = "\n\n".join(bundle.chunks).strip()
                if not joined or doc_budget <= 0:
                    continue
                if bundle.kind == "attachment" and job.save_to_kb and not bundle.document_id:
                    att_doc = await kb.create_document(
                        session,
                        collection="default",
                        title=bundle.title,
                        content=joined,
                        source=bundle.title,
                        mime=bundle.mime,
                        meta={"origin": "semantic_decompose", "source_id": ds.id},
                        status="processing",
                    )
                    await session.flush()
                    try:
                        await kb.process_document(session, att_doc)
                    except Exception as kb_exc:  # noqa: BLE001
                        logger.warning("附件文档切块索引降级（%s）：%s", att_doc.id, kb_exc)
                        att_doc.status = "ready"
                    kb_documents.append({"id": att_doc.id, "title": bundle.title, "created_now": True})
                    seen_kb_ids.add(att_doc.id)
                elif bundle.document_id and bundle.document_id not in seen_kb_ids:
                    kb_documents.append({"id": bundle.document_id, "title": bundle.title, "created_now": False})
                    seen_kb_ids.add(bundle.document_id)

                snippet = joined[: min(len(joined), doc_budget)]
                doc_budget -= len(snippet)
                doc_sections.append(f"--- 文档《{bundle.title}》 ---\n{snippet}")

            prep_detail = f"已扫描 {len(selected_tables)} 张物理表"
            if doc_sections:
                prep_detail += f"，载入 {len(doc_sections)} 份口径文档"
            if any(d.get("created_now") for d in kb_documents):
                prep_detail += "并同步归档至知识库"
            steps = _update_step_list(
                steps,
                "prepare",
                status="succeeded",
                detail=prep_detail,
                count=len(selected_tables),
                elapsed_ms=int((time.perf_counter() - t_step) * 1000),
            )
            await _commit_progress(
                session,
                job,
                phase="prepared",
                phase_label=prep_detail,
                progress=22,
                steps=steps,
            )

            model, model_id = await catalog_assistant.resolve_model(session)

            # ---------------- 阶段 2：拆解字段符号映射与表目录补丁 ----------------
            catalog_patches: list[dict[str, Any]] = []
            catalog_skipped: list[str] = []
            guardrail_notes: list[dict[str, str]] = []
            overlay_notes: dict[str, dict[str, Any]] = {}

            if "catalog" in scopes:
                t_cat = time.perf_counter()
                if not doc_sections and not (job.note or "").strip():
                    steps = _update_step_list(
                        steps,
                        "catalog",
                        status="skipped",
                        detail="未提供业务口径文档，跳过字段目录拆解",
                        count=0,
                        elapsed_ms=0,
                    )
                else:
                    steps = _update_step_list(
                        steps,
                        "catalog",
                        status="running",
                        detail="正在识别字段符号映射、单位、流量或存量角色与存储陷阱",
                    )
                    await _commit_progress(
                        session,
                        job,
                        phase="extracting_catalog",
                        phase_label="正在拆解字段符号映射与数据目录补丁（1/3）…",
                        progress=34,
                        steps=steps,
                    )
                    schema_block = _build_schema_prompt_block(
                        ds, tables, selected_tables, entries, masked_set, col_entity
                    )
                    cat_prompt_parts = [schema_block]
                    if doc_sections:
                        cat_prompt_parts.append("\n【业务口径文档】\n" + "\n\n".join(doc_sections))
                    if job.note:
                        cat_prompt_parts.append(f"\n【用户补充要求】\n{job.note}")

                    cat_reply = await model.ainvoke(
                        [
                            SystemMessage(content=CATALOG_DECOMPOSE_SYSTEM_PROMPT),
                            HumanMessage(content="\n".join(cat_prompt_parts)),
                        ]
                    )
                    cat_json = _parse_json_obj(message_text(cat_reply))

                    for g in cat_json.get("guardrail_notes") or []:
                        if isinstance(g, Mapping) and str(g.get("rule") or "").strip():
                            guardrail_notes.append(
                                {
                                    "title": str(g.get("title") or "口径规则提醒").strip()[:120],
                                    "rule": str(g.get("rule") or "").strip()[:600],
                                    "category": str(g.get("category") or "redline").strip()[:32],
                                }
                            )

                    total_cat_changes = 0
                    for tbl_item in cat_json.get("tables") or []:
                        if not isinstance(tbl_item, Mapping):
                            continue
                        raw_tbl = str(tbl_item.get("table") or "").strip()
                        resolved_tbl = catalog.resolve_table_name(cache, raw_tbl)
                        if not resolved_tbl:
                            if raw_tbl:
                                catalog_skipped.append(f"跳过不存在的表「{raw_tbl}」")
                            continue
                        tinfo = tables.get(resolved_tbl) or {}
                        aligned_changes = _align_catalog_changes_for_table(
                            tbl_item.get("changes") or [],
                            tinfo=tinfo,
                            masked_set=masked_set,
                        )
                        if not aligned_changes:
                            continue
                        entry = entries.get(resolved_tbl)
                        plan = catalog.plan_patch(
                            entry.notes if entry else {},
                            aligned_changes,
                            table=resolved_tbl,
                            tables=tables,
                        )
                        catalog_skipped.extend(f"{resolved_tbl}：{p}" for p in plan.problems)
                        active_changes = [c for c in plan.changes if c.state != "same"]
                        if not active_changes:
                            continue

                        # 将通过核验的目录补丁写入内存覆盖层，直接供给下一阶段指标提取！
                        tbl_ov = overlay_notes.setdefault(resolved_tbl, {"columns": {}})
                        serialized_changes: list[dict[str, Any]] = []
                        for c_idx, ch in enumerate(active_changes):
                            slot = catalog.parse_path(ch.path)
                            slot_label = catalog.describe_slot(slot) if slot[0] != "r" else "关联关系"
                            serialized_changes.append(
                                {
                                    "id": f"{resolved_tbl}:{ch.path}:{c_idx}",
                                    "table": resolved_tbl,
                                    "path": ch.path,
                                    "slot_label": slot_label,
                                    "before": ch.before,
                                    "after": ch.after,
                                    "value": ch.value,
                                    "reason": ch.reason,
                                    "state": ch.state,
                                }
                            )
                            if slot[0] == "t":
                                tbl_ov[slot[1]] = {"value": ch.after}
                            elif slot[0] == "c":
                                col_ov = tbl_ov["columns"].setdefault(slot[1], {})
                                col_ov[slot[2]] = {"value": ch.after}

                        total_cat_changes += len(serialized_changes)
                        catalog_patches.append(
                            {
                                "table": resolved_tbl,
                                "if_version": int(entry.version) if entry else 0,
                                "changes": serialized_changes,
                            }
                        )

                    steps = _update_step_list(
                        steps,
                        "catalog",
                        status="succeeded",
                        detail=f"提取出 {total_cat_changes} 项字段与表目录补丁"
                        + (f"、{len(guardrail_notes)} 条判异红线" if guardrail_notes else ""),
                        count=total_cat_changes,
                        elapsed_ms=int((time.perf_counter() - t_cat) * 1000),
                    )
                await _commit_progress(
                    session,
                    job,
                    phase="catalog_done",
                    phase_label="字段目录拆解完成，准备提取语义指标口径…",
                    progress=52,
                    steps=steps,
                )

            # ---------------- 阶段 3：提取单表与多表参数化指标口径 ----------------
            metric_candidates: list[dict[str, Any]] = []
            metric_skipped: list[dict[str, str]] = []

            if "metrics" in scopes:
                t_met = time.perf_counter()
                steps = _update_step_list(
                    steps,
                    "metrics",
                    status="running",
                    detail="正在结合字段目录映射生成单表聚合与多表模板指标并预编译校验",
                )
                await _commit_progress(
                    session,
                    job,
                    phase="extracting_metrics",
                    phase_label="正在生成单表与多表参数化 SQL 指标口径并逐条校验…",
                    progress=64,
                    steps=steps,
                )

                enriched_schema_block = _build_schema_prompt_block(
                    ds,
                    tables,
                    selected_tables,
                    entries,
                    masked_set,
                    col_entity,
                    overlay_notes=overlay_notes,
                )
                met_lines = [enriched_schema_block]
                if existing_metric_keys:
                    met_lines.append(f"\n已存在的指标标识（若口径需要升级可沿用并更新，否则请勿无故重复）：{', '.join(sorted(existing_metric_keys))}")
                if doc_sections:
                    met_lines.append("\n【参考业务口径文档】\n" + "\n\n".join(doc_sections))
                if job.note:
                    met_lines.append(f"\n【用户补充要求】\n{job.note}")

                met_reply = await model.ainvoke(
                    [
                        SystemMessage(content=SUGGEST_SYSTEM_PROMPT),
                        HumanMessage(content="\n".join(met_lines)),
                    ]
                )
                raw_candidates = _parse_suggested_metrics(message_text(met_reply))
                seen_batch_keys: set[str] = set()
                for raw in raw_candidates:
                    candidate = {**raw, "source_id": ds.id, "source": "ai", "status": "enabled"}
                    raw_k = str(candidate.get("key") or "").strip().lower()
                    if raw_k and raw_k in seen_batch_keys:
                        metric_skipped.append({"key": raw_k, "reason": "同批候选指标标识重复"})
                        continue
                    try:
                        _, norm = await _normalize_metric_payload(session, candidate, existing=None)
                        seen_batch_keys.add(norm["key"])
                        metric_candidates.append(
                            {
                                **norm,
                                "source_name": ds.name,
                                "exists_in_db": norm["key"] in existing_metric_keys,
                            }
                        )
                    except Exception as exc:  # noqa: BLE001
                        metric_skipped.append(
                            {"key": raw_k or str(candidate.get("name") or "未命名指标"), "reason": str(exc)}
                        )

                tpl_cnt = sum(1 for m in metric_candidates if m.get("calc_mode") == "sql_template")
                expr_cnt = len(metric_candidates) - tpl_cnt
                met_detail = f"核验通过 {len(metric_candidates)} 个指标（单表 {expr_cnt} 个、多表模板 {tpl_cnt} 个）"
                steps = _update_step_list(
                    steps,
                    "metrics",
                    status="succeeded",
                    detail=met_detail,
                    count=len(metric_candidates),
                    elapsed_ms=int((time.perf_counter() - t_met) * 1000),
                )
                await _commit_progress(
                    session,
                    job,
                    phase="metrics_done",
                    phase_label=met_detail,
                    progress=82,
                    steps=steps,
                )

            # ---------------- 阶段 4：识别标准业务实体与编码映射 ----------------
            entity_changes: list[dict[str, Any]] = []
            entity_skipped: list[str] = []

            if "entities" in scopes:
                t_ent = time.perf_counter()
                steps = _update_step_list(
                    steps,
                    "entities",
                    status="running",
                    detail="正在识别共用业务实体维度与表列绑定",
                )
                await _commit_progress(
                    session,
                    job,
                    phase="extracting_entities",
                    phase_label="正在识别标准业务实体与物理列编码映射…",
                    progress=88,
                    steps=steps,
                )
                enriched_schema_block = _build_schema_prompt_block(
                    ds,
                    tables,
                    selected_tables,
                    entries,
                    masked_set,
                    col_entity,
                    overlay_notes=overlay_notes,
                )
                ent_lines = [enriched_schema_block]
                if entities_list:
                    ent_summary = "、".join(f"{e.key}({e.label})" for e in entities_list[:30])
                    ent_lines.append(f"\n已有标准业务实体：{ent_summary}")
                if doc_sections:
                    ent_lines.append("\n【业务口径文档】\n" + "\n\n".join(doc_sections))
                if job.note:
                    ent_lines.append(f"\n【用户补充要求】\n{job.note}")

                ent_reply = await model.ainvoke(
                    [
                        SystemMessage(content=ENTITY_DECOMPOSE_SYSTEM_PROMPT),
                        HumanMessage(content="\n".join(ent_lines)),
                    ]
                )
                ent_json = _parse_json_obj(message_text(ent_reply))
                raw_ops = ent_json.get("operations") if isinstance(ent_json.get("operations"), list) else []

                safe_ops: list[dict[str, Any]] = []
                for op_item in raw_ops:
                    if not isinstance(op_item, Mapping):
                        continue
                    op_copy = dict(op_item)
                    if op_copy.get("op") == "upsert_binding":
                        if not op_copy.get("source_name"):
                            op_copy["source_name"] = ds.name
                        col_candidate = str(op_copy.get("column_name") or "").strip()
                        if name_key(col_candidate) in masked_set:
                            entity_skipped.append(f"跳过敏感遮罩列绑定「{col_candidate}」")
                            continue
                    safe_ops.append(op_copy)

                if safe_ops:
                    ent_plan = await entity_patch.plan_entity_patch(session, safe_ops, mode="confirmed")
                    entity_changes = [ch.as_dict() for ch in ent_plan.changes]
                    entity_skipped.extend(ent_plan.problems)

                steps = _update_step_list(
                    steps,
                    "entities",
                    status="succeeded",
                    detail=f"识别出 {len(entity_changes)} 项标准实体与绑定变更",
                    count=len(entity_changes),
                    elapsed_ms=int((time.perf_counter() - t_ent) * 1000),
                )

            # ---------------- 完成汇总 ----------------
            catalog_change_count = sum(len(t.get("changes") or []) for t in catalog_patches)
            summary = {
                "model_id": model_id,
                "kb_synced_count": sum(1 for d in kb_documents if d.get("created_now")),
                "kb_doc_count": len(kb_documents),
                "catalog_table_count": len(catalog_patches),
                "catalog_change_count": catalog_change_count,
                "metric_count": len(metric_candidates),
                "metric_template_count": sum(1 for m in metric_candidates if m.get("calc_mode") == "sql_template"),
                "metric_skipped_count": len(metric_skipped),
                "entity_change_count": len(entity_changes),
                "guardrail_count": len(guardrail_notes),
            }
            job.status = "succeeded"
            job.phase = "done"
            job.phase_label = (
                f"拆解完成：{len(metric_candidates)} 个指标"
                + (f"、{catalog_change_count} 项字段目录补丁" if catalog_change_count else "")
                + (f"、{len(entity_changes)} 项实体映射" if entity_changes else "")
            )
            job.progress = 100
            job.steps = steps
            job.summary = summary
            job.result = {
                "model_id": model_id,
                "kb_documents": kb_documents,
                "guardrail_notes": guardrail_notes,
                "catalog_patches": catalog_patches,
                "catalog_skipped": catalog_skipped,
                "metric_candidates": metric_candidates,
                "metric_skipped": metric_skipped,
                "entity_changes": entity_changes,
                "entity_skipped": entity_skipped,
                "applied": None,
            }
            job.finished_at = utcnow()
            await session.commit()

    except asyncio.CancelledError:
        async with SessionLocal() as session:
            job = await session.get(SemanticDecomposeJob, job_id)
            if job is not None and job.status in OPEN_STATUSES:
                job.status = "cancelled"
                job.phase = "cancelled"
                job.phase_label = "已取消后台拆解任务"
                job.finished_at = utcnow()
                await session.commit()
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning("口径拆解任务执行失败（%s）：%s", job_id, exc, exc_info=True)
        async with SessionLocal() as session:
            job = await session.get(SemanticDecomposeJob, job_id)
            if job is not None and job.status in OPEN_STATUSES:
                steps = [
                    {**s, "status": "failed", "detail": first_line(exc) or describe_exception(exc)}
                    if s.get("status") == "running"
                    else s
                    for s in (job.steps or [])
                ]
                job.status = "failed"
                job.phase = "failed"
                job.phase_label = "拆解任务执行失败"
                job.steps = steps
                job.error = JOB_FAILED_TEXT.format(reason=first_line(exc) or describe_exception(exc))
                job.finished_at = utcnow()
                await session.commit()


async def cancel_decompose_job(session: AsyncSession, job_id: str) -> SemanticDecomposeJob:
    job = await get_job(session, job_id)
    if job.status not in OPEN_STATUSES:
        raise DecomposeFinished("该口径拆解任务已经结束")
    task = _ACTIVE_TASKS.get(job.id)
    if task is not None and not task.done():
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await asyncio.wait_for(asyncio.shield(task), timeout=2.0)
    await session.refresh(job)
    if job.status in OPEN_STATUSES:
        job.status = "cancelled"
        job.phase = "cancelled"
        job.phase_label = "已取消后台拆解任务"
        job.finished_at = utcnow()
        await session.commit()
        await session.refresh(job)
    return job


async def apply_decompose_job(
    session: AsyncSession,
    job_id: str,
    *,
    catalog_patches: Sequence[Mapping[str, Any]] | None = None,
    metrics: Sequence[Mapping[str, Any]] | None = None,
    entity_operations: Sequence[Mapping[str, Any]] | None = None,
    overwrite_existing_metrics: bool = True,
    actor: str | None = None,
) -> dict[str, Any]:
    """一键应用用户在审阅抽屉中勾选的「字段目录补丁 + 语义指标口径 + 标准业务实体」。"""
    job = await get_job(session, job_id)
    if job.status != "succeeded":
        raise MetricInvalid("仅已完成的拆解任务支持一键应用")

    ds = await _resolve_source(session, job.source_id)
    cache = ds.schema_cache if isinstance(ds.schema_cache, Mapping) else {}
    tables = cache.get("tables") if isinstance(cache, Mapping) else {}

    res = dict(job.result or {})
    selected_cat = (
        list(catalog_patches)
        if catalog_patches is not None
        else [
            {
                "table": t["table"],
                "changes": [
                    {"path": c["path"], "value": c["value"], "reason": c.get("reason", "")}
                    for c in (t.get("changes") or [])
                ],
            }
            for t in (res.get("catalog_patches") or [])
        ]
    )
    selected_metrics = list(metrics) if metrics is not None else list(res.get("metric_candidates") or [])
    selected_ent_ops = (
        list(entity_operations)
        if entity_operations is not None
        else [c["op_payload"] for c in (res.get("entity_changes") or []) if isinstance(c, Mapping) and c.get("op_payload")]
    )

    catalog_applied_count = 0
    catalog_errors: list[str] = []
    pending_after_reviews: list[tuple[str, str, Any, Any]] = []
    for tbl_patch in selected_cat:
        tbl_name = str(tbl_patch.get("table") or "").strip()
        changes = list(tbl_patch.get("changes") or [])
        if not tbl_name or not changes:
            continue
        resolved_tbl = catalog.resolve_table_name(cache, tbl_name) or tbl_name
        try:
            async with session.begin_nested():
                entry = await catalog.read_entry(session, ds.id, resolved_tbl)
                cur_ver = int(entry.version) if entry else 0
                new_notes = catalog.apply_patch(
                    entry.notes if entry else {},
                    changes,
                    table=resolved_tbl,
                    tables=tables if isinstance(tables, Mapping) else None,
                )
                written = await catalog.write_entry(
                    session, ds.id, resolved_tbl, new_notes, if_version=cur_ver, actor=actor, commit=False
                )
                pending_after_reviews.append((ds.id, resolved_tbl, entry.notes if entry else None, written))
                catalog_applied_count += len(changes)
        except Exception as exc:  # noqa: BLE001
            catalog_errors.append(f"{resolved_tbl}：{exc}")

    created_metrics: list[dict[str, Any]] = []
    updated_metrics: list[dict[str, Any]] = []
    metric_errors: list[dict[str, str]] = []
    for m_item in selected_metrics:
        payload = {**dict(m_item), "source_id": ds.id}
        payload.pop("exists_in_db", None)
        payload.pop("source_name", None)
        m_key = str(payload.get("key") or "").strip().lower()
        try:
            async with session.begin_nested():
                existing_row = (
                    await session.execute(select(SemanticMetric).where(SemanticMetric.key == m_key))
                ).scalar_one_or_none()
                if existing_row is not None:
                    if overwrite_existing_metrics:
                        row, _ = await update_metric(session, existing_row.id, payload, actor=actor)
                        updated_metrics.append(metric_to_dict(row, source_name=ds.name))
                    else:
                        metric_errors.append({"key": m_key, "reason": f"指标「{m_key}」已存在，已跳过覆盖"})
                else:
                    row, _ = await create_metric(session, payload, actor=actor)
                    created_metrics.append(metric_to_dict(row, source_name=ds.name))
        except (MetricConflict, MetricInvalid, MetricNotFound, Exception) as exc:  # noqa: BLE001
            metric_errors.append({"key": m_key or "unknown", "reason": str(exc)})

    entity_apply_summary: dict[str, Any] = {"applied_count": 0, "problems": []}
    if selected_ent_ops:
        try:
            async with session.begin_nested():
                entity_apply_summary = await entity_patch.apply_entity_patch(
                    session,
                    selected_ent_ops,
                    mode="confirmed",
                    actor=actor,
                )
        except Exception as exc:  # noqa: BLE001
            entity_apply_summary = {"applied_count": 0, "problems": [str(exc)]}

    applied_record = {
        "applied_at": _iso(utcnow()),
        "applied_by": actor,
        "catalog_applied_count": catalog_applied_count,
        "catalog_errors": catalog_errors,
        "metrics_created_count": len(created_metrics),
        "metrics_updated_count": len(updated_metrics),
        "metric_errors": metric_errors,
        "entities_applied_count": int(entity_apply_summary.get("applied_count") or 0),
        "entity_errors": list(entity_apply_summary.get("problems") or []),
    }
    res["applied"] = applied_record
    job.result = res
    job.summary = {**dict(job.summary or {}), "applied": True, "applied_at": applied_record["applied_at"]}
    await session.commit()

    for rev_sid, rev_tbl, rev_before, rev_written in pending_after_reviews:
        catalog.after_review(rev_sid, rev_tbl, rev_before, rev_written)

    return {
        "job_id": job.id,
        "applied": applied_record,
        "created_metrics": created_metrics,
        "updated_metrics": updated_metrics,
    }


async def startup(*, live: Any = None) -> int:
    """服务启动或清理孤儿任务时，将上次残留未完成的拆解任务标记为 failed（中断说明）。"""
    _ = live
    running_ids = set(_ACTIVE_TASKS.keys())
    async with SessionLocal() as session:
        rows = list(
            (
                await session.execute(
                    select(SemanticDecomposeJob.id).where(SemanticDecomposeJob.status.in_(OPEN_STATUSES))
                )
            ).scalars()
        )
        stale_ids = [jid for jid in rows if jid not in running_ids]
        if stale_ids:
            await session.execute(
                update(SemanticDecomposeJob)
                .where(SemanticDecomposeJob.id.in_(stale_ids), SemanticDecomposeJob.status.in_(OPEN_STATUSES))
                .values(
                    status="failed",
                    phase="interrupted",
                    phase_label="服务重启导致任务中断",
                    error=INTERRUPTED_ERROR,
                    finished_at=utcnow(),
                )
            )
            await session.commit()
        return len(stale_ids)


async def shutdown() -> None:
    """服务关停时取消进程内正在执行的拆解任务。"""
    tasks = list(_ACTIVE_TASKS.values())
    for task in tasks:
        if not task.done():
            task.cancel()
    for task in tasks:
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
    await startup()
