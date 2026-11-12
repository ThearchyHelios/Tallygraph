"""实体助手（E2）：在标准业务实体页与使用者流式对话，跨数据源与业务文档对齐维护 CanonicalEntity。

核心规矩：
1. 只提结构化提案（propose_entity_changes），绝不静默直接写库；使用者在界面看过后点击「保存修改」才经
   apply_entity_patch(mode="confirmed") 落库。
2. 跨源只读探查（run_query）仅对已开启数据剖析的数据源放行，严格经 guard.check(readonly=True) 与遮罩列脱敏
   （mask_query_result）；遮罩列只给列名，不给注释或码值。
3. 支持直接读取本次对话挂载的上传附件或知识库文档片段（read_document），并要求在提案 reason 中区分「使用者说：……」、
   「文档《…》写着：……」与「数据探查显示：……」，同时主动向使用者汇报文档码表与实表取值的差异。
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.data import catalog, catalog_assistant, catalog_profile, entities, entity_docs, entity_patch, introspect, table_versions
from app.data.draft_model import DRAFT_MODEL_PLACE
from app.data.engine import SnapshotTampered, masked_columns
from app.db.base import SessionLocal
from app.db.models import DataSource, Workflow, WorkflowVersion

logger = logging.getLogger(__name__)

MAX_MESSAGES = 40
MESSAGE_MAX = 4000
MAX_ROUNDS = 8
MAX_QUERIES = 6
QUERY_ROWS = 50
SHOWN_ROWS = 10
CELL_MAX = 300
TOOL_CHARS = 12_000
RETRY_DELAYS_S = (3.0, 10.0)
HEARTBEAT_S = 10.0

INSPECT_TOOL = "inspect_sources"
READ_DOC_TOOL = "read_document"
QUERY_TOOL = "run_query"
PROPOSE_TOOL = "propose_entity_changes"

CODE_MODEL = "assistant_model_error"
CODE_TRUNCATED = "assistant_truncated"
CODE_TIMEOUT = "assistant_timeout"
CODE_ROUNDS = "assistant_rounds"
CODE_FAILED = "assistant_failed"

# ---- 面向界面展示的错误文案
ERROR_MODEL = (
    "调用模型「{model}」失败：{reason}。请稍后重试；如持续失败，请在{place}中换一个模型，"
    "或到「设置 → 模型接入」测试该模型"
)
ERROR_TRUNCATED = "{cut}，回答没有写完。可以把问题拆小一点再问，或在{place}中换一个模型"
ERROR_TIMEOUT = "模型 {seconds} 秒内没有回答完，已停止这一轮。可以把问题拆小一点再问，或在{place}中换一个模型"
ERROR_ROUNDS = "这一轮已经调用了 {n} 次模型仍没有答完，已停止。可以换个问法，或者分几次问"
ERROR_FAILED = "实体助手出错：{line}"
LAST_NOT_USER = "对话的最后一条应当是使用者的话，请重新发送"
QUERY_TAMPERED = "数据源「{name}」的{error}。请联系管理员检查数据目录"

# ---- 面向模型的提示词（命名含 _SYSTEM / _PROMPT）
ENTITY_ASSISTANT_SYSTEM = """你是 AgentLab 的「实体助手」，协助搭建者围绕当前选定范围的数据源与业务文档建立、完善「标准业务实体」（CanonicalEntity）：
包括实体的标准标识（key）、中文名称（label）、常用叫法（aliases）、物理表列绑定（bindings）、标准成员编码映射（members.source_codes）与口径入口参数映射（caliber_params）。

核心规矩：
1. 你不能修改业务数据库数据，也不直接写入标准业务实体：一切新增或修改都通过 propose_entity_changes 工具整理成结构化提案，由使用者在界面审阅后点击保存才生效。
2. 严格限定在「当前分析范围的数据源」内工作：只探查、查询和绑定范围内的数据源，绝不提及、猜测或扩展到未纳入范围的其他数据源。若当前范围只有 1 个数据源，聚焦盘点该数据源内的核心业务实体、多表共享主外键、码值字典与口径入参；若选了多个数据源，则重点对齐这些选定数据源之间的同义实体与编码映射。
3. 表名、列名、数据源名、工作流 ID、口径参数名必须先通过 inspect_sources 核实，严禁猜测不存在的表名或列名。
4. 若挂载了业务文档或编码对照表，优先用 read_document 阅读原文定义或码值映射，并结合 inspect_sources / run_query 去真实数据源交叉核验：
   - 如果文档里提到的编码与数据库实表抽样不一致（例如实表多出文档未登记的新编码，或文档编码在实表未出现），必须在回答正文中明确告知使用者，询问是否一并纳入。
   - 如果拿不准两套编码如何对应，先简短具体地向使用者提问，不要硬猜。
5. 提案中每条操作的 reason 字段必须如实写明依据来源：
   - 来自本次对话使用者亲口说明的，写「使用者说：……」；
   - 来自挂载文档或知识库文档的，写「文档《文档名》写着：……」；
   - 来自表目录或只读 SQL 核查的，写「数据探查显示：……」。
6. 标着「已确认」或「已验证」的既有绑定，除非使用者明确要求调整，否则不要擅自改动或删除；标着「已驳回」的绑定使用者曾明确拒绝过，不要重复提议。
7. 标着「遮罩列」的敏感字段禁止查询明细取值，也禁止为其生成成员编码映射（members.source_codes）。
8. 回答用简短直接的中文。

propose_entity_changes 的 operations 数组支持以下 5 种增量操作：
- upsert_entity: {"op": "upsert_entity", "entity_key": "小写英文下划线标识", "label": "中文名称", "description": "业务粒度说明", "aliases": ["常用叫法"], "reason": "依据"}
- upsert_binding: {"op": "upsert_binding", "entity_key": "实体标识", "entity_label": "中文名（新建时必填）", "source_name": "数据源名", "table_name": "物理表名", "column_name": "物理列名", "note": "备注", "reason": "依据"}
- remove_binding: {"op": "remove_binding", "entity_key": "实体标识", "source_name": "数据源名", "table_name": "物理表名", "column_name": "物理列名", "reason": "依据"}
- upsert_member: {"op": "upsert_member", "entity_key": "实体标识", "entity_label": "中文名", "value": "标准值", "label": "标准展示名", "aliases": ["成员别名"], "source_codes": {"数据源名": "该源底层编码"}, "reason": "依据"}
- upsert_caliber_param: {"op": "upsert_caliber_param", "entity_key": "实体标识", "entity_label": "中文名", "workflow_id": "工作流ID", "card_id": "可选口径卡节点ID", "param_name": "入口参数名", "note": "备注", "reason": "依据"}
"""

ENTITY_QUERY_PROMPT = (
    "跨源只读查询：可对开启了数据剖析的数据源调用 run_query 执行只读 SQL（如 SELECT DISTINCT 核对编码取值与行数分布），"
    "单次最多返回 {rows} 行，本轮最多执行 {limit} 次。可查询的数据源：{sources}。"
)
ENTITY_NO_QUERY_PROMPT = "当前范围内没有开启数据剖析的数据源，不能执行 run_query 查实表数据，请根据表结构、数据目录与业务文档推断对齐。"
ENTITY_OPENING_PROMPT = (
    "（使用者刚打开实体助手，尚未发言。）请结合当前分析范围的数据源、既有实体与挂载文档概况，用两三句话指出当前范围内最值得优先建立或补齐的业务实体，并提出一两个具体问题；先不要调用 propose_entity_changes。"
)
ENTITY_OPENED_PROMPT = "（使用者打开了实体助手。）"

ENTITY_TOOL_PROMPT = {
    "query_disabled_source": "数据源「{source}」未开启数据剖析或不存在，无法执行只读查询。可查询的数据源：{allowed}",
    "query_capped": "这一轮的只读查询次数已达上限（{n} 次），请根据已有信息回答或提出提案。",
    "empty_sql": "请填写要执行的只读 SQL 语句。",
    "query_failed": "查询未成功：{error}",
    "no_ops": "operations 为空，请提供至少一条修改操作。",
    "proposed": "已通过核验并向界面提交 {n} 项实体变更提案：{items}",
    "nothing": "没有产生新的有效变更（可能与当前状态完全相同，或全部未通过核验）。",
    "dropped": "以下项未通过平台核验，请核对表列名或参数后修正：{reasons}",
    "no_docs": "当前对话未挂载任何业务文档或对照表附件。",
    "unknown_tool": "未知工具「{name}」，可用工具：{tools}",
    "last_round": "注意：下一轮是最后一次模型调用，请直接用中文向使用者总结结论，不要再调用工具。",
}

INSPECT_TOOL_PROMPT: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": INSPECT_TOOL,
        "description": "在已启用的数据源表结构、数据目录（列中文名、含义、码值）及已发布口径入口参数中检索与业务实体相关的表列和入参。",
        "parameters": {
            "type": "object",
            "properties": {
                "keyword": {
                    "type": "string",
                    "description": "检索关键词（如「产线」「line」「仓库」「region」）；留空则返回各数据源表概览与口径入参清单。",
                },
                "source_name": {
                    "type": "string",
                    "description": "可选：限定只查看某一个数据源名称。",
                },
            },
        },
    },
}

READ_DOC_TOOL_PROMPT: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": READ_DOC_TOOL,
        "description": "读取本次对话挂载的业务文档、数据字典或编码对照表片段。",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "按关键词检索相关段落或表格行；留空则按顺序读取分段。",
                },
                "doc_ref": {
                    "type": "string",
                    "description": "可选：限定某一份文档的 ID 或标题关键词。",
                },
                "start_chunk": {
                    "type": "integer",
                    "description": "顺序读取时的起始段号（从 0 开始）。",
                },
                "limit": {
                    "type": "integer",
                    "description": "最多返回几段（默认 6，最多 15）。",
                },
            },
        },
    },
}

QUERY_TOOL_PROMPT: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": QUERY_TOOL,
        "description": "在指定数据源上执行一条只读 SQL（如 SELECT DISTINCT code, name FROM ... LIMIT 50），用于核对跨源成员编码与分布。",
        "parameters": {
            "type": "object",
            "properties": {
                "source_name": {"type": "string", "description": "要查询的数据源名称"},
                "sql": {"type": "string", "description": "只读 SELECT 查询语句"},
            },
            "required": ["source_name", "sql"],
        },
    },
}

PROPOSE_TOOL_PROMPT: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": PROPOSE_TOOL,
        "description": "提交一批标准业务实体的新建或修改提案（经平台核验后展示在界面待保存区，不直接写库）。",
        "parameters": {
            "type": "object",
            "properties": {
                "operations": {
                    "type": "array",
                    "description": "增量操作数组（upsert_entity / upsert_binding / remove_binding / upsert_member / upsert_caliber_param）",
                    "items": {"type": "object"},
                },
            },
            "required": ["operations"],
        },
    },
}


@dataclass(slots=True)
class _SourceEntry:
    row: DataSource
    resolved: Any
    kind: str
    masked: set[str]
    settings: catalog_profile.ProfileSettings
    can_query: bool
    catalog_notes: dict[str, Any]


@dataclass(slots=True)
class EntityAssistantContext:
    sources: dict[str, _SourceEntry]
    workflows: list[dict[str, Any]]
    doc_bundles: list[entity_docs.EntityDocBundle]
    pending_ops: list[dict[str, Any]] = field(default_factory=list)
    system: str = ""

    @property
    def queryable_sources(self) -> list[str]:
        return [entry.row.name for entry in self.sources.values() if entry.can_query]

    def tools(self) -> list[dict[str, Any]]:
        items = [INSPECT_TOOL_PROMPT]
        if self.doc_bundles:
            items.append(READ_DOC_TOOL_PROMPT)
        if self.queryable_sources:
            items.append(QUERY_TOOL_PROMPT)
        items.append(PROPOSE_TOOL_PROMPT)
        return items


resolve_model = catalog_assistant.resolve_model


def conversation_problem(messages: Iterable[Mapping[str, Any]]) -> str | None:
    said = [m for m in messages if str(m.get("content") or "").strip()]
    if any(m.get("role") == "user" for m in said) and said[-1].get("role") != "user":
        return LAST_NOT_USER
    return None


def _history(messages: Iterable[Mapping[str, Any]]) -> list[Any]:
    from langchain_core.messages import AIMessage, HumanMessage

    merged: list[tuple[str, str]] = []
    for m in messages:
        text = str(m.get("content") or "").strip()
        if not text:
            continue
        role = "user" if m.get("role") == "user" else "assistant"
        if merged and merged[-1][0] == role:
            merged[-1] = (role, f"{merged[-1][1]}\n\n{text}")
        else:
            merged.append((role, text))
    out: list[Any] = []
    if merged and merged[0][0] == "assistant":
        out.append(HumanMessage(content=ENTITY_OPENED_PROMPT))
    for role, text in merged:
        out.append(HumanMessage(content=text) if role == "user" else AIMessage(content=text))
    if not any(role == "user" for role, _ in merged):
        out.append(HumanMessage(content=ENTITY_OPENING_PROMPT))
    return out


def _extract_wf_summary(
    wf_rows: Sequence[Workflow],
    ver_rows: Sequence[WorkflowVersion],
    *,
    allowed_sources: set[str] | None = None,
) -> list[dict[str, Any]]:
    from app.tools.datasource import source_of_tool

    published_by_wf: dict[str, WorkflowVersion] = {}
    for ver in ver_rows:
        cur = published_by_wf.get(ver.workflow_id)
        if cur is None or int(ver.version or 0) > int(cur.version or 0):
            published_by_wf[ver.workflow_id] = ver

    summaries: list[dict[str, Any]] = []
    for wf in wf_rows:
        ver = published_by_wf.get(wf.id)
        graph = (ver.graph if ver and isinstance(ver.graph, Mapping) else wf.graph) or {}
        if not isinstance(graph, Mapping):
            continue
        cards: list[dict[str, Any]] = []
        inputs: list[dict[str, Any]] = []
        wf_sources: set[str] = set()
        for node in graph.get("nodes") or []:
            if not isinstance(node, Mapping):
                continue
            ntype = str(node.get("type") or "")
            nid = str(node.get("id") or "")
            data = node.get("data") if isinstance(node.get("data"), Mapping) else {}
            cfg = data.get("config") if isinstance(data.get("config"), Mapping) else (
                node.get("config") if isinstance(node.get("config"), Mapping) else {}
            )
            t_single = str(cfg.get("tool") or "").strip()
            if s := source_of_tool(t_single):
                if "{{" not in s:
                    wf_sources.add(s.lower())
            for t_item in cfg.get("tools") or []:
                if isinstance(t_item, str) and (s := source_of_tool(t_item.strip())) and "{{" not in s:
                    wf_sources.add(s.lower())
            for skey in ("source", "source_name", "datasource", "source_id"):
                sv = str(cfg.get(skey) or "").strip()
                if sv and "{{" not in sv:
                    wf_sources.add(sv.lower())

            if ntype == "metrics" and nid:
                m_names = [
                    str(m.get("name") or m.get("id") or "")
                    for m in (cfg.get("metrics") or [])
                    if isinstance(m, Mapping)
                ]
                cards.append({"card_id": nid, "metrics": [x for x in m_names if x][:8]})
            elif ntype == "input":
                for f in cfg.get("fields") or []:
                    if isinstance(f, Mapping) and str(f.get("name") or "").strip():
                        inputs.append({
                            "name": str(f["name"]).strip(),
                            "label": str(f.get("label") or f.get("description") or "").strip(),
                            "type": str(f.get("type") or "string"),
                        })
        if allowed_sources is not None:
            if wf_sources:
                if not (wf_sources & allowed_sources):
                    continue
            elif not allowed_sources:
                continue
        if cards and inputs:
            summaries.append({
                "workflow_id": wf.id,
                "name": wf.name,
                "published": ver is not None,
                "inputs": inputs,
                "cards": cards,
                "sources": sorted(wf_sources),
            })
    return summaries


async def prepare(
    session: AsyncSession,
    *,
    source_ids: Sequence[str] | None = None,
    focus_entity_key: str | None = None,
    attachment_ids: Sequence[str] = (),
    document_ids: Sequence[str] = (),
    pending_ops: Sequence[Mapping[str, Any]] = (),
) -> EntityAssistantContext:
    """组装实体助手上下文：按选定范围过滤的数据源表目录、既有标准实体、口径入参、挂载文档摘要与待保存提案。"""
    all_enabled = list(
        (await session.execute(select(DataSource).where(DataSource.enabled.is_(True)).order_by(DataSource.name))).scalars()
    )
    if source_ids is not None:
        wanted = {str(s or "").strip().lower() for s in source_ids if str(s or "").strip()}
        src_rows = [r for r in all_enabled if r.id.lower() in wanted or r.name.lower() in wanted]
    else:
        src_rows = all_enabled

    allowed_sources: set[str] | None = (
        {x for r in src_rows for x in (r.id.lower(), r.name.lower())}
        if source_ids is not None
        else None
    )

    sources: dict[str, _SourceEntry] = {}
    source_lines: list[str] = ["当前分析范围的数据源："]
    for row in src_rows:
        try:
            resolved = await table_versions.resolve_source(session, row)
        except table_versions.SnapshotMissing:
            resolved = row
        opts = dict(row.options or {}) if isinstance(row.options, Mapping) else {}
        masked = {c.lower() for c in masked_columns(opts)}
        p_settings = catalog_profile.profile_settings(opts)
        can_query = bool(p_settings.enabled and row.enabled)
        cat_entries = await catalog.read_catalog(session, resolved.id)
        notes_map = {tname: entry.notes for tname, entry in cat_entries.items()}
        sources[row.name.lower()] = _SourceEntry(
            row=row,
            resolved=resolved,
            kind=str(row.kind or ""),
            masked=masked,
            settings=p_settings,
            can_query=can_query,
            catalog_notes=notes_map,
        )
        tables = ((resolved.schema_cache or {}).get("tables") or {}) if isinstance(resolved.schema_cache, Mapping) else {}
        q_flag = "可只读查询" if can_query else "未开剖析"
        source_lines.append(f"- {row.name}（{row.kind}，共 {len(tables)} 张表，{q_flag}）{('：' + row.description) if row.description else ''}")
    if len(source_lines) == 1:
        source_lines.append("- （当前未选定任何数据源，仅基于挂载文档与既有实体分析）")

    clean_pending = [dict(op) for op in (pending_ops or ()) if isinstance(op, Mapping)]
    pending_keys = {
        str(op.get("entity_key") or "").strip().lower()
        for op in clean_pending
        if str(op.get("entity_key") or "").strip()
    }

    ent_rows = await entities.list_entities(session)
    focus_norm = (focus_entity_key or "").strip().lower()
    in_scope_entities: list[Any] = []
    other_domain_keys: list[str] = []
    for erow in ent_rows:
        ekey_low = erow.key.lower()
        b_list = [b for b in (erow.bindings or []) if isinstance(b, Mapping)]
        if allowed_sources is None or ekey_low == focus_norm or ekey_low in pending_keys or not b_list:
            in_scope_entities.append(erow)
            continue
        touches_scope = any(
            str(b.get("source_name") or "").strip().lower() in allowed_sources
            or str(b.get("source_id") or "").strip().lower() in allowed_sources
            for b in b_list
        )
        if touches_scope:
            in_scope_entities.append(erow)
        else:
            other_domain_keys.append(f"{erow.key}({erow.label})")

    entity_lines: list[str] = ["", f"当前范围相关的标准业务实体（共 {len(in_scope_entities)} 个）："]
    for erow in in_scope_entities[:60]:
        b_list = [b for b in (erow.bindings or []) if isinstance(b, Mapping)]
        if allowed_sources is not None and erow.key.lower() != focus_norm:
            b_list = [
                b for b in b_list
                if str(b.get("source_name") or "").strip().lower() in allowed_sources
                or str(b.get("source_id") or "").strip().lower() in allowed_sources
            ]
        active_b = [f"{b.get('source_name')}.{b.get('table_name')}.{b.get('column_name')}" for b in b_list if b.get("status") in ("confirmed", "verified")]
        prop_b = [f"{b.get('source_name')}.{b.get('table_name')}.{b.get('column_name')}" for b in b_list if b.get("status") == "proposed"]
        rej_b = [f"{b.get('source_name')}.{b.get('table_name')}.{b.get('column_name')}" for b in b_list if b.get("status") == "rejected"]
        m_count = len(erow.members or [])
        head = f"- {erow.label}（key={erow.key}，v{erow.version}，成员 {m_count} 项）"
        if active_b:
            head += f"｜已确认绑定：{', '.join(active_b[:8])}"
        if prop_b:
            head += f"｜待确认：{', '.join(prop_b[:6])}"
        if rej_b:
            head += f"｜已驳回（勿重提）：{', '.join(rej_b[:6])}"
        entity_lines.append(head)
        if focus_norm and erow.key.lower() == focus_norm:
            entity_lines.append(f"  【当前使用者正在查看该实体】说明：{erow.description or '无'}；别名：{', '.join(erow.aliases or []) or '无'}")
            for m in (erow.members or [])[:25]:
                if isinstance(m, Mapping):
                    codes_txt = ", ".join(f"{k}={v}" for k, v in (m.get("source_codes") or {}).items())
                    entity_lines.append(f"  · 成员 {m.get('value')}（{m.get('label')}）：{codes_txt or '无跨源编码'}")
    if other_domain_keys:
        entity_lines.append(
            f"（其他未选定数据源已占用的全局实体 key，避免重名冲突，无需分析未选定源：{', '.join(other_domain_keys[:30])}）"
        )

    wf_rows = list((await session.execute(select(Workflow))).scalars())
    ver_rows = list((await session.execute(select(WorkflowVersion).where(WorkflowVersion.published_at.is_not(None)))).scalars())
    workflows = _extract_wf_summary(wf_rows, ver_rows, allowed_sources=allowed_sources)

    doc_bundles = await entity_docs.load_doc_bundles(session, attachment_ids=attachment_ids, document_ids=document_ids)
    doc_lines: list[str] = []
    if doc_bundles:
        doc_lines.append("")
        doc_lines.append(f"本次对话挂载的业务文档/对照表（共 {len(doc_bundles)} 份，可用 read_document 检索详情）：")
        for b in doc_bundles:
            preview = b.chunks[0][:300].replace("\n", " ") if b.chunks else ""
            doc_lines.append(f"- 《{b.title}》（ref={b.ref_id}，共 {len(b.chunks)} 段，{b.char_count} 字）首段预览：{preview}…")

    pending_lines: list[str] = []
    if clean_pending:
        pending_lines.append("")
        pending_lines.append(f"界面待保存区已有的修改提案（共 {len(clean_pending)} 项，避免重复提议）：")
        for pop in clean_pending[-40:]:
            pending_lines.append(f"- {json.dumps(pop, ensure_ascii=False, default=str)}")

    queryable = [e.row.name for e in sources.values() if e.can_query]
    query_prompt = (
        ENTITY_QUERY_PROMPT.format(rows=QUERY_ROWS, limit=MAX_QUERIES, sources="、".join(queryable))
        if queryable
        else ENTITY_NO_QUERY_PROMPT
    )

    system = "\n\n".join(
        part
        for part in [
            ENTITY_ASSISTANT_SYSTEM,
            query_prompt,
            "\n".join([*source_lines, *entity_lines, *doc_lines, *pending_lines]),
        ]
        if part
    )
    return EntityAssistantContext(
        sources=sources,
        workflows=workflows,
        doc_bundles=doc_bundles,
        pending_ops=clean_pending,
        system=system,
    )


def _cell(value: Any, limit: int | None = None) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, (datetime, date)):
        text = value.isoformat()
    elif isinstance(value, Decimal):
        text = str(value)
    elif isinstance(value, (bytes, bytearray)):
        text = f"<{len(value)} 字节>"
    else:
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    if limit is not None and len(text) > limit:
        return text[:limit] + "…"
    return text


def _rows(rows: Any, n: int, limit: int | None) -> list[list[Any]]:
    out: list[list[Any]] = []
    for row in (rows or [])[:n]:
        cells = row if isinstance(row, list) else list(row.values()) if isinstance(row, dict) else [row]
        out.append([_cell(v, limit) for v in cells])
    return out


class _EntityTools:
    def __init__(self, ctx: EntityAssistantContext) -> None:
        from app.engine.masking import SeenValues

        self.ctx = ctx
        self.queries = 0
        self.seen_by_source: dict[str, Any] = {k: SeenValues() for k in ctx.sources}

    def inspect(self, keyword: Any, source_name: Any) -> str:
        kw = str(keyword or "").strip().lower()
        src_filter = str(source_name or "").strip().lower()
        lines: list[str] = []

        for skey, entry in self.ctx.sources.items():
            if src_filter and skey != src_filter and entry.row.id.lower() != src_filter:
                continue
            tables = ((entry.resolved.schema_cache or {}).get("tables") or {}) if isinstance(entry.resolved.schema_cache, Mapping) else {}
            if not tables:
                lines.append(f"数据源 {entry.row.name}：尚未探查表结构")
                continue
            matched_tables: list[str] = []
            for tname, tmeta in tables.items():
                if not isinstance(tmeta, Mapping):
                    continue
                notes = entry.catalog_notes.get(tname) if isinstance(entry.catalog_notes.get(tname), Mapping) else {}
                t_label_item = notes.get("label") if isinstance(notes.get("label"), Mapping) else {}
                t_label = str(t_label_item.get("value") or "") if t_label_item.get("status") != "rejected" else ""
                col_notes = notes.get("columns") if isinstance(notes.get("columns"), Mapping) else {}
                cols = introspect.table_columns(dict(tmeta))

                col_hits: list[str] = []
                for col in cols:
                    cname = str(col.get("name") or "")
                    is_masked = (
                        cname.lower() in entry.masked
                        or f"{tname.lower()}.{cname.lower()}" in entry.masked
                    )
                    cnote = col_notes.get(cname) if isinstance(col_notes.get(cname), Mapping) else {}
                    clabel = ""
                    cmeaning = ""
                    ccodes = ""
                    if not is_masked and isinstance(cnote, Mapping):
                        li = cnote.get("label")
                        if isinstance(li, Mapping) and li.get("status") != "rejected":
                            clabel = str(li.get("value") or "")
                        mi = cnote.get("meaning")
                        if isinstance(mi, Mapping) and mi.get("status") != "rejected":
                            cmeaning = str(mi.get("value") or "")
                        ci = cnote.get("codes")
                        if isinstance(ci, Mapping) and ci.get("status") != "rejected" and isinstance(ci.get("value"), Mapping):
                            pairs = [f"{k}={v}" for k, v in list(ci["value"].items())[:12]]
                            ccodes = f" 码值[{', '.join(pairs)}]" if pairs else ""
                    comment = "" if is_masked else str(col.get("comment") or "")
                    haystack = " ".join([tname, t_label, cname, clabel, cmeaning, comment, ccodes]).lower()
                    if not kw or kw in haystack:
                        desc_parts = [p for p in [clabel, cmeaning, comment] if p]
                        mask_tag = " [遮罩列]" if is_masked else ""
                        col_hits.append(
                            f"    · {cname} ({col.get('type') or 'unknown'}){mask_tag}"
                            + (f"：{' / '.join(desc_parts)}" if desc_parts else "")
                            + ccodes
                        )
                if col_hits:
                    head = f"  表 {tname}" + (f"（{t_label}）" if t_label else "")
                    matched_tables.append(head + "\n" + "\n".join(col_hits[:25]))
            if matched_tables:
                lines.append(f"【数据源 {entry.row.name}】\n" + "\n".join(matched_tables[:15]))

        wf_hits: list[str] = []
        for wf in self.ctx.workflows:
            hay = json.dumps(wf, ensure_ascii=False).lower()
            if not kw or kw in hay:
                in_txt = ", ".join(f"{i['name']}({i['label'] or i['type']})" for i in wf["inputs"])
                cd_txt = ", ".join(f"{c['card_id']}[{'/'.join(c['metrics'])}]" for c in wf["cards"])
                pub_tag = "已发布" if wf["published"] else "草稿"
                wf_hits.append(f"- 工作流 {wf['name']}（workflow_id={wf['workflow_id']}，{pub_tag}）｜入口参数：{in_txt}｜口径卡：{cd_txt}")
        if wf_hits:
            lines.append("【口径工作流与入口参数】\n" + "\n".join(wf_hits[:20]))

        if not lines:
            return f"未找到与「{keyword}」匹配的表列或口径参数，请尝试更短的词根或留空 keyword 查看全景概览。"
        text = "\n\n".join(lines)
        return text[:TOOL_CHARS] + ("…（已截断）" if len(text) > TOOL_CHARS else "")

    def read_doc(self, query: Any, doc_ref: Any, start_chunk: Any, limit: Any) -> str:
        if not self.ctx.doc_bundles:
            return ENTITY_TOOL_PROMPT["no_docs"]
        hits = entity_docs.search_doc_bundles(
            self.ctx.doc_bundles,
            query=str(query or ""),
            doc_ref=str(doc_ref or ""),
            start_chunk=int(start_chunk) if isinstance(start_chunk, int) else 0,
            limit=int(limit) if isinstance(limit, int) else 6,
        )
        if not hits:
            return "未检索到匹配的文档段落，可清空 query 按 start_chunk=0 顺序浏览。"
        blocks = [
            f"【{h['citation']}（共 {h['total_chunks']} 段，ref={h['doc_ref']}）】\n{h['content']}"
            for h in hits
        ]
        text = "\n\n".join(blocks)
        return text[:TOOL_CHARS] + ("…（已截断）" if len(text) > TOOL_CHARS else "")

    async def query(self, source_name: Any, sql: Any) -> tuple[dict[str, Any] | None, str]:
        prompts = ENTITY_TOOL_PROMPT
        src_key = str(source_name or "").strip().lower()
        entry = self.ctx.sources.get(src_key)
        if entry is None or not entry.can_query:
            allowed = "、".join(self.ctx.queryable_sources) or "无"
            return None, prompts["query_disabled_source"].format(source=source_name or "", allowed=allowed)
        if self.queries >= MAX_QUERIES:
            return None, prompts["query_capped"].format(n=MAX_QUERIES)
        clean_sql = sql.strip() if isinstance(sql, str) else ""
        if not clean_sql:
            return None, prompts["empty_sql"]

        self.queries += 1
        seen = self.seen_by_source[src_key]
        mask = masked_columns(dict(entry.row.options or {}) if isinstance(entry.row.options, Mapping) else None)
        known = await asyncio.to_thread(seen.pooled)
        known = known if known.strong else None
        error: str | None = None
        result: Any = None
        try:
            result = await catalog_profile.run_single_query(
                entry.resolved,
                clean_sql,
                settings=entry.settings,
                max_rows=QUERY_ROWS,
                kind=entry.kind,
            )
        except catalog_profile.QueryFailed as exc:
            error = exc.detail
        except SnapshotTampered as exc:
            error = QUERY_TAMPERED.format(name=entry.row.name, error=exc)
        except ValueError as exc:
            error = str(exc)

        if error is not None:
            if mask or known is not None:
                from app.engine.masking import mask_error_text

                error = mask_error_text(error, clean_sql, known)
            event = {
                "type": "query",
                "source": entry.row.name,
                "sql": clean_sql,
                "columns": [],
                "rows": [],
                "row_count": 0,
                "truncated": False,
                "error": error,
            }
            return event, prompts["query_failed"].format(error=error)

        payload = result.to_payload()
        if mask or known is not None:
            from app.engine.masking import mask_query_result

            schema = getattr(entry.resolved, "schema_cache", None)
            done = await asyncio.to_thread(mask_query_result, payload, mask, entry.kind, known, schema=schema)
            payload = done.payload
            await asyncio.to_thread(seen.add, done.secrets)

        columns = [str(c) for c in payload.get("columns") or []]
        rows = payload.get("rows") or []
        event = {
            "type": "query",
            "source": entry.row.name,
            "sql": str(payload.get("sql") or clean_sql),
            "columns": columns,
            "rows": _rows(rows, SHOWN_ROWS, None),
            "row_count": int(payload.get("row_count") or len(rows)),
            "truncated": bool(payload.get("truncated")),
            "error": None,
        }
        given = {
            "source": entry.row.name,
            "columns": columns,
            "rows": _rows(rows, QUERY_ROWS, CELL_MAX),
            "row_count": event["row_count"],
            "truncated": event["truncated"],
        }
        text = json.dumps(given, ensure_ascii=False, default=str)
        while len(text) > TOOL_CHARS and given["rows"]:
            given["rows"] = given["rows"][: max(1, len(given["rows"]) // 2)] if len(given["rows"]) > 1 else []
            text = json.dumps(given, ensure_ascii=False, default=str)
        return event, text

    async def propose(self, operations: Any) -> tuple[dict[str, Any] | None, str]:
        prompts = ENTITY_TOOL_PROMPT
        if not isinstance(operations, list) or not operations:
            return None, prompts["no_ops"]
        async with SessionLocal() as session:
            plan = await entity_patch.plan_entity_patch(
                session,
                operations,
                pending_ops=self.ctx.pending_ops,
                mode="confirmed",
            )
        said: list[str] = []
        event: dict[str, Any] | None = None
        if plan.changes:
            for ch in plan.changes:
                self.ctx.pending_ops.append(ch.op_payload)
            event = {
                "type": "proposal",
                "changes": [ch.as_dict() for ch in plan.changes],
                "expected_versions": plan.expected_versions,
            }
            said.append(
                prompts["proposed"].format(
                    n=len(plan.changes),
                    items="；".join(ch.summary for ch in plan.changes),
                )
            )
        else:
            said.append(prompts["nothing"])
        if plan.problems:
            said.append(prompts["dropped"].format(reasons="；".join(plan.problems)))
        return event, "\n".join(said)


_SENTENCE_END = ("。", "！", "？", "；", "：", ".", "!", "?", ";", ":")


def _sse(obj: Mapping[str, Any]) -> str:
    return f"data: {json.dumps(obj, ensure_ascii=False, default=str)}\n\n"


class _CallFailed(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _seconds(value: Any) -> str:
    number = float(value)
    return str(int(number)) if number.is_integer() else f"{number:g}"


def _transient(exc: BaseException) -> bool:
    from app.core.errors import NETWORK, RATE_LIMIT, SERVER, classify

    return classify(exc) in (RATE_LIMIT, SERVER, NETWORK)


async def _pump(runner: Any, history: list[Any], queue: asyncio.Queue[tuple[str, Any]]) -> None:
    try:
        async for chunk in runner.astream(history):
            await queue.put(("chunk", chunk))
        await queue.put(("end", None))
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        await queue.put(("error", exc))


async def _round(runner: Any, history: list[Any], model_id: str) -> AsyncIterator[tuple[str, Any]]:
    from app.core.errors import TIMEOUT, classify, describe_exception
    from app.engine.state import message_text
    from app.providers.budget import cut_off

    loop = asyncio.get_running_loop()
    attempt = 0
    while True:
        queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()
        task = asyncio.ensure_future(_pump(runner, history, queue))
        deadline = loop.time() + float(catalog.MODEL_TIMEOUT_S)
        message: Any = None
        cut = False
        started = False
        failure: BaseException | None = None
        try:
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise _CallFailed(
                        CODE_TIMEOUT,
                        ERROR_TIMEOUT.format(seconds=_seconds(catalog.MODEL_TIMEOUT_S), place=DRAFT_MODEL_PLACE),
                    )
                try:
                    kind, value = await asyncio.wait_for(queue.get(), timeout=min(remaining, HEARTBEAT_S))
                except asyncio.TimeoutError:
                    yield "beat", None
                    continue
                if kind == "end":
                    break
                if kind == "error":
                    failure = value
                    break
                message = value if message is None else message + value
                cut = cut or bool(cut_off(value))
                text = message_text(value)
                if text:
                    started = True
                    yield "text", text
        finally:
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        if failure is None:
            yield "message", (message, cut)
            return
        if not started and attempt < len(RETRY_DELAYS_S) and _transient(failure):
            logger.info("实体助手：调用模型出错（%s），%s 秒后重试", type(failure).__name__, RETRY_DELAYS_S[attempt])
            await asyncio.sleep(RETRY_DELAYS_S[attempt])
            attempt += 1
            continue
        if isinstance(failure, TimeoutError) or classify(failure) == TIMEOUT:
            raise _CallFailed(
                CODE_TIMEOUT,
                ERROR_TIMEOUT.format(seconds=_seconds(catalog.MODEL_TIMEOUT_S), place=DRAFT_MODEL_PLACE),
            ) from failure
        raise _CallFailed(
            CODE_MODEL,
            ERROR_MODEL.format(model=model_id or "", reason=describe_exception(failure), place=DRAFT_MODEL_PLACE),
        ) from failure


async def stream(
    model: Any,
    model_id: str,
    ctx: EntityAssistantContext,
    messages: Iterable[Mapping[str, Any]],
) -> AsyncIterator[str]:
    """实体助手流式生成器：逐行产出 SSE data 事件与心跳注释。"""
    from langchain_core.messages import SystemMessage, ToolMessage, message_chunk_to_message

    from app.providers.budget import cut_words

    history: list[Any] = [SystemMessage(content=ctx.system), *_history(messages)]
    tools = _EntityTools(ctx)
    usage = {"input_tokens": 0, "output_tokens": 0}
    wrote = ""
    tool_defs = ctx.tools()
    names = [t["function"]["name"] for t in tool_defs]
    try:
        runner = model.bind_tools(tool_defs)
        for round_no in range(MAX_ROUNDS):
            fresh = True
            message: Any = None
            cut = False
            async with contextlib.aclosing(_round(runner, history, model_id)) as rounds:
                async for kind, value in rounds:
                    if kind == "beat":
                        yield ": keep-alive\n\n"
                    elif kind == "text":
                        if fresh and wrote.endswith(_SENTENCE_END):
                            yield _sse({"type": "text", "delta": "\n\n"})
                        fresh = False
                        wrote = value
                        yield _sse({"type": "text", "delta": value})
                    else:
                        message, cut = value
            meta = getattr(message, "usage_metadata", None) or {}
            usage["input_tokens"] += int(meta.get("input_tokens") or 0)
            usage["output_tokens"] += int(meta.get("output_tokens") or 0)
            if cut:
                limit = getattr(model, "max_tokens", None)
                yield _sse({
                    "type": "error",
                    "code": CODE_TRUNCATED,
                    "message": ERROR_TRUNCATED.format(cut=cut_words("实体助手", limit), place=DRAFT_MODEL_PLACE),
                })
                return
            calls = list(getattr(message, "tool_calls", None) or []) if message is not None else []
            if not calls:
                yield _sse({"type": "done", "usage": usage, "queries": tools.queries})
                return
            if round_no == MAX_ROUNDS - 1:
                yield _sse({"type": "error", "code": CODE_ROUNDS, "message": ERROR_ROUNDS.format(n=MAX_ROUNDS)})
                return
            history.append(message_chunk_to_message(message))
            for call in calls:
                name = str(call.get("name") or "")
                args = call.get("args") if isinstance(call.get("args"), Mapping) else {}
                call_id = str(call.get("id") or f"{name}-{round_no}-{len(history)}")
                event: dict[str, Any] | None = None
                if name == INSPECT_TOOL:
                    content = tools.inspect(args.get("keyword"), args.get("source_name"))
                elif name == READ_DOC_TOOL:
                    content = tools.read_doc(
                        args.get("query"),
                        args.get("doc_ref"),
                        args.get("start_chunk"),
                        args.get("limit"),
                    )
                elif name == QUERY_TOOL:
                    event, content = await tools.query(args.get("source_name"), args.get("sql"))
                elif name == PROPOSE_TOOL:
                    event, content = await tools.propose(args.get("operations"))
                else:
                    content = ENTITY_TOOL_PROMPT["unknown_tool"].format(name=name, tools="、".join(names))
                if event is not None:
                    yield _sse(event)
                if round_no == MAX_ROUNDS - 2:
                    content = f"{content}\n{ENTITY_TOOL_PROMPT['last_round']}"
                history.append(ToolMessage(content=content, tool_call_id=call_id))
    except _CallFailed as exc:
        yield _sse({"type": "error", "code": exc.code, "message": exc.message})
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        from app.core.errors import describe_exception, first_line

        logger.exception("实体助手执行异常")
        yield _sse({
            "type": "error",
            "code": CODE_FAILED,
            "message": ERROR_FAILED.format(line=first_line(exc) or describe_exception(exc)),
        })
