"""跨数据源健康与漂移快速探针（source_health，W1-b）。

用于指标异动与跨源穿透时的第 1 顺位核查（先排数，后归因）：
1. 读取 `SourceChange` 待处理变化记录（`date_stalled`、`date_gap`、`volume_drop`、`rows_drop`、`codes_new`、
   `codes_shift`、`coverage_drop`、`table_removed`、`column_removed`、`column_type` 等）；
2. 对目标表非遮罩的时间列做有界只读鲜度与时间窗覆盖探查（`PROBE_TIMEOUT_S = 10.0s`，`guard.check + run_query`），
   超时或失败优雅降级为 `unverified`，绝不拖垮主运行；
3. 严守遮罩铁律（铁律 3）：绝不探查或返回任何 `mask_columns` 列的值；`restricted=True`（普通 user 角色）时自动隐去
   物理表名、物理列名与底层码值细节，只输出业务数据源说明与高层健康判定。
"""
from __future__ import annotations

import asyncio
from collections.abc import Iterable, Mapping
from datetime import date, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import artifact_store
from app.data import schema_diff as sd
from app.data import source_changes as sc
from app.data.coverage import _as_day
from app.data.engine import QueryLimits, masked_columns, run_query
from app.data.guard import check, is_write
from app.data.names import name_key
from app.db.models import DataSource, Workflow, WorkflowVersion
from app.db.models_sources import SourceChange

ARTIFACT_KIND = "source_health"
LEDGER_KIND = "source_health"
CATALOG_PREFIX = "H"
PROBE_TIMEOUT_S = 10.0
MAX_TABLES_PROBED = 6

HEALTHY = "healthy"
DEGRADED = "degraded"
ANOMALOUS = "anomalous"
UNVERIFIED = "unverified"
VERDICTS = (HEALTHY, DEGRADED, ANOMALOUS, UNVERIFIED)

_SEVERE_CHANGE_KINDS = frozenset({
    sd.TABLE_REMOVED,
    sd.COLUMN_REMOVED,
    sd.COLUMN_TYPE,
    sc.DATE_STALLED,
    sc.DATE_GAP,
    sc.VOLUME_DROP,
    sc.ROWS_DROP,
    sc.COVERAGE_DROP,
})
_WARN_CHANGE_KINDS = frozenset({
    sd.TABLE_REFLECT_FAILED,
    sd.TABLE_LEFT_SCOPE,
    sc.CODES_NEW,
    sc.CODES_SHIFT,
    sc.ROWS_JUMP,
})


def _quote_ident(name: str, dialect: str) -> str:
    clean = str(name)
    if dialect in ("mysql", "mariadb"):
        return "`" + clean.replace("`", "``") + "`"
    return '"' + clean.replace('"', '""') + '"'


def _date_columns_of(table_meta: Mapping[str, Any], masked: set[str]) -> list[str]:
    cols = table_meta.get("columns")
    if not isinstance(cols, list):
        return []
    out: list[str] = []
    for col in cols:
        if not isinstance(col, Mapping):
            continue
        cname = str(col.get("name") or "").strip()
        if not cname or cname.lower() in masked:
            continue
        ctype = str(col.get("type") or "").lower()
        low = cname.lower()
        if any(tok in ctype for tok in ("date", "time", "timestamp")) or any(
            low == k or low.endswith(f"_{k}") or low.startswith(f"{k}_")
            for k in ("date", "day", "dt", "shift_date", "production_day", "biz_date", "created_at")
        ):
            out.append(cname)
    return out[:2]


async def _resolve_caliber_targets(
    session: AsyncSession,
    *,
    workflow_id: str,
    version: int | None,
    card_id: str | None,
    metric_id: str | None,
) -> list[tuple[str, list[str]]]:
    """根据已发布（或指定版本）口径自动反查 (source_name, [table_names])。"""
    from app.ledger.ontology import workflow_ontology_index

    wf = await session.get(Workflow, workflow_id)
    if wf is None:
        return []
    ver = version if version is not None else wf.published_version or wf.version
    snap = (
        await session.execute(
            select(WorkflowVersion).where(
                WorkflowVersion.workflow_id == workflow_id,
                WorkflowVersion.version == int(ver),
            )
        )
    ).scalar_one_or_none()
    graph = (snap.graph if snap is not None else wf.graph) or {}
    idx = workflow_ontology_index(workflow_id=workflow.id if (workflow := wf) else workflow_id, version=int(ver), graph=graph)
    by_src: dict[str, set[str]] = {}
    for card in idx["cards"]:
        if card_id and card["card_id"] != card_id:
            continue
        if metric_id:
            for m in card["metrics"]:
                if m["metric_id"] == metric_id:
                    for item in m["lineage"]:
                        by_src.setdefault(item["source"], set()).update(item["tables"])
        else:
            for src, tbls in card["tables_by_source"].items():
                by_src.setdefault(src, set()).update(tbls)
    return [(src, sorted(tbls)) for src, tbls in sorted(by_src.items())]


def _parse_window_day(raw: str | date | None) -> date | None:
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw
    d = _as_day(str(raw).strip())
    return d.day if d is not None else None


async def _probe_table_coverage(
    source: DataSource,
    table_name: str,
    table_meta: Mapping[str, Any],
    *,
    masked: set[str],
    start_day: date | None,
    end_day: date | None,
) -> dict[str, Any]:
    """对单张表做只读行数与非遮罩日期边界探查，超时或报错返回 status='unverified'。"""
    dialect = (source.kind or "sqlite").lower()
    q_table = _quote_ident(table_name, dialect)
    date_cols = _date_columns_of(table_meta, masked)
    select_parts = ["COUNT(*) AS row_cnt"]
    for idx, col in enumerate(date_cols):
        qc = _quote_ident(col, dialect)
        select_parts.append(f"MIN({qc}) AS min_d{idx}")
        select_parts.append(f"MAX({qc}) AS max_d{idx}")
    sql = f"SELECT {', '.join(select_parts)} FROM {q_table}"
    if is_write(sql, dialect=dialect):
        return {"table": table_name, "status": UNVERIFIED, "reason": "探查语句未通过只读校验"}
    check(sql, readonly=True, source_name=source.name, dialect=dialect)

    try:
        res = await asyncio.wait_for(
            run_query(source, sql, limits=QueryLimits(max_rows=5, timeout_seconds=PROBE_TIMEOUT_S)),
            timeout=PROBE_TIMEOUT_S + 1.0,
        )
    except Exception:  # noqa: BLE001 - 探查失败优雅降级，绝不中断主流程
        return {"table": table_name, "status": UNVERIFIED, "reason": "实时探查超时或暂时不可达"}

    if not res.rows:
        return {"table": table_name, "status": ANOMALOUS, "issue": "empty_table", "row_count": 0}
    first = res.rows[0]
    row_cnt = int(first[0] or 0) if first and first[0] is not None else 0
    if row_cnt == 0:
        return {"table": table_name, "status": ANOMALOUS, "issue": "empty_table", "row_count": 0}

    ranges: list[dict[str, Any]] = []
    window_issues: list[dict[str, Any]] = []
    for idx, col in enumerate(date_cols):
        pos = 1 + idx * 2
        raw_min = first[pos] if pos < len(first) else None
        raw_max = first[pos + 1] if (pos + 1) < len(first) else None
        d_min = _as_day(raw_min)
        d_max = _as_day(raw_max)
        ranges.append({
            "column": col,
            "min": d_min.day.isoformat() if d_min else (str(raw_min) if raw_min is not None else None),
            "max": d_max.day.isoformat() if d_max else (str(raw_max) if raw_max is not None else None),
        })
        if start_day and d_max and d_max.day < start_day:
            window_issues.append({
                "kind": sc.DATE_STALLED,
                "severity": "action",
                "column": col,
                "max_date": d_max.day.isoformat(),
                "requested_start": start_day.isoformat(),
            })
        elif end_day and d_min and d_min.day >= end_day:
            window_issues.append({
                "kind": sc.DATE_GAP,
                "severity": "action",
                "column": col,
                "min_date": d_min.day.isoformat(),
                "requested_end": end_day.isoformat(),
            })
    return {
        "table": table_name,
        "status": ANOMALOUS if window_issues else HEALTHY,
        "row_count": row_cnt,
        "ranges": ranges,
        "window_issues": window_issues,
    }


def _render_phrase(source_title: str, verdict: str, issues: list[dict[str, Any]], *, probe_unverified: bool) -> str:
    if verdict == ANOMALOUS:
        labels = list(dict.fromkeys(str(i.get("label") or i.get("kind") or "数据异常") for i in issues))
        shown = "、".join(labels[:3])
        return f"数据源「{source_title}」检测到数据健康异常（{shown}），当前异动优先归因于上游同步或主数据质量问题，不宜直接归结为业务实质变化。"
    if verdict == DEGRADED:
        labels = list(dict.fromkeys(str(i.get("label") or i.get("kind") or "取值变化") for i in issues))
        shown = "、".join(labels[:3])
        return f"数据源「{source_title}」存在待审阅的数据变化提醒（{shown}），跨源对比与归因时需留意码值或分布漂移。"
    if verdict == UNVERIFIED or probe_unverified:
        return f"数据源「{source_title}」未发现已登记的变化告警，但实时范围探查未完成（状态记为待核实）。"
    return f"数据源「{source_title}」同步状态与查询区间覆盖正常，未发现断更、缺口或未处理的结构漂移。"


async def check_source_health(
    session: AsyncSession,
    *,
    source_name: str | None = None,
    tables: Iterable[str] | None = None,
    workflow_id: str | None = None,
    version: int | None = None,
    card_id: str | None = None,
    metric_id: str | None = None,
    start: str | date | None = None,
    end: str | date | None = None,
    restricted: bool = False,
    live_probe: bool = True,
) -> dict[str, Any]:
    """检查目标数据源（或目标口径底层依赖源）的健康度与数据漂移，返回确定性结构化结论。"""
    targets: list[tuple[str, list[str]]] = []
    if source_name and str(source_name).strip():
        explicit_tables = [str(t).strip() for t in (tables or ()) if str(t).strip()]
        targets.append((str(source_name).strip(), explicit_tables))
    elif workflow_id and str(workflow_id).strip():
        targets = await _resolve_caliber_targets(
            session,
            workflow_id=str(workflow_id).strip(),
            version=version,
            card_id=card_id,
            metric_id=metric_id,
        )
    if not targets:
        return {
            "kind": ARTIFACT_KIND,
            "verdict": UNVERIFIED,
            "sources": [],
            "issues": [],
            "phrase": "未能定位目标数据源，健康状态记为未核实。",
        }

    start_day = _parse_window_day(start)
    end_day = _parse_window_day(end)

    source_reports: list[dict[str, Any]] = []
    all_issues: list[dict[str, Any]] = []
    overall_rank = 0  # 0 healthy, 1 unverified, 2 degraded, 3 anomalous
    rank_to_verdict = {0: HEALTHY, 1: UNVERIFIED, 2: DEGRADED, 3: ANOMALOUS}

    for src_name, target_tables in targets:
        src = (
            await session.execute(select(DataSource).where(DataSource.name == src_name).limit(1))
        ).scalar_one_or_none()
        if src is None or not src.enabled:
            overall_rank = max(overall_rank, 3 if src is not None and not src.enabled else 1)
            status = ANOMALOUS if (src is not None and not src.enabled) else UNVERIFIED
            issue_label = "数据源已停用" if status == ANOMALOUS else "数据源未找到"
            issue = {"kind": "source_unavailable", "severity": "action", "label": issue_label}
            if not restricted:
                issue["source"] = src_name
            all_issues.append(issue)
            source_reports.append({
                "source_label": (src.description or src.name) if src else ("关联数据源" if restricted else src_name),
                **({} if restricted else {"source": src_name}),
                "verdict": status,
                "issues": [issue],
            })
            continue

        masked = {c.lower() for c in masked_columns(src.options)}
        schema_tables = (src.schema_cache or {}).get("tables") if isinstance(src.schema_cache, Mapping) else {}
        if not isinstance(schema_tables, Mapping):
            schema_tables = {}

        wanted_keys = {name_key(t) for t in target_tables} if target_tables else None
        change_rows = list(
            (
                await session.execute(
                    select(SourceChange)
                    .where(SourceChange.source_id == src.id, SourceChange.status.in_(sc.PENDING))
                    .order_by(SourceChange.seq.desc())
                )
            ).scalars()
        )
        src_issues: list[dict[str, Any]] = []
        src_rank = 0
        for ch in change_rows:
            if wanted_keys is not None and ch.table_name and name_key(ch.table_name) not in wanted_keys:
                continue
            # 遮罩列上的变化不暴露列名与取值细节（铁律 3）
            if ch.column_name and ch.column_name.lower() in masked:
                continue
            label = sc.KIND_LABEL.get(ch.kind, ch.kind)
            is_severe = ch.kind in _SEVERE_CHANGE_KINDS or ch.severity == "action"
            src_rank = max(src_rank, 3 if is_severe else 2)
            entry: dict[str, Any] = {
                "kind": ch.kind,
                "severity": ch.severity,
                "status": ch.status,
                "label": label,
            }
            if not restricted:
                entry["source"] = src.name
                if ch.table_name:
                    entry["table"] = ch.table_name
                if ch.column_name:
                    entry["column"] = ch.column_name
                entry["summary"] = sc.summary(ch)
            src_issues.append(entry)

        probe_unverified = False
        probed_tables: list[dict[str, Any]] = []
        if live_probe:
            candidate_names = target_tables or list(schema_tables.keys())[:MAX_TABLES_PROBED]
            for tbl_name in candidate_names[:MAX_TABLES_PROBED]:
                # 找到 schema_cache 里精确的表名键
                matched_name = next((k for k in schema_tables if name_key(k) == name_key(tbl_name)), tbl_name)
                meta = schema_tables.get(matched_name) if isinstance(schema_tables.get(matched_name), Mapping) else {}
                probe = await _probe_table_coverage(
                    src,
                    matched_name,
                    meta,
                    masked=masked,
                    start_day=start_day,
                    end_day=end_day,
                )
                if probe["status"] == UNVERIFIED:
                    probe_unverified = True
                    if src_rank == 0:
                        src_rank = 1
                elif probe["status"] == ANOMALOUS:
                    src_rank = max(src_rank, 3)
                    if probe.get("issue") == "empty_table":
                        item: dict[str, Any] = {
                            "kind": sc.ROWS_DROP,
                            "severity": "action",
                            "label": "目标表当前无数据（0 行）",
                        }
                        if not restricted:
                            item["source"] = src.name
                            item["table"] = matched_name
                        src_issues.append(item)
                    for wi in probe.get("window_issues") or []:
                        item = {
                            "kind": wi["kind"],
                            "severity": wi["severity"],
                            "label": sc.KIND_LABEL.get(wi["kind"], wi["kind"]),
                        }
                        if not restricted:
                            item["source"] = src.name
                            item["table"] = matched_name
                            item["column"] = wi["column"]
                            if "max_date" in wi:
                                item["max_date"] = wi["max_date"]
                            if "min_date" in wi:
                                item["min_date"] = wi["min_date"]
                        src_issues.append(item)
                if not restricted:
                    probed_tables.append(probe)

        src_verdict = rank_to_verdict[src_rank]
        overall_rank = max(overall_rank, src_rank)
        all_issues.extend(src_issues)
        display_title = (src.description or "关联业务源") if restricted else src.name
        rep: dict[str, Any] = {
            "source_label": display_title,
            "verdict": src_verdict,
            "issue_count": len(src_issues),
            "issues": src_issues,
            "phrase": _render_phrase(display_title, src_verdict, src_issues, probe_unverified=probe_unverified),
        }
        if not restricted:
            rep["source"] = src.name
            rep["tables_probed"] = probed_tables
        source_reports.append(rep)

    overall_verdict = rank_to_verdict[overall_rank]
    lead_phrase = source_reports[0]["phrase"] if len(source_reports) == 1 else (
        "；".join(r["phrase"] for r in source_reports)
    )
    out: dict[str, Any] = {
        "kind": ARTIFACT_KIND,
        "verdict": overall_verdict,
        "sources": source_reports,
        "issues": all_issues,
        "phrase": lead_phrase,
    }
    if workflow_id:
        out["caliber_ref"] = {
            "workflow_id": workflow_id,
            **({"version": version} if version is not None else {}),
            **({"card_id": card_id} if card_id else {}),
            **({"metric_id": metric_id} if metric_id else {}),
        }
    return out


def is_health_result(content: Any) -> bool:
    return (
        isinstance(content, Mapping)
        and content.get("kind") == ARTIFACT_KIND
        and content.get("verdict") in VERDICTS
        and isinstance(content.get("sources"), list)
    )


async def store_source_health(result: Mapping[str, Any], *, run_id: str | None, node_id: str | None = None) -> str:
    return await artifact_store.put_json(dict(result), kind=ARTIFACT_KIND, run_id=run_id, meta={
        "verdict": result.get("verdict"),
        "node_id": node_id or "",
    })


def ledger_entry(
    result: Mapping[str, Any],
    *,
    node_id: str,
    exec_no: int,
    artifact: str,
    call_id: str | None = None,
) -> dict[str, Any]:
    sources = [
        str(s.get("source") or s.get("source_label") or "")
        for s in (result.get("sources") or [])
        if isinstance(s, Mapping)
    ]
    entry: dict[str, Any] = {
        "kind": LEDGER_KIND,
        "node_id": node_id,
        "exec": exec_no,
        "artifact": artifact,
        "verdict": result.get("verdict"),
        "sources": [s for s in sources if s],
        "issue_count": len(result.get("issues") or []),
    }
    if call_id is not None:
        entry["call_id"] = call_id
    return entry
