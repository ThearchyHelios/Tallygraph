"""后台主动发现候选标准业务实体任务（E2：扫描发现候选实体）。

扫描范围：
1. 所选已启用数据源的表结构（主键、外键、标识/属性/状态列）与数据目录（中文名、含义、非遮罩码值）；
2. 已发布与草稿口径工作流的入口参数（entry_fields / input 节点）及口径卡；
3. 可选挂载的业务文档或跨系统编码对照表（知识库 Document 或临时上传附件）。

铁律：
- 发现结果一律经 entity_patch.apply_entity_patch(..., mode="proposed") 写入，所有表列绑定与口径入参映射强制标记为
  status="proposed", source="ai"，绝不直接参与线上跨源穿透，也绝不覆盖人工已确认、已验证或已驳回的项。
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import time
from collections.abc import Mapping, Sequence
from datetime import timezone
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import describe_exception, first_line
from app.data import catalog, catalog_assistant, entities, entity_assistant, entity_docs, entity_patch, introspect, table_versions
from app.data.engine import masked_columns
from app.db.base import SessionLocal, utcnow
from app.db.models import DataSource, Workflow, WorkflowVersion
from app.db.models_ontology import EntityDiscoveryJob

logger = logging.getLogger(__name__)

OPEN_STATUSES = ("queued", "running")
NOTE_MAX = 1000
MAX_DOC_CHARS_IN_DISCOVERY = 28_000
MAX_SCHEMA_CHARS_IN_DISCOVERY = 36_000

DISCOVERY_BUSY_CODE = "discovery_job_busy"
DISCOVERY_FINISHED_CODE = "discovery_job_finished"

INTERRUPTED_ERROR = "服务重启，候选实体扫描中断"
JOB_FAILED_TEXT = "扫描候选实体失败：{reason}"

_FENCE_RE = re.compile(r"```(?:json)?\s*([\s\S]*?)```", re.IGNORECASE)
_ACTIVE_TASKS: dict[str, asyncio.Task[Any]] = {}
_CREATE_LOCK = asyncio.Lock()


class DiscoveryBusy(Exception):
    """已有正在排队或执行的候选实体扫描任务。"""


class DiscoveryFinished(Exception):
    """要取消的候选实体扫描任务已经结束。"""


class DiscoveryEmptyScope(ValueError):
    """扫描范围内没有任何可用的数据源表结构或业务文档。"""


DISCOVERY_SYSTEM_PROMPT = """你是 AgentLab 的「业务实体发现专家」。请根据给定的数据源表结构与数据目录、口径工作流入口参数以及业务文档/编码对照表，识别共用的「标准业务实体」（如产线、工厂、车间、仓库、门店、销售大区、渠道、客户等级、不良代码等），并输出增量操作 JSON。

硬性纪律：
1. 只输出一个合法 JSON 对象，格式为 {"operations": [...]}，不要输出任何额外解释文字。
2. 若输入包含多个数据源，优先发现在多源之间形成桥接的核心业务对象；若仅包含单个数据源，则发现在该源多张业务表、口径入口参数或业务文档之间共用的核心业务维度实体。纯技术自增流水主键不要提议为业务实体。
3. 表名、列名、数据源名、工作流 ID、参数名必须逐字取自输入材料，严禁编造不存在的字段。
4. 标着「[遮罩列]」的敏感字段严禁为其生成 upsert_member 编码映射；标着「已驳回」的历史绑定严禁重复提议。
5. 每条操作必须填写简明的中文 reason：
   - 来自文档或对照表的写「文档《文档名》写着：……」；
   - 来自表目录或字段语义对齐的写「目录语义对齐：……」。

operations 支持以下 4 种结构：
- {"op": "upsert_entity", "entity_key": "小写英文下划线标识（如 production_line）", "label": "中文名称", "description": "粒度与业务对齐说明", "aliases": ["同义词"], "reason": "依据"}
- {"op": "upsert_binding", "entity_key": "实体标识", "entity_label": "中文名称", "source_name": "数据源名称", "table_name": "物理表名", "column_name": "物理列名", "note": "字段角色备注", "reason": "依据"}
- {"op": "upsert_member", "entity_key": "实体标识", "entity_label": "中文名称", "value": "标准编码值", "label": "标准展示名", "aliases": ["别名"], "source_codes": {"数据源名称": "该源编码值"}, "reason": "依据"}
- {"op": "upsert_caliber_param", "entity_key": "实体标识", "entity_label": "中文名称", "workflow_id": "工作流ID", "card_id": "口径卡节点ID（可空串）", "param_name": "入口参数名", "note": "参数对齐说明", "reason": "依据"}
"""

DISCOVERY_USER_PROMPT = """请扫描以下数据源结构、口径参数与业务文档，提取候选标准业务实体及其跨源绑定与编码映射，输出 {{"operations": [...]}} JSON：

{body}
"""


def _iso(val: Any) -> str | None:
    if val is None or not hasattr(val, "isoformat"):
        return None
    if getattr(val, "tzinfo", None) is None:
        val = val.replace(tzinfo=timezone.utc)
    return val.isoformat()


def job_to_dict(row: EntityDiscoveryJob) -> dict[str, Any]:
    return {
        "id": row.id,
        "status": row.status,
        "source_ids": list(row.source_ids or []),
        "document_ids": list(row.document_ids or []),
        "attachment_ids": list(row.attachment_ids or []),
        "note": row.note or "",
        "phase": row.phase or "",
        "summary": dict(row.summary or {}),
        "error": row.error,
        "started_by": row.started_by,
        "started_at": _iso(row.started_at),
        "finished_at": _iso(row.finished_at),
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
    }


def parse_discovery_operations(raw_text: str) -> list[dict[str, Any]]:
    """从模型回复中提取 operations 列表。"""
    text = (raw_text or "").strip()
    if not text:
        return []
    m = _FENCE_RE.search(text)
    candidate = m.group(1).strip() if m else text
    if not candidate.startswith(("{", "[")):
        brace = candidate.find("{")
        bracket = candidate.find("[")
        starts = [i for i in (brace, bracket) if i >= 0]
        if starts:
            candidate = candidate[min(starts) :]
    try:
        parsed = json.loads(candidate)
    except ValueError:
        # 尝试截取最后一个匹配括号
        end_brace = candidate.rfind("}")
        if end_brace > 0:
            try:
                parsed = json.loads(candidate[: end_brace + 1])
            except ValueError:
                return []
        else:
            return []
    if isinstance(parsed, list):
        return [dict(x) for x in parsed if isinstance(x, Mapping)]
    if isinstance(parsed, Mapping):
        ops = parsed.get("operations")
        if isinstance(ops, list):
            return [dict(x) for x in ops if isinstance(x, Mapping)]
    return []


async def get_active_or_latest_job(session: AsyncSession) -> dict[str, Any]:
    active = (
        await session.execute(
            select(EntityDiscoveryJob)
            .where(EntityDiscoveryJob.status.in_(OPEN_STATUSES))
            .order_by(EntityDiscoveryJob.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    latest = (
        await session.execute(
            select(EntityDiscoveryJob)
            .order_by(EntityDiscoveryJob.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return {
        "job": job_to_dict(active) if active is not None else None,
        "latest": job_to_dict(latest) if latest is not None else None,
    }


async def get_job(session: AsyncSession, job_id: str) -> EntityDiscoveryJob | None:
    return await session.get(EntityDiscoveryJob, str(job_id or "").strip())


async def _build_discovery_prompt_body(
    session: AsyncSession,
    *,
    source_ids: Sequence[str],
    document_ids: Sequence[str],
    attachment_ids: Sequence[str],
    note: str,
) -> tuple[str, dict[str, Any]]:
    """返回 (prompt_body, 扫描范围量化统计)。"""
    stmt = select(DataSource).where(DataSource.enabled.is_(True)).order_by(DataSource.name)
    if source_ids:
        stmt = stmt.where(DataSource.id.in_(list(source_ids)))
    src_rows = list((await session.execute(stmt)).scalars())

    schema_blocks: list[str] = []
    valid_sources = 0
    source_names: list[str] = []
    tables_scanned = 0
    columns_scanned = 0
    for row in src_rows:
        try:
            resolved = await table_versions.resolve_source(session, row)
        except table_versions.SnapshotMissing:
            resolved = row
        tables = ((resolved.schema_cache or {}).get("tables") or {}) if isinstance(resolved.schema_cache, Mapping) else {}
        if not tables:
            continue
        valid_sources += 1
        source_names.append(row.name)
        opts = dict(row.options or {}) if isinstance(row.options, Mapping) else {}
        masked = {c.lower() for c in masked_columns(opts)}
        cat_entries = await catalog.read_catalog(session, resolved.id)
        t_lines = [f"### 数据源：{row.name}（{row.kind}）"]
        for tname, tmeta in list(tables.items())[:40]:
            if not isinstance(tmeta, Mapping):
                continue
            entry = cat_entries.get(tname)
            notes = entry.notes if entry else {}
            t_label_item = notes.get("label") if isinstance(notes.get("label"), Mapping) else {}
            t_label = str(t_label_item.get("value") or "") if t_label_item.get("status") != "rejected" else ""
            col_notes = notes.get("columns") if isinstance(notes.get("columns"), Mapping) else {}
            cols = introspect.table_columns(dict(tmeta))
            c_parts: list[str] = []
            for col in cols[:40]:
                cname = str(col.get("name") or "")
                is_masked = cname.lower() in masked or f"{tname.lower()}.{cname.lower()}" in masked
                cnote = col_notes.get(cname) if isinstance(cnote := col_notes.get(cname), Mapping) else {}
                measure = ""
                clabel = ""
                cmeaning = ""
                ccodes = ""
                if not is_masked and isinstance(cnote, Mapping):
                    mi = cnote.get("measure")
                    if isinstance(mi, Mapping) and mi.get("status") != "rejected":
                        measure = str(mi.get("value") or "")
                    # 跳过明显的纯度量浮点流量列，聚焦维度/标识/状态/外键或未分类列
                    if measure in ("flow", "stock", "ratio"):
                        continue
                    li = cnote.get("label")
                    if isinstance(li, Mapping) and li.get("status") != "rejected":
                        clabel = str(li.get("value") or "")
                    mgi = cnote.get("meaning")
                    if isinstance(mgi, Mapping) and mgi.get("status") != "rejected":
                        cmeaning = str(mgi.get("value") or "")
                    ci = cnote.get("codes")
                    if isinstance(ci, Mapping) and ci.get("status") != "rejected" and isinstance(ci.get("value"), Mapping):
                        pairs = [f"{k}={v}" for k, v in list(ci["value"].items())[:15]]
                        ccodes = f" 码值[{', '.join(pairs)}]" if pairs else ""
                comment = "" if is_masked else str(col.get("comment") or "")
                tags = [p for p in [clabel, cmeaning, comment] if p]
                pk_tag = "[PK]" if col.get("pk") else ""
                mask_tag = "[遮罩列]" if is_masked else ""
                c_parts.append(
                    f"  - {cname} ({col.get('type') or ''}){pk_tag}{mask_tag}"
                    + (f"：{' / '.join(tags)}" if tags else "")
                    + ccodes
                )
            if c_parts:
                tables_scanned += 1
                columns_scanned += len(c_parts)
                t_lines.append(f"- 表 {tname}" + (f"（{t_label}）" if t_label else "") + "\n" + "\n".join(c_parts))
        block = "\n".join(t_lines)
        schema_blocks.append(block)

    schema_text = "\n\n".join(schema_blocks)
    if len(schema_text) > MAX_SCHEMA_CHARS_IN_DISCOVERY:
        schema_text = schema_text[:MAX_SCHEMA_CHARS_IN_DISCOVERY] + "\n…（表结构摘要已截断）"

    allowed_sources: set[str] | None = (
        {x for r in src_rows for x in (r.id.lower(), r.name.lower())}
        if source_ids
        else None
    )

    wf_rows = list((await session.execute(select(Workflow))).scalars())
    ver_rows = list((await session.execute(select(WorkflowVersion).where(WorkflowVersion.published_at.is_not(None)))).scalars())
    workflows = entity_assistant._extract_wf_summary(wf_rows, ver_rows, allowed_sources=allowed_sources)
    wf_lines: list[str] = []
    if workflows:
        wf_lines.append("## 口径工作流与入口参数")
        for wf in workflows[:30]:
            ins = ", ".join(f"{i['name']}({i['label'] or i['type']})" for i in wf["inputs"])
            cds = ", ".join(f"{c['card_id']}[{'/'.join(c['metrics'])}]" for c in wf["cards"])
            wf_lines.append(f"- {wf['name']}（workflow_id={wf['workflow_id']}）｜入口参数：{ins}｜口径卡：{cds}")

    ent_rows = await entities.list_entities(session)
    ent_lines: list[str] = []
    if ent_rows:
        ent_lines.append("## 既有标准业务实体（请复用同域已有 entity_key 增补绑定，勿重提已驳回项）")
        other_keys: list[str] = []
        for erow in ent_rows[:60]:
            b_list = [b for b in (erow.bindings or []) if isinstance(b, Mapping)]
            if allowed_sources is not None and b_list:
                b_scoped = [
                    b for b in b_list
                    if str(b.get("source_name") or "").strip().lower() in allowed_sources
                    or str(b.get("source_id") or "").strip().lower() in allowed_sources
                ]
                if not b_scoped:
                    other_keys.append(f"{erow.key}({erow.label})")
                    continue
                b_list = b_scoped
            active_b = [f"{b.get('source_name')}.{b.get('table_name')}.{b.get('column_name')}" for b in b_list if b.get("status") != "rejected"]
            rej_b = [f"{b.get('source_name')}.{b.get('table_name')}.{b.get('column_name')}" for b in b_list if b.get("status") == "rejected"]
            line = f"- {erow.label}（entity_key={erow.key}）｜已有绑定：{', '.join(active_b) or '无'}"
            if rej_b:
                line += f"｜已驳回：{', '.join(rej_b)}"
            ent_lines.append(line)
        if other_keys:
            ent_lines.append(f"（其他未选定数据源已占用的实体标识：{', '.join(other_keys[:30])}）")

    bundles = await entity_docs.load_doc_bundles(session, attachment_ids=attachment_ids, document_ids=document_ids)
    doc_blocks: list[str] = []
    used_doc_chars = 0
    for bundle in bundles:
        piece_text = "\n\n".join(bundle.chunks)
        remain = MAX_DOC_CHARS_IN_DISCOVERY - used_doc_chars
        if remain <= 0:
            break
        clipped = piece_text[:remain]
        used_doc_chars += len(clipped)
        doc_blocks.append(f"### 文档《{bundle.title}》\n{clipped}")

    sections: list[str] = []
    if note.strip():
        sections.append(f"## 使用者补充的扫描重点\n{note.strip()}")
    if schema_text:
        sections.append(f"## 数据源与表目录\n{schema_text}")
    if wf_lines:
        sections.append("\n".join(wf_lines))
    if ent_lines:
        sections.append("\n".join(ent_lines))
    if doc_blocks:
        sections.append("## 业务文档与主数据对照表\n" + "\n\n".join(doc_blocks))

    scope_stats: dict[str, Any] = {
        "sources_scanned": valid_sources,
        "source_names": source_names,
        "tables_scanned": tables_scanned,
        "columns_scanned": columns_scanned,
        "workflows_scanned": len(workflows),
        "documents_scanned": len(bundles),
        "doc_titles": [b.title for b in bundles[:15]],
        "chunks_scanned": sum(len(b.chunks) for b in bundles),
    }
    return "\n\n".join(sections), scope_stats


async def create_job(
    session: AsyncSession,
    *,
    source_ids: Sequence[str] | None = None,
    document_ids: Sequence[str] | None = None,
    attachment_ids: Sequence[str] | None = None,
    note: str | None = None,
    actor: str | None = None,
) -> EntityDiscoveryJob:
    clean_note = (note or "").strip()
    if len(clean_note) > NOTE_MAX:
        raise ValueError(f"扫描补充说明最多 {NOTE_MAX} 字，当前 {len(clean_note)} 字")

    # 建任务前先校验起草模型可用性
    await catalog_assistant.resolve_model(session)

    async with _CREATE_LOCK:
        existing = (
            await session.execute(
                select(EntityDiscoveryJob).where(EntityDiscoveryJob.status.in_(OPEN_STATUSES)).limit(1)
            )
        ).scalar_one_or_none()
        if existing is not None:
            raise DiscoveryBusy("已有一个正在执行的候选实体扫描任务，请等待完成后再发起")

        clean_sources = list(dict.fromkeys(str(s).strip() for s in (source_ids or []) if str(s or "").strip()))
        clean_docs = list(dict.fromkeys(str(d).strip() for d in (document_ids or []) if str(d or "").strip()))
        clean_atts = list(dict.fromkeys(str(a).strip() for a in (attachment_ids or []) if str(a or "").strip()))

        prompt_body, scope_stats = await _build_discovery_prompt_body(
            session,
            source_ids=clean_sources,
            document_ids=clean_docs,
            attachment_ids=clean_atts,
            note=clean_note,
        )
        if scope_stats["sources_scanned"] == 0 and scope_stats["documents_scanned"] == 0 or not prompt_body.strip():
            raise DiscoveryEmptyScope("所选范围内没有已探查表结构的数据源或有效业务文档，请先探查表结构或添加文档")

        job = EntityDiscoveryJob(
            status="queued",
            source_ids=clean_sources,
            document_ids=clean_docs,
            attachment_ids=clean_atts,
            note=clean_note,
            phase="已加入扫描队列…",
            summary={
                **scope_stats,
                "step_index": 1,
                "step_total": 4,
                "progress_pct": 8,
            },
            started_by=actor,
        )
        session.add(job)
        await session.commit()
        await session.refresh(job)

        task = asyncio.create_task(_execute_job(job.id))
        _ACTIVE_TASKS[job.id] = task
        task.add_done_callback(lambda _t, jid=job.id: _ACTIVE_TASKS.pop(jid, None))
        return job


async def _flush_job_progress(job_id: str, *, phase: str, summary: Mapping[str, Any]) -> None:
    async with SessionLocal() as session:
        await session.execute(
            update(EntityDiscoveryJob)
            .where(EntityDiscoveryJob.id == job_id, EntityDiscoveryJob.status.in_(OPEN_STATUSES))
            .values(phase=phase, summary=dict(summary))
        )
        await session.commit()


async def _execute_job(job_id: str) -> None:
    from langchain_core.messages import HumanMessage, SystemMessage

    from app.engine.state import message_text

    try:
        async with SessionLocal() as session:
            job = await session.get(EntityDiscoveryJob, job_id)
            if job is None or job.status not in OPEN_STATUSES:
                return
            job.status = "running"
            job.started_at = utcnow()
            base_summary = dict(job.summary or {})
            base_summary.update({"step_index": 1, "step_total": 4, "progress_pct": 18})
            job.summary = base_summary
            job.phase = "步骤 1/4：正在读取数据源表目录与口径参数…"
            await session.commit()

            prompt_body, scope_stats = await _build_discovery_prompt_body(
                session,
                source_ids=job.source_ids or [],
                document_ids=job.document_ids or [],
                attachment_ids=job.attachment_ids or [],
                note=job.note or "",
            )
            model, model_id = await catalog_assistant.resolve_model(session)
            doc_note = (
                f"、{scope_stats['documents_scanned']} 份文档（{scope_stats['chunks_scanned']} 段）"
                if scope_stats["documents_scanned"] > 0
                else ""
            )
            live_summary: dict[str, Any] = {
                **scope_stats,
                "model_id": model_id,
                "step_index": 3,
                "step_total": 4,
                "progress_pct": 38,
                "model_chars": 0,
                "ops_detected": 0,
                "candidate_keys": [],
            }
            job.summary = live_summary
            job.phase = (
                f"步骤 3/4：已汇总 {scope_stats['sources_scanned']} 个源（{scope_stats['tables_scanned']} 张表）{doc_note}，"
                "正在调用模型识别候选实体…"
            )
            await session.commit()

        msgs = [
            SystemMessage(content=DISCOVERY_SYSTEM_PROMPT),
            HumanMessage(content=DISCOVERY_USER_PROMPT.format(body=prompt_body)),
        ]

        first_token_seen = False
        call_started = time.monotonic()

        async def _thinking_heartbeat() -> None:
            nonlocal live_summary
            while not first_token_seen:
                await asyncio.sleep(2.0)
                if first_token_seen:
                    break
                waited = int(time.monotonic() - call_started)
                pct = min(52, 38 + waited // 2)
                live_summary = {
                    **live_summary,
                    "step_index": 3,
                    "step_total": 4,
                    "progress_pct": pct,
                }
                await _flush_job_progress(
                    job_id,
                    phase="步骤 3/4：模型正在深度分析表目录与业务关系，等待生成结果…",
                    summary=live_summary,
                )

        hb_task = asyncio.create_task(_thinking_heartbeat())
        try:
            if hasattr(model, "astream") and not getattr(model, "invoke_reply", ""):
                pieces: list[str] = []
                last_flush = time.monotonic()
                async for chunk in model.astream(msgs):
                    piece = message_text(chunk)
                    if piece:
                        first_token_seen = True
                        pieces.append(piece)
                    now_mono = time.monotonic()
                    if now_mono - last_flush >= 0.8 and pieces:
                        last_flush = now_mono
                        so_far = "".join(pieces)
                        op_count = len(re.findall(r'"op"\s*:', so_far))
                        found_keys = list(dict.fromkeys(re.findall(r'"entity_key"\s*:\s*"([a-z][a-z0-9_]{0,63})"', so_far)))
                        pct = min(86, 53 + min(33, len(so_far) // 80))
                        live_summary = {
                            **live_summary,
                            "step_index": 3,
                            "step_total": 4,
                            "progress_pct": pct,
                            "model_chars": len(so_far),
                            "ops_detected": op_count,
                            "candidate_keys": found_keys[:15],
                        }
                        phase_txt = (
                            f"步骤 3/4：模型正在识别候选实体（已识别 {op_count} 项操作，输出 {len(so_far)} 字）…"
                            if op_count > 0
                            else f"步骤 3/4：模型正在流式输出候选结果（已输出 {len(so_far)} 字）…"
                        )
                        await _flush_job_progress(job_id, phase=phase_txt, summary=live_summary)
                raw_text = "".join(pieces)
            else:
                reply = await model.ainvoke(msgs)
                first_token_seen = True
                raw_text = message_text(reply)
        finally:
            first_token_seen = True
            hb_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await hb_task

        ops = parse_discovery_operations(raw_text)
        found_keys_final = list(
            dict.fromkeys(
                str(op.get("entity_key") or "").strip().lower()
                for op in ops
                if isinstance(op, Mapping) and str(op.get("entity_key") or "").strip()
            )
        )

        async with SessionLocal() as session:
            job = await session.get(EntityDiscoveryJob, job_id)
            if job is None or job.status == "cancelled":
                return
            job.phase = f"步骤 4/4：正在核验 {len(ops)} 项候选的表列真实性并写入待确认列表…"
            job.summary = {
                **live_summary,
                "step_index": 4,
                "step_total": 4,
                "progress_pct": 90,
                "model_chars": len(raw_text),
                "ops_detected": len(ops),
                "candidate_keys": found_keys_final[:15],
            }
            await session.commit()

            if ops:
                applied = await entity_patch.apply_entity_patch(
                    session,
                    ops,
                    mode="proposed",
                    actor=job.started_by,
                )
            else:
                applied = {
                    "applied_count": 0,
                    "created_entities": 0,
                    "updated_entities": 0,
                    "bindings_changed": 0,
                    "caliber_params_changed": 0,
                    "members_changed": 0,
                    "entities": [],
                    "problems": [],
                }
            touched_entities = [
                {"id": e["id"], "key": e["key"], "label": e["label"]}
                for e in (applied.get("entities") or [])[:30]
                if isinstance(e, Mapping) and e.get("id") and e.get("key")
            ]
            problems = [str(p) for p in (applied.get("problems") or [])]
            job.status = "succeeded"
            job.phase = "扫描完成"
            job.finished_at = utcnow()
            job.summary = {
                **scope_stats,
                "model_id": model_id,
                "step_index": 4,
                "step_total": 4,
                "progress_pct": 100,
                "model_chars": len(raw_text),
                "ops_detected": len(ops),
                "candidate_keys": found_keys_final[:15],
                "applied_count": applied["applied_count"],
                "created_entities": applied["created_entities"],
                "updated_entities": applied["updated_entities"],
                "proposed_bindings": applied["bindings_changed"],
                "proposed_params": applied["caliber_params_changed"],
                "added_members": applied["members_changed"],
                "skipped_count": len(problems),
                "skipped_reasons": problems[:15],
                "touched_entities": touched_entities,
            }
            await session.commit()
    except asyncio.CancelledError:
        async with SessionLocal() as session:
            job = await session.get(EntityDiscoveryJob, job_id)
            if job is not None and job.status in OPEN_STATUSES:
                job.status = "cancelled"
                job.phase = "已停止扫描"
                job.finished_at = utcnow()
                await session.commit()
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("候选实体扫描任务 %s 失败", job_id)
        async with SessionLocal() as session:
            job = await session.get(EntityDiscoveryJob, job_id)
            if job is not None and job.status in OPEN_STATUSES:
                job.status = "failed"
                job.phase = "扫描失败"
                job.error = JOB_FAILED_TEXT.format(reason=first_line(exc) or describe_exception(exc))
                job.finished_at = utcnow()
                await session.commit()


async def cancel_job(session: AsyncSession, job_id: str) -> EntityDiscoveryJob:
    job = await session.get(EntityDiscoveryJob, str(job_id or "").strip())
    if job is None:
        raise LookupError("候选实体扫描任务不存在")
    if job.status not in OPEN_STATUSES:
        raise DiscoveryFinished("该候选实体扫描任务已经结束")
    task = _ACTIVE_TASKS.get(job.id)
    if task is not None and not task.done():
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await asyncio.wait_for(asyncio.shield(task), timeout=2.0)
    await session.refresh(job)
    if job.status in OPEN_STATUSES:
        job.status = "cancelled"
        job.phase = "已停止扫描"
        job.finished_at = utcnow()
        await session.commit()
        await session.refresh(job)
    return job


async def startup(*, live: Any = None) -> int:
    """服务启动或清理孤儿任务时，将上次残留的未结束扫描任务标为 interrupted。"""
    _ = live
    running_ids = set(_ACTIVE_TASKS.keys())
    async with SessionLocal() as session:
        rows = list(
            (
                await session.execute(
                    select(EntityDiscoveryJob.id).where(EntityDiscoveryJob.status.in_(OPEN_STATUSES))
                )
            ).scalars()
        )
        stale_ids = [jid for jid in rows if jid not in running_ids]
        if stale_ids:
            await session.execute(
                update(EntityDiscoveryJob)
                .where(EntityDiscoveryJob.id.in_(stale_ids), EntityDiscoveryJob.status.in_(OPEN_STATUSES))
                .values(
                    status="interrupted",
                    phase="扫描中断",
                    error=INTERRUPTED_ERROR,
                    finished_at=utcnow(),
                )
            )
            await session.commit()
        return len(stale_ids)


async def shutdown() -> None:
    """服务关停时取消进程内正在执行的扫描任务并标为 interrupted。"""
    tasks = list(_ACTIVE_TASKS.values())
    for task in tasks:
        if not task.done():
            task.cancel()
    for task in tasks:
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
    await startup()
