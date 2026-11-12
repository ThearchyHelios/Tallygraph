"""跨源对账算子（reconcile，W1-b）：对齐同一份或两份查询快照上的镜像指标，按容差判定对账一致、存在偏差或对侧缺失。

输入方式：
- 两份跨源快照：`base_query`（主源/左侧）+ `target_query`（对账源/右侧），`measure`（左侧度量列），可选 `target_measure`
  （右侧度量列，缺省同 `measure`）、`by`（左侧对齐键列，0..2 列）、`target_by`（右侧对齐键列，缺省同 `by`）、
  `unit_factor`（物理计量单位换算系数，右侧乘以该系数后与左侧比较，缺省 1）；
- 单份已合并快照：`query` + `measure`（左侧列）+ `target_measure`（右侧列），可选 `by`、`unit_factor`。

阈值契约（D4）：请求参数不收容差阈值，统一使用平台分析设置里的 `flat_pct`（相对容差，缺省 0.5%）与 `table_top_k`。
纯函数，全程精确有理数（Fraction），支持 `recompute` 逐字节复算核对（`contract.same`）。
"""
from __future__ import annotations

import math
from collections.abc import Mapping
from fractions import Fraction
from typing import Any

from app.analysis import phrases as ph
from app.analysis.common import Env, build, column_label, column_types, label_for, need, refuse, reserved_check
from app.analysis.contract import CONV_PCT, CallError
from app.analysis.inputs import Snap, as_list, hashable
from app.analysis.numbers import canon, to_fraction

USED = ("flat_pct", "table_top_k")
FIXED = ("left_value", "right_value", "diff", "rel_diff", "row_status")
_MAX_BY = 2


def refusal_basis(settings: Mapping[str, Any]) -> tuple[list[str], str]:
    tol = ph.pct(settings["flat_pct"], signed=False)
    return list(USED), f"按相对误差不超过 {tol} 核对两侧口径数值是否对齐"


def _parse_unit_factor(raw_val: Any) -> tuple[Fraction, float | int | None]:
    if raw_val in (None, ""):
        return Fraction(1), None
    if isinstance(raw_val, bool) or not isinstance(raw_val, (int, float)):
        f = to_fraction(raw_val)
        if f is None or f <= 0:
            raise CallError("bad_unit_factor", "单位换算系数（unit_factor）须为大于 0 的有限数字（如吨转千克写 1000）")
        as_num: float | int = int(f) if f.denominator == 1 else float(f)
        return f, as_num
    if not math.isfinite(float(raw_val)) or float(raw_val) <= 0:
        raise CallError("bad_unit_factor", "单位换算系数（unit_factor）须为大于 0 的有限数字（如吨转千克写 1000）")
    f = to_fraction(raw_val)
    assert f is not None and f > 0
    as_num = int(raw_val) if isinstance(raw_val, int) else float(raw_val)
    return f, as_num


def _norm_key(raw_key: tuple[Any, ...]) -> tuple[str, ...]:
    return tuple("" if v is None else str(hashable(v)).strip() for v in raw_key)


def _extract_side(
    env: Env,
    snap: Snap,
    by_cols: list[str],
    measure_col: str,
    *,
    role_name: str,
) -> dict[tuple[str, ...], tuple[list[Any], Fraction | None]]:
    out: dict[tuple[str, ...], tuple[list[Any], Fraction | None]] = {}
    if not by_cols:
        if len(snap.rows) > 1:
            raise CallError(
                "duplicate_rows",
                f"{snap.ref} 有 {len(snap.rows)} 行，未指定对齐维度列（by）时每侧只能是汇总后的 1 行；请加上 by 列或在 SQL 中汇总",
            )
        val = snap.number(0, measure_col, role_name) if snap.rows else None
        out[()] = ([], val)
        return out

    idx_by = [snap.index(c) for c in by_cols]
    for r in range(len(snap.rows)):
        row = snap.rows[r]
        raw_vals = [row[i] if i < len(row) else None for i in idx_by]
        key = _norm_key(tuple(raw_vals))
        if key in out:
            shown = ph.key_text(raw_vals)
            cols_label = "、".join(column_label(env, snap, c) for c in by_cols)
            raise CallError(
                "duplicate_rows",
                f"{snap.ref} 在维度 {cols_label} = {shown} 上有多行：请先在 SQL 中按对齐维度汇总到唯一一行",
            )
        val = snap.number(r, measure_col, role_name)
        out[key] = (raw_vals, val)
    return out


def reconcile(raw: Mapping[str, Any], env: Env) -> Any:
    tol = env.threshold("flat_pct")
    top_k = int(env.settings["table_top_k"])
    _, criterion = refusal_basis(env.settings)

    has_single = raw.get("query") not in (None, "")
    has_two = raw.get("base_query") not in (None, "") or raw.get("target_query") not in (None, "")
    if has_single and has_two:
        raise CallError("conflicting_queries", "跨源对账不能同时写 query 和 base_query / target_query：单表对账写 query，两份快照对账写 base_query 与 target_query")
    if not has_single and not has_two:
        raise CallError("missing_query", "跨源对账请提供两份查询编号（base_query 与 target_query），或单份合并查询编号（query）")

    by_raw = [str(c).strip() for c in as_list(raw.get("by")) if str(c).strip()]
    if len(by_raw) > _MAX_BY:
        raise CallError("too_many_by", f"对账对齐维度最多支持 {_MAX_BY} 列")

    factor_frac, factor_param = _parse_unit_factor(raw.get("unit_factor"))
    params: dict[str, Any] = {}

    if has_single:
        snap = env.inputs.snap(need(raw, "query", "查询编号"), "查询编号", role="data")
        by_cols = [snap.column(c, "对齐维度列") for c in by_raw]
        for c in by_cols:
            reserved_check(c, FIXED, "对齐维度列")
        if raw.get("target_by") not in (None, [], ""):
            raise CallError("unexpected_target_by", "单份查询（query）对账共用同一组维度列 by，不需要写 target_by")
        left_col = snap.column(need(raw, "measure", "左侧度量列"), "左侧度量列")
        right_col = snap.column(need(raw, "target_measure", "对侧度量列"), "对侧度量列")
        if left_col == right_col and factor_frac == 1:
            raise CallError("same_column", f"单表对账的左侧度量列与对侧度量列不能是同一列「{left_col}」")
        params["query"] = snap.ref
        if by_cols:
            params["by"] = by_cols
        params["measure"] = left_col
        params["target_measure"] = right_col
        if factor_param is not None:
            params["unit_factor"] = factor_param
        label = label_for(env, raw.get("label"), snap, left_col)
        if raw.get("label") is not None:
            params["label"] = ph.clean_label(raw.get("label")) or None
        env.inputs.use(snap, "data", [*by_cols, left_col, right_col])
        if snap.truncated:
            return refuse(env, params, USED, "truncated", f"{snap.ref} 的查询结果已被截断，无法做完整对账", criterion=criterion)
        left_map = _extract_side(env, snap, by_cols, left_col, role_name="左侧度量列")
        right_map = _extract_side(env, snap, by_cols, right_col, role_name="对侧度量列")
        dim_types = {c: snap.kind(c) for c in by_cols}
        dim_labels = {c: column_label(env, snap, c) for c in by_cols}
    else:
        left_snap = env.inputs.snap(need(raw, "base_query", "主侧查询编号"), "主侧查询编号", role="base")
        right_snap = env.inputs.snap(need(raw, "target_query", "对侧查询编号"), "对侧查询编号", role="target")
        left_by = [left_snap.column(c, "对齐维度列") for c in by_raw]
        for c in left_by:
            reserved_check(c, FIXED, "对齐维度列")
        t_by_raw = [str(c).strip() for c in as_list(raw.get("target_by")) if str(c).strip()]
        if t_by_raw and len(t_by_raw) != len(left_by):
            raise CallError("target_by_mismatch", f"对侧维度列（target_by，{len(t_by_raw)} 列）数量须与对齐维度列（by，{len(left_by)} 列）一致")
        right_by = [right_snap.column(c, "对侧维度列") for c in (t_by_raw or by_raw)]
        left_col = left_snap.column(need(raw, "measure", "左侧度量列"), "左侧度量列")
        right_col = right_snap.column(raw.get("target_measure") or left_col, "对侧度量列")

        params["base_query"] = left_snap.ref
        params["target_query"] = right_snap.ref
        if left_by:
            params["by"] = left_by
        if t_by_raw:
            params["target_by"] = right_by
        params["measure"] = left_col
        if raw.get("target_measure") not in (None, ""):
            params["target_measure"] = right_col
        if factor_param is not None:
            params["unit_factor"] = factor_param
        label = label_for(env, raw.get("label"), left_snap, left_col)
        if raw.get("label") is not None:
            params["label"] = ph.clean_label(raw.get("label")) or None

        env.inputs.use(left_snap, "base", [*left_by, left_col])
        env.inputs.use(right_snap, "target", [*right_by, right_col])
        if left_snap.truncated:
            return refuse(env, params, USED, "truncated", f"{left_snap.ref} 的查询结果已被截断，无法做完整对账", criterion=criterion)
        if right_snap.truncated:
            return refuse(env, params, USED, "truncated", f"{right_snap.ref} 的查询结果已被截断，无法做完整对账", criterion=criterion)
        left_map = _extract_side(env, left_snap, left_by, left_col, role_name="左侧度量列")
        right_map = _extract_side(env, right_snap, right_by, right_col, role_name="对侧度量列")
        by_cols = left_by
        dim_types = {c: left_snap.kind(c) for c in left_by}
        dim_labels = {c: column_label(env, left_snap, c) for c in left_by}

    all_keys = sorted(set(left_map) | set(right_map))
    paired_records: list[dict[str, Any]] = []
    left_sum = Fraction(0)
    right_sum = Fraction(0)
    matched_count = 0
    diverged_count = 0
    missing_count = 0
    max_abs = Fraction(0)
    max_rel = Fraction(0)

    for key in all_keys:
        l_entry = left_map.get(key)
        r_entry = right_map.get(key)
        raw_dims = (l_entry[0] if l_entry is not None else (r_entry[0] if r_entry is not None else []))
        l_val = l_entry[1] if l_entry is not None else None
        r_raw = r_entry[1] if r_entry is not None else None
        r_val = (r_raw * factor_frac) if r_raw is not None else None

        if l_val is None and r_val is None:
            continue
        if l_val is not None:
            left_sum += l_val
        if r_val is not None:
            right_sum += r_val

        if l_val is None or r_val is None:
            missing_count += 1
            status_code = "missing_right" if l_val is not None else "missing_left"
            sort_gap = abs(l_val if l_val is not None else r_val)  # type: ignore[arg-type]
            paired_records.append({
                "dims": raw_dims,
                "key": key,
                "left": l_val,
                "right": r_val,
                "diff": None,
                "rel": None,
                "status": status_code,
                "sort_rank": (2, sort_gap),
            })
            continue

        matched_count += 1
        diff = l_val - r_val
        abs_diff = abs(diff)
        denom = max(abs(l_val), abs(r_val))
        rel = (abs_diff / denom) if denom != 0 else Fraction(0)
        is_div = (abs_diff > tol * denom) if denom != 0 else False
        if is_div:
            diverged_count += 1
        if abs_diff > max_abs:
            max_abs = abs_diff
        if rel > max_rel:
            max_rel = rel
        paired_records.append({
            "dims": raw_dims,
            "key": key,
            "left": l_val,
            "right": r_val,
            "diff": diff,
            "rel": rel,
            "status": "diverged" if is_div else "aligned",
            "sort_rank": (1 if is_div else 0, abs_diff),
        })

    if not paired_records:
        return refuse(env, params, USED, "too_few_segments", "两侧查询均无有效数值行，无法对账", criterion=criterion)

    paired_records.sort(key=lambda item: (-item["sort_rank"][0], -item["sort_rank"][1], item["key"]))
    net_diff = left_sum - right_sum
    net_denom = max(abs(left_sum), abs(right_sum))
    net_rel = (abs(net_diff) / net_denom) if net_denom != 0 else Fraction(0)

    if diverged_count == 0 and missing_count > 0:
        verdict_code = "missing_counterpart"
        direction = None
        missing_recs = [r for r in paired_records if r["status"] != "aligned"]
        subjects = [[str(x) for x in r["dims"]] for r in missing_recs[:3] if r["dims"]]
        matched_note = f"（已配对的 {matched_count} 项数值一致）" if matched_count > 0 else ""
        phrase = ph.say(
            f"「{label}」跨源对账发现对侧缺失（共 {missing_count} 项未能配对）{matched_note}，两侧无法完全闭合，请优先排查对侧数据源同步缺口或主数据编码未映射。"
        )
    elif diverged_count == 0 and missing_count == 0:
        verdict_code = "aligned"
        direction = None
        subjects = []
        phrase = ph.say(
            f"「{label}」两侧对账一致（合计 {ph.amount(left_sum)} 对 {ph.amount(right_sum)}，最大相对偏差 {ph.pct(max_rel, signed=False)}，在容差 {ph.pct(tol, signed=False)} 以内）。"
        )
    else:
        verdict_code = "diverged"
        direction = "up" if net_diff > 0 else ("down" if net_diff < 0 else None)
        flagged = [r for r in paired_records if r["status"] != "aligned"]
        subjects = [[str(x) for x in r["dims"]] for r in flagged[:3] if r["dims"]]
        extra_missing = f"，另有 {missing_count} 项对侧缺失" if missing_count else ""
        top_hint = ""
        if subjects:
            top_hint = f"（偏差集中在 {'、'.join(ph.key_text(s) for s in subjects)}）"
        phrase = ph.say(
            f"「{label}」跨源对账存在显著偏差：主侧合计 {ph.amount(left_sum)}，对侧合计 {ph.amount(right_sum)}，净差额 {ph.amount(net_diff, signed=True)}"
            f"（最大相对偏差 {ph.pct(max_rel, signed=False)}，超出容差 {ph.pct(tol, signed=False)}，共 {diverged_count} 项超差{extra_missing}）{top_hint}。"
        )

    cols = [*by_cols, *FIXED]
    labels_map = {
        **dim_labels,
        "left_value": "主侧数值",
        "right_value": "对侧数值",
        "diff": "差额",
        "rel_diff": "相对偏差",
        "row_status": "对账状态",
    }
    status_cn = {
        "aligned": "一致",
        "diverged": "超差",
        "missing_right": "对侧缺失",
        "missing_left": "主侧缺失",
    }
    shown_rows = [
        [
            *rec["dims"],
            canon(rec["left"]),
            canon(rec["right"]),
            canon(rec["diff"]),
            canon(rec["rel"]),
            status_cn[rec["status"]],
        ]
        for rec in paired_records[:top_k]
    ]
    table = {
        "columns": cols,
        "labels": labels_map,
        "rows": shown_rows,
        "column_types": column_types(cols, dims=dim_types, texts=("row_status",)),
    }
    values = {
        "left_total": canon(left_sum),
        "right_total": canon(right_sum),
        "net_diff": canon(net_diff),
        "net_rel_diff": canon(net_rel),
        "max_abs_diff": canon(max_abs),
        "max_rel_diff": canon(max_rel),
        "matched_rows": str(matched_count),
        "diverged_rows": str(diverged_count),
        "missing_counterpart_rows": str(missing_count),
    }
    verdict: dict[str, Any] = {"code": verdict_code, "subjects": subjects, "direction": direction}
    return build(
        env,
        params,
        USED,
        table=table,
        values=values,
        verdict=verdict,
        criterion=criterion,
        phrase=phrase,
        formats={"rel_diff": CONV_PCT},
        named={"net_rel_diff": CONV_PCT, "max_rel_diff": CONV_PCT},
    )
