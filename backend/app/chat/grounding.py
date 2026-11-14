"""问数入口毫秒级确定性语义预命中引擎（Semantic Grounding，0 次模型调用）。

把此前分散、未被问数热路径直接利用的三类资产在提问入口一次性打通：
1. **独立语义指标卡片（SemanticMetric）**：按指标名、同义词、描述与词元重合度匹配已启用指标，直接向 Agent 提供
   标准口径表达式与 `metric_query` 调用指引；
2. **标准业务实体（CanonicalEntity）**：扫描已确认（verified / confirmed）实体的成员名称、别名、源侧编码及实体维度名，
   在 SQL 编写前完成跨源物理表列定位与底层编码确定性消歧（无需 `SELECT DISTINCT` 盲猜）；
3. **数据目录与数据剖析（DataCatalog + Profile）**：联合指标底表、实体绑定表与 BM25 表检索（schema_view.TableSearch）
   直选最相关的前若干张物理表（<= 6 张），直接注入字段说明、粒度、业务日期列（business_date）、有效记录过滤条件
   （valid_filter）、存量列禁跨期求和警示（stock）以及剖析最新日期范围，彻底省去建图阶段的 LLM 选表耗时。

安全与权限边界：
- 严格遵循 `schema_view.masked_set` / `masked_notes`，敏感遮罩列的注释、码值与剖析范围绝不进入系统上下文或前端摘要；
- 当 `restricted=True`（普通 user 角色）时，`ui_summary` 自动隐去物理表名、物理列名、SQL 表达式与底层编码值，仅展示业务层面的
  命中指标名、实体名与规则统计。
"""
from __future__ import annotations

import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import deps
from app.data import catalog, catalog_profile_jobs, schema_view
from app.data.entities import confirmed_bindings, list_entities, translate_entity_value
from app.data.names import name_key
from app.data.semantic_metrics import authorized_metric_source_ids, find_companion_lookup_tables, list_metrics
from app.db.models import DataSource
from app.memory.embeddings import _tokens

MAX_GROUNDED_METRICS = 5
MAX_GROUNDED_ENTITIES = 5
MAX_GROUNDED_TABLES = 6
MAX_TABLE_DETAIL_CHARS = 18_000

_ASCII_WORD_RE = re.compile(r"[a-z0-9_]+", re.IGNORECASE)


@dataclass
class GroundingBundle:
    elapsed_ms: int = 0
    candidate_sources: list[DataSource] = field(default_factory=list)
    narrowed_sources: list[DataSource] = field(default_factory=list)
    matched_metrics: list[dict[str, Any]] = field(default_factory=list)
    matched_entities: list[dict[str, Any]] = field(default_factory=list)
    matched_tables: list[dict[str, Any]] = field(default_factory=list)
    guardrails: list[dict[str, str]] = field(default_factory=list)
    system_context: str = ""
    ui_summary: dict[str, Any] = field(default_factory=dict)
    has_enabled_metrics: bool = False


def _term_in_text(term: str, text_lower: str, ascii_words: set[str]) -> bool:
    clean = str(term or "").strip().lower()
    if not clean:
        return False
    # 纯 ASCII 短词要求整词边界匹配，防止如 "id"、"pv"、"l1" 误中长英文串内部
    if clean.isascii() and re.fullmatch(r"[a-z0-9_]+", clean):
        if len(clean) < 2:
            return False
        return clean in ascii_words or (len(clean) >= 4 and clean in text_lower)
    if len(clean) < 2:
        return False
    return clean in text_lower


def _match_entities_fast(
    entities: Sequence[Any],
    *,
    question: str,
    allowed_source_names: set[str],
) -> list[dict[str, Any]]:
    text_lower = question.lower()
    ascii_words = {m.group(0).lower() for m in _ASCII_WORD_RE.finditer(question)}
    hits: list[tuple[int, dict[str, Any]]] = []

    for ent in entities:
        active_bindings = [
            b
            for b in confirmed_bindings(ent)
            if not allowed_source_names or name_key(str(b.get("source_name") or "")) in allowed_source_names
        ]
        if not active_bindings:
            continue

        # 1) 优先匹配具体成员值（如「西区」「L2产线」「静安寺店」）
        matched_members: list[dict[str, Any]] = []
        for m in ent.members or []:
            if not isinstance(m, Mapping):
                continue
            m_val = str(m.get("value") or "").strip()
            m_label = str(m.get("label") or m_val).strip()
            raw_source_codes = m.get("source_codes") if isinstance(m.get("source_codes"), Mapping) else {}
            flat_codes = [
                str(x).strip()
                for c in raw_source_codes.values()
                for x in (c if isinstance(c, (list, tuple, set)) else [c])
                if str(x or "").strip()
            ]
            candidate_terms = [
                m_label,
                m_val,
                *(str(a) for a in (m.get("aliases") or []) if a),
                *flat_codes,
            ]
            hit_term = next((t for t in candidate_terms if _term_in_text(t, text_lower, ascii_words)), None)
            if hit_term:
                resolved_by_source: dict[str, str] = {}
                target_codes_by_source: dict[str, list[str]] = {}
                for b in active_bindings:
                    sname = str(b.get("source_name") or "")
                    translated = translate_entity_value(ent, m_val, to_source=sname)
                    if translated is not None:
                        target_val = (
                            str(translated.get("target_value") or "").strip()
                            if isinstance(translated, Mapping)
                            else str(translated).strip()
                        )
                        if target_val:
                            resolved_by_source[sname] = target_val
                            target_codes_by_source[sname] = [target_val]
                matched_members.append(
                    {
                        "member_id": str(m.get("id") or ""),
                        "matched_term": hit_term,
                        "value": m_val,
                        "label": m_label,
                        "canonical_value": m_val,
                        "canonical_label": m_label,
                        "resolved_codes": resolved_by_source,
                        "target_codes": target_codes_by_source,
                    }
                )

        # 2) 再匹配实体维度名本身（如「各产线」「按门店」「分区域」）
        ent_terms = [str(ent.label or ""), str(ent.key or ""), *(str(a) for a in (ent.aliases or []) if a)]
        dim_hit_term = next((t for t in ent_terms if _term_in_text(t, text_lower, ascii_words)), None)

        if not matched_members and not dim_hit_term:
            continue

        score = (20 * len(matched_members)) + (8 if dim_hit_term else 0)
        hits.append(
            (
                score,
                {
                    "id": ent.id,
                    "key": ent.key,
                    "label": ent.label,
                    "matched_term": matched_members[0]["matched_term"] if matched_members else (dim_hit_term or ent.label),
                    "matched_members": matched_members[:4],
                    "bindings": [
                        {
                            "source_id": b.get("source_id"),
                            "source_name": str(b.get("source_name") or ""),
                            "table_name": str(b.get("table_name") or ""),
                            "column_name": str(b.get("column_name") or ""),
                        }
                        for b in active_bindings[:6]
                    ],
                },
            )
        )

    hits.sort(key=lambda pair: (-pair[0], pair[1]["key"]))
    return [item for _, item in hits[:MAX_GROUNDED_ENTITIES]]


def _match_metrics_fast(
    metrics: Sequence[Any],
    *,
    question: str,
    source_map: Mapping[str, DataSource],
) -> list[dict[str, Any]]:
    if not metrics:
        return []
    text_lower = question.lower()
    ascii_words = {m.group(0).lower() for m in _ASCII_WORD_RE.finditer(question)}
    q_tokens = set(_tokens(question))

    scored: list[tuple[float, dict[str, Any]]] = []
    for row in metrics:
        ds = source_map.get(row.source_id)
        if ds is None:
            continue
        score = 0.0
        matched_by: list[str] = []
        if _term_in_text(row.key, text_lower, ascii_words):
            score += 14.0
            matched_by.append(row.key)
        if _term_in_text(row.name, text_lower, ascii_words):
            score += 12.0
            matched_by.append(row.name)
        for alias in row.aliases or []:
            if _term_in_text(str(alias), text_lower, ascii_words):
                score += 10.0
                matched_by.append(str(alias))
                break

        doc_str = " ".join([row.name or "", " ".join(str(a) for a in (row.aliases or [])), row.description or "", row.table_name or ""])
        m_tokens = set(_tokens(doc_str))
        overlap = len(q_tokens & m_tokens)
        if overlap >= 2:
            score += min(6.0, overlap * 1.2)
        # 若数据源名本身也在问题里出现，给该源下的相关指标轻微加权
        if _term_in_text(ds.name, text_lower, ascii_words) and overlap >= 1:
            score += 2.5

        if score < 3.5:
            continue

        scored.append(
            (
                score,
                {
                    "id": row.id,
                    "key": row.key,
                    "name": row.name,
                    "unit": row.unit or "",
                    "description": row.description or "",
                    "source_id": ds.id,
                    "source_name": ds.name,
                    "calc_mode": getattr(row, "calc_mode", None) or "expression",
                    "table_name": row.table_name,
                    "sql_expression": row.sql_expression,
                    "date_column": row.date_column or "",
                    "filter_sql": row.filter_sql or "",
                    "dimensions": list(row.dimensions or []),
                    "matched_term": matched_by[0] if matched_by else row.name,
                    "score": round(score, 2),
                },
            )
        )

    scored.sort(key=lambda pair: (-pair[0], pair[1]["key"]))
    return [item for _, item in scored[:MAX_GROUNDED_METRICS]]


def _extract_table_guardrails(
    source_name: str,
    table_name: str,
    visible_notes: Mapping[str, Any],
    masked: set[str],
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    t_label = str((visible_notes.get("label") or {}).get("value") or "").strip() if isinstance(visible_notes.get("label"), Mapping) else ""
    display_tbl = f"{source_name}.{table_name}" + (f"（{t_label}）" if t_label else "")

    b_date = visible_notes.get("business_date")
    if isinstance(b_date, Mapping) and b_date.get("value"):
        b_val = b_date["value"]
        if isinstance(b_val, Mapping):
            col = str(b_val.get("column") or "").strip()
            rule_extra = str(b_val.get("rule") or "").strip()
        else:
            col = str(b_val or "").strip()
            rule_extra = ""
        if col and col.lower() not in masked:
            rule_str = f"业务时间列：{col}" + (f"（{rule_extra}）" if rule_extra else "")
            out.append(
                {
                    "kind": "business_date",
                    "table": display_tbl,
                    "column": col,
                    "rule": rule_str,
                    "note": rule_str,
                }
            )

    v_filt = visible_notes.get("valid_filter")
    if isinstance(v_filt, Mapping) and v_filt.get("value"):
        cond = str(v_filt["value"]).strip()
        rule_str = f"有效记录过滤：{cond}"
        out.append(
            {
                "kind": "valid_filter",
                "table": display_tbl,
                "filter": cond,
                "rule": rule_str,
                "note": rule_str,
            }
        )

    dedup_note = visible_notes.get("dedup")
    if isinstance(dedup_note, Mapping) and dedup_note.get("value"):
        dedup_val = str(dedup_note["value"]).strip()
        if dedup_val:
            rule_str = f"去重与分层防混算：{dedup_val}"
            out.append(
                {
                    "kind": "dedup",
                    "table": display_tbl,
                    "rule": rule_str,
                    "note": rule_str,
                }
            )

    cols = visible_notes.get("columns")
    if isinstance(cols, Mapping):
        stock_cols: list[str] = []
        for cname, citems in cols.items():
            if str(cname).lower() in masked or not isinstance(citems, Mapping):
                continue
            m_item = citems.get("measure")
            if isinstance(m_item, Mapping) and str(m_item.get("value") or "") == "stock":
                stock_cols.append(str(cname))
        if stock_cols:
            rule_str = f"存量列（禁跨日直接 SUM）：{', '.join(stock_cols[:5])}"
            out.append(
                {
                    "kind": "stock_columns",
                    "table": display_tbl,
                    "columns": stock_cols[:8],
                    "rule": rule_str,
                    "note": rule_str,
                }
            )
    return out


async def ground_question(
    session: AsyncSession,
    question: str,
    *,
    datasource_ids: Sequence[str] | None = None,
    history_text: str = "",
    restricted: bool = False,
) -> GroundingBundle:
    """在问数入口毫秒级同步完成「指标卡片 + 业务实体 + 数据目录」三重语义预命中。"""
    t0 = time.perf_counter()
    stmt = select(DataSource).where(DataSource.enabled.is_(True)).order_by(DataSource.name.asc())
    all_enabled = list((await session.execute(stmt)).scalars().all())
    if not all_enabled:
        return GroundingBundle(elapsed_ms=int((time.perf_counter() - t0) * 1000))

    if datasource_ids:
        wanted = {str(x).strip() for x in datasource_ids if str(x).strip()}
        wanted_lower = {x.lower() for x in wanted}
        candidate_sources = [
            ds for ds in all_enabled if ds.id in wanted or ds.name in wanted or ds.name.lower() in wanted_lower
        ]
    else:
        candidate_sources = list(all_enabled)

    if not candidate_sources:
        return GroundingBundle(elapsed_ms=int((time.perf_counter() - t0) * 1000))

    source_by_id = {ds.id: ds for ds in candidate_sources}
    allowed_source_keys = {name_key(ds.name) for ds in candidate_sources}

    # 合并当前问题与上一轮简短上下文用于消歧指代（权重以当前问题为主）
    combined_q = question.strip()
    if history_text and len(combined_q) < 24:
        combined_q = f"{combined_q}\n{history_text[-300:]}"

    q_lower = question.lower()
    q_ascii = {m.group(0).lower() for m in _ASCII_WORD_RE.finditer(question)}

    # 1. 识别用户是否显式点名了某个数据源（如 "park_demo 今天客流咋样"）
    explicit_sources = [
        ds
        for ds in candidate_sources
        if _term_in_text(ds.name, q_lower, q_ascii)
        or (ds.description and len(ds.description.strip()) >= 2 and ds.description.strip().lower() in q_lower)
    ]

    # 2. 标准业务实体匹配
    all_entities = await list_entities(session)
    matched_entities = _match_entities_fast(
        all_entities,
        question=combined_q,
        allowed_source_names=allowed_source_keys,
    )

    # 3. 启用态独立语义指标匹配（受限普通用户仅可见已获 AppGrant 授权的数据源下的指标）
    deps_restricted = deps.restricted()
    is_restricted = bool(restricted or deps_restricted)
    enabled_metrics = await list_metrics(session, status="enabled")
    if deps_restricted:
        permitted_source_ids = await authorized_metric_source_ids(session, deps.current_principal())
        enabled_metrics = [m for m in enabled_metrics if m.source_id in permitted_source_ids]
    if explicit_sources:
        exp_ids = {ds.id for ds in explicit_sources}
        scoped_metrics = [m for m in enabled_metrics if m.source_id in exp_ids] or enabled_metrics
    else:
        scoped_metrics = [m for m in enabled_metrics if m.source_id in source_by_id]
    matched_metrics = _match_metrics_fast(scoped_metrics, question=combined_q, source_map=source_by_id)

    # 4. 收敛本轮优先挂载与检索的数据源集合
    if datasource_ids:
        narrowed_sources = list(candidate_sources)
    elif explicit_sources:
        extra_ids = {m["source_id"] for m in matched_metrics}
        narrowed_sources = list(explicit_sources) + [
            ds for ds in candidate_sources if ds.id in extra_ids and ds not in explicit_sources
        ]
    else:
        narrowed_sources = list(candidate_sources)

    # 5. 数据目录 BM25 直选 + 指标底表 + 实体绑定表 -> 跨源全局打分选表，避免首源独占全部槽位
    matched_tables: list[dict[str, Any]] = []
    guardrails: list[dict[str, str]] = []
    table_blocks: list[str] = []
    total_detail_chars = 0

    ds_ctx_list: list[dict[str, Any]] = []
    for ds_idx, ds in enumerate(narrowed_sources):
        cache = ds.schema_cache if isinstance(ds.schema_cache, Mapping) else {}
        tables_map = (cache.get("tables") or {}) if isinstance(cache, Mapping) else {}
        if not isinstance(tables_map, Mapping) or not tables_map:
            continue

        entries = await catalog.read_catalog(session, ds.id)
        raw_notes = {t: e.notes for t, e in entries.items()}
        visible_by_table = {t: catalog.visible_notes(e.notes) for t, e in entries.items()}
        try:
            facts = await catalog_profile_jobs.last_facts(session, ds.id)
        except Exception:  # noqa: BLE001
            facts = {}
        masked = schema_view.masked_set(ds)
        try:
            rel_graph = schema_view.relation_graph(ds, raw_notes)
        except Exception:  # noqa: BLE001
            rel_graph = {}

        ds_ctx_list.append(
            {
                "idx": ds_idx,
                "ds": ds,
                "cache": cache,
                "tables_map": tables_map,
                "raw_notes": raw_notes,
                "visible_by_table": visible_by_table,
                "facts": facts,
                "masked": masked,
                "rel_graph": rel_graph,
                "picked": [],
                "reasons": {},
            }
        )

    total_picked = 0

    def _pick_table(ctx: dict[str, Any], tname: str, reason: str, *, cap: int = MAX_GROUNDED_TABLES) -> bool:
        nonlocal total_picked
        if total_picked >= cap or tname in ctx["picked"]:
            return False
        ctx["picked"].append(tname)
        ctx["reasons"][tname] = reason
        total_picked += 1
        return True

    # (a) 命中指标的底表跨源最优先入选（受限普通用户不把指标底表物理名暴露进预命中表块）
    if not deps_restricted:
        for ctx in ds_ctx_list:
            ds = ctx["ds"]
            for mm in matched_metrics:
                if mm["source_id"] == ds.id:
                    resolved = catalog.resolve_table_name(ctx["cache"], mm["table_name"])
                    if resolved:
                        _pick_table(ctx, resolved, f"指标「{mm['name']}」底表", cap=MAX_GROUNDED_TABLES + 2)

    # (b) 命中实体的绑定表跨源次优先入选
    for ctx in ds_ctx_list:
        ds = ctx["ds"]
        for me in matched_entities:
            for b in me["bindings"]:
                if name_key(b["source_name"]) == name_key(ds.name):
                    resolved = catalog.resolve_table_name(ctx["cache"], b["table_name"])
                    if resolved:
                        _pick_table(ctx, resolved, f"实体「{me['label']}」绑定表")

    # (c) 各候选源 BM25 检索统一收集并按得分全局排序选表（0 LLM 调用）
    if total_picked < MAX_GROUNDED_TABLES:
        global_bm25_hits: list[tuple[float, int, int, dict[str, Any], str]] = []
        for ctx in ds_ctx_list:
            hits = schema_view.search_tables(
                ctx["ds"],
                ctx["visible_by_table"],
                combined_q,
                limit=MAX_GROUNDED_TABLES,
            )
            for rank, h in enumerate(hits):
                if h.in_scope and h.name not in ctx["picked"]:
                    global_bm25_hits.append((float(h.score), -ctx["idx"], -rank, ctx, h.name))
        global_bm25_hits.sort(key=lambda item: (item[0], item[1], item[2]), reverse=True)
        for _score, _neg_ds_idx, _neg_rank, ctx, tname in global_bm25_hits:
            if total_picked >= MAX_GROUNDED_TABLES:
                break
            _pick_table(ctx, tname, "目录语义检索命中")

    # (d) 若用户明确指定了该小库（<= 5 张表）且尚未命中任何表，直接全给
    for ctx in ds_ctx_list:
        ds = ctx["ds"]
        if not ctx["picked"] and ds in explicit_sources and len(ctx["tables_map"]) <= 5:
            for tname in list(ctx["tables_map"].keys()):
                if not _pick_table(ctx, tname, "指定数据源核心表"):
                    break

    # 按数据源顺序补齐关联维表与一跳连接表，渲染表结构与护栏
    for ctx in ds_ctx_list:
        ds = ctx["ds"]
        picked_for_ds: list[str] = ctx["picked"]
        if not picked_for_ds:
            continue
        reasons_for_table: dict[str, str] = ctx["reasons"]
        tables_map = ctx["tables_map"]
        raw_notes = ctx["raw_notes"]
        visible_by_table = ctx["visible_by_table"]
        facts = ctx["facts"]
        masked = ctx["masked"]
        rel_graph = ctx["rel_graph"]

        companion_links = find_companion_lookup_tables(
            tables_map,
            picked_for_ds,
            visible_by_table=visible_by_table,
            rel_graph=rel_graph,
            masked_set=masked,
        )
        for comp in companion_links:
            if comp["col_count"] > 12:
                continue
            ct_name = comp["table_name"]
            if _pick_table(ctx, ct_name, "关联维表与字典", cap=MAX_GROUNDED_TABLES + 1):
                break

        # 补一跳连接中间表（若有）
        try:
            completed_tables, _ = schema_view.complete(picked_for_ds, rel_graph, room=1) if rel_graph else (picked_for_ds, [])
        except Exception:  # noqa: BLE001
            completed_tables = picked_for_ds

        ds_sections: list[str] = [f"### 数据源 `{ds.name}`（{ds.kind}）预命中表与目录"]
        for tname in completed_tables:
            if len(matched_tables) >= MAX_GROUNDED_TABLES + 1:
                break
            v_notes = visible_by_table.get(tname) or {}
            t_label = str((v_notes.get("label") or {}).get("value") or "").strip() if isinstance(v_notes.get("label"), Mapping) else ""
            t_grain = str((v_notes.get("grain") or {}).get("value") or "").strip() if isinstance(v_notes.get("grain"), Mapping) else ""
            raw_bdate = (v_notes.get("business_date") or {}).get("value") if isinstance(v_notes.get("business_date"), Mapping) else None
            t_bdate = (
                str(raw_bdate.get("column") or "").strip()
                if isinstance(raw_bdate, Mapping)
                else str(raw_bdate or "").strip()
            )
            t_vfilt = str((v_notes.get("valid_filter") or {}).get("value") or "").strip() if isinstance(v_notes.get("valid_filter"), Mapping) else ""
            matched_tables.append(
                {
                    "datasource_id": ds.id,
                    "datasource_name": ds.name,
                    "source_id": ds.id,
                    "source_name": ds.name,
                    "table_name": tname,
                    "label": t_label,
                    "grain": t_grain,
                    "business_date": t_bdate,
                    "valid_filter": t_vfilt,
                    "reason": reasons_for_table.get(tname, "关联中间表"),
                }
            )
            guardrails.extend(_extract_table_guardrails(ds.name, tname, v_notes, masked))

            if total_detail_chars < MAX_TABLE_DETAIL_CHARS:
                detail = schema_view.table_detail(ds, tname, catalog_notes=raw_notes, profile=facts)
                total_detail_chars += len(detail)
                ds_sections.append(detail)
            else:
                ds_sections.append(schema_view.table_brief(ds, tname))

        # 若该源有多张表入选，附上已确认/推断的 JOIN 条件与共享键关联提示
        if len(completed_tables) >= 2:
            qual = schema_view.qualifier(ds)
            join_lines: list[str] = []
            if rel_graph:
                edges = schema_view._edges_within(rel_graph, completed_tables)
                join_lines.extend(f"  - {schema_view._edge_line(e, qual)}" for e in edges[:10])
            completed_set = set(completed_tables)
            for comp in companion_links:
                if comp["linked_to"] in completed_set and comp["table_name"] in completed_set:
                    cond = " AND ".join(
                        f"{qual(comp['linked_to'])}.{sc} = {qual(comp['table_name'])}.{lc}"
                        for sc, lc in zip(comp["source_columns"], comp["lookup_columns"])
                    )
                    if cond and not any(cond in jl for jl in join_lines):
                        join_lines.append(f"  - {cond}（共享编码列关联）")
            if join_lines:
                ds_sections.append("表间标准连接条件：\n" + "\n".join(join_lines[:12]))

        table_blocks.append("\n".join(ds_sections))
        if len(matched_tables) >= MAX_GROUNDED_TABLES + 1:
            break

    elapsed_ms = max(1, int((time.perf_counter() - t0) * 1000))

    # 6. 组装注入给直查 Agent 的高密度语义上下文
    ctx_parts: list[str] = []
    if matched_metrics:
        m_lines = ["## 一、已命中的标准语义指标卡片（优先调 `metric_query` 工具直接取数）"]
        for mm in matched_metrics:
            dims_desc = "、".join(
                f"{d.get('key')}（{d.get('label')}" + (f"→实体：{d.get('entity_key')}" if d.get("entity_key") else "") + "）"
                for d in (mm.get("dimensions") or [])
                if isinstance(d, Mapping)
            ) or "无"
            if is_restricted:
                m_lines.append(
                    f"- 指标 `{mm['key']}`｜显示名称：{mm['name']}"
                    + (f"（单位：{mm['unit']}）" if mm["unit"] else "")
                    + f"｜可分组/过滤维度：{dims_desc}"
                )
            else:
                mode_tag = "｜模式：多表参数化 SQL 模板（必须调用 `metric_query` 执行，勿手写跨表拼凑）" if mm.get("calc_mode") == "sql_template" else ""
                m_lines.append(
                    f"- 指标 `{mm['key']}`｜名称：{mm['name']}"
                    + (f"（单位：{mm['unit']}）" if mm["unit"] else "")
                    + f"｜数据源：`{mm['source_name']}`｜底表：`{mm['table_name']}`{mode_tag}"
                    + (f"｜时间列：`{mm['date_column']}`" if mm["date_column"] else "")
                    + f"\n  口径表达式：`{mm['sql_expression']}`"
                    + (f"｜有效过滤：`{mm['filter_sql']}`" if mm["filter_sql"] else "")
                    + f"｜可分组/过滤维度：{dims_desc}"
                )
            if mm["description"]:
                m_lines.append(f"  口径说明：{mm['description']}")
        ctx_parts.append("\n".join(m_lines))

    if matched_entities:
        e_lines = ["## 二、已命中的标准业务实体与物理编码映射（写 WHERE 条件或调工具时直接使用，勿盲猜编码）"]
        for me in matched_entities:
            bind_desc = "；".join(
                f"`{b['source_name']}.{b['table_name']}.{b['column_name']}`" for b in me["bindings"]
            )
            e_lines.append(f"- 标准实体 `{me['key']}`（{me['label']}）｜物理列绑定：{bind_desc}")
            for mem in me["matched_members"]:
                codes_str = "，".join(f"{s}=`{c}`" for s, c in mem["resolved_codes"].items()) or f"标准值=`{mem['value']}`"
                e_lines.append(f"  · 提问词「{mem['matched_term']}」→ 标准成员「{mem['label']}」→ 各库底层编码：{codes_str}")
        ctx_parts.append("\n".join(e_lines))

    if guardrails:
        g_lines = ["## 三、数据目录强制防错护栏（写 SQL 时必须遵守）"]
        for g in guardrails[:12]:
            g_lines.append(f"- `{g['table']}`：{g['rule']}")
        ctx_parts.append("\n".join(g_lines))

    if table_blocks:
        ctx_parts.append("## 四、预命中物理表结构、数据目录与剖析范围（可直接据此写 SQL，无需再调 db_schema 查看以下表）\n" + "\n\n".join(table_blocks))

    # 补充候选数据源概览，便于预命中未覆盖时 Agent 按需使用 db_schema__
    if len(narrowed_sources) > 0:
        overview = [
            f"- `{ds.name}`（{ds.kind}）：共 {len(((ds.schema_cache or {}).get('tables') or {}))} 张已探查表"
            + (f" — {ds.description}" if ds.description else "")
            for ds in narrowed_sources
        ]
        ctx_parts.append("## 五、本轮可用数据源清单\n" + "\n".join(overview))

    # 7. 组装前端可观测摘要（restricted 或 deps.restricted() 时隐去物理表名、列名与底层编码）
    if is_restricted:
        ui_metrics = [
            {
                "id": m["id"],
                "key": m["key"],
                "name": m["name"],
                "unit": m["unit"],
                "datasource_id": m["source_id"],
                "datasource_name": m["source_name"],
                "source_name": m["source_name"],
            }
            for m in matched_metrics
        ]
        ui_entities = [
            {
                "entity_id": e["id"],
                "entity_key": e["key"],
                "key": e["key"],
                "label": e["label"],
                "matched_member": (
                    {
                        "canonical_value": e["matched_members"][0]["value"],
                        "canonical_label": e["matched_members"][0]["label"],
                        "matched_term": e["matched_members"][0]["matched_term"],
                    }
                    if e["matched_members"]
                    else None
                ),
                "matched_members": [{"label": mem["label"]} for mem in e["matched_members"]],
            }
            for e in matched_entities
        ]
        ui_tables = [
            {
                "datasource_id": t["datasource_id"],
                "datasource_name": t["datasource_name"],
                "source_name": t["source_name"],
                "table_name": t["label"] or "业务数据表",
                "label": t["label"] or "业务数据表",
                "reason": t["reason"],
            }
            for t in matched_tables
        ]
        ui_guardrails = [
            {
                "kind": g["kind"],
                "rule": "已应用数据目录有效口径与汇总约束",
                "note": "已应用数据目录有效口径与汇总约束",
            }
            for g in guardrails[:4]
        ]
    else:
        ui_metrics = [
            {
                "id": m["id"],
                "key": m["key"],
                "name": m["name"],
                "unit": m["unit"],
                "calc_mode": m.get("calc_mode", "expression"),
                "datasource_id": m["source_id"],
                "datasource_name": m["source_name"],
                "source_name": m["source_name"],
                "table_name": m["table_name"],
                "sql_expression": m["sql_expression"],
                "date_column": m["date_column"],
            }
            for m in matched_metrics
        ]
        ui_entities = [
            {
                "entity_id": e["id"],
                "entity_key": e["key"],
                "key": e["key"],
                "label": e["label"],
                "matched_member": (
                    {
                        "member_id": e["matched_members"][0].get("member_id", ""),
                        "canonical_value": e["matched_members"][0]["value"],
                        "canonical_label": e["matched_members"][0]["label"],
                        "matched_term": e["matched_members"][0]["matched_term"],
                        "target_codes": e["matched_members"][0]["target_codes"],
                    }
                    if e["matched_members"]
                    else None
                ),
                "matched_members": e["matched_members"],
                "bound_columns": e["bindings"],
                "bindings": e["bindings"],
            }
            for e in matched_entities
        ]
        ui_tables = matched_tables
        ui_guardrails = guardrails[:8]

    ui_summary = {
        "elapsed_ms": elapsed_ms,
        "metrics": ui_metrics,
        "entities": ui_entities,
        "tables": ui_tables,
        "guardrails": ui_guardrails,
        "narrowed_sources": [{"id": ds.id, "name": ds.name} for ds in narrowed_sources],
    }

    return GroundingBundle(
        elapsed_ms=elapsed_ms,
        candidate_sources=candidate_sources,
        narrowed_sources=narrowed_sources,
        matched_metrics=matched_metrics,
        matched_entities=matched_entities,
        matched_tables=matched_tables,
        guardrails=guardrails,
        system_context="\n\n".join(ctx_parts),
        ui_summary=ui_summary,
        has_enabled_metrics=bool(scoped_metrics),
    )
