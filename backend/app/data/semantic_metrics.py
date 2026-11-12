"""独立语义指标库（SemanticMetric）校验、确定性 SQL 编译、试算与 AI 提取服务。

核心能力：
1. 即存即用指标卡片 CRUD：严格校验指标 key、物理表存在性、聚合表达式与过滤条件安全性（拒绝写语句、多语句、子查询与遮罩列引用）。
2. 确定性单条 SQL 编译器（compile_metric_sql）：支持按时间区间（date_from / date_to）、按时间序列趋势（group_by_date）、
   按预定义业务维度切片（group_by）及维度过滤（filters）；当维度绑定了 CanonicalEntity 时，自动将业务别名确定性翻译为
   目标库物理编码。
3. T+1 空结果自动探底：单日查询返回空或全 NULL 时，自动探测底表最新可用业务日期（latest_available_date），省去 Agent 多轮试探。
4. AI 辅助提取（suggest_metrics_from_source）：结合数据目录（中文名、粒度、业务日期、有效过滤）与知识库文档（如《指标公式总表》），
   自动生成通过表列与语法核验的候选指标卡片。
"""
from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from typing import Any

import sqlglot
from langchain_core.messages import HumanMessage, SystemMessage
from sqlglot import exp
from sqlalchemy import or_, select
from sqlalchemy.dialects import oracle as oracle_dialect
from sqlalchemy.ext.asyncio import AsyncSession

from app.engine.state import message_text
from app.data import catalog, catalog_assistant
from app.data.engine import QueryLimits, masked_columns, run_query
from app.data.entities import (
    ACTIVE_ENTITY_STATUSES,
    confirmed_bindings,
    list_entities,
    translate_entity_value,
)
from app.data.guard import SqlRejected, _ALWAYS_DENIED, _HIDDEN_WRITE, _SIDE_EFFECT_CALL, check as guard_check, first_verb
from app.data.names import name_key
from app.data.sqlcheck import dialect_of
from app.db.models import DataSource, Document
from app.db.models_metrics import SemanticMetric

logger = logging.getLogger(__name__)

METRIC_STATUSES = ("enabled", "draft", "disabled")
METRIC_SOURCES = ("human", "ai")
CALC_MODES = ("expression", "sql_template")
TIME_GRAINS = ("day", "month", "hour", "none")
ALLOWED_TEMPLATE_SLOTS = frozenset(
    {
        "select_dims",
        "where",
        "and_where",
        "group_by",
        "order_by",
        "date_from",
        "date_to",
        "period_days",
        "limit",
    }
)

_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_IDENT_SAFE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$|^[\u4e00-\u9fffA-Za-z0-9_]+$")
_ORACLE_SIMPLE_IDENT_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_$#]*$")
_ORACLE_PSEUDOCOLUMNS = frozenset({"rownum", "rowid", "sysdate", "systimestamp", "level"})
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_FENCE_RE = re.compile(r"```(?:json)?\s*([\s\S]*?)```", re.IGNORECASE)
_SLOT_TOKEN_RE = re.compile(r"\{\{\s*([a-zA-Z0-9_]+)\s*\}\}")
_SELECT_DIMS_SLOT_RE = re.compile(r"\{\{\s*select_dims\s*\}\}\s*,?", re.IGNORECASE)
_WHERE_SLOT_RE = re.compile(r"(?P<kw>\bWHERE\s+)?\{\{\s*where\s*\}\}", re.IGNORECASE)
_AND_WHERE_SLOT_RE = re.compile(r"(?:\bAND\s+)?\{\{\s*and_where\s*\}\}", re.IGNORECASE)
_GROUP_BY_SLOT_RE = re.compile(r"(?:\bGROUP\s+BY\s+)?\{\{\s*group_by\s*\}\}", re.IGNORECASE)
_ORDER_BY_SLOT_RE = re.compile(r"(?:\bORDER\s+BY\s+)?\{\{\s*order_by\s*\}\}", re.IGNORECASE)
_DATE_FROM_SLOT_RE = re.compile(r"\{\{\s*date_from\s*\}\}", re.IGNORECASE)
_DATE_TO_SLOT_RE = re.compile(r"\{\{\s*date_to\s*\}\}", re.IGNORECASE)
_PERIOD_DAYS_SLOT_RE = re.compile(r"\{\{\s*period_days\s*\}\}", re.IGNORECASE)
_LIMIT_SLOT_RE = re.compile(r"\{\{\s*limit\s*\}\}", re.IGNORECASE)
_TRAILING_LIMIT_RE = re.compile(r"\s+\bLIMIT\s+(\d+)\s*$", re.IGNORECASE)
_HAS_ORACLE_ROW_LIMIT_RE = re.compile(
    r"(?:\bFETCH\s+(?:FIRST|NEXT)\s+\d+\s+ROWS?\s+ONLY\s*$|\bROWNUM\s*<=?\s*\d+\s*$)",
    re.IGNORECASE,
)


class MetricInvalid(ValueError):
    """指标定义或查询参数不合法。"""

    def __init__(self, message: str, *, field: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.field = field


class MetricConflict(ValueError):
    """指标唯一键冲突或乐观锁版本冲突。"""

    def __init__(self, message: str, *, field: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.field = field


class MetricNotFound(LookupError):
    """指标或关联数据源不存在。"""


def _iso(val: Any) -> str | None:
    if val is None or not hasattr(val, "isoformat"):
        return None
    if getattr(val, "tzinfo", None) is None:
        val = val.replace(tzinfo=timezone.utc)
    return val.isoformat()


def normalize_metric_key(raw: Any, *, field: str = "key") -> str:
    val = str(raw or "").strip().lower()
    if not _KEY_RE.match(val):
        raise MetricInvalid("指标标识（key）须以小写字母开头，仅含小写字母、数字和下划线（最长 64 字符）", field=field)
    return val


def _clean_aliases(raw: Any, *, field: str = "aliases") -> list[str]:
    if raw in (None, ""):
        return []
    if isinstance(raw, str):
        items = [p.strip() for p in re.split(r"[,，、;\n]+", raw) if p.strip()]
    elif isinstance(raw, list):
        items = [str(p or "").strip() for p in raw if str(p or "").strip()]
    else:
        raise MetricInvalid(f"字段「{field}」需要是字符串列表", field=field)
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        low = item.lower()
        if low not in seen:
            seen.add(low)
            out.append(item[:80])
    return out[:24]


def _validate_column_identifier(
    col: str,
    *,
    field: str,
    masked_cols: set[str] | None = None,
    allow_qualified: bool = False,
) -> str:
    clean = str(col or "").strip()
    if not clean:
        raise MetricInvalid(f"字段「{field}」不能为空", field=field)
    if len(clean) > 128 or any(ch in clean for ch in (";", "'", '"', "`", "\n", "\r", "\x00", "(", ")")):
        raise MetricInvalid(f"列名「{clean}」包含非法字符", field=field)
    if "." in clean and not allow_qualified:
        raise MetricInvalid(f"单表模式下字段「{field}」请直接填写物理列名，不要带表别名前缀（{clean}）", field=field)
    parts = [p.strip() for p in clean.split(".")]
    if any(not p for p in parts) or len(parts) > 2:
        raise MetricInvalid(f"列名「{clean}」格式不合法", field=field)
    leaf_col = parts[-1]
    if masked_cols and (name_key(clean) in masked_cols or name_key(leaf_col) in masked_cols):
        raise MetricInvalid(f"列「{clean}」属于数据源敏感遮罩列，不能用于指标定义", field=field)
    return clean


def normalize_dimension(
    raw: Any,
    *,
    index: int = 0,
    masked_cols: set[str] | None = None,
    allow_qualified: bool = False,
) -> dict[str, Any]:
    at = f"dimensions[{index}]"
    if not isinstance(raw, Mapping):
        raise MetricInvalid("指标维度需要是对象", field=at)
    col = _validate_column_identifier(
        str(raw.get("column") or ""),
        field=f"{at}.column",
        masked_cols=masked_cols,
        allow_qualified=allow_qualified,
    )
    raw_key = str(raw.get("key") or "").strip().lower()
    if not raw_key:
        leaf = col.rsplit(".", 1)[-1]
        slug = re.sub(r"[^a-z0-9_]+", "_", leaf.lower()).strip("_")
        raw_key = slug if slug and slug[0].isalpha() else f"dim_{index + 1}"
    if not _KEY_RE.match(raw_key):
        raise MetricInvalid(f"维度标识「{raw_key}」须以小写字母开头，仅含小写字母、数字和下划线", field=f"{at}.key")
    label = str(raw.get("label") or raw_key).strip() or raw_key
    entity_key = str(raw.get("entity_key") or "").strip().lower()
    if entity_key and not _KEY_RE.match(entity_key):
        raise MetricInvalid(f"关联实体标识「{entity_key}」格式不合法", field=f"{at}.entity_key")
    return {
        "key": raw_key,
        "label": label[:80],
        "column": col,
        "entity_key": entity_key,
    }


def infer_aggregation_type(sql_expression: str) -> str:
    """从聚合 SQL 表达式推断展示用聚合类型标签（sum / count_distinct / count / avg / max / min / ratio / custom）。"""
    expr = str(sql_expression or "").strip()
    if not expr:
        return "custom"
    upper = expr.upper()
    if "/" in upper and ("NULLIF" in upper or upper.startswith("ROUND(") or upper.count("(") >= 2):
        if not upper.startswith(("SUM(", "COUNT(", "AVG(", "MAX(", "MIN(")):
            return "ratio"
    if re.match(r"^COUNT\s*\(\s*DISTINCT\b", upper):
        return "count_distinct"
    for prefix, tag in (
        ("SUM(", "sum"),
        ("COUNT(", "count"),
        ("AVG(", "avg"),
        ("MAX(", "max"),
        ("MIN(", "min"),
    ):
        if upper.startswith(prefix) or re.match(rf"^{prefix[:-1]}\s*\(", upper):
            return tag
    if "/" in upper:
        return "ratio"
    return "custom"


def validate_sql_fragment(
    fragment: str,
    *,
    dialect: str | None,
    field: str,
    is_filter: bool = False,
    allow_empty: bool = False,
    masked_cols: set[str] | None = None,
    known_cols: Mapping[str, str] | set[str] | None = None,
    table_name: str = "",
) -> str:
    """校验聚合表达式或过滤条件为单层安全只读表达式，不含注释、分号、写操作、子查询、不存在列或敏感遮罩列。"""
    clean = str(fragment or "").strip()
    if not clean:
        if allow_empty:
            return ""
        raise MetricInvalid(f"字段「{field}」不能为空", field=field)
    if len(clean) > 2000:
        raise MetricInvalid(f"字段「{field}」长度不能超过 2000 字符", field=field)
    if ";" in clean or "--" in clean or "/*" in clean or "*/" in clean:
        raise MetricInvalid(f"字段「{field}」不能包含分号或 SQL 注释", field=field)
    first_word = clean.split(None, 1)[0].lower() if clean.split() else ""
    if first_word in _ALWAYS_DENIED or _HIDDEN_WRITE.search(clean) or _SIDE_EFFECT_CALL.search(clean):
        raise MetricInvalid(f"字段「{field}」包含不允许的关键字或有副作用的函数", field=field)

    probe_sql = f"SELECT 1 FROM _metric_tbl WHERE ({clean})" if is_filter else f"SELECT ({clean}) AS _val FROM _metric_tbl"
    read_dialect = dialect or "mysql"
    try:
        parsed = sqlglot.parse_one(probe_sql, read=read_dialect)
    except Exception as exc:  # noqa: BLE001
        # 回退默认方言再试一次，兼容通用 SQL 写法
        try:
            parsed = sqlglot.parse_one(probe_sql)
        except Exception as exc2:  # noqa: BLE001
            raise MetricInvalid(f"字段「{field}」不是合法的 SQL 表达式（{exc2}）", field=field) from exc2

    # 禁止嵌套 SELECT / 子查询 / UNION（保持指标底表血缘清晰且可受控加 WHERE 与 GROUP BY）
    sub_selects = list(parsed.find_all(exp.Select))
    if len(sub_selects) > 1 or list(parsed.find_all(exp.Union)):
        raise MetricInvalid(f"字段「{field}」不允许包含嵌套 SELECT 子查询，请使用聚合表达式（如 SUM(CASE WHEN cond THEN val END)）", field=field)

    for col_node in parsed.find_all(exp.Column):
        cname = str(col_node.name or "").strip()
        if not cname or cname == "*":
            continue
        if read_dialect == "oracle" and cname.lower() in _ORACLE_PSEUDOCOLUMNS:
            continue
        ckey = name_key(cname)
        if masked_cols and ckey in masked_cols:
            raise MetricInvalid(f"字段「{field}」引用了数据源敏感遮罩列「{cname}」，不允许用于指标定义", field=field)
        if known_cols and ckey not in known_cols:
            tbl_desc = f"表「{table_name}」" if table_name else "目标物理表"
            raise MetricInvalid(f"字段「{field}」引用了{tbl_desc}中不存在的列「{cname}」", field=field)

    return clean


def metric_to_dict(
    row: SemanticMetric,
    *,
    source_name: str = "",
    restricted: bool = False,
) -> dict[str, Any]:
    dims = list(row.dimensions or [])
    if restricted:
        dims = [
            {"key": d.get("key"), "label": d.get("label"), "entity_key": d.get("entity_key") or ""}
            for d in dims
            if isinstance(d, Mapping)
        ]
    calc_mode = str(getattr(row, "calc_mode", None) or "expression").strip().lower() or "expression"
    sql_tpl = str(getattr(row, "sql_template", None) or "").strip()
    base: dict[str, Any] = {
        "id": row.id,
        "key": row.key,
        "name": row.name,
        "aliases": list(row.aliases or []),
        "description": row.description or "",
        "unit": row.unit or "",
        "source_id": row.source_id,
        "source_name": source_name,
        "calc_mode": calc_mode,
        "aggregation_type": "template" if calc_mode == "sql_template" else infer_aggregation_type(row.sql_expression),
        "dimensions": dims,
        "default_time_grain": row.default_time_grain or "day",
        "has_date_column": bool(str(row.date_column or "").strip()),
        "status": row.status or "enabled",
        "source": row.source or "human",
        "version": int(row.version or 1),
        "created_by": row.created_by,
        "updated_by": row.updated_by,
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
    }
    if not restricted:
        base.update(
            table_name=row.table_name,
            sql_expression=row.sql_expression,
            sql_template=sql_tpl,
            date_column=row.date_column or "",
            filter_sql=row.filter_sql or "",
        )
    return base


async def _resolve_source(session: AsyncSession, source_id_or_name: str) -> DataSource:
    clean = str(source_id_or_name or "").strip()
    if not clean:
        raise MetricInvalid("请选择指标归属的数据源（source_id）", field="source_id")
    ds = await session.get(DataSource, clean)
    if ds is None:
        ds = (await session.execute(select(DataSource).where(DataSource.name == clean))).scalar_one_or_none()
    if ds is None:
        raise MetricNotFound(f"找不到数据源「{clean}」")
    return ds


def _resolve_table_and_columns(ds: DataSource, raw_table: str) -> tuple[str, dict[str, str]]:
    """返回对齐后的表名与该表的 {name_key(col): col_type} 字典（未探查时仅做基本标识符校验）。"""
    clean_table = str(raw_table or "").strip()
    if not clean_table:
        raise MetricInvalid("请填写物理表名（table_name）", field="table_name")
    if any(ch in clean_table for ch in (";", "'", '"', "`", "\n", "\r", "(", ")")):
        raise MetricInvalid(f"表名「{clean_table}」包含非法字符", field="table_name")
    cache = ds.schema_cache if isinstance(ds.schema_cache, Mapping) else {}
    tables = cache.get("tables") if isinstance(cache, Mapping) else None
    if isinstance(tables, Mapping) and tables:
        resolved = catalog.resolve_table_name(cache, clean_table)
        if resolved is None:
            raise MetricInvalid(f"数据源「{ds.name}」的已探查表结构中不存在表「{clean_table}」", field="table_name")
        tinfo = tables.get(resolved) or {}
        cols: dict[str, str] = {}
        for c in tinfo.get("columns") or []:
            if isinstance(c, Mapping) and c.get("name"):
                cols[name_key(str(c["name"]))] = str(c.get("type") or "").lower()
        return resolved, cols
    return clean_table, {}


def _validate_and_analyze_sql_template(
    raw_template: str,
    *,
    ds: DataSource,
    dialect: str | None,
    masked_set: set[str],
    metric_key: str,
    raw_table: str,
    sql_expression: str,
    date_column: str,
    filter_sql: str,
    dims: list[dict[str, Any]],
    grain: str,
) -> tuple[str, str, str]:
    """校验多表 / 参数化 SQL 模板（支持 {{select_dims}}、{{and_where}}、{{period_days}} 等安全槽位），
    返回 (clean_template, primary_table_name, effective_sql_expression)。
    """
    clean_tpl = str(raw_template or "").strip().rstrip(";").strip()
    if not clean_tpl:
        raise MetricInvalid("多表或参数化模板模式下，请填写「SQL 模板」内容", field="sql_template")
    if len(clean_tpl) > 8000:
        raise MetricInvalid("「SQL 模板」长度不能超过 8000 字符", field="sql_template")
    if ";" in clean_tpl or "--" in clean_tpl or "/*" in clean_tpl or "*/" in clean_tpl:
        raise MetricInvalid("「SQL 模板」不能包含多语句分号或 SQL 注释", field="sql_template")
    if _HIDDEN_WRITE.search(clean_tpl) or _SIDE_EFFECT_CALL.search(clean_tpl):
        raise MetricInvalid("「SQL 模板」包含不允许的写操作或有副作用的函数", field="sql_template")

    slots_found = [m.group(1).strip().lower() for m in _SLOT_TOKEN_RE.finditer(clean_tpl)]
    unknown_slots = sorted({s for s in slots_found if s not in ALLOWED_TEMPLATE_SLOTS})
    if unknown_slots:
        allowed_txt = ", ".join(f"{{{{{s}}}}}" for s in sorted(ALLOWED_TEMPLATE_SLOTS))
        raise MetricInvalid(
            f"「SQL 模板」使用了不支持的占位符：{', '.join(unknown_slots)}；仅支持：{allowed_txt}",
            field="sql_template",
        )

    cache = ds.schema_cache if isinstance(ds.schema_cache, Mapping) else {}
    tables = cache.get("tables") if isinstance(cache, Mapping) else None
    has_schema = isinstance(tables, Mapping) and bool(tables)

    # 先预编译两种典型形态（无筛选汇总 + 带日期/维度切片），双重验证语法与只读安全守卫
    draft_metric = {
        "key": metric_key,
        "name": metric_key,
        "calc_mode": "sql_template",
        "table_name": raw_table or "_anchor",
        "sql_expression": sql_expression,
        "sql_template": clean_tpl,
        "date_column": date_column,
        "filter_sql": filter_sql,
        "dimensions": dims,
        "default_time_grain": grain,
    }
    test_variants: list[dict[str, Any]] = [
        {"date_from": None, "date_to": None, "group_by_date": False, "group_by": []},
    ]
    if date_column or dims:
        test_variants.append(
            {
                "date_from": "2026-01-01" if date_column else None,
                "date_to": "2026-01-07" if date_column else None,
                "group_by_date": bool(date_column),
                "group_by": [dims[0]["key"]] if dims else [],
            }
        )

    referenced_physical_tables: list[str] = []
    all_valid_cols: set[str] = set()
    read_dialect = dialect or "mysql"

    for v_idx, variant in enumerate(test_variants):
        compiled_sql, _ = compile_metric_sql(
            draft_metric,
            source=ds,
            entities=(),
            date_from=variant["date_from"],
            date_to=variant["date_to"],
            group_by_date=variant["group_by_date"],
            group_by=variant["group_by"],
            limit=50,
        )
        try:
            checked_stmt = guard_check(compiled_sql, readonly=True, dialect=dialect, source_name=ds.name)
        except SqlRejected as exc:
            raise MetricInvalid(f"「SQL 模板」未通过只读安全守卫：{exc}", field="sql_template") from exc
        verb = first_verb(checked_stmt, dialect=dialect)
        if verb not in ("select", "with"):
            raise MetricInvalid("「SQL 模板」必须是只读的 SELECT 或带 WITH 子句的查询语句", field="sql_template")

        try:
            parsed = sqlglot.parse_one(checked_stmt, read=read_dialect)
        except Exception:  # noqa: BLE001
            try:
                parsed = sqlglot.parse_one(checked_stmt)
            except Exception as exc2:  # noqa: BLE001
                mode_label = "汇总态" if v_idx == 0 else "按日期或维度分组态"
                raise MetricInvalid(f"「SQL 模板」在{mode_label}下编译出的 SQL 语法有误：{exc2}", field="sql_template") from exc2

        cte_names = {name_key(str(cte.alias_or_name or "")) for cte in parsed.find_all(exp.CTE) if cte.alias_or_name}
        cte_names.add(name_key("_metric_src"))
        cte_names.add(name_key("metric_src"))
        if read_dialect == "oracle":
            cte_names.add(name_key("dual"))

        if v_idx == 0:
            for tbl_node in parsed.find_all(exp.Table):
                t_raw = str(tbl_node.name or "").strip()
                if not t_raw or name_key(t_raw) in cte_names:
                    continue
                db_part = str(tbl_node.db or "").strip()
                qualified = f"{db_part}.{t_raw}" if db_part else t_raw
                if has_schema:
                    resolved = catalog.resolve_table_name(cache, qualified) or catalog.resolve_table_name(cache, t_raw)
                    if resolved is None:
                        raise MetricInvalid(
                            f"「SQL 模板」引用了数据源「{ds.name}」中不存在的物理表「{qualified}」",
                            field="sql_template",
                        )
                    if resolved not in referenced_physical_tables:
                        referenced_physical_tables.append(resolved)
                    tinfo = (tables or {}).get(resolved) or {}
                    for c in tinfo.get("columns") or []:
                        if isinstance(c, Mapping) and c.get("name"):
                            all_valid_cols.add(name_key(str(c["name"])))
                else:
                    if qualified not in referenced_physical_tables:
                        referenced_physical_tables.append(qualified)

        derived_aliases = {
            name_key(str(al.alias or ""))
            for al in parsed.find_all(exp.Alias)
            if str(al.alias or "").strip()
        }
        derived_aliases.update(name_key(str(d.get("key") or "")) for d in dims if d.get("key"))
        for col_node in parsed.find_all(exp.Column):
            cname = str(col_node.name or "").strip()
            if not cname or cname == "*":
                continue
            if read_dialect == "oracle" and cname.lower() in _ORACLE_PSEUDOCOLUMNS:
                continue
            ckey = name_key(cname)
            if masked_set and ckey in masked_set:
                raise MetricInvalid(
                    f"「SQL 模板」引用了数据源敏感遮罩列「{cname}」，不允许用于指标定义",
                    field="sql_template",
                )
            if has_schema and all_valid_cols and ckey not in all_valid_cols and ckey not in derived_aliases:
                if cname.lower() not in ("period", "stat_date", metric_key.lower()):
                    raise MetricInvalid(
                        f"「SQL 模板」引用了所涉物理表中不存在的列「{cname}」",
                        field="sql_template",
                    )

    if raw_table.strip():
        primary_table, _ = _resolve_table_and_columns(ds, raw_table)
    elif referenced_physical_tables:
        primary_table = referenced_physical_tables[0]
    else:
        raise MetricInvalid("「SQL 模板」至少需要引用一张物理业务表", field="sql_template")

    effective_expr = sql_expression.strip()
    if not effective_expr:
        joined_hint = " + ".join(referenced_physical_tables[:3])
        if len(referenced_physical_tables) > 3:
            joined_hint += f" 等 {len(referenced_physical_tables)} 表"
        effective_expr = f"[多表模板] {joined_hint or primary_table}"
    return clean_tpl, primary_table, effective_expr


async def _normalize_metric_payload(
    session: AsyncSession,
    payload: Mapping[str, Any],
    *,
    existing: SemanticMetric | None = None,
) -> tuple[DataSource, dict[str, Any]]:
    source_ref = payload.get("source_id") if "source_id" in payload else (existing.source_id if existing else "")
    ds = await _resolve_source(session, str(source_ref or ""))
    masked_set = {name_key(c) for c in masked_columns(ds.options)}
    dialect = dialect_of(ds.kind)

    key = normalize_metric_key(
        payload.get("key") if "key" in payload else (existing.key if existing else ""),
        field="key",
    )
    name = str(payload.get("name") if "name" in payload else (existing.name if existing else "")).strip()
    if not name:
        raise MetricInvalid("指标名称（name）不能为空", field="name")
    if len(name) > 200:
        raise MetricInvalid("指标名称不能超过 200 字符", field="name")

    raw_tpl = (
        payload.get("sql_template")
        if "sql_template" in payload
        else (getattr(existing, "sql_template", "") if existing else "")
    )
    raw_mode = (
        payload.get("calc_mode")
        if "calc_mode" in payload
        else (getattr(existing, "calc_mode", "expression") if existing else "")
    )
    calc_mode = str(raw_mode or "").strip().lower()
    if not calc_mode:
        calc_mode = "sql_template" if str(raw_tpl or "").strip() else "expression"
    if calc_mode not in CALC_MODES:
        raise MetricInvalid(f"计算模式「{calc_mode}」不支持，可选：{', '.join(CALC_MODES)}", field="calc_mode")

    raw_table = str(
        payload.get("table_name") if "table_name" in payload else (existing.table_name if existing else "")
    ).strip()
    raw_expr = str(
        payload.get("sql_expression") if "sql_expression" in payload else (existing.sql_expression if existing else "")
    ).strip()
    raw_date = str(
        payload.get("date_column") if "date_column" in payload else (existing.date_column if existing else "")
    ).strip()
    raw_filter = str(
        payload.get("filter_sql") if "filter_sql" in payload else (existing.filter_sql if existing else "")
    ).strip()

    grain = str(
        payload.get("default_time_grain")
        if "default_time_grain" in payload
        else (existing.default_time_grain if existing else "day")
    ).strip().lower() or "day"
    if grain not in TIME_GRAINS:
        raise MetricInvalid(f"默认时间粒度「{grain}」不支持，可选：{', '.join(TIME_GRAINS)}", field="default_time_grain")

    raw_dims = payload.get("dimensions") if "dimensions" in payload else (existing.dimensions if existing else [])
    if raw_dims is None:
        raw_dims = []
    if not isinstance(raw_dims, list):
        raise MetricInvalid("维度列表（dimensions）需要是数组", field="dimensions")

    is_tpl = calc_mode == "sql_template"
    dims: list[dict[str, Any]] = []
    seen_dim_keys: set[str] = set()
    for idx, item in enumerate(raw_dims):
        norm = normalize_dimension(item, index=idx, masked_cols=masked_set, allow_qualified=is_tpl)
        if norm["key"] in seen_dim_keys:
            raise MetricInvalid(f"维度标识「{norm['key']}」重复", field=f"dimensions[{idx}].key")
        seen_dim_keys.add(norm["key"])
        dims.append(norm)

    if not is_tpl:
        table_name, known_cols = _resolve_table_and_columns(ds, raw_table)
        sql_expression = validate_sql_fragment(
            raw_expr,
            dialect=dialect,
            field="sql_expression",
            is_filter=False,
            allow_empty=False,
            masked_cols=masked_set,
            known_cols=known_cols,
            table_name=table_name,
        )
        date_column = ""
        if raw_date:
            date_column = _validate_column_identifier(raw_date, field="date_column", masked_cols=masked_set)
            if known_cols and name_key(date_column) not in known_cols:
                raise MetricInvalid(f"表「{table_name}」中不存在时间列「{date_column}」", field="date_column")
        filter_sql = validate_sql_fragment(
            raw_filter,
            dialect=dialect,
            field="filter_sql",
            is_filter=True,
            allow_empty=True,
            masked_cols=masked_set,
            known_cols=known_cols,
            table_name=table_name,
        )
        for idx, norm in enumerate(dims):
            if known_cols and name_key(norm["column"]) not in known_cols:
                raise MetricInvalid(f"表「{table_name}」中不存在维度列「{norm['column']}」", field=f"dimensions[{idx}].column")
        sql_template = ""
    else:
        date_column = ""
        if raw_date:
            date_column = _validate_column_identifier(
                raw_date, field="date_column", masked_cols=masked_set, allow_qualified=True
            )
        filter_sql = validate_sql_fragment(
            raw_filter,
            dialect=dialect,
            field="filter_sql",
            is_filter=True,
            allow_empty=True,
            masked_cols=masked_set,
            known_cols=None,
            table_name=raw_table,
        )
        # 若用户在无占位符的基础多表查询之上另填了外层聚合表达式，也校验其聚合安全性（跳过以 [多表模板] 开头的自动摘要）
        clean_outer_expr = ""
        if raw_expr and not raw_expr.startswith("[多表模板]") and not _SLOT_TOKEN_RE.search(str(raw_tpl or "")):
            clean_outer_expr = validate_sql_fragment(
                raw_expr,
                dialect=dialect,
                field="sql_expression",
                is_filter=False,
                allow_empty=True,
                masked_cols=masked_set,
                known_cols=None,
                table_name=raw_table,
            )
        sql_template, table_name, sql_expression = _validate_and_analyze_sql_template(
            str(raw_tpl or ""),
            ds=ds,
            dialect=dialect,
            masked_set=masked_set,
            metric_key=key,
            raw_table=raw_table,
            sql_expression=clean_outer_expr,
            date_column=date_column,
            filter_sql=filter_sql,
            dims=dims,
            grain=grain,
        )

    aliases = _clean_aliases(
        payload.get("aliases") if "aliases" in payload else (existing.aliases if existing else []),
        field="aliases",
    )
    description = str(
        payload.get("description") if "description" in payload else (existing.description if existing else "")
    ).strip()[:2000]
    unit = str(payload.get("unit") if "unit" in payload else (existing.unit if existing else "")).strip()[:40]

    status = str(
        payload.get("status") if "status" in payload else (existing.status if existing else "enabled")
    ).strip().lower() or "enabled"
    if status not in METRIC_STATUSES:
        raise MetricInvalid(f"指标状态「{status}」不支持，可选：{', '.join(METRIC_STATUSES)}", field="status")

    source_origin = str(
        payload.get("source") if "source" in payload else (existing.source if existing else "human")
    ).strip().lower() or "human"
    if source_origin not in METRIC_SOURCES:
        source_origin = "human"

    return ds, {
        "key": key,
        "name": name,
        "aliases": aliases,
        "description": description,
        "unit": unit,
        "source_id": ds.id,
        "calc_mode": calc_mode,
        "table_name": table_name,
        "sql_expression": sql_expression,
        "sql_template": sql_template,
        "date_column": date_column,
        "filter_sql": filter_sql,
        "dimensions": dims,
        "default_time_grain": grain,
        "status": status,
        "source": source_origin,
    }


async def list_metrics(
    session: AsyncSession,
    *,
    source_id: str | None = None,
    status: str | None = None,
    q: str | None = None,
) -> list[SemanticMetric]:
    stmt = select(SemanticMetric).order_by(SemanticMetric.updated_at.desc(), SemanticMetric.key.asc())
    if source_id:
        stmt = stmt.where(SemanticMetric.source_id == source_id.strip())
    if status:
        stmt = stmt.where(SemanticMetric.status == status.strip())
    rows = list((await session.execute(stmt)).scalars().all())
    if q and q.strip():
        needle = q.strip().lower()
        rows = [
            r
            for r in rows
            if needle in (r.key or "").lower()
            or needle in (r.name or "").lower()
            or needle in (r.description or "").lower()
            or needle in (r.table_name or "").lower()
            or any(needle in str(a).lower() for a in (r.aliases or []))
        ]
    return rows


async def get_metric(session: AsyncSession, id_or_key: str) -> SemanticMetric:
    clean = str(id_or_key or "").strip()
    if not clean:
        raise MetricNotFound("未指定指标标识")
    row = await session.get(SemanticMetric, clean)
    if row is None:
        row = (
            await session.execute(
                select(SemanticMetric).where(or_(SemanticMetric.key == clean.lower(), SemanticMetric.name == clean))
            )
        ).scalars().first()
    if row is None:
        raise MetricNotFound(f"找不到语义指标「{clean}」")
    return row


async def create_metric(
    session: AsyncSession,
    payload: Mapping[str, Any],
    *,
    actor: str | None = None,
) -> tuple[SemanticMetric, DataSource]:
    ds, fields = await _normalize_metric_payload(session, payload, existing=None)
    dup = (await session.execute(select(SemanticMetric).where(SemanticMetric.key == fields["key"]))).scalar_one_or_none()
    if dup is not None:
        raise MetricConflict(f"指标标识「{fields['key']}」已存在，请换用其他标识或直接编辑现有指标", field="key")
    row = SemanticMetric(
        **fields,
        version=1,
        created_by=actor,
        updated_by=actor,
    )
    session.add(row)
    await session.flush()
    return row, ds


async def update_metric(
    session: AsyncSession,
    metric_id: str,
    payload: Mapping[str, Any],
    *,
    actor: str | None = None,
) -> tuple[SemanticMetric, DataSource]:
    row = await get_metric(session, metric_id)
    expected_version = payload.get("expected_version")
    if expected_version is not None and int(expected_version) != int(row.version or 1):
        raise MetricConflict("该指标已被其他人修改，请刷新后重试", field="version")
    ds, fields = await _normalize_metric_payload(session, payload, existing=row)
    if fields["key"] != row.key:
        dup = (
            await session.execute(select(SemanticMetric).where(SemanticMetric.key == fields["key"], SemanticMetric.id != row.id))
        ).scalar_one_or_none()
        if dup is not None:
            raise MetricConflict(f"指标标识「{fields['key']}」已被其他指标占用", field="key")
    for k, v in fields.items():
        setattr(row, k, v)
    row.version = int(row.version or 1) + 1
    row.updated_by = actor
    await session.flush()
    return row, ds


async def delete_metric(session: AsyncSession, metric_id: str) -> SemanticMetric:
    row = await get_metric(session, metric_id)
    await session.delete(row)
    await session.flush()
    return row


# --------------------------------------------------------------------------
# 确定性 SQL 编译与业务实体编码翻译
# --------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _oracle_reserved_words() -> frozenset[str]:
    try:
        return frozenset(str(w).lower() for w in oracle_dialect.dialect().identifier_preparer._reserved_words)
    except Exception:  # noqa: BLE001
        return frozenset({"date", "level", "number", "uid", "user", "comment", "group", "order", "size", "mode", "share"})


def _quote_single_part(part: str, kind: str | None, *, force_quote: bool = False, is_alias: bool = False) -> str:
    clean = str(part or "").strip()
    if not clean:
        return ""
    k = (kind or "").strip().lower()
    if k == "oracle":
        if force_quote:
            return '"' + clean.replace('"', '""') + '"'
        if _ORACLE_SIMPLE_IDENT_RE.match(clean):
            if clean.lower() in _oracle_reserved_words():
                val = clean if is_alias else clean.upper()
                return '"' + val.replace('"', '""') + '"'
            return clean
        return '"' + clean.replace('"', '""') + '"'
    q = "`" if k in ("mysql", "mariadb") else '"'
    return f"{q}{clean.replace(q, q + q)}{q}"


def _quote_ident(ident: str, kind: str | None, *, is_alias: bool = False) -> str:
    parts = [p.strip() for p in str(ident or "").split(".") if p.strip()]
    return ".".join(_quote_single_part(p, kind, is_alias=is_alias and idx == len(parts) - 1) for idx, p in enumerate(parts))


def _format_table_ref(source: DataSource, table_name: str) -> str:
    clean = str(table_name or "").strip()
    if not clean:
        return ""
    cache = source.schema_cache if isinstance(source.schema_cache, Mapping) else {}
    tables = cache.get("tables") if isinstance(cache, Mapping) else None
    if isinstance(tables, Mapping) and tables:
        resolved = catalog.resolve_table_name(cache, clean)
        if resolved and resolved in tables:
            meta = tables.get(resolved) or {}
            force_t = bool(meta.get("quote_name") is True)
            if "." in resolved:
                schema_part, tbl_part = resolved.split(".", 1)
            else:
                schema_part = str(meta.get("schema") or "").strip()
                tbl_part = resolved
            tbl_sql = _quote_single_part(tbl_part, source.kind, force_quote=force_t)
            if schema_part:
                return f"{_quote_single_part(schema_part, source.kind)}.{tbl_sql}"
            return tbl_sql
    return _quote_ident(clean, source.kind)


def _lookup_column_meta(source: DataSource, table_name: str, column_name: str) -> Mapping[str, Any] | None:
    cache = source.schema_cache if isinstance(source.schema_cache, Mapping) else {}
    tables = cache.get("tables") if isinstance(cache, Mapping) else None
    if not isinstance(tables, Mapping) or not tables:
        return None
    target = name_key(str(column_name or "").rsplit(".", 1)[-1])
    if not target:
        return None
    if table_name:
        resolved = catalog.resolve_table_name(cache, table_name) or table_name
        tinfo = tables.get(resolved) or {}
        for col in tinfo.get("columns") or []:
            if isinstance(col, Mapping) and name_key(str(col.get("name") or "")) == target:
                return col
    for tinfo in tables.values():
        if not isinstance(tinfo, Mapping):
            continue
        for col in tinfo.get("columns") or []:
            if isinstance(col, Mapping) and name_key(str(col.get("name") or "")) == target:
                return col
    return None


def _format_col_ref(source: DataSource, table_name: str, col_expr: str) -> str:
    parts = [p.strip() for p in str(col_expr or "").split(".") if p.strip()]
    if not parts:
        return ""
    col_meta = _lookup_column_meta(source, table_name, parts[-1])
    force_col = bool(col_meta and col_meta.get("quote_name") is True)
    col_name = str(col_meta["name"]) if (force_col and col_meta and col_meta.get("name")) else parts[-1]
    if len(parts) == 1:
        return _quote_single_part(col_name, source.kind, force_quote=force_col)
    prefix = ".".join(_quote_single_part(p, source.kind) for p in parts[:-1])
    return f"{prefix}.{_quote_single_part(col_name, source.kind, force_quote=force_col)}"


def _sql_literal(val: Any) -> str:
    if isinstance(val, bool):
        return "1" if val else "0"
    if isinstance(val, (int, float)):
        return str(val)
    text = str(val if val is not None else "")
    escaped = text.replace("'", "''")
    return f"'{escaped}'"


def _column_temporal_kind(source: DataSource, table_name: str, column_name: str) -> str:
    """识别业务时间列的底层存储形态：text | datetime_text | date | datetime。
    注意：Oracle 的 DATE 类型自带时分秒（YYYY-MM-DD HH24:MI:SS），必须按 datetime 处理。
    """
    k = (source.kind or "").strip().lower()
    col_meta = _lookup_column_meta(source, table_name, column_name)
    ctype = str((col_meta or {}).get("type") or "").lower()
    if k == "sqlite":
        if "timestamp" in ctype or "datetime" in ctype:
            return "datetime_text"
        return "text"
    if ctype:
        if any(tok in ctype for tok in ("char", "text", "clob", "string")):
            return "text"
        if k == "oracle":
            if "date" in ctype or "timestamp" in ctype:
                return "datetime"
        else:
            if "timestamp" in ctype or "datetime" in ctype:
                return "datetime"
            if "date" in ctype:
                return "date"
    if k == "oracle":
        return "datetime"
    if k in ("postgres", "postgresql", "mysql", "mariadb"):
        return "date"
    return "text"


def _date_expr_for_grain(quoted_col: str, grain: str, kind: str | None, *, temporal_kind: str = "") -> str:
    k = (kind or "").strip().lower()
    if k == "oracle":
        if temporal_kind == "text":
            if grain == "month":
                return f"SUBSTR({quoted_col}, 1, 7)"
            if grain == "hour":
                return f"SUBSTR({quoted_col}, 1, 13) || ':00'"
            return f"SUBSTR({quoted_col}, 1, 10)"
        if grain == "month":
            return f"TO_CHAR({quoted_col}, 'YYYY-MM')"
        if grain == "hour":
            return f"TO_CHAR({quoted_col}, 'YYYY-MM-DD HH24') || ':00'"
        return f"TO_CHAR({quoted_col}, 'YYYY-MM-DD')"
    if k in ("postgres", "postgresql"):
        if temporal_kind == "text":
            if grain == "month":
                return f"SUBSTR({quoted_col}, 1, 7)"
            if grain == "hour":
                return f"SUBSTR({quoted_col}, 1, 13) || ':00'"
            return f"SUBSTR({quoted_col}, 1, 10)"
        if grain == "month":
            return f"TO_CHAR({quoted_col}, 'YYYY-MM')"
        if grain == "hour":
            return f"TO_CHAR({quoted_col}, 'YYYY-MM-DD HH24') || ':00'"
        return f"CAST({quoted_col} AS DATE)"
    if k in ("mysql", "mariadb"):
        if grain == "month":
            return f"DATE_FORMAT({quoted_col}, '%Y-%m')"
        if grain == "hour":
            return f"DATE_FORMAT({quoted_col}, '%Y-%m-%d %H:00')"
        return f"DATE({quoted_col})"
    if grain == "month":
        return f"SUBSTR(CAST({quoted_col} AS TEXT), 1, 7)"
    if grain == "hour":
        return f"SUBSTR(CAST({quoted_col} AS TEXT), 1, 13) || ':00'"
    return f"SUBSTR(CAST({quoted_col} AS TEXT), 1, 10)"


def _format_date_bound_sql(bound: str, *, kind: str | None, temporal_kind: str) -> str:
    k = (kind or "").strip().lower()
    if temporal_kind in ("text", "datetime_text") or k == "sqlite":
        return _sql_literal(bound)
    if k == "oracle":
        if _ISO_DATE_RE.match(bound):
            return f"DATE '{bound}'"
        norm_ts = bound.replace("T", " ")
        if len(norm_ts) == 16:
            norm_ts += ":00"
        return f"TO_DATE({_sql_literal(norm_ts)}, 'YYYY-MM-DD HH24:MI:SS')"
    return _sql_literal(bound)


def _validate_date_bound(raw: str, *, field: str) -> str:
    clean = str(raw or "").strip()
    if not clean:
        return ""
    if not re.match(r"^\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}(?::\d{2})?)?$", clean):
        raise MetricInvalid(f"日期参数「{field}={clean}」格式无效，请使用 YYYY-MM-DD", field=field)
    return clean


def _compute_period_days_sql(
    *,
    source: DataSource,
    table_name: str,
    d_from: str,
    d_to: str,
    group_by_date: bool,
    grain: str,
    date_col: str,
    kind: str | None,
) -> str:
    if group_by_date and grain == "day":
        return "1"
    if not group_by_date and d_from and d_to:
        try:
            d1 = date.fromisoformat(d_from[:10])
            d2 = date.fromisoformat(d_to[:10])
            return str(max(1, (d2 - d1).days + 1))
        except ValueError:
            pass
    if date_col:
        t_kind = _column_temporal_kind(source, table_name, date_col)
        q_col = _format_col_ref(source, table_name, date_col)
        day_expr = _date_expr_for_grain(q_col, "day", kind, temporal_kind=t_kind)
        return f"COALESCE(NULLIF(COUNT(DISTINCT {day_expr}), 0), 1)"
    return "1"


def _split_top_level_with_clause(sql: str) -> tuple[str, str]:
    """若 SQL 以顶层 WITH 子句开头，拆分为 (with_prefix, main_select)；否则返回 ('', sql)。
    用于 Oracle 套 ROWNUM 或外层子查询包装时将 WITH 保持在最外层，避免 ORA-32034。
    """
    stripped = sql.strip()
    if not re.match(r"^WITH\b", stripped, re.IGNORECASE):
        return "", stripped
    n = len(stripped)
    i = 0
    depth = 0
    while i < n:
        ch = stripped[i]
        if ch == "'":
            i += 1
            while i < n:
                if stripped[i] == "'":
                    if i + 1 < n and stripped[i + 1] == "'":
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            continue
        if ch in ('"', "`"):
            quote_ch = ch
            i += 1
            while i < n:
                if stripped[i] == quote_ch:
                    if i + 1 < n and stripped[i + 1] == quote_ch:
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            continue
        if ch == "(":
            depth += 1
            i += 1
            continue
        if ch == ")":
            if depth > 0:
                depth -= 1
                if depth == 0:
                    j = i + 1
                    while j < n and stripped[j].isspace():
                        j += 1
                    if j < n and stripped[j] == ",":
                        i = j + 1
                        continue
                    if re.match(r"^(?:SELECT\b|\(\s*SELECT\b)", stripped[j:], re.IGNORECASE):
                        return stripped[: i + 1].rstrip(), stripped[j:].lstrip()
            i += 1
            continue
        i += 1
    return "", stripped


def _apply_row_limit(sql: str, limit: int, kind: str | None, *, force_if_missing: bool = True) -> str:
    clean = sql.strip()
    safe_n = max(1, int(limit))
    k = (kind or "").strip().lower()
    if k == "oracle":
        # Oracle 不支持 LIMIT N（会报 ORA-00933）；统一剥离并转换为兼容 Oracle 11g/19c 的顶层 WITH + ROWNUM 包裹
        m_lim = _TRAILING_LIMIT_RE.search(clean)
        if m_lim:
            safe_n = max(1, int(m_lim.group(1)))
            clean = clean[: m_lim.start()].rstrip()
            force_if_missing = True
        if not force_if_missing or _HAS_ORACLE_ROW_LIMIT_RE.search(clean):
            return clean
        with_prefix, main_select = _split_top_level_with_clause(clean)
        wrapped = f"SELECT * FROM ({main_select}) WHERE ROWNUM <= {safe_n}"
        return f"{with_prefix} {wrapped}" if with_prefix else wrapped

    if not force_if_missing:
        return clean
    if _TRAILING_LIMIT_RE.search(clean) or _HAS_ORACLE_ROW_LIMIT_RE.search(clean):
        return clean
    return f"{clean} LIMIT {safe_n}"


def compile_metric_sql(
    metric: Mapping[str, Any] | SemanticMetric,
    *,
    source: DataSource,
    entities: Sequence[Any] = (),
    date_from: str | None = None,
    date_to: str | None = None,
    group_by_date: bool = False,
    time_grain: str | None = None,
    group_by: Sequence[str] | None = None,
    filters: Mapping[str, Any] | None = None,
    limit: int = 200,
) -> tuple[str, list[dict[str, Any]]]:
    """把语义指标卡片（单表表达式或多表参数化 SQL 模板）与动态查询条件编译为单条确定性只读 SELECT 语句。

    返回 (sql, entity_alignments)。
    """
    m = (
        {
            "key": metric.key,
            "name": metric.name,
            "calc_mode": getattr(metric, "calc_mode", None) or "expression",
            "table_name": metric.table_name,
            "sql_expression": metric.sql_expression,
            "sql_template": getattr(metric, "sql_template", None) or "",
            "date_column": metric.date_column or "",
            "filter_sql": metric.filter_sql or "",
            "dimensions": list(metric.dimensions or []),
            "default_time_grain": metric.default_time_grain or "day",
        }
        if isinstance(metric, SemanticMetric)
        else dict(metric)
    )
    kind = source.kind
    calc_mode = str(m.get("calc_mode") or ("sql_template" if m.get("sql_template") else "expression")).strip().lower()
    table_name = str(m.get("table_name") or "").strip()
    sql_expr = str(m.get("sql_expression") or "").strip()
    sql_template = str(m.get("sql_template") or "").strip().rstrip(";").strip()
    date_col = str(m.get("date_column") or "").strip()
    filter_sql = str(m.get("filter_sql") or "").strip()
    dims = [d for d in (m.get("dimensions") or []) if isinstance(d, Mapping)]

    # 构建维度查找索引（支持按 key / label / column / bare_column / entity_key 引用）
    dim_lookup: dict[str, Mapping[str, Any]] = {}
    for d in dims:
        raw_col = str(d.get("column") or "")
        bare_col = raw_col.rsplit(".", 1)[-1]
        for tok in (d.get("key"), d.get("label"), raw_col, bare_col, d.get("entity_key")):
            s = str(tok or "").strip().lower()
            if s and s not in dim_lookup:
                dim_lookup[s] = d

    # 构建实体索引（key -> CanonicalEntity，以及 col -> CanonicalEntity）
    entity_by_key: dict[str, Any] = {}
    entity_by_col: dict[str, Any] = {}
    for ent in entities or []:
        entity_by_key[str(ent.key).lower()] = ent
        for b in confirmed_bindings(ent):
            if name_key(str(b.get("source_name") or "")) == name_key(source.name):
                b_tbl = name_key(str(b.get("table_name") or "").rsplit(".", 1)[-1])
                c_key = name_key(str(b.get("column_name") or ""))
                if not table_name or b_tbl == name_key(table_name.rsplit(".", 1)[-1]) or calc_mode == "sql_template":
                    entity_by_col.setdefault(c_key, ent)

    dim_selects: list[str] = []
    group_exprs: list[str] = []
    order_parts: list[str] = []
    tpl_order_parts: list[str] = []

    grain = str(time_grain or m.get("default_time_grain") or "day").strip().lower()
    if grain not in TIME_GRAINS or grain == "none":
        grain = "day"

    date_temporal_kind = _column_temporal_kind(source, table_name, date_col) if date_col else "text"

    if group_by_date:
        if not date_col:
            raise MetricInvalid(f"指标「{m.get('key')}」未配置业务时间列（date_column），无法按日期分组", field="group_by_date")
        q_date = _format_col_ref(source, table_name, date_col)
        # 当粒度是 day 且列本身是不含时分秒的纯 DATE 类型（非 Oracle DATE / 非 TIMESTAMP）时直接按原列分组
        if grain == "day" and date_temporal_kind == "date":
            bare_date_lower = date_col.rsplit(".", 1)[-1].lower()
            date_alias = _quote_ident("stat_date" if bare_date_lower != "stat_date" else date_col.rsplit(".", 1)[-1], kind, is_alias=True)
            dim_selects.append(f"{q_date} AS {date_alias}" if bare_date_lower != "stat_date" else q_date)
            group_exprs.append(q_date)
            order_parts.append("1 ASC")
            tpl_order_parts.append(f"{q_date} ASC")
        else:
            d_expr = _date_expr_for_grain(q_date, grain, kind, temporal_kind=date_temporal_kind)
            dim_selects.append(f"{d_expr} AS {_quote_ident('period', kind, is_alias=True)}")
            group_exprs.append(d_expr)
            order_parts.append("1 ASC")
            tpl_order_parts.append(f"{d_expr} ASC")

    selected_dims: list[Mapping[str, Any]] = []
    for raw_g in group_by or []:
        g_tok = str(raw_g or "").strip().lower()
        if not g_tok:
            continue
        matched_dim = dim_lookup.get(g_tok)
        if matched_dim is None:
            avail = "、".join(f"{d.get('key')}({d.get('label')})" for d in dims) or "无已声明维度"
            raise MetricInvalid(f"指标「{m.get('key')}」不支持按「{raw_g}」分组；可用维度：{avail}", field="group_by")
        if matched_dim not in selected_dims:
            selected_dims.append(matched_dim)

    for d in selected_dims:
        raw_d_col = str(d["column"])
        q_col = _format_col_ref(source, table_name, raw_d_col)
        d_key = str(d["key"])
        if raw_d_col.lower() == d_key.lower():
            dim_selects.append(q_col)
        else:
            dim_selects.append(f"{q_col} AS {_quote_ident(d_key, kind, is_alias=True)}")
        group_exprs.append(q_col)

    where_parts: list[str] = []
    if filter_sql:
        where_parts.append(f"({filter_sql})")

    d_from = _validate_date_bound(date_from or "", field="date_from")
    d_to = _validate_date_bound(date_to or "", field="date_to")
    if (d_from or d_to) and not date_col:
        raise MetricInvalid(f"指标「{m.get('key')}」未配置业务时间列，不支持按日期区间筛选", field="date_from")

    if d_from:
        q_date = _format_col_ref(source, table_name, date_col)
        where_parts.append(f"{q_date} >= {_format_date_bound_sql(d_from, kind=kind, temporal_kind=date_temporal_kind)}")
    if d_to:
        q_date = _format_col_ref(source, table_name, date_col)
        if _ISO_DATE_RE.match(d_to) and date_temporal_kind in ("datetime", "datetime_text"):
            next_day = (date.fromisoformat(d_to) + timedelta(days=1)).isoformat()
            where_parts.append(f"{q_date} < {_format_date_bound_sql(next_day, kind=kind, temporal_kind=date_temporal_kind)}")
        else:
            where_parts.append(f"{q_date} <= {_format_date_bound_sql(d_to, kind=kind, temporal_kind=date_temporal_kind)}")

    alignments: list[dict[str, Any]] = []
    for raw_fk, raw_fv in (filters or {}).items():
        if raw_fv is None or raw_fv == "" or raw_fv == []:
            continue
        fk_tok = str(raw_fk or "").strip().lower()
        matched_dim = dim_lookup.get(fk_tok)
        if matched_dim is None:
            avail = "、".join(f"{d.get('key')}({d.get('label')})" for d in dims) or "无已声明过滤维度"
            raise MetricInvalid(f"指标「{m.get('key')}」不支持按「{raw_fk}」过滤；可用维度：{avail}", field="filters")
        col_name = str(matched_dim["column"])
        bare_col_key = name_key(col_name.rsplit(".", 1)[-1])
        ent = (
            entity_by_key.get(str(matched_dim.get("entity_key") or "").lower())
            or entity_by_col.get(name_key(col_name))
            or entity_by_col.get(bare_col_key)
        )
        values_list = list(raw_fv) if isinstance(raw_fv, (list, tuple, set)) else [raw_fv]
        resolved_vals: list[Any] = []
        for v in values_list:
            if ent is not None:
                translated = translate_entity_value(ent, v, to_source=source.name)
                if translated is not None:
                    target_val = (
                        str(translated.get("target_value") or "").strip()
                        if isinstance(translated, Mapping)
                        else str(translated).strip()
                    )
                    if target_val:
                        if target_val != str(v):
                            alignments.append(
                                {
                                    "dimension": matched_dim["key"],
                                    "entity_key": ent.key,
                                    "entity_label": ent.label,
                                    "input": str(v),
                                    "input_value": v,
                                    "translated": [target_val],
                                    "resolved_code": target_val,
                                    "canonical_value": translated.get("canonical", "") if isinstance(translated, Mapping) else "",
                                    "canonical_label": translated.get("label", "") if isinstance(translated, Mapping) else "",
                                }
                            )
                        resolved_vals.append(target_val)
                        continue
            resolved_vals.append(v)
        q_col = _format_col_ref(source, table_name, col_name)
        if len(resolved_vals) == 1:
            where_parts.append(f"{q_col} = {_sql_literal(resolved_vals[0])}")
        else:
            in_list = ", ".join(_sql_literal(x) for x in resolved_vals)
            where_parts.append(f"{q_col} IN ({in_list})")

    safe_limit = max(1, min(int(limit or 200), 1000))
    metric_col_alias = _quote_ident(str(m.get("key") or "metric_value"), kind, is_alias=True)

    if calc_mode == "sql_template" and sql_template:
        slots_used = {tok.group(1).strip().lower() for tok in _SLOT_TOKEN_RE.finditer(sql_template)}
        uses_outer_wrapper = (
            not slots_used
            and bool(sql_expr)
            and not sql_expr.startswith("[多表模板]")
        )
        if uses_outer_wrapper:
            with_prefix, main_select = _split_top_level_with_clause(sql_template)
            select_parts = [*dim_selects, f"({sql_expr}) AS {metric_col_alias}"]
            # 子查询别名不写 AS，兼容 Oracle（Oracle 表或子查询别名写 AS 会报 ORA-00933）
            sql = f"SELECT {', '.join(select_parts)} FROM ({main_select}) metric_src"
            if with_prefix:
                sql = f"{with_prefix} {sql}"
            if where_parts:
                sql += " WHERE " + " AND ".join(where_parts)
            if group_exprs:
                sql += " GROUP BY " + ", ".join(group_exprs)
                if not order_parts:
                    order_parts.append(f"{len(select_parts)} DESC")
            if order_parts:
                sql += " ORDER BY " + ", ".join(order_parts)
            sql = _apply_row_limit(sql, safe_limit, kind, force_if_missing=bool(group_exprs))
            return sql, alignments

        if group_exprs and not tpl_order_parts:
            tpl_order_parts.append(f"{len(dim_selects) + 1} DESC")

        period_days_sql = _compute_period_days_sql(
            source=source,
            table_name=table_name,
            d_from=d_from,
            d_to=d_to,
            group_by_date=group_by_date,
            grain=grain,
            date_col=date_col,
            kind=kind,
        )
        compiled = _SELECT_DIMS_SLOT_RE.sub(
            (f"{', '.join(dim_selects)}, " if dim_selects else ""),
            sql_template,
        )
        compiled = _WHERE_SLOT_RE.sub(
            (f"WHERE {' AND '.join(where_parts)}" if where_parts else ""),
            compiled,
        )
        compiled = _AND_WHERE_SLOT_RE.sub(
            (f"AND ({' AND '.join(where_parts)})" if where_parts else ""),
            compiled,
        )
        compiled = _GROUP_BY_SLOT_RE.sub(
            (f"GROUP BY {', '.join(group_exprs)}" if group_exprs else ""),
            compiled,
        )
        compiled = _ORDER_BY_SLOT_RE.sub(
            (f"ORDER BY {', '.join(tpl_order_parts)}" if tpl_order_parts else ""),
            compiled,
        )
        compiled = _DATE_FROM_SLOT_RE.sub(_sql_literal(d_from) if d_from else "NULL", compiled)
        compiled = _DATE_TO_SLOT_RE.sub(_sql_literal(d_to) if d_to else "NULL", compiled)
        compiled = _PERIOD_DAYS_SLOT_RE.sub(period_days_sql, compiled)
        compiled = _LIMIT_SLOT_RE.sub(str(safe_limit), compiled)
        compiled = re.sub(r"[ \t]+\n", "\n", compiled).strip()
        compiled = _apply_row_limit(
            compiled,
            safe_limit,
            kind,
            force_if_missing=bool(group_exprs and "limit" not in slots_used),
        )
        return compiled, alignments

    select_parts = [*dim_selects, f"({sql_expr}) AS {metric_col_alias}"]
    sql = f"SELECT {', '.join(select_parts)} FROM {_format_table_ref(source, table_name)}"
    if where_parts:
        sql += " WHERE " + " AND ".join(where_parts)
    if group_exprs:
        sql += " GROUP BY " + ", ".join(group_exprs)
        if not order_parts:
            order_parts.append(f"{len(select_parts)} DESC")
    if order_parts:
        sql += " ORDER BY " + ", ".join(order_parts)
    sql = _apply_row_limit(sql, safe_limit, kind, force_if_missing=bool(group_exprs))
    return sql, alignments


async def probe_latest_metric_date(
    source: DataSource,
    *,
    table_name: str,
    date_column: str,
    filter_sql: str = "",
) -> str | None:
    """当某日查询无结果或聚合值为 NULL 时，快速探查底表最新可用业务日期（限时 3s）。"""
    if not date_column or not table_name:
        return None
    bare_date = date_column.rsplit(".", 1)[-1]
    q_date = _format_col_ref(source, table_name, bare_date)
    q_table = _format_table_ref(source, table_name)
    sql = f"SELECT MAX({q_date}) AS latest_date FROM {q_table}"
    if filter_sql and "." not in filter_sql:
        sql += f" WHERE ({filter_sql})"
    try:
        res = await run_query(source, sql, limits=QueryLimits(timeout_seconds=3, max_rows=1))
        if res.rows and res.rows[0] and res.rows[0][0] is not None:
            return str(res.rows[0][0])
    except Exception:  # noqa: BLE001
        return None
    return None


def is_empty_metric_result(rows: Sequence[Sequence[Any]], *, has_groups: bool) -> bool:
    if not rows:
        return True
    if not has_groups and len(rows) == 1 and all(cell is None for cell in rows[0]):
        return True
    return False


async def preview_metric(
    session: AsyncSession,
    *,
    metric_id: str | None = None,
    definition: Mapping[str, Any] | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    group_by_date: bool = False,
    time_grain: str | None = None,
    group_by: Sequence[str] | None = None,
    filters: Mapping[str, Any] | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """在线试算一条已保存或未保存草稿状态的指标定义（搭建者工作台预览用）。"""
    if metric_id:
        row = await get_metric(session, metric_id)
        ds = await _resolve_source(session, row.source_id)
        normalized = metric_to_dict(row, source_name=ds.name)
    elif definition is not None:
        ds, normalized = await _normalize_metric_payload(session, definition, existing=None)
    else:
        raise MetricInvalid("请提供要试算的 metric_id 或 definition")

    entities = await list_entities(session)
    sql, alignments = compile_metric_sql(
        normalized,
        source=ds,
        entities=entities,
        date_from=date_from,
        date_to=date_to,
        group_by_date=group_by_date,
        time_grain=time_grain,
        group_by=group_by,
        filters=filters,
        limit=limit,
    )
    started = time.perf_counter()
    result = await run_query(ds, sql, limits=QueryLimits(timeout_seconds=15, max_rows=max(1, min(int(limit or 50), 200))))
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    payload = result.to_payload()

    has_groups = bool(group_by_date or group_by)
    latest_date: str | None = None
    if is_empty_metric_result(result.rows, has_groups=has_groups) and normalized.get("date_column"):
        latest_date = await probe_latest_metric_date(
            ds,
            table_name=str(normalized["table_name"]),
            date_column=str(normalized["date_column"]),
            filter_sql=str(normalized.get("filter_sql") or ""),
        )

    return {
        "sql": sql,
        "source_name": ds.name,
        "columns": payload["columns"],
        "rows": payload["rows"],
        "row_count": payload["row_count"],
        "truncated": payload["truncated"],
        "elapsed_ms": elapsed_ms,
        "entity_alignments": alignments,
        "latest_available_date": latest_date,
    }


# --------------------------------------------------------------------------
# AI 辅助从数据目录与业务文档批量提取指标卡片
# --------------------------------------------------------------------------

SUGGEST_SYSTEM_PROMPT = """你是 AgentLab 的「语义指标建模专家」。请根据给定的数据源物理表结构、数据目录（中文名、粒度、业务日期、有效过滤条件、列角色与符号映射）以及可选的业务指标文档，提取高价值、可直接查询的「独立语义指标卡片（SemanticMetric）」。

硬性纪律：
1. 只输出一个合法 JSON 对象，格式为 {"metrics": [...]}，不要输出任何额外解释文字。
2. 表名（table_name）、时间列（date_column）、维度列（dimensions[].column）及表达式或模板引用的所有物理表名与列名，必须 100% 逐字存在于输入提供的表结构中，严禁臆造不存在的表或列。若文档里某指标注明「暂无数据源」，不要生成该指标。
3. 标有「[遮罩列]」的敏感列严禁出现在 sql_expression、sql_template、filter_sql 或 dimensions 中。
4. 支持两种计算模式（calc_mode）：
   - 单表模式（"calc_mode": "expression"，默认优先使用）：指标可由单张物理表聚合得到时使用。sql_expression 写单表聚合表达式（如 SUM(net_visitors)、ROUND(SUM(qualified_qty) * 100.0 / NULLIF(SUM(total_qty), 0), 2)），严禁写完整 SELECT 或子查询；sql_template 置空串 ""。注意文档提示的存储陷阱（如某比率列已存为百分数 99.65 时，切勿再乘 100 或重复除 100）。
   - 多表 / 参数化模板模式（"calc_mode": "sql_template"）：当口径依赖跨表 JOIN、WITH CTE 预聚合、兜底系数（如 COALESCE(cf.conv_factor, 0.5)）或动态统计周期天数（如 TEEP 自然日历秒数 = {{period_days}} * 86400 * COUNT(DISTINCT s.line_code)）时使用！
     · sql_template 写单条只读 SELECT 或 WITH ... SELECT 语句，不含分号与 SQL 注释；
     · 仅允许使用以下安全槽位：{{select_dims}}（放在 SELECT 开头用于动态展开分组列）、{{and_where}}（放在 WHERE 1=1 之后承接日期与维度筛选）、{{where}}、{{group_by}}、{{order_by}}、{{period_days}}、{{date_from}}、{{date_to}}、{{limit}}；
     · 多表模板下，date_column 与 dimensions[].column 必须带主表别名前缀（如 "s.stat_date"、"s.line_code"），table_name 填主事实表名；若表旁标注了「SQL 全名：<schema.table>」，在 sql_template 的 FROM / JOIN 中务必使用该带 schema 前缀的全名；
     · 严格遵循目标数据源方言语法：例如当方言为 oracle 时，严禁写 LIMIT（行数限制由系统自动套外层 ROWNUM）、严禁写 CAST(... AS TEXT)（转字符用 TO_CHAR、转数值用 TO_NUMBER）、表或子查询别名不要写 AS（写 FROM tbl t）。
5. 若数据目录注明某度量列为「存量（stock，不可跨期直接求和）」，优先使用 AVG(...) 或在 description 中明确标注「存量指标，多日查询请开启按日分组查看趋势」。
6. 若物理列旁标注了「已绑定标准实体：<entity_key>」，在对应 dimension 的 entity_key 字段填入该实体键。
7. 避免与「已存在的指标标识」重复；提取文档或目录中最核心的 4～16 个业务指标（单表与多表复杂指标均可覆盖）。

单表指标示例：
{
  "key": "park_daily_visitors",
  "name": "园区日客流（去重口径）",
  "aliases": ["客流", "总客流"],
  "description": "统计每日有效去重客流人次",
  "unit": "人次",
  "calc_mode": "expression",
  "table_name": "park_daily_traffic",
  "sql_expression": "SUM(net_visitors)",
  "sql_template": "",
  "date_column": "stat_date",
  "filter_sql": "",
  "dimensions": [{"key": "zone", "label": "区域", "column": "zone_code", "entity_key": ""}]
}

多表 / 参数化 SQL 模板指标示例：
{
  "key": "oee_multi_table",
  "name": "设备综合效率 OEE（跨表精算）",
  "aliases": ["OEE", "综合效率"],
  "description": "联合班次日报、不合格品记录与折算系数表精算 OEE；含判异红线说明",
  "unit": "%",
  "calc_mode": "sql_template",
  "table_name": "mes_shift_daily",
  "sql_expression": "",
  "sql_template": "WITH cf AS (SELECT prod_code, AVG(conv_factor) AS conv_factor FROM dim_carrier_factor GROUP BY prod_code), sc AS (SELECT line_code, stat_date, SUM(scrap_qty) AS bad_qty FROM fact_scrap_record GROUP BY line_code, stat_date) SELECT {{select_dims}} ROUND(SUM(s.good_qty * COALESCE(cf.conv_factor, 0.5)) * 100.0 / NULLIF(SUM(s.good_qty + COALESCE(sc.bad_qty, 0)), 0), 2) AS oee_multi_table FROM mes_shift_daily s LEFT JOIN cf ON cf.prod_code = s.prod_code LEFT JOIN sc ON sc.line_code = s.line_code AND sc.stat_date = s.stat_date WHERE 1=1 {{and_where}} {{group_by}} {{order_by}}",
  "date_column": "s.stat_date",
  "filter_sql": "",
  "dimensions": [{"key": "line_code", "label": "产线", "column": "s.line_code", "entity_key": ""}]
}
"""


def _parse_suggested_metrics(raw_text: str) -> list[dict[str, Any]]:
    text = (raw_text or "").strip()
    if not text:
        return []
    m = _FENCE_RE.search(text)
    candidate = m.group(1).strip() if m else text
    for snippet in (candidate, text):
        start = snippet.find("{")
        end = snippet.rfind("}")
        if start >= 0 and end > start:
            try:
                obj = json.loads(snippet[start : end + 1])
                if isinstance(obj, Mapping) and isinstance(obj.get("metrics"), list):
                    return [x for x in obj["metrics"] if isinstance(x, Mapping)]
            except json.JSONDecodeError:
                continue
    return []


async def suggest_metrics_from_source(
    session: AsyncSession,
    *,
    source_id: str,
    table_names: Sequence[str] | None = None,
    document_ids: Sequence[str] | None = None,
    note: str = "",
) -> dict[str, Any]:
    """基于数据源目录、标准业务实体绑定与可选知识库文档，智能生成已通过语法与物理列核验的候选指标卡片。"""
    ds = await _resolve_source(session, source_id)
    cache = ds.schema_cache if isinstance(ds.schema_cache, Mapping) else {}
    tables = cache.get("tables") if isinstance(cache, Mapping) else None
    if not isinstance(tables, Mapping) or not tables:
        raise MetricInvalid(f"数据源「{ds.name}」尚未探查表结构，请先在数据源设置中探查结构", field="source_id")

    entries = await catalog.read_catalog(session, ds.id)
    masked_set = {name_key(c) for c in masked_columns(ds.options)}
    entities = await list_entities(session)
    existing = await list_metrics(session, source_id=ds.id)
    existing_keys = {m.key for m in existing}

    # (table_short_key, col_key) -> entity_key
    col_entity: dict[tuple[str, str], str] = {}
    for ent in entities:
        for b in confirmed_bindings(ent):
            if name_key(str(b.get("source_name") or "")) == name_key(ds.name):
                t_short = name_key(str(b.get("table_name") or "").rsplit(".", 1)[-1])
                c_key = name_key(str(b.get("column_name") or ""))
                col_entity[(t_short, c_key)] = ent.key

    selected_tables: list[str] = []
    if table_names:
        for raw_t in table_names:
            resolved = catalog.resolve_table_name(cache, str(raw_t))
            if resolved and resolved not in selected_tables:
                selected_tables.append(resolved)
    else:
        # 优先排有数据目录说明或名称含 stat/daily/fact/order/summary/detail 的业务表（最多 30 张控制上下文）
        def _table_priority(tname: str) -> tuple[int, str]:
            has_cat = 0 if (tname in entries and catalog.visible_notes(entries[tname].notes)) else 1
            return (has_cat, tname)

        selected_tables = sorted(tables.keys(), key=_table_priority)[:30]

    lines: list[str] = [f"数据源：{ds.name}（方言：{ds.kind}）"]
    if (ds.kind or "").strip().lower() == "oracle":
        lines.append("方言注意：Oracle 严禁写 LIMIT、严禁写 CAST(x AS TEXT)、表或子查询别名不写 AS；动态周期天数请直接用 {{period_days}} 槽位。")
    if existing_keys:
        lines.append(f"已存在的指标标识（请勿重复）：{', '.join(sorted(existing_keys))}")
    lines.append("\n【物理表与数据目录】")
    for tname in selected_tables:
        tinfo = tables.get(tname) or {}
        notes = catalog.visible_notes(entries[tname].notes) if tname in entries else {}
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
        col_descs: list[str] = []
        for col in (tinfo.get("columns") or [])[:45]:
            if not isinstance(col, Mapping) or not col.get("name"):
                continue
            cname = str(col["name"])
            ctype = str(col.get("type") or "")
            if name_key(cname) in masked_set:
                col_descs.append(f"  · {cname} ({ctype}) [遮罩列]")
                continue
            cn = col_notes.get(cname) if isinstance(col_notes, Mapping) else None
            c_label = (cn.get("label") or {}).get("value") if isinstance(cn, Mapping) and isinstance(cn.get("label"), Mapping) else ""
            c_unit = (cn.get("unit") or {}).get("value") if isinstance(cn, Mapping) and isinstance(cn.get("unit"), Mapping) else ""
            c_measure = (cn.get("measure") or {}).get("value") if isinstance(cn, Mapping) and isinstance(cn.get("measure"), Mapping) else ""
            ent_key = col_entity.get((t_short, name_key(cname)), "")
            extra_bits = [b for b in (
                c_label,
                f"单位：{c_unit}" if c_unit else "",
                f"度量：{c_measure}" if c_measure else "",
                f"已绑定标准实体：{ent_key}" if ent_key else "",
            ) if b]
            suffix = f" — {'，'.join(extra_bits)}" if extra_bits else ""
            col_descs.append(f"  · {cname} ({ctype}){suffix}")
        lines.extend(col_descs)

    doc_titles: list[str] = []
    if document_ids:
        docs = list(
            (await session.execute(select(Document).where(Document.id.in_([str(d) for d in document_ids])))).scalars().all()
        )
        if docs:
            lines.append("\n【参考业务口径文档】")
            budget = 16_000
            for doc in docs:
                body = str(doc.content or "").strip()
                if not body or budget <= 0:
                    continue
                doc_label = str(doc.title or doc.source or doc.id)
                doc_titles.append(doc_label)
                snippet = body[: min(len(body), budget)]
                budget -= len(snippet)
                lines.append(f"--- 文档《{doc_label}》 ---\n{snippet}")

    if note and note.strip():
        lines.append(f"\n【用户补充要求】\n{note.strip()[:1000]}")

    model, model_id = await catalog_assistant.resolve_model(session)
    msgs = [
        SystemMessage(content=SUGGEST_SYSTEM_PROMPT),
        HumanMessage(content="\n".join(lines)),
    ]
    reply = await model.ainvoke(msgs)
    raw_candidates = _parse_suggested_metrics(message_text(reply))

    accepted: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    seen_batch_keys: set[str] = set(existing_keys)
    for raw in raw_candidates:
        candidate = {**raw, "source_id": ds.id, "source": "ai", "status": "enabled"}
        raw_k = str(candidate.get("key") or "").strip().lower()
        if raw_k in seen_batch_keys:
            skipped.append({"key": raw_k, "reason": "与已有指标或同批候选标识重复"})
            continue
        try:
            _, norm = await _normalize_metric_payload(session, candidate, existing=None)
            seen_batch_keys.add(norm["key"])
            accepted.append({**norm, "source_name": ds.name})
        except Exception as exc:  # noqa: BLE001
            skipped.append({"key": raw_k or str(candidate.get("name") or "?"), "reason": str(exc)})

    return {
        "source_id": ds.id,
        "source_name": ds.name,
        "model_id": model_id,
        "documents_used": doc_titles,
        "candidates": accepted,
        "skipped": skipped,
    }
