"""从文档起草口径的应用服务；HTTP 层只负责权限、参数和任务启动。"""
from __future__ import annotations
import asyncio
import copy
import logging
import re
from typing import Any
from uuid import uuid4
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.db.base import SessionLocal
from app.db.models import CaliberDraft, Chunk, DataSource, Document, Workflow

logger = logging.getLogger(__name__)
MAX_ROUNDS = 4
MAX_METRICS = 60
DEFINITIONS_PER_BATCH = 6
DEFINITION_ATTEMPTS = 2
_QUESTION_KEY = 30
_CLAUSE = "，,。；;：:！!？?"
_PUNCT = set(" \t\r\n，。；：、！？,.!?;:'\"（）()[]【】《》<>-_/\\|`~")


def _question_count(payload: dict[str, Any]) -> int:
    """未决问题数：跨指标的 open_questions + 各指标自己的。status 就按它算，PATCH 之后重算也用它。"""
    def count(items: Any) -> int:
        return len([q for q in items if isinstance(q, str) and q.strip()]) if isinstance(items, list) else 0

    total = count(payload.get("open_questions"))
    metrics = payload.get("metrics")
    for metric in metrics if isinstance(metrics, list) else []:
        if isinstance(metric, dict):
            total += count(metric.get("open_questions"))
    return total


def _status_for(payload: dict[str, Any]) -> str:
    """还有问题 → questions_open；没有问题且有图 → ready；没有图的老草稿 → path_pending。

    questions_open 现在也有图和数，只是问题还没清完，不阻断试跑；path_pending 只留给这次改版之前起草、还没有取数路径的草稿。
    """
    if _question_count(payload):
        return "questions_open"
    if payload.get("workflow_id") and payload.get("sql"):
        return "ready"
    return "path_pending"


def _source_tables(source: DataSource) -> dict[str, Any]:
    cache = source.schema_cache if isinstance(source.schema_cache, dict) else {}
    tables = cache.get("tables")
    return tables if isinstance(tables, dict) else {}


def require_authoring_source(source: DataSource | None) -> None:
    """Shared eligibility gate for entrypoints, retry and the actual worker."""
    if source is None:
        raise ValueError("数据源不存在，可能已被删除")
    if not source.enabled:
        raise ValueError("数据源已停用，请启用后再生成口径")
    if not _source_tables(source):
        raise ValueError("这个数据源还没有表结构：先探查表结构，再起草口径")


async def _resume_finalization(draft_id: str, *, run_trial: Any, save_trial: Any, trial_binding: Any) -> bool:
    """Resume durable graph acceptance without another model call or graph creation."""
    from app.core.artifact_store import graph_hash
    async with SessionLocal() as session:
        draft = await session.get(CaliberDraft, draft_id)
        if draft is None or draft.status != "drafting" or not draft.workflow_id:
            return False
        payload = dict(draft.payload or {})
        source = await session.get(DataSource, draft.datasource_id) if draft.datasource_id else None
        try:
            require_authoring_source(source)
            workflow = await session.get(Workflow, draft.workflow_id)
            if (workflow is None or not payload.get("finalization_pending")
                    or graph_hash(workflow.graph) != payload.get("accepted_graph_hash")):
                raise ValueError("已生成的工作流发生变化，已保留当前编辑，请人工核对后试运行")
        except ValueError as exc:
            draft.status, draft.error = "failed", str(exc)
            await session.commit()
            return True
        graph = workflow.graph
        await session.commit()
        baseline = await _capture_field_baseline(draft=draft, graph=graph, workflow_id=workflow.id,
                                                 source_snapshot=payload.get("source_snapshot"))
        draft.payload = {**payload, **baseline, "finalization_pending": False}
        draft.status, draft.error = _status_for(draft.payload), None
        await session.commit()
        fields = payload.get("entry_fields") or []
        values = {f["name"]: f.get("default") for f in fields if f.get("default") not in (None, "")}
    try:
        trial = await run_trial(draft_id=draft_id, workflow_id=workflow.id, graph=graph, values=values,
                                started_by=draft.created_by, entry_fields=fields)
    except Exception as exc:
        trial = {**trial_binding(1, graph, values), "trial_id": uuid4().hex, "run_id": "",
                 "status": "failed", "error": f"试运行出错：{exc}"}
    await save_trial(draft_id, trial)
    return True


_GENERIC_COLUMN_TOKENS = frozenset({
    "select", "from", "where", "group", "order", "count", "value", "values", "total", "status",
    "state", "start", "end", "date", "time", "code", "name", "type", "line", "plant", "daily",
    "weekly", "monthly", "period", "report", "table", "column", "remark", "notes", "load_time",
    "create_time", "update_time", "insert_time",
})


def _focus_tables(tables: list[str], text: str, limit: int = 8, *,
                  source: DataSource | None = None) -> list[str]:
    """文档里点到的表优先展开：按它在原文里第一次出现的位置排，没点到的按目录顺序补几张。

    目录动辄十万字符，全塞给模型会把文档和别的表挤掉。先给全量列名（见 _columns_digest），再把文档点到的
    那几张表展开注释和目录——写 SQL 需要的列名一个不少，细节只给用得上的。
    数仓视图常带层级前缀（如 dw_v_scrap_record 对应文档写的 SCRAP_RECORD），以及文档直接点名特征字段（如 ext_val_36），
    依次在精确表名匹配之后补充，且正式表优先于 his 历史归档表。
    """
    lowered = (text or "").lower()
    exact_hit: list[tuple[int, str]] = []
    suffix_hit: list[tuple[int, int, str]] = []
    matched: set[str] = set()
    for table in tables:
        name = str(table).lower()
        short = name.split(".")[-1]
        at = lowered.find(name)
        if at < 0:
            at = lowered.find(short)
        if at >= 0:
            exact_hit.append((at, table))
            matched.add(table)
            continue
        parts = [p for p in short.split("_") if p]
        for idx in range(1, len(parts)):
            suffix = "_".join(parts[idx:])
            if len(suffix) < 5 or ("_" not in suffix and len(suffix) < 7):
                continue
            m = re.search(rf"(?<![a-z0-9_]){re.escape(suffix)}(?![a-z0-9_])", lowered)
            if m:
                is_his = 1 if "_his_" in f"_{short}_" else 0
                suffix_hit.append((is_his, m.start(), table))
                matched.add(table)
                break
    exact_hit.sort(key=lambda pair: pair[0])
    suffix_hit.sort(key=lambda item: (item[0], item[1]))
    picked = [table for _, table in exact_hit][:limit]
    for _, _, table in suffix_hit:
        if len(picked) >= limit:
            break
        if table not in picked:
            picked.append(table)
    if source is not None and len(picked) < limit and lowered:
        from app.data import introspect

        tokens = {tok for tok in re.findall(r"[a-z][a-z0-9_]{3,}", lowered) if tok not in _GENERIC_COLUMN_TOKENS}
        if tokens:
            col_hits: list[tuple[int, int, str]] = []
            for table in tables:
                if table in picked:
                    continue
                short = str(table).lower().split(".")[-1]
                meta = introspect.find_table(source, table)
                cols = {str(c.get("name") or "").lower() for c in (meta or {}).get("columns") or [] if c.get("name")}
                overlap = len((cols - _GENERIC_COLUMN_TOKENS) & tokens)
                if overlap > 0:
                    is_his = 1 if "_his_" in f"_{short}_" else 0
                    col_hits.append((-overlap, is_his, table))
            col_hits.sort()
            for _, _, table in col_hits:
                if len(picked) >= limit:
                    break
                picked.append(table)
    for table in tables:
        if len(picked) >= min(limit, 5):
            break
        if table not in picked:
            picked.append(table)
    return picked


def _columns_digest(source: DataSource, tables: list[str], *, focused: list[str] | None = None) -> str:
    """每张表的列名清单（只给名字）：要写 SQL，先得知道有哪些列。注释和目录留给下面展开的那几张。"""
    from app.data import introspect

    focus_set = set(focused or ())
    ordered = [*(t for t in (focused or ()) if t in set(tables)), *(t for t in tables if t not in focus_set)]
    compact_unfocused = len(tables) > 20
    lines = ["各表的列（只列名字；下面展开的表另有类型、注释和数据目录）："]
    for table in ordered:
        meta = introspect.find_table(source, table)
        columns = [str(c.get("name")) for c in (meta or {}).get("columns") or [] if c.get("name")]
        if not columns:
            continue
        if compact_unfocused and table not in focus_set and len(columns) > 45:
            shown = ", ".join(columns[:35])
            lines.append(f"{table}: {shown} …（共 {len(columns)} 列）")
        else:
            lines.append(f"{table}: {', '.join(columns)}")
    return "\n".join(lines)


async def _schema_material(session: AsyncSession, source: DataSource, focus_text: str = ""
                           ) -> tuple[str, set[str]]:
    """这个数据源给模型看的表结构 + 数据目录文本，以及 SQL 允许出现的表名集合。

    文本按「概览 → 全量表索引 → 全量列名（重点表优先） → 文档点到的表展开」的顺序排：预算不够时先丢的是最后那一段，
    列名不会丢。表名一律照目录原文，模型照着抄。
    """
    from app.data import catalog, schema_view

    cache = source.schema_cache if isinstance(source.schema_cache, dict) else {}
    tables = _source_tables(source)
    if not tables:
        return "", set()
    entries = await catalog.read_catalog(session, source.id)
    notes = {table: entry.notes for table, entry in entries.items()}
    focused = _focus_tables(tables, focus_text, source=source)
    lines = [schema_view.overview_line(source, catalog_tables=sum(
        1 for entry in entries.values() if catalog.visible_notes(entry.notes)))]
    lines.append(schema_view.table_index(source, notes))
    lines.append(_columns_digest(source, tables, focused=focused))
    for table in focused:
        lines.append(schema_view.table_detail(source, table, catalog_notes=notes))
    schema = cache.get("schema")
    allowed: set[str] = set()
    for table in tables:
        allowed.add(str(table))
        if isinstance(schema, str) and schema:
            allowed.add(f"{schema}.{table}")
    return "\n\n".join(line for line in lines if line), allowed


async def _published_metrics(session: AsyncSession) -> list[dict[str, Any]]:
    """已发布口径里的每一项指标，给 docdraft.alignment 用：{workflow_id, version, node_id, name, synonyms, unit,
    definition_text}。直接复用 descriptor.list_calibers 的认法（只看每个工作流推荐发布版里的源头口径卡，K07）。"""
    from app.ledger.descriptor import list_calibers

    out: list[dict[str, Any]] = []
    for described in await list_calibers(session):
        for metric in described.get("metrics") or []:
            if not isinstance(metric, dict):
                continue
            definition = metric.get("definition")
            text = definition.get("text") if isinstance(definition, dict) else None
            name = metric.get("name") or metric.get("id")
            if not name:
                continue
            out.append({"workflow_id": described["workflow_id"], "version": described["version"],
                        "node_id": described["card"]["node_id"], "name": str(name),
                        "synonyms": metric.get("synonyms") if isinstance(metric.get("synonyms"), list) else [],
                        "unit": metric.get("unit") or "", "definition_text": text or ""})
    return out


def _merged_fields(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """各组声明的入口字段合成一份，给界面看：它和建图用的是同一套合并（draftplan.merge_entry_fields）。

    早先这里是「先出现的为准」的另一份实现，建图那边还会因为同名冲突抛错——两份逻辑说不到一起。现在只有一处。
    """
    from app.ledger import draftplan

    fields, _ = draftplan.merge_entry_fields(blocks)
    return fields


def _entry_notes(blocks: list[dict[str, Any]]) -> list[str]:
    """入口字段在各组里声明不一致的地方（必填取并集、类型取更宽），摆给起草的人看。"""
    from app.ledger import draftplan

    return draftplan.merge_entry_fields(blocks)[1]


def _question_key(text: str) -> str:
    """一句话的「问的是什么」：取第一个分句（问的是同一件事，后面常跟着各轮的补充说明）。"""
    head = text
    for mark in _CLAUSE:
        at = head.find(mark)
        if at > 0:
            head = head[:at]
    return "".join(ch for ch in head if ch not in _PUNCT)[:_QUESTION_KEY]


def _merge_questions(kept: list[str], incoming: list[str]) -> list[str]:
    """跨轮合并未决问题：同一件事每轮都会重新问一遍（68 条里有一半是重复），按归一化后的前 24 个字去重，
    保留先出现的那一句。措辞不同但意思一样的仍会留下——机器分不出来，交给人决定要不要回。"""
    seen = {_question_key(q) for q in kept}
    out = list(kept)
    for item in incoming:
        text = item.strip() if isinstance(item, str) else ""
        if not text:
            continue
        key = _question_key(text)
        if key in seen:
            continue
        seen.add(key)
        out.append(text)
    return out


def _can_repair(group: Any) -> bool:
    """这一组有指标可救时才再调一次模型：没有指标就没有可保留的东西，重写等于重新起草。"""
    metrics = group.get("metrics") if isinstance(group, dict) else None
    return isinstance(metrics, list) and any(isinstance(metric, dict) for metric in metrics)


def _fresh_metrics(metrics: list[dict[str, Any]], covered: list[str]) -> list[dict[str, Any]]:
    """这一轮里还没起草过的指标，按名字认；同一轮重复交同名指标的只留第一条。

    提示词里已经写了「这些起草过、别重复」，但模型偶尔照抄一遍上一轮的指标。照单全收会出现同名两条指标，口径卡上
    读的是哪条也说不清，所以在这里挡掉。
    """
    seen = set(covered)
    fresh: list[dict[str, Any]] = []
    for metric in metrics:
        name = str(metric.get("name") or "").strip()
        if name:
            if name in seen:
                continue
            seen.add(name)
        fresh.append(metric)
    return fresh


def _draft_payload(*, entries: list[dict[str, Any]], blocks: list[dict[str, Any]], problems: list[dict[str, Any]],
                   questions: list[str], uncovered: list[str], progress: dict[str, int],
                   doc_truncated: bool, catalog_truncated: bool, model_id: str,
                   salvaged: bool, source_snapshot: dict[str, Any] | None = None,
                   prompts: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """草稿 payload 的形状：整份指标、各组 SQL、进度、来源快照引用、提示记录。
    起草中途也按它写回，页面靠 progress 显示第几轮、覆盖了几条。

    blocks 是组数（不是内容）；sql 把各组的取数拼在一起（组间空一行），人看的是「这次一共要跑哪几条」。groups 逐组给出
    sql、入口字段和这一组的指标 id（与 metrics 里出现的顺序一致）：页面按组画「这条 SQL 出哪几个指标」，不必再从拼接
    出来的 sql 里猜边界。truncated 仍是 doc_truncated 与 catalog_truncated 的「或」，老前端只认这一个键。

    source_snapshot 是这次抽取读到的来源材料在工件库里的引用（原文在工件里，这里只记 id）；prompts 记录每次模型提示
    实际看到了哪几段。两样都不放进 checkpoint：checkpoint 只作任务恢复，来源基线与它无关（见 extract）。

    所有路径（每轮落库、失败、出图）都经过这里，groups 只从同一份 blocks 生成，不另存一份状态。
    """
    return {
        "documents": entries,
        "metrics": [m for block in blocks for m in block["metrics"]],
        "entry_fields": _merged_fields(blocks),
        "entry_notes": _entry_notes(blocks),
        "sql": "\n\n".join(str(block["sql"]) for block in blocks),
        "groups": [{"sql": str(block.get("sql") or ""),
                    "entry_fields": [dict(field) for field in block.get("entry_fields") or []
                                     if isinstance(field, dict)],
                    "metric_ids": [str(metric["id"]) for metric in block.get("metrics") or []
                                   if isinstance(metric, dict) and metric.get("id")]}
                   for block in blocks],
        "version": 1,
        "blocks": len(blocks),
        "progress": progress,
        "trial": {"run_id": "", "status": "running", "error": ""},
        "dropped": problems,
        "truncated": doc_truncated or catalog_truncated,
        "doc_truncated": doc_truncated,
        "catalog_truncated": catalog_truncated,
        "model_id": model_id,
        "source_snapshot": source_snapshot,
        "prompts": list(prompts or []),
        "open_questions": questions,
        "uncovered": uncovered,
        "salvaged": salvaged,
    }


async def _capture_source(*, documents: list[dict[str, Any]], chunks: dict[str, dict[str, Any]],
                          datasource: dict[str, Any], catalog: str, guidance: str, fixed_filters: str,
                          draft_id: str) -> dict[str, Any] | None:
    """把这次抽取读到的来源材料存成一份不可变工件，返回草稿里要记的引用；落库失败返回 None。

    存的是**可还原的原文**（逐份、逐段）加上目录文本和补充要求，不是一串哈希——只有哈希还原不了 B。工件按内容寻址
    （app/core/artifact_store），同一份材料只存一份，读的时候还会复验哈希。落库失败不让整份草稿失败：草稿照出，但
    source_snapshot 为空（source_baseline 会照实说 baseline_unknown），不假装有基线。
    """
    from app.core.artifact_store import put_json
    from app.ledger import docdraft

    try:
        snapshot = docdraft.source_snapshot(documents=documents, chunks=chunks, datasource=datasource,
                                            catalog=catalog, guidance=guidance, fixed_filters=fixed_filters)
        artifact_id = await put_json(snapshot, kind=docdraft.SNAPSHOT_KIND, node_id=draft_id,
                                     meta={"draft_id": draft_id,
                                           "source_snapshot_version": docdraft.SNAPSHOT_VERSION})
    except Exception:  # noqa: BLE001 - 快照落库失败不该把整份草稿判死，照实记「没有基线」
        logger.exception("来源快照落库失败（草稿 %s）", draft_id)
        return None
    return docdraft.snapshot_ref(artifact_id, snapshot)


def _prompt_trace(*, round: int, purpose: str, fitted: tuple[dict[str, dict], str, bool, bool],
                  model_id: str, salvaged: bool = False, groups: int | None = None) -> dict[str, Any]:
    """记一次模型提示：这一轮实际让它看了哪几段原文、用的是哪一版提示词/解析、截断没有。

    visible_chunk_ids 是**真正进了提示**的那几段。段落被 fit_material 整段丢掉时它不在这里，被截掉尾巴的那一段仍在这里
    （引用核对按截断后的内容算，尾巴上的话对不上）。这份记录是「模型当时看到了什么」的凭据，和来源快照各管一段：
    快照保原文，这里保可见范围。
    """
    kept, _tables, doc_truncated, catalog_truncated = fitted
    from app.ledger import docdraft, draftplan

    return {
        "round": round,
        "purpose": purpose,
        "model_id": model_id,
        "prompt_version": draftplan.PROMPT_VERSION,
        "parse_version": docdraft.PARSE_VERSION,
        "visible_chunk_ids": sorted(str(c) for c in kept),
        "visible_chars": {str(cid): len(str((chunk or {}).get("content") or ""))
                          for cid, chunk in kept.items()},
        "doc_truncated": bool(doc_truncated),
        "catalog_truncated": bool(catalog_truncated),
        "salvaged": bool(salvaged),
        **({"groups": groups} if groups is not None else {}),
    }


async def _capture_field_baseline(*, draft: Any, graph: dict[str, Any], workflow_id: str,
                                  source_snapshot: dict[str, Any] | None,
                                  accepted_revision: int = 1) -> dict[str, Any]:
    """首次接受导入：把**被接受进图里的字段值**按 metric_id + field_path 存成 B，返回 payload 里要记的那几项。

    「接受」这一刻就是这份导入成功规范化、落进初始草稿图的那一刻（见 extract）：取的是图上那张权威源头口径卡的指标
    （不是草稿 payload 里的快照，也不是草稿后来改成什么样）。存不下时返回空 dict：调用方不写 field_baseline，
    读接口照实说 baseline_unknown——不假装有基线，也不拿当前值倒填。
    """
    from app.ledger import fieldbaseline

    card_id, metrics = fieldbaseline.accepted_card(graph)
    if not metrics:
        return {}
    ref = await fieldbaseline.put_metrics(metrics=metrics, card_id=card_id, workflow_id=workflow_id,
                                          accepted_revision=accepted_revision, source_snapshot=source_snapshot,
                                          draft_id=draft.id, accepted_by=draft.created_by)
    if ref is None:
        return {}
    history = fieldbaseline.history(draft.payload if isinstance(draft.payload, dict) else {})
    return {fieldbaseline.REF_KEY: ref, fieldbaseline.HISTORY_KEY: [*history, ref]}


def _dedupe_problems(problems: list[dict[str, Any]]) -> list[dict[str, Any]]:
    has_specific = any(isinstance(p, dict) and p.get("name") != "metrics" for p in problems)
    seen: set[tuple[str, str]] = set()
    out: list[dict[str, Any]] = []
    for item in problems:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "")
        reason = str(item.get("reason") or "")
        if has_specific and name == "metrics" and reason == "没有一条指标通过校验":
            continue
        key = (name, reason)
        if key not in seen:
            seen.add(key)
            out.append({"name": name, "reason": reason})
    return out


def _scoped_definition_chunks(chunks: dict[str, dict[str, Any]], semantic_inventory: dict[str, Any],
                              active_definitions: list[str]) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    from app.ledger import docdraft

    ordered_ids = docdraft._order(chunks)
    active_set = set(active_definitions)
    active_items = [item for item in semantic_inventory.get("items") or [] if item.get("id") in active_set]
    hints: dict[str, str] = {}
    matched_chunks: set[str] = set()
    for item in active_items:
        quote = str((item.get("source") or {}).get("quote") or "")
        doc_id = str((item.get("source") or {}).get("document_id") or "")
        if len(docdraft._collapse(quote)) < docdraft.MIN_QUOTE_CHARS:
            continue
        for cid in ordered_ids:
            chunk = chunks[cid]
            if doc_id and str(chunk.get("document_id") or "") != doc_id:
                continue
            if docdraft.verify_quote(str(chunk.get("content") or ""), quote) is not None:
                hints[str(item["id"])] = cid
                matched_chunks.add(cid)
                break
    if not active_items or len(hints) < len(active_items) or len(chunks) <= 2:
        return chunks, hints
    return {cid: chunks[cid] for cid in ordered_ids if cid in matched_chunks}, hints


def _attach_verified_definition_citations(raw_group: Any, *, visible: dict[str, dict[str, Any]],
                                          semantic_inventory: dict[str, Any], active_definitions: list[str],
                                          chunk_hints: dict[str, str]) -> Any:
    from app.ledger import docdraft

    if not isinstance(raw_group, dict):
        return raw_group
    raw_metrics = raw_group.get("metrics")
    if not isinstance(raw_metrics, list):
        return raw_group
    active_set = set(active_definitions)
    by_id = {str(i["id"]): i for i in semantic_inventory.get("items") or [] if i.get("id") in active_set}
    by_name = {str(i["name"]): i for i in by_id.values() if i.get("name")}
    patched: list[Any] = []
    for item in raw_metrics:
        if not isinstance(item, dict):
            patched.append(item)
            continue
        existing = [c for c in docdraft._citation_list(item) if docdraft._check_citation(c, visible)]
        if existing:
            patched.append(item)
            continue
        def_id = str(item.get("source_definition_id") or "")
        def_item = by_id.get(def_id) or by_name.get(str(item.get("name") or "").strip())
        cid = chunk_hints.get(str(def_item["id"])) if def_item else None
        quote = str((def_item.get("source") or {}).get("quote") or "") if def_item else ""
        fallback = {"chunk": cid, "quote": quote} if cid and cid in visible and quote else None
        if fallback and docdraft._check_citation(fallback, visible):
            copied = dict(item)
            definition = dict(copied.get("definition")) if isinstance(copied.get("definition"), dict) else {}
            definition["citations"] = [fallback]
            copied["definition"] = definition
            copied.pop("citations", None)
            copied.setdefault("source_definition_id", str(def_item["id"]))
            patched.append(copied)
        else:
            patched.append(item)
    return {**raw_group, "metrics": patched}


async def extract(draft_id: str, *, run_trial: Any, save_trial: Any, trial_binding: Any) -> None:
    """后台起草：读文档的段落和数据目录，分组反复调模型直到覆盖完，收成一张多取数的草稿图、建工作流、再跑一次试跑。

    用新的会话：请求那一份在响应发出后就关了，后台接着用它写库是错的。没有数据源或没有表结构时起草必须失败——SQL 只能
    对照真实目录写，没有目录就不让模型猜。个别指标不合格只丢那一条、写进 dropped / open_questions；SQL 本身不合格才整份
    失败（图跑不了，留着也没有意义）。

    每轮可交多组能共用 SQL 的指标，按 uncovered 续写，直到覆盖完、没有新指标或到轮数/条数上限。每轮先提交进度和
    已校验分组的检查点，再等待模型。材料指纹相同的重试可接续检查点；并行修复各用独立数据库会话。

    **来源侧前置（K05）。** 读到材料的那一刻就把原文按内容寻址存进工件库（_capture_source），草稿 payload 只记
    source_snapshot 引用；每次模型提示实际看到了哪几段记进 prompts。引用核对只认实际进提示的段落（fit_material 的
    结果），被截断丢掉的段落引用它核不上。checkpoint 仍旧只作任务恢复用——它记的是「哪些组已经起草好了」，不是
    字段基线 B；来源内容的还原靠工件，两者不混。

    **字段基线 B（K05）。** 这份导入成功规范化、收成图的那一刻（`_capture_field_baseline`），被接受进图里的字段值
    按 metric_id + field_path 存成一件不可变工件，payload 记引用（field_baseline）并追加一条历史。之后人工按字段改
    只改 L，不动这份 B；重导入拿它当基线（K11）。
    """
    from app.core.artifact_store import graph_hash
    from app.db.models import WorkflowVersion
    from app.ledger import docdraft, draftplan

    if await _resume_finalization(draft_id, run_trial=run_trial, save_trial=save_trial, trial_binding=trial_binding):
        return

    async with SessionLocal() as session:
        draft = await session.get(CaliberDraft, draft_id)
        if draft is None or draft.status != "drafting":
            return
        origin = draft.origin if isinstance(draft.origin, dict) else {}
        wanted = [str(d) for d in origin.get("documents") or []]
        docs: list[Document] = []
        for doc_id in wanted:
            doc = await session.get(Document, doc_id)
            if doc is not None:
                docs.append(doc)
        chunks: dict[str, dict[str, Any]] = {}
        entries: list[dict[str, Any]] = []
        definition_state = dict(draft.payload or {})
        has_inventory = isinstance(definition_state.get("semantic_inventory"), dict)
        for doc in docs:
            rows = (await session.execute(select(Chunk).where(Chunk.document_id == doc.id)
                                          .order_by(Chunk.ordinal, Chunk.id))).scalars()
            chunk_ids: list[str] = []
            for chunk in rows:
                chunks[chunk.id] = {"ordinal": chunk.ordinal, "content": chunk.content or "",
                                    "document_id": doc.id}
                chunk_ids.append(chunk.id)
            if not chunk_ids and has_inventory and (doc.content or "").strip():
                fallback_cid = f"doc-{doc.id}"
                chunks[fallback_cid] = {"ordinal": 1, "content": doc.content or "", "document_id": doc.id}
                chunk_ids.append(fallback_cid)
            entries.append({"document_id": doc.id, "title": doc.title or doc.source or doc.id,
                            "chunk_ids": chunk_ids})

        source = await session.get(DataSource, draft.datasource_id) if draft.datasource_id else None
        try:
            require_authoring_source(source)
        except ValueError as exc:
            draft.error = str(exc)
            draft.status = "failed"
            await session.commit()
            return
        guidance = str(origin.get("guidance") or "")
        fixed_filters = str(origin.get("fixed_filters") or "")
        doc_body = "\n".join(str(c.get("content") or "") for c in chunks.values())
        if not doc_body:
            doc_body = "\n".join(str(d.content or "") for d in docs)
        focus_text = "\n".join(part for part in (doc_body, guidance, fixed_filters) if part)
        tables_text, allowed_tables = await _schema_material(session, source, focus_text)
        if not allowed_tables:
            draft.error = "这个数据源还没有表结构：先探查表结构，再起草口径"
            draft.status = "failed"
            await session.commit()
            return
        datasource = {"name": source.name, "type": source.kind, "description": source.description}

        semantic_inventory = definition_state.get("semantic_inventory")
        selected_definitions = definition_state.get("selected_definition_ids") or []
        selected_names = None
        if semantic_inventory and selected_definitions:
            from app.ledger.semantic_import import implementation_guidance, source_matches

            if not source_matches(semantic_inventory, [{"document_id": d.id, "content": d.content} for d in docs]):
                draft.error = "原文与导入定义的版本不一致，请重新导入后再实现"
                draft.status = "failed"
                await session.commit()
                return

            selected_names = {i["name"] for i in semantic_inventory["items"] if i["id"] in selected_definitions}

        active_definitions = list(selected_definitions)

        def selected_metrics(metrics):
            if selected_names is None:
                return metrics
            definitions = {i["name"]: i["id"] for i in semantic_inventory["items"] if i["id"] in active_definitions}
            kept = []
            for metric in metrics:
                definition_id = metric.get("source_definition_id") or definitions.get(metric.get("name"))
                if definition_id in active_definitions:
                    kept.append({**metric, "source_definition_id": definition_id})
            return kept

        # 提取这一刻就把来源存下来：原文按内容寻址进工件库，草稿只记引用。重导入要拿来当 B 的是这份快照，
        # 不是草稿现在的样子；存原文是为了还原，光有哈希还原不了。先提交一次会话，落工件时不再握着读事务。
        await session.commit()
        source_snapshot = await _capture_source(
            documents=[{"document_id": doc.id, "title": doc.title or doc.source or doc.id,
                        "source": doc.source or "", "collection": doc.collection or "",
                        "content": doc.content or ""} for doc in docs],
            chunks=chunks, datasource=datasource, catalog=tables_text, guidance=guidance,
            fixed_filters=fixed_filters, draft_id=draft_id)

        alignment: dict[str, list[Any]] = {"new": [], "duplicate": [], "conflict": []}
        # 每组一组指标：{sql, entry_fields, metrics}，循环结束时拼成一张图
        blocks: list[dict[str, Any]] = []
        problems: list[dict[str, Any]] = []
        covered: list[str] = []
        questions: list[str] = []
        uncovered: list[str] = []
        progress: dict[str, int] = {}
        diagnostics = ""
        doc_truncated = False
        catalog_truncated = False
        salvaged = False
        model_id = ""
        #: 每次模型提示实际看到了哪几段（提示记录）：visible_chunk_ids 与版本，见 _prompt_trace
        prompts: list[dict[str, Any]] = []

        from app.core.artifact_store import canonical_json, content_hash

        material_hash = content_hash(canonical_json({"documents": entries, "chunks": chunks,
            "datasource": datasource, "catalog": tables_text, "guidance": guidance, "filters": fixed_filters,
            **({"inventory": semantic_inventory, "selected": selected_definitions, "batch_policy": 1}
               if selected_names is not None else {}),
            "plan_policy": draftplan.PROMPT_VERSION}))
        previous = draft.payload if isinstance(draft.payload, dict) else {}
        attempts: dict[str, int] = {}
        definition_diagnostics: dict[str, dict[str, Any]] = {}
        round_offset = 0
        if previous.get("material_hash") == material_hash:
            blocks = copy.deepcopy((previous.get("checkpoint") or {}).get("groups") or [])
            covered = [str(m["name"]) for block in blocks for m in block.get("metrics") or [] if m.get("name")]
            questions = list(previous.get("open_questions") or [])
            problems = _dedupe_problems(list(previous.get("dropped") or []))
            attempts = dict((previous.get("checkpoint") or {}).get("definition_attempts") or {})
            saved_diag = ((previous.get("checkpoint") or {}).get("definition_diagnostics")
                          or previous.get("definition_diagnostics") or {})
            if isinstance(saved_diag, dict):
                definition_diagnostics = copy.deepcopy(saved_diag)
            round_offset = int((previous.get("progress") or {}).get("round") or 0)
            progress = dict(previous.get("progress") or {})
            prompts = list(previous.get("prompts") or [])

        def implemented_ids() -> set[str]:
            return {m.get("source_definition_id") for b in blocks for m in b["metrics"]}

        def implementation_progress() -> dict:
            done = implemented_ids()
            blocked = sum(key not in done and attempts.get(key, 0) >= DEFINITION_ATTEMPTS for key in selected_definitions)
            return {"total": len(selected_definitions), "completed": len(set(selected_definitions) & done),
                    "pending": len(set(selected_definitions) - done) - blocked, "blocked": blocked,
                    "active_names": [i["name"] for i in semantic_inventory["items"] if i["id"] in active_definitions]}

        def state() -> dict[str, Any]:
            payload = _draft_payload(entries=entries, blocks=blocks, problems=problems, questions=questions,
                                     uncovered=uncovered, progress=progress, doc_truncated=doc_truncated,
                                     catalog_truncated=catalog_truncated, model_id=model_id, salvaged=salvaged,
                                     source_snapshot=source_snapshot, prompts=prompts)
            # 来源基线状态现算现写：有可还原的快照就是 known，没有就是 baseline_unknown。它只说明「当时的原文
            # 还原得出来」，不等于字段基线，也不等于这条口径验证通过。
            payload["source_baseline"] = docdraft.source_baseline(payload)
            done_ids = implemented_ids() if semantic_inventory else set()
            unfinished_diagnostics = {
                str(k): v for k, v in definition_diagnostics.items()
                if str(k) in set(selected_definitions) and str(k) not in done_ids
            } if semantic_inventory else {}
            return {**payload, **({"semantic_inventory": semantic_inventory,
                                  "selected_definition_ids": selected_definitions,
                                  "implementation_mode": definition_state.get("implementation_mode", "selected"),
                                  "definition_issues": definition_state.get("definition_issues", []),
                                  **({"migration_baseline": definition_state["migration_baseline"]}
                                     if definition_state.get("migration_baseline") else {}),
                                  "implementation_progress": implementation_progress(),
                                  "definition_diagnostics": unfinished_diagnostics,
                                  "definition_artifact": definition_state.get("definition_artifact")}
                                 if semantic_inventory else {}), "material_hash": material_hash,
                    "checkpoint": {"groups": copy.deepcopy(blocks),
                                   **({"definition_attempts": dict(attempts),
                                       "definition_diagnostics": copy.deepcopy(definition_diagnostics)}
                                      if semantic_inventory else {})}}

        try:
            rounds = len(selected_definitions) * DEFINITION_ATTEMPTS if selected_names is not None else MAX_ROUNDS
            for step in range(1, rounds + 1):
                rnd = round_offset + step
                round_guidance = guidance
                round_chunks = chunks
                chunk_hints: dict[str, str] = {}
                if selected_names is not None:
                    done = implemented_ids()
                    pending = [key for key in selected_definitions if key not in done
                               and attempts.get(key, 0) < DEFINITION_ATTEMPTS]
                    active_definitions = sorted(pending, key=lambda key: attempts.get(key, 0))[:DEFINITIONS_PER_BATCH]
                    if not active_definitions:
                        break
                    round_chunks, chunk_hints = _scoped_definition_chunks(chunks, semantic_inventory, active_definitions)
                    round_guidance += "\n" + implementation_guidance(
                        semantic_inventory, active_definitions, chunk_hints=chunk_hints)
                    # Commit the previous completed work before waiting on another model request.
                    draft.payload = state()
                await session.commit()
                # 先算一次材料：这一份既进提示，也拿来核对引用、记提示记录——两处用同一份，引用才核得上实际可见范围
                fitted = draftplan.fit_material(entries, round_chunks, round_guidance, datasource, tables_text,
                                                covered, fixed_filters)
                visible = fitted[0]
                async with SessionLocal() as planning:
                    result = await draftplan.plan_groups(planning, docs=entries, chunks=round_chunks,
                                                         guidance=round_guidance, datasource=datasource,
                                                         tables_text=tables_text, covered=covered,
                                                         fixed=fixed_filters, fitted=fitted)
                doc_truncated = doc_truncated or fitted[2]
                catalog_truncated = catalog_truncated or fitted[3]
                salvaged = salvaged or bool(result.get("salvaged"))
                model_id = result.get("model_id") or model_id
                prompts.append(_prompt_trace(round=rnd, purpose="plan", fitted=fitted, model_id=model_id,
                                             salvaged=bool(result.get("salvaged")),
                                             groups=len(result.get("groups") or [])))
                if selected_names is not None:
                    prompts[-1]["definition_ids"] = list(active_definitions)
                new_count = 0
                failed: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
                round_recorded_problems: list[dict[str, Any]] = []
                round_open_questions: list[str] = []
                for raw_group in result.get("groups") or []:
                    prepared_group = (
                        _attach_verified_definition_citations(
                            raw_group, visible=visible, semantic_inventory=semantic_inventory,
                            active_definitions=active_definitions, chunk_hints=chunk_hints)
                        if selected_names is not None else raw_group
                    )
                    # 引用核对只认实际进提示的段落：visible 里没有的段落，模型引用它也核不上
                    plan, round_problems = draftplan.normalize_plan(prepared_group, visible,
                                                                    allowed_tables=allowed_tables, strict_scalar=True)
                    fresh = _fresh_metrics(plan["metrics"], covered)
                    fresh = selected_metrics(fresh)
                    if plan["sql"] and fresh:
                        blocks.append({"sql": plan["sql"], "entry_fields": plan["entry_fields"], "metrics": fresh})
                        covered += [m["name"] for m in fresh if m.get("name")]
                        new_count += len(fresh)
                        problems += round_problems
                        round_recorded_problems += round_problems
                    elif _can_repair(prepared_group):
                        failed.append((prepared_group, round_problems))
                    else:
                        problems += round_problems
                        round_recorded_problems += round_problems
                    round_open_questions = _merge_questions(round_open_questions, plan["open_questions"])
                    questions = _merge_questions(questions, plan["open_questions"])

                if failed:
                    # 一组修不好不该拖垮别的组：每组独立重写一次，最多三组同时进行
                    semaphore = asyncio.Semaphore(3)
                    # 重写和起草的材料一样，但 fit 的入参不同（covered 是空的、fixed 照旧，见 repair_group）：
                    # 先算一次，各次重写与引用核对共用同一份可见范围
                    repair_fitted = draftplan.fit_material(entries, round_chunks, round_guidance, datasource,
                                                           tables_text, (), fixed_filters)
                    prompts.append(_prompt_trace(round=rnd, purpose="repair", fitted=repair_fitted,
                                                 model_id=model_id, groups=len(failed)))

                    async def repair(raw: dict[str, Any], issues: list[dict[str, Any]]):
                        async with semaphore:
                            try:
                                async with SessionLocal() as repairing:
                                    fixed = await draftplan.repair_group(
                                        repairing, group=raw, problems=issues, docs=entries, chunks=round_chunks,
                                        guidance=round_guidance, datasource=datasource, tables_text=tables_text,
                                        fixed=fixed_filters, fitted=repair_fitted)
                            except docdraft.DraftUnavailable as e:
                                return None, [{"name": "重写这一组", "reason": str(e) or "AI 起草不可用"}]
                            except Exception as e:  # noqa: BLE001 - 单组重写出错只记这一组
                                return None, [{"name": "重写这一组", "reason": f"重写出错：{e}"}]
                            if not fixed:
                                return None, []
                        prepared_fixed = (
                            _attach_verified_definition_citations(
                                fixed, visible=repair_fitted[0], semantic_inventory=semantic_inventory,
                                active_definitions=active_definitions, chunk_hints=chunk_hints)
                            if selected_names is not None else fixed
                        )
                        return draftplan.normalize_plan(prepared_fixed, repair_fitted[0], allowed_tables=allowed_tables,
                                                        strict_scalar=True)

                    repaired = await asyncio.gather(*(repair(raw, issues) for raw, issues in failed))
                    for (_raw, issues), item in zip(failed, repaired):
                        plan, repair_problems = item
                        fresh = _fresh_metrics(plan["metrics"], covered) if plan is not None else []
                        fresh = selected_metrics(fresh)
                        if plan is not None and plan["sql"] and fresh:
                            blocks.append({"sql": plan["sql"], "entry_fields": plan["entry_fields"],
                                           "metrics": fresh})
                            covered += [m["name"] for m in fresh if m.get("name")]
                            new_count += len(fresh)
                            problems += repair_problems
                            round_recorded_problems += repair_problems
                        else:
                            # 重写没成：原来这一组的问题照实留着，再附上重写这轮的说明
                            problems += issues + repair_problems
                            round_recorded_problems += issues + repair_problems
                        if plan is not None:
                            round_open_questions = _merge_questions(round_open_questions, plan["open_questions"])
                            questions = _merge_questions(questions, plan["open_questions"])
                top_questions = list(result.get("open_questions") or [])
                round_open_questions = _merge_questions(round_open_questions, top_questions)
                questions = _merge_questions(questions, top_questions)
                problems = _dedupe_problems(problems)
                uncovered = list(result.get("uncovered") or [])
                if selected_names is not None:
                    for key in active_definitions:
                        attempts[key] = attempts.get(key, 0) + 1
                    implemented = implemented_ids()
                    for done_id in list(definition_diagnostics):
                        if done_id in implemented:
                            definition_diagnostics.pop(done_id, None)
                    deduped_round = _dedupe_problems(round_recorded_problems)
                    active_by_id = {str(i["id"]): i for i in semantic_inventory["items"] if i["id"] in active_definitions}
                    for def_id in active_definitions:
                        if def_id in implemented:
                            continue
                        def_item = active_by_id.get(str(def_id)) or {}
                        item_name = str(def_item.get("name") or "")
                        matched_reasons = [
                            str(p["reason"]) for p in deduped_round
                            if p.get("reason") and (
                                p.get("name") == item_name or (item_name and item_name in str(p.get("name") or ""))
                            )
                        ]
                        if not matched_reasons:
                            matched_reasons = [
                                str(p["reason"]) for p in deduped_round
                                if p.get("reason") and p.get("name") in ("sql", "metrics", "重写这一组")
                            ]
                        other_names = {
                            str(v.get("name") or "") for k, v in active_by_id.items()
                            if k != str(def_id) and v.get("name")
                        }
                        matched_questions = [q for q in round_open_questions if item_name and item_name in q]
                        if not matched_questions:
                            matched_questions = [
                                q for q in round_open_questions
                                if not any(other in q for other in other_names)
                            ]
                        prev_diag = definition_diagnostics.get(str(def_id)) or {}
                        reasons = list(dict.fromkeys([*(prev_diag.get("reasons") or []), *matched_reasons]))[:5]
                        item_questions = list(dict.fromkeys([*(prev_diag.get("questions") or []), *matched_questions]))[:5]
                        if reasons or item_questions or attempts.get(def_id, 0) >= DEFINITION_ATTEMPTS:
                            kind = "validation_failed" if reasons and not item_questions else "needs_info"
                            definition_diagnostics[str(def_id)] = {
                                "kind": kind, "reasons": reasons, "questions": item_questions,
                            }
                    uncovered = [i["name"] for i in semantic_inventory["items"]
                                 if i["id"] in selected_definitions and i["id"] not in implemented]
                diagnostics = result.get("diagnostics") or diagnostics
                progress = {"round": rnd, "covered": len(covered), "uncovered": len(uncovered)}
                # 每轮先落一次库：这一轮要等一次模型调用，页面这时就能显示「第几轮、起草了几条、还剩几条」
                draft.payload = state()
                await session.commit()
                if selected_names is None and (not new_count or not uncovered or len(covered) >= MAX_METRICS):
                    break

            if selected_names is not None:
                active_definitions = []
                covered_names = set(covered)
                problems = [p for p in _dedupe_problems(problems) if p.get("name") not in covered_names]
                unresolved = [i for i in semantic_inventory["items"] if i["id"] in selected_definitions
                              and i["id"] not in implemented_ids()]
                issue_questions = [f"{i['name']}：{i['reason']}" for i in definition_state.get("definition_issues", [])]
                if blocks or questions or issue_questions or not problems:
                    if len(unresolved) <= 10:
                        per_item: list[str] = []
                        for i in unresolved:
                            if any(i["name"] in q for q in questions):
                                continue
                            diag = definition_diagnostics.get(str(i["id"])) or {}
                            specific = str((diag.get("questions") or diag.get("reasons") or [""])[0] or "").strip()
                            note = specific or "尚未生成可用实现，请补充字段映射或确认口径"
                            per_item.append(note if note.startswith(f"{i['name']}：") else f"{i['name']}：{note}")
                        questions = _merge_questions(questions, per_item)
                    elif not questions and not issue_questions:
                        questions = _merge_questions(
                            questions, [f"另有 {len(unresolved)} 项指标尚未生成可用实现，请按上方定义清单补充字段映射或确认口径"])
                    questions = _merge_questions(questions, issue_questions)

            # 多组从各自 m1 开始：先把 id 在全图里改成唯一，图、payload、groups 和试跑值才指同一件事
            draftplan.unique_metric_ids(blocks)

            if not blocks:
                error = _problems_text(problems) or "模型没有给出可用的取数 SQL 和指标"
                if diagnostics:
                    error = f"{error}（{diagnostics}）"
                if questions:
                    # 模型把缺什么说清楚了（目录里没有这些列、业务上还没定）：这是一份要补信息的草稿，不是失败
                    draft.payload = {**state(), "trial": {"run_id": "", "status": "", "error": ""}}
                    draft.model_id = model_id or None
                    draft.error = ""
                    draft.status = "questions_open"
                    await session.commit()
                    return
                draft.payload = {**state(), "trial": {"run_id": "", "status": "failed", "error": error}}
                draft.model_id = model_id or None
                draft.error = error
                draft.status = "failed"
                await session.commit()
                return

            try:
                graph = draftplan.build_graph(blocks, name=draft.title, question=guidance or draft.title,
                                              datasource=source.name)
            except ValueError as e:
                # build_graph 现在合并入口字段不再抛错；这条留着兜底：真出现合不了的图，照实记为失败，别留半成品
                draft.payload = {**state(), "dropped": [*problems, {"name": "入口字段", "reason": str(e)}],
                                 "trial": {"run_id": "", "status": "failed", "error": str(e)}}
                draft.model_id = model_id or None
                draft.error = str(e)
                draft.status = "failed"
                await session.commit()
                return

            # 对齐要扫所有已发布口径：某一份读不出来不该让整份草稿失败，照实不做对齐（alignment 三个空列表）
            try:
                existing = await _published_metrics(session)
            except Exception:  # noqa: BLE001 - 对齐是辅助信息，不是草稿本身
                logger.exception("读已发布口径失败，草稿 %s 不做对齐", draft_id)
                existing = []
            metrics = [m for block in blocks for m in block["metrics"]]
            alignment = docdraft.alignment(metrics, existing)

            covered_set = set(covered)
            asked = list(dict.fromkeys(questions + [
                f"{p['name']}：{p['reason']}" for p in problems
                if p.get("name") not in ("sql", "metrics")
                and not (selected_names is not None and p.get("name") in covered_set)]))
            workflow = Workflow(
                name=(draft.title.strip() or "文档口径草稿")[:200],
                description=(f"从业务文档起草的口径：{len(metrics)} 个指标、{len(blocks)} 组取数，取数 SQL 由模型对照"
                             f"数据目录生成，后台自动只读试运行；人工审改后按受管级别发布"),
                graph=graph, status="draft", tags=["doc_draft"],
                origin={"kind": "doc_draft", "draft_id": draft.id, "datasource_id": source.id,
                        "documents": [doc.id for doc in docs], "guidance": guidance})
            session.add(workflow)
            await session.flush()
            session.add(WorkflowVersion(workflow_id=workflow.id, version=1, graph=graph,
                                        graph_hash=graph_hash(graph), note="从业务文档分组生成"))
            # Commit ownership and phase with the graph: cancellation cannot orphan it.
            draft.workflow_id = workflow.id
            draft.model_id = model_id or None
            draft.payload = {**state(), "workflow_id": workflow.id, "open_questions": asked,
                             "alignment": alignment, "finalization_pending": True,
                             "accepted_graph_hash": graph_hash(graph)}
            await session.commit()
            payload = dict(draft.payload)
            baseline = await _capture_field_baseline(draft=draft, graph=graph, workflow_id=workflow.id,
                                                     source_snapshot=source_snapshot)
            payload = {**payload, **baseline, "finalization_pending": False}
        except docdraft.DraftUnavailable as e:
            draft.error = str(e) or "AI 起草不可用"
            draft.status = "failed"
            await session.commit()
            return
        except Exception as e:  # noqa: BLE001 - 后台出错要写回这一行，不然界面永远停在「起草中」
            logger.exception("从文档起草口径出错（草稿 %s）", draft_id)
            draft.error = f"起草出错：{e}"
            draft.status = "failed"
            await session.commit()
            return
        draft.payload = payload
        draft.workflow_id = workflow.id
        draft.model_id = model_id or None
        draft.error = None
        draft.status = _status_for(payload)
        await session.commit()
        values = {field["name"]: field.get("default") for field in _merged_fields(blocks)
                  if field.get("default") not in (None, "")}

    try:
        trial = await run_trial(draft_id=draft_id, workflow_id=workflow.id, graph=graph, values=values,
                                 started_by=draft.created_by, entry_fields=_merged_fields(blocks))
    except Exception as e:  # noqa: BLE001 - 试运行出错也要写进 payload，不能让界面停在「试运行中」
        logger.exception("草稿 %s 的试运行出错", draft_id)
        # 这一条试运行也要有稳定身份（K05 的验证记录按它去重/认得是哪一次）：出错这条没有运行，trial_id 由这里给
        trial = {**trial_binding(1, graph, values), "trial_id": uuid4().hex, "run_id": "",
                 "status": "failed", "error": f"试运行出错：{e}"}
    await save_trial(draft_id, trial)


def _problems_text(problems: list[dict[str, Any]]) -> str:
    """把校验问题拼成一句给人看的原因（接口的 error 只有一个字段）。"""
    return "；".join(f"{p.get('name') or '问题'}：{p.get('reason') or ''}".strip("：")
                    for p in problems if p.get("reason"))[:2000]
