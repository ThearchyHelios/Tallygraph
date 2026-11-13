"""快速直查（Fast Direct Ask）零延迟数字证据封装。

设计原则：
- 零额外模型调用（0 LLM calls，纯 Python 内存处理 < 5ms），不退回深度模式 25–30s 的二次报告重写；
- 对 Agent 单轮直出文本中的显式标记（`[[v:Q1.r0.col]]`、`[[table:Q1 ...]]`、`[[see:Q1]]`）做容错归一与遮罩守卫，
  杜绝漏写 `.r0.`、列名大小写差异或未解析占位符 `⟦?...⟧` 暴露给用户；
- 对 Agent 未包裹标记但可唯一无歧义回指到未遮罩查询单元格的裸数字（含 Markdown 表格与正文），确定性升级为
  `[[v:Qn.rR.col]]` 引用标记，交由 `compose_doc` 统一生成内容哈希封存的 `report_doc` 工件。
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import re
from typing import Any

from app.core.artifact_store import load as load_artifact
from app.engine.evidence import (
    MARKER_MAX,
    MARKER_RE,
    TABLE_MAX_COLS,
    _INLINE_SEE,
    _SYNTAX_BLANK,
    _structural_number,
    parse_markers,
    placeholder,
    resolve_marker,
    resolve_support,
)
from app.engine.expressions import cell_value, column_kind
from app.engine.issuance import _close, _tolerance, extract_numbers

MASKED_WORD = "已遮罩"

_CELL_REF_RE = re.compile(
    r"^(?P<alias>Q\d+)\.(?:r(?P<row>\d+)|(?P<bare_row>\d+))\.(?P<col>.+)$",
    re.I,
)
_CELL_NOROW_RE = re.compile(r"^(?P<alias>Q\d+)\.(?P<col>[^.]+)$", re.I)
_UNSAFE_COL_RE = re.compile(r"[\[\]|\n\r]")
_FENCE_LINE_RE = re.compile(r"^\s*(`{3,}|~{3,})")
_INLINE_CODE_RE = re.compile(r"`+[^`\n]*`+")
_MD_LINK_URL_RE = re.compile(r"\]\([^\)\n]*\)")
_TABLE_SEP_LINE_RE = re.compile(r"^\s*\|[\s:\-|]+\|\s*$")
_ZERO_ROW_COUNT_RE = re.compile(r"^\s*(?:行|条记录|条数据)")
_NUMERIC_CODE_TEXT_RE = re.compile(r"^[+-]?\d+(?:\.\d+)?$")
_LEADING_ZERO_INT_RE = re.compile(r"^[+-]?0\d+$")
_ID_LIKE_COL_SUFFIXES = ("_id", "_code", "_no", "_key", "_type", "_group", "id", "code", "type", "group")
_CN_MONTH_DAY_RE = re.compile(r"(?:\d{4}\s*年\s*)?\d{1,2}\s*月\s*\d{1,2}\s*[日号]?")
_FORMULA_MUL_DIV_PREFIX_RE = re.compile(r"[×*÷/]\s*$")


@dataclass(frozen=True)
class _CellCandidate:
    alias: str
    query_order: int
    row: int
    col: str
    col_idx: int
    dec_value: Decimal
    row_keys: tuple[str, ...]
    raw_text: str = ""
    is_text_code: bool = False


@dataclass
class _QueryInfo:
    alias: str
    artifact: str
    columns: list[str]
    col_by_lower: dict[str, str]
    rows: list[list[Any]]
    kinds: list[str | None]
    masked_lower: set[str]
    truncated: bool = False


def _to_decimal(val: Any) -> Decimal | None:
    if val is None or isinstance(val, bool):
        return None
    if isinstance(val, Decimal):
        return val if val.is_finite() else None
    if isinstance(val, int):
        return Decimal(val)
    if isinstance(val, float):
        if val != val or val in (float("inf"), float("-inf")):
            return None
        return Decimal(str(val))
    if isinstance(val, str):
        s = val.strip()
        if not s or s == MASKED_WORD:
            return None
        try:
            d = Decimal(s)
            return d if d.is_finite() else None
        except InvalidOperation:
            return None
    return None


def _load_query_infos(
    catalog: dict[str, Any],
    masked_columns: dict[str, Any] | None = None,
    loader: Any = None,
) -> dict[str, _QueryInfo]:
    read = loader or load_artifact
    infos: dict[str, _QueryInfo] = {}
    for alias, entry in catalog.items():
        if not isinstance(entry, dict) or entry.get("kind") != "query":
            continue
        artifact = str(entry.get("artifact") or "")
        if not artifact:
            continue
        try:
            snap = read(artifact)
        except Exception:  # noqa: BLE001
            snap = None
        if not isinstance(snap, dict):
            continue
        cols = [str(c) for c in (snap.get("columns") or [])]
        raw_rows = snap.get("rows") or []
        rows: list[list[Any]] = []
        for r in raw_rows:
            if isinstance(r, list):
                rows.append(r)
            elif isinstance(r, tuple):
                rows.append(list(r))
            elif isinstance(r, dict):
                rows.append([r.get(c) for c in cols])
        kinds = [column_kind(snap, c) for c in cols]
        masked_set: set[str] = set()
        for mc in snap.get("mask_columns") or []:
            if mc:
                masked_set.add(str(mc).lower())
        if masked_columns and artifact in masked_columns:
            extra = masked_columns[artifact]
            for mc in (extra.keys() if isinstance(extra, dict) else extra):
                if mc:
                    masked_set.add(str(mc).lower())
        # 若单元格值在工具边界已被替换为「已遮罩」，整列按遮罩列保护
        for j, c in enumerate(cols):
            if any(j < len(r) and r[j] == MASKED_WORD for r in rows):
                masked_set.add(c.lower())
        infos[alias] = _QueryInfo(
            alias=alias,
            artifact=artifact,
            columns=cols,
            col_by_lower={c.lower(): c for c in cols},
            rows=rows,
            kinds=kinds,
            masked_lower=masked_set,
            truncated=bool(snap.get("truncated") or entry.get("truncated")),
        )
    return infos


def _fenced_line_ranges(text: str) -> list[tuple[int, int]]:
    """返回围栏代码块（```...``` / ~~~...~~~）覆盖的字符区间，代码块内不做标记改写与自动回绑。"""
    ranges: list[tuple[int, int]] = []
    in_fence = False
    fence_char = ""
    fence_len = 0
    fence_start = 0
    pos = 0
    for line in text.splitlines(keepends=True):
        m = _FENCE_LINE_RE.match(line)
        if m:
            run = m.group(1)
            if not in_fence:
                in_fence = True
                fence_char = run[0]
                fence_len = len(run)
                fence_start = pos
            elif run[0] == fence_char and len(run) >= fence_len and line[m.end():].strip() == "":
                in_fence = False
                ranges.append((fence_start, pos + len(line)))
        pos += len(line)
    if in_fence:
        ranges.append((fence_start, len(text)))
    return ranges


def _in_ranges(idx: int, end: int, ranges: list[tuple[int, int]]) -> bool:
    return any(a <= idx and end <= b or (a < end and idx < b) for a, b in ranges)


def _normalize_v_marker(
    marker: dict[str, Any],
    catalog: dict[str, Any],
    qinfos: dict[str, _QueryInfo],
    loader: Any = None,
) -> str:
    ref = str(marker.get("ref") or "").strip()
    conv = marker.get("conv")
    conv_suffix = f"|{conv}" if conv else ""

    alias = ""
    row_idx: int | None = None
    col_raw = ""

    m_full = _CELL_REF_RE.match(ref)
    if m_full:
        q_upper = m_full.group("alias").upper()
        alias = q_upper if q_upper in qinfos else m_full.group("alias")
        row_idx = int(m_full.group("row") if m_full.group("row") is not None else m_full.group("bare_row"))
        col_raw = m_full.group("col").strip()
    else:
        m_short = _CELL_NOROW_RE.match(ref)
        if m_short:
            q_upper = m_short.group("alias").upper()
            qinfo = qinfos.get(q_upper)
            if qinfo and len(qinfo.rows) == 1:
                alias = q_upper
                row_idx = 0
                col_raw = m_short.group("col").strip()

    if alias and alias in qinfos and row_idx is not None:
        qinfo = qinfos[alias]
        exact_col = qinfo.col_by_lower.get(col_raw.lower(), col_raw)
        if exact_col.lower() in qinfo.masked_lower:
            return MASKED_WORD
        candidate_raw = f"[[v:{alias}.r{row_idx}.{exact_col}{conv_suffix}]]"
        if len(candidate_raw) <= MARKER_MAX:
            parsed = parse_markers(candidate_raw)
            if parsed:
                cite = resolve_marker(parsed[0], catalog, cells_allowed=True, loader=loader)
                if cite.get("status") == "resolved":
                    return candidate_raw
                # 若只是换算不兼容（例如文本列写了 |int），退回无换算再试一次
                if conv:
                    plain_raw = f"[[v:{alias}.r{row_idx}.{exact_col}]]"
                    parsed_plain = parse_markers(plain_raw)
                    if parsed_plain:
                        cite_plain = resolve_marker(parsed_plain[0], catalog, cells_allowed=True, loader=loader)
                        if cite_plain.get("status") == "resolved":
                            return plain_raw

    # 原样试试（例如引用的是分析结论 A1.r0.col）
    cite_orig = resolve_marker(marker, catalog, cells_allowed=True, loader=loader)
    if cite_orig.get("status") == "resolved":
        return str(marker["raw"])
    rendered = str(cite_orig.get("rendered") or "")
    if rendered and rendered != placeholder(marker["kind"], marker["body"]):
        return rendered
    return "—"


def _normalize_table_marker(
    marker: dict[str, Any],
    catalog: dict[str, Any],
    qinfos: dict[str, _QueryInfo],
    loader: Any = None,
) -> str:
    ref = str(marker.get("ref") or "").strip()
    q_upper = ref.upper()
    alias = q_upper if q_upper in qinfos else ref
    options = dict(marker.get("options") or {})

    if alias in qinfos:
        qinfo = qinfos[alias]
        if not qinfo.rows:
            return "（未返回记录）"
        if "cols" in options:
            wanted_raw = [c.strip() for c in options["cols"].split(",") if c.strip()]
            valid_cols = [
                qinfo.col_by_lower[c.lower()]
                for c in wanted_raw
                if c.lower() in qinfo.col_by_lower and c.lower() not in qinfo.masked_lower
            ]
            if valid_cols:
                options["cols"] = ",".join(valid_cols[:TABLE_MAX_COLS])
            else:
                options.pop("cols", None)
        else:
            unmasked_cols = [c for c in qinfo.columns if c.lower() not in qinfo.masked_lower]
            if not unmasked_cols:
                return MASKED_WORD
            if len(unmasked_cols) < len(qinfo.columns) or len(unmasked_cols) > TABLE_MAX_COLS:
                options["cols"] = ",".join(unmasked_cols[:TABLE_MAX_COLS])

        opt_str = " ".join(f"{k}={v}" for k, v in options.items())
        body = f"{alias} {opt_str}".strip()
        candidate_raw = f"[[table:{body}]]"
        if len(candidate_raw) <= MARKER_MAX:
            parsed = parse_markers(candidate_raw)
            if parsed:
                cite = resolve_marker(parsed[0], catalog, cells_allowed=True, loader=loader)
                if cite.get("status") == "resolved":
                    return candidate_raw
        # 回退到仅保留安全列的基础整表标记
        safe_cols = [
            c for c in qinfo.columns
            if c.lower() not in qinfo.masked_lower and c.strip() == c and not _UNSAFE_COL_RE.search(c)
        ][:TABLE_MAX_COLS]
        if safe_cols:
            fallback_raw = (
                f"[[table:{alias}]]"
                if len(safe_cols) == len(qinfo.columns)
                else f"[[table:{alias} cols={','.join(safe_cols)}]]"
            )
            parsed_fb = parse_markers(fallback_raw)
            if parsed_fb and resolve_marker(parsed_fb[0], catalog, cells_allowed=True, loader=loader).get("status") == "resolved":
                return fallback_raw

    cite_orig = resolve_marker(marker, catalog, cells_allowed=True, loader=loader)
    if cite_orig.get("status") == "resolved":
        return str(marker["raw"])
    return ""


def _sanitize_existing_markers(
    text: str,
    catalog: dict[str, Any],
    qinfos: dict[str, _QueryInfo],
    loader: Any = None,
) -> str:
    """第一遍：修复与清理模型显式写出的 [[...]] 标记，杜绝未解析占位符和句中 [[see:]] 断句。"""
    # 先移除会造成断句的句中 [[see:...]]（例如「根据 [[see:Q1]]，」->「」）
    fences = _fenced_line_ranges(text)
    markers = parse_markers(text)
    if not markers:
        return text

    out: list[str] = []
    last = 0
    for m in markers:
        start, end = int(m["start"]), int(m["end"])
        if _in_ranges(start, end, fences):
            continue
        out.append(text[last:start])
        kind = m["kind"]
        if kind == "v":
            out.append(_normalize_v_marker(m, catalog, qinfos, loader=loader))
        elif kind == "table":
            out.append(_normalize_table_marker(m, catalog, qinfos, loader=loader))
        elif kind == "see":
            valid_refs = [
                r for r in (m.get("refs") or [])
                if resolve_support(r, catalog, loader=loader).get("status") == "resolved"
            ]
            if valid_refs:
                # 检查周围是否构成句中 _INLINE_SEE 违规，若是则丢弃此标记避免留下「以 为依据」断句
                window = text[max(0, start - 6):min(len(text), end + 6)]
                if _INLINE_SEE.search(window):
                    out.append("")
                else:
                    out.append(f"[[see:{','.join(valid_refs)}]]")
            else:
                out.append("")
        else:
            cite = resolve_marker(m, catalog, cells_allowed=True, loader=loader)
            if cite.get("status") == "resolved":
                out.append(str(m["raw"]))
            else:
                rendered = str(cite.get("rendered") or "")
                if rendered and rendered != placeholder(kind, m["body"]):
                    out.append(rendered)
                elif kind in ("t", "c") and m.get("ref"):
                    out.append(f"`{m['ref']}`")
                else:
                    out.append("")
        last = end
    out.append(text[last:])
    return "".join(out)


def _alias_order(alias: str) -> int:
    m = re.match(r"^Q(\d+)$", alias.strip(), re.I)
    return int(m.group(1)) if m else 0


def _build_cell_candidates(qinfos: dict[str, _QueryInfo]) -> list[_CellCandidate]:
    candidates: list[_CellCandidate] = []
    for q_pos, (alias, qinfo) in enumerate(qinfos.items()):
        q_order = _alias_order(alias) or (q_pos + 1)
        for r_idx, row in enumerate(qinfo.rows):
            row_keys: list[str] = []
            for j, col in enumerate(qinfo.columns):
                if j >= len(row) or col.lower() in qinfo.masked_lower:
                    continue
                val = row[j]
                if val is None or isinstance(val, bool):
                    continue
                s = str(val).strip()
                col_low = col.lower()
                is_id_col = j == 0 or col_low.endswith(_ID_LIKE_COL_SUFFIXES)
                if s and s != MASKED_WORD and (_to_decimal(val) is None or qinfo.kinds[j] == "text" or is_id_col):
                    row_keys.append(s)
                if isinstance(val, str) and re.match(r"^\d{4}-\d{2}(?:-\d{2})?", s):
                    row_keys.append(s[:10])
            keys_tuple = tuple(dict.fromkeys(k for k in row_keys if len(k) >= 2))

            for j, col in enumerate(qinfo.columns):
                if j >= len(row):
                    continue
                if col.lower() in qinfo.masked_lower:
                    continue
                if not col.strip() or col != col.strip() or _UNSAFE_COL_RE.search(col):
                    continue
                kind = qinfo.kinds[j]
                raw_val = row[j]
                if kind == "text":
                    s_val = str(raw_val).strip() if raw_val is not None else ""
                    if not s_val or s_val == MASKED_WORD or not _NUMERIC_CODE_TEXT_RE.match(s_val):
                        continue
                    dec_code = _to_decimal(s_val)
                    if dec_code is None:
                        continue
                    candidates.append(
                        _CellCandidate(
                            alias=alias,
                            query_order=q_order,
                            row=r_idx,
                            col=col,
                            col_idx=j,
                            dec_value=dec_code,
                            row_keys=keys_tuple,
                            raw_text=s_val,
                            is_text_code=True,
                        )
                    )
                    continue
                parsed_val = cell_value(raw_val, kind)
                dec = _to_decimal(parsed_val)
                if dec is None:
                    continue
                candidates.append(
                    _CellCandidate(
                        alias=alias,
                        query_order=q_order,
                        row=r_idx,
                        col=col,
                        col_idx=j,
                        dec_value=dec,
                        row_keys=keys_tuple,
                        raw_text=str(raw_val).strip() if raw_val is not None else "",
                        is_text_code=False,
                    )
                )
    return candidates


def _table_headers_by_line(lines: list[str]) -> dict[int, list[str]]:
    """识别手写 Markdown 表格每行对应的表头列名，辅助同名数值按列对齐消歧。"""
    mapping: dict[int, list[str]] = {}
    i = 0
    n = len(lines)
    while i + 1 < n:
        head = lines[i].strip()
        sep = lines[i + 1].strip()
        if head.startswith("|") and head.endswith("|") and _TABLE_SEP_LINE_RE.match(sep):
            headers = [c.strip().strip("`*") for c in head.strip("|").split("|")]
            k = i + 2
            while k < n and lines[k].lstrip().startswith("|") and not _TABLE_SEP_LINE_RE.match(lines[k]):
                mapping[k] = headers
                k += 1
            i = k
            continue
        i += 1
    return mapping


def _auto_bind_bare_numbers(
    text: str,
    catalog: dict[str, Any],
    qinfos: dict[str, _QueryInfo],
    loader: Any = None,
) -> str:
    """第二遍：对正文中未写标记、但能唯一无歧义匹配未遮罩查询结果单元格的裸数字，自动绑定 [[v:Qn.rR.col]]。"""
    cells = _build_cell_candidates(qinfos)
    if not cells:
        return text

    cell_order = {id(c): idx for idx, c in enumerate(cells)}
    text_code_by_raw: dict[str, list[_CellCandidate]] = {}
    numeric_cells: list[_CellCandidate] = []
    numeric_by_exact: dict[Decimal, list[_CellCandidate]] = {}
    for c in cells:
        if c.is_text_code:
            text_code_by_raw.setdefault(c.raw_text, []).append(c)
        else:
            numeric_cells.append(c)
            numeric_by_exact.setdefault(c.dec_value, []).append(c)

    quant_cache: dict[tuple[str, int], dict[Decimal, list[_CellCandidate]]] = {}

    def _quant_buckets(scale_kind: str, places: int) -> dict[Decimal, list[_CellCandidate]]:
        key = (scale_kind, places)
        cached = quant_cache.get(key)
        if cached is not None:
            return cached
        buckets: dict[Decimal, list[_CellCandidate]] = {}
        quant = Decimal(1).scaleb(-places)
        if scale_kind == "pct":
            for c in numeric_cells:
                q_val = (c.dec_value * Decimal(100)).quantize(quant, rounding=ROUND_HALF_UP)
                buckets.setdefault(q_val, []).append(c)
        else:
            divisor = Decimal(10000) if scale_kind == "万" else Decimal(100_000_000)
            for c in numeric_cells:
                q_val = (c.dec_value / divisor).quantize(quant, rounding=ROUND_HALF_UP)
                buckets.setdefault(q_val, []).append(c)
        quant_cache[key] = buckets
        return buckets

    fences = _fenced_line_ranges(text)
    lines = text.splitlines(keepends=True)
    table_headers = _table_headers_by_line(lines)

    line_offsets: list[int] = []
    acc = 0
    for ln in lines:
        line_offsets.append(acc)
        acc += len(ln)

    out_lines: list[str] = []
    for line_idx, line in enumerate(lines):
        line_start = line_offsets[line_idx]
        line_end = line_start + len(line)
        if _in_ranges(line_start, line_end, fences) or _TABLE_SEP_LINE_RE.match(line):
            out_lines.append(line)
            continue
        # 表头行本身不自动替换列名里的数字
        if (line_idx + 1) in table_headers and line.strip().startswith("|"):
            out_lines.append(line)
            continue

        protected: list[tuple[int, int]] = []
        for m in MARKER_RE.finditer(line):
            protected.append((m.start(), m.end()))
        for m in _INLINE_CODE_RE.finditer(line):
            protected.append((m.start(), m.end()))
        for m in _MD_LINK_URL_RE.finditer(line):
            protected.append((m.start(), m.end()))

        masked_chars = list(line)
        for a, b in protected:
            for k in range(max(0, a), min(len(masked_chars), b)):
                if masked_chars[k] != "\n":
                    masked_chars[k] = " "
        masked_line = "".join(masked_chars)

        tokens = [
            t for t in extract_numbers(masked_line)
            if not _structural_number(masked_line, t)
        ]
        if not tokens:
            out_lines.append(line)
            continue

        headers_here = table_headers.get(line_idx)
        # 若该行是 Markdown 表格数据行，计算各竖线位置以便定位当前 token 在第几列
        pipe_positions = [idx for idx, ch in enumerate(line) if ch == "|"] if headers_here else []

        replacements: list[tuple[int, int, str]] = []
        for tok in tokens:
            if _in_ranges(tok.start, tok.end, protected):
                continue
            # 只自动回绑阿拉伯数字（跳过「两天」「三成」等中文数字词）
            if not any(ch.isascii() and ch.isdigit() for ch in tok.raw):
                continue
            # 「0 行」「0 条记录」不绑定到业务数值 0
            if tok.value == 0 and _ZERO_ROW_COUNT_RE.match(line[tok.end:tok.end + 6]):
                continue
            raw_clean = tok.raw.strip()
            unit_conv: str | None = None
            unit_scale: Decimal | None = None
            if raw_clean.endswith(("万", "亿")) and not raw_clean.endswith("万亿"):
                unit_conv = raw_clean[-1]
                unit_scale = Decimal(10000) if unit_conv == "万" else Decimal(100_000_000)
                num_str = raw_clean[:-1].replace(",", "").strip()
            else:
                num_str = raw_clean.rstrip("%％").replace(",", "").strip()
            target_dec = _to_decimal(num_str)
            if target_dec is None:
                continue

            has_leading_zero = bool(_LEADING_ZERO_INT_RE.match(raw_clean))
            places = max(int(tok.decimals or 0), 0)
            matched: list[tuple[_CellCandidate, str | None]] = []
            if has_leading_zero:
                matched = [(c, None) for c in text_code_by_raw.get(raw_clean, ())]
            elif unit_conv and unit_scale is not None:
                matched = [(c, unit_conv) for c in _quant_buckets(unit_conv, places).get(target_dec, ())]
            elif not tok.is_percent:
                tc_hits = [(c, None) for c in text_code_by_raw.get(raw_clean, ())]
                num_hits = [(c, None) for c in numeric_by_exact.get(target_dec, ())]
                if tc_hits and num_hits:
                    matched = sorted(tc_hits + num_hits, key=lambda pair: cell_order[id(pair[0])])
                else:
                    matched = tc_hits or num_hits
            else:
                # 单元格存 12.6（直接相等）或存 0.1258（正文写 12.6% 或 12.58%）
                exact_hits = [(c, None) for c in numeric_by_exact.get(target_dec, ())]
                pct_hits = [
                    (c, "pct")
                    for c in _quant_buckets("pct", places).get(target_dec, ())
                    if c.dec_value != target_dec
                ]
                if exact_hits and pct_hits:
                    matched = sorted(exact_hits + pct_hits, key=lambda pair: cell_order[id(pair[0])])
                else:
                    matched = exact_hits or pct_hits

            if not matched:
                continue

            # 多候选消歧 1：按同行出现的行键命中数（如同时命中编码、名称、日期的明细行优先于仅命中编码的汇总行）收窄
            if len(matched) > 1:
                scored_rk = [
                    (sum(1 for rk in pair[0].row_keys if rk in line), pair)
                    for pair in matched
                ]
                max_rk_hits = max((cnt for cnt, _ in scored_rk), default=0)
                if max_rk_hits > 0:
                    matched = [pair for cnt, pair in scored_rk if cnt == max_rk_hits]

            # 多候选消歧 2：Markdown 表格行内先按表头列名精确对齐，表头改写为中文时再按同列数查询的列序号对齐
            if len(matched) > 1 and headers_here and len(pipe_positions) >= 2:
                col_pos = sum(1 for p in pipe_positions if p < tok.start) - 1
                if 0 <= col_pos < len(headers_here):
                    hdr = headers_here[col_pos].lower()
                    by_hdr = [pair for pair in matched if pair[0].col.lower() == hdr]
                    if by_hdr:
                        matched = by_hdr
                    if len(matched) > 1:
                        by_col_pos = [
                            pair
                            for pair in matched
                            if pair[0].col_idx == col_pos
                            and len(qinfos[pair[0].alias].columns) == len(headers_here)
                        ]
                        if by_col_pos:
                            matched = by_col_pos

            # 多候选消歧 3：跨汇总/明细查询的同名列同值，或同编码字符串在多行重复出现时，确定性取最新查询首行
            unique_locs = {(c.alias, c.row, c.col) for c, _ in matched}
            if len(unique_locs) > 1:
                convs = {conv for _, conv in matched}
                if len(convs) == 1:
                    col_names = {c.col.lower() for c, _ in matched}
                    all_same_text_code = all(c.is_text_code for c, _ in matched) and len({c.raw_text for c, _ in matched}) == 1
                    if len(col_names) == 1 or all_same_text_code:
                        matched = [
                            max(
                                matched,
                                key=lambda pair: (pair[0].query_order, -pair[0].row, -pair[0].col_idx),
                            )
                        ]
                        unique_locs = {(c.alias, c.row, c.col) for c, _ in matched}

            if len(unique_locs) != 1:
                continue

            chosen, conv = matched[0]
            conv_part = f"|{conv}" if conv else ""
            marker_str = f"[[v:{chosen.alias}.r{chosen.row}.{chosen.col}{conv_part}]]"
            if len(marker_str) > MARKER_MAX:
                continue
            parsed = parse_markers(marker_str)
            if not parsed:
                continue
            cite = resolve_marker(parsed[0], catalog, cells_allowed=True, loader=loader)
            if cite.get("status") != "resolved":
                continue

            rendered = str(cite.get("rendered") or "")
            rep_start, rep_end = tok.start, tok.end
            # 若原文带 %（如 40%），但无 pct 换算的单元格本身存的是 40、渲染为 "40"，保留原文末尾的 %
            if tok.is_percent and not rendered.endswith("%") and line[rep_end - 1:rep_end] in ("%", "％"):
                rep_end -= 1
            replacements.append((rep_start, rep_end, marker_str))

        if not replacements:
            out_lines.append(line)
            continue

        rebuilt: list[str] = []
        cursor = 0
        for a, b, rep in replacements:
            if a < cursor:
                continue
            rebuilt.append(line[cursor:a])
            rebuilt.append(rep)
            cursor = b
        rebuilt.append(line[cursor:])
        out_lines.append("".join(rebuilt))

    return "".join(out_lines)


_TRAILING_V_CLUSTER_RE = re.compile(
    r"(?P<punct>[。！？；.!?;])[ \t]*(?P<tail>(?:\[\[v:[^\]]+\]\][ \t,，、；;。.]*)+)(?P<nl>\r?\n?)$"
)
_ADJACENT_DUP_RE = re.compile(
    r"(?<![A-Za-z0-9_])(?P<num>[+-]?\d[\d,]*(?:\.\d+)?[%％万亿]?)(?:[ \t]*(?P<marker>\[\[v:[^\]]+\]\])|[ \t]*[（(][ \t]*(?P<pmarker>\[\[v:[^\]]+\]\])[ \t]*[）)])"
)


def _comparable_decimal(raw_num: str) -> Decimal | None:
    s = raw_num.strip().lstrip("+")
    if not s:
        return None
    if s.endswith(("万", "亿")) and not s.endswith("万亿"):
        s = s[:-1].strip()
    else:
        s = s.rstrip("%％").strip()
    return _to_decimal(s.replace(",", ""))


def _dedupe_marker_echoes(
    text: str,
    catalog: dict[str, Any],
    loader: Any = None,
) -> str:
    """消除模型误把 [[v:...]] 当作脚注追加在裸数字后或句末造成的重复渲染（如 `12,340 [[v:...]]` 或句末堆叠 `。[[v:...]]`）。"""
    if "[[v:" not in text:
        return text
    fences = _fenced_line_ranges(text)
    lines = text.splitlines(keepends=True)
    acc = 0
    out_lines: list[str] = []

    for line in lines:
        l_start, l_end = acc, acc + len(line)
        acc = l_end
        if _in_ranges(l_start, l_end, fences) or "[[v:" not in line:
            out_lines.append(line)
            continue

        # 1) 紧邻重复：`12,340 [[v:Q1.r0.net_visitors]]` 或 `12,340（[[v:Q1.r0.net_visitors]]）`
        def _collapse_adj(match: re.Match[str]) -> str:
            num_txt = match.group("num")
            m_txt = match.group("marker") or match.group("pmarker") or ""
            d_num = _comparable_decimal(num_txt)
            parsed = parse_markers(m_txt)
            if d_num is not None and parsed:
                cite = resolve_marker(parsed[0], catalog, cells_allowed=True, loader=loader)
                if cite.get("status") == "resolved":
                    d_cite = _comparable_decimal(str(cite.get("rendered") or ""))
                    if d_cite is not None and d_cite == d_num:
                        return m_txt
            return match.group(0)

        cur = _ADJACENT_DUP_RE.sub(_collapse_adj, line)

        # 2) 句末脚注式堆叠：`... 12,340 ... 8,560。[[v:Q1.r0.a]] [[v:Q1.r0.b]]`
        m_tail = _TRAILING_V_CLUSTER_RE.search(cur)
        if m_tail:
            prefix = cur[: m_tail.start("tail")]
            tail_str = m_tail.group("tail")
            tail_markers = parse_markers(tail_str)
            if tail_markers:
                prefix_decs: set[Decimal] = set()
                for tok in extract_numbers(prefix):
                    if any(ch.isascii() and ch.isdigit() for ch in tok.raw):
                        d = _comparable_decimal(tok.raw)
                        if d is not None:
                            prefix_decs.add(d)
                for pm in parse_markers(prefix):
                    c = resolve_marker(pm, catalog, cells_allowed=True, loader=loader)
                    if c.get("status") == "resolved":
                        d = _comparable_decimal(str(c.get("rendered") or ""))
                        if d is not None:
                            prefix_decs.add(d)

                all_dup = True
                for tm in tail_markers:
                    c = resolve_marker(tm, catalog, cells_allowed=True, loader=loader)
                    d = _comparable_decimal(str(c.get("rendered") or "")) if c.get("status") == "resolved" else None
                    if d is None or d not in prefix_decs:
                        all_dup = False
                        break
                if all_dup:
                    cur = prefix + m_tail.group("nl")

        out_lines.append(cur)

    return "".join(out_lines)


def collect_fast_allow_numbers(
    prepared_text: str,
    catalog: dict[str, Any],
    *,
    masked_columns: Mapping[str, Any] | None = None,
    question: str = "",
    loader: Any = None,
) -> list[str]:
    """为快速直查（numbers=off）提取有事实依据的上下文豁免字面量，消除标题复述、公式常数与行数统计的误报，同时保留对真编造数字的拦截。"""
    if not prepared_text or not prepared_text.strip():
        return []

    qinfos = _load_query_infos(catalog, masked_columns=dict(masked_columns) if masked_columns else None, loader=loader)
    fences = _fenced_line_ranges(prepared_text)
    inline_code_spans = [
        m.span()
        for m in _INLINE_CODE_RE.finditer(prepared_text)
        if not _in_ranges(m.start(), m.end(), fences)
    ]

    # 构造与 compose_doc._bare_numbers 对齐的裸数字扫描面（抹去代码块与已绑定 [[...]] 标记）
    chars = list(prepared_text)
    for a, b in fences:
        for k in range(max(0, a), min(len(chars), b)):
            if chars[k] != "\n":
                chars[k] = " "
    for m in MARKER_RE.finditer(prepared_text):
        for k in range(max(0, m.start()), min(len(chars), m.end())):
            if chars[k] != "\n":
                chars[k] = " "
    for idx, ch in enumerate(chars):
        if ch in _SYNTAX_BLANK:
            chars[idx] = _SYNTAX_BLANK[ch]
    masked_doc = "".join(chars)

    bare_tokens = [t for t in extract_numbers(masked_doc) if not _structural_number(masked_doc, t)]
    if not bare_tokens:
        return []

    verified_floats: set[float] = set()
    verified_pct_floats: set[float] = set()

    # 1) 用户原问题中显式给出的数字与编码（先剥离「11月11日」类中文月日，避免粘连编码如「210011月11」误拆成 210011）
    if question and question.strip():
        q_clean = _CN_MONTH_DAY_RE.sub(" ", question)
        for qt in extract_numbers(q_clean):
            verified_floats.add(float(qt.value))
        for dm in re.finditer(r"(?<![A-Za-z0-9_.])\d+(?:\.\d+)?(?![A-Za-z0-9_])", q_clean):
            try:
                verified_floats.add(float(dm.group(0)))
            except ValueError:
                pass

    # 2) 未遮罩查询快照中真实存在的单元格值（多列同值未能唯一绑定单个单元格时，数值本身仍有快照依据）与未截断结果集的精确计数
    for cand in _build_cell_candidates(qinfos):
        fv = float(cand.dec_value)
        verified_floats.add(fv)
        if not cand.is_text_code and "." in cand.raw_text and Decimal(0) < cand.dec_value < Decimal(1):
            verified_pct_floats.add(float(cand.dec_value * Decimal(100)))

    for qinfo in qinfos.values():
        if qinfo.truncated or not qinfo.rows:
            continue
        verified_floats.add(float(len(qinfo.rows)))
        for j, col in enumerate(qinfo.columns):
            if col.lower() in qinfo.masked_lower:
                continue
            non_null_strs: list[str] = []
            zero_cnt = 0
            pos_cnt = 0
            kind = qinfo.kinds[j]
            for r in qinfo.rows:
                if j >= len(r) or r[j] is None or r[j] == MASKED_WORD:
                    continue
                s = str(r[j]).strip()
                if s:
                    non_null_strs.append(s)
                if kind != "text":
                    dv = _to_decimal(cell_value(r[j], kind))
                    if dv is not None:
                        if dv == 0:
                            zero_cnt += 1
                        elif dv > 0:
                            pos_cnt += 1
            if non_null_strs:
                verified_floats.add(float(len(set(non_null_strs))))
                if kind == "text" or j == 0 or col.lower().endswith(_ID_LIKE_COL_SUFFIXES):
                    for g_cnt in Counter(non_null_strs).values():
                        verified_floats.add(float(g_cnt))
            if zero_cnt > 0:
                verified_floats.add(float(zero_cnt))
            if pos_cnt > 0:
                verified_floats.add(float(pos_cnt))

    allow_out: list[str] = []
    seen_allow: set[str] = set()

    def _add_allow(token_str: str) -> None:
        s = token_str.strip()
        if s and s not in seen_allow:
            seen_allow.add(s)
            allow_out.append(s)

    arabic_verified: list[tuple[Any, str]] = []
    arabic_contextual: list[tuple[Any, str]] = []
    arabic_unverified: list[Any] = []

    for tok in bare_tokens:
        is_arabic = any(ch.isascii() and ch.isdigit() for ch in tok.raw)
        if not is_arabic:
            # 中文数量词（如「两个班次」中的「两」）：仅当对应数值在查询事实中存在时放行原词字面量，不污染阿拉伯数字容差集
            if any(_close(tok.value, vf, _tolerance(tok)) for vf in verified_floats):
                _add_allow(tok.raw)
            continue

        norm_raw = tok.raw.rstrip("%％").strip()
        matched_verified = any(_close(tok.value, vf, _tolerance(tok)) for vf in verified_floats) or (
            tok.is_percent and any(_close(tok.value, pf, _tolerance(tok)) for pf in verified_pct_floats)
        )
        if matched_verified:
            arabic_verified.append((tok, norm_raw))
            continue

        in_code = _in_ranges(tok.start, tok.end, inline_code_spans)
        before_slice = prepared_text[max(0, tok.start - 6):tok.start]
        is_pct_formula_const = abs(tok.value - 100.0) < 1e-9 and bool(_FORMULA_MUL_DIV_PREFIX_RE.search(before_slice))
        if in_code or is_pct_formula_const:
            arabic_contextual.append((tok, norm_raw))
        else:
            arabic_unverified.append(tok)

    for tok, norm_raw in arabic_verified:
        if not any(_close(u.value, tok.value, _tolerance(u)) for u in arabic_unverified):
            _add_allow(norm_raw)

    for tok, norm_raw in arabic_contextual:
        # 上下文豁免（行内反引号字面量、乘除 100% 公式换算常数）绝不能连带放过正文里同值的无出处编造数字
        if not any(_close(u.value, tok.value, _tolerance(u)) for u in arabic_unverified):
            _add_allow(norm_raw)

    return allow_out


def normalize_and_bind_fast_citations(
    raw: str,
    catalog: dict[str, Any],
    *,
    masked_columns: dict[str, Any] | None = None,
    loader: Any = None,
) -> str:
    """将快速直查 Agent 的输出归一为干净、无未解析标记且自动补齐唯一单元格出处的报告源文本。"""
    if not raw or not raw.strip():
        return raw or ""
    qinfos = _load_query_infos(catalog, masked_columns=masked_columns, loader=loader)
    step1 = _sanitize_existing_markers(raw, catalog, qinfos, loader=loader)
    step2 = _dedupe_marker_echoes(step1, catalog, loader=loader)
    if not qinfos:
        return step2
    return _auto_bind_bare_numbers(step2, catalog, qinfos, loader=loader)
