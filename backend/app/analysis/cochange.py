"""跨源伴随异动算子（cochange，W1-b）：检验主指标异动时，上游关联指标（可跨数据源、可设领先滞后期 lag）是否在对应期次同步超出各自历史常规波动范围。

设计铁律（计划书决策 D6）：
- 只检验两条序列各自相对自身稳健历史基线（中位数 + 1.4826·MAD）是否同步超出常规波动带（`k_warn` / `k_alert`），
  不需要在小样本下硬算不可靠的相关系数 p 值；
- 系统渲染的判定语严格表述为「同期/领先期统计伴随异动（属关联共变线索，不直接构成因果证明）」，绝不输出「导致/致使」等硬因果断言；
- 全程有理数（Fraction）确定性计算，支持 `recompute` 逐字节复算（`contract.same`）。
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from fractions import Fraction
from typing import Any

from app.analysis import periods as pd
from app.analysis import phrases as ph
from app.analysis import stats
from app.analysis.common import Env, build, column_label, column_types, label_for, need, refuse, reserved_check
from app.analysis.contract import CONV_PCT, CallError
from app.analysis.inputs import Snap, same_value
from app.analysis.numbers import canon
from app.analysis.series import MAD_SCALE, unreadable_text

USED = ("k_warn", "k_alert", "min_history", "max_history")
FIXED = ("driver_period_val", "primary_value", "primary_eval", "driver_value", "driver_eval", "role")
_LAG_MAX = 12


def refusal_basis(settings: Mapping[str, Any]) -> tuple[list[str], str]:
    k_w = ph.amount(settings["k_warn"])
    min_h = settings["min_history"]
    return list(USED), f"两侧序列各取至少 {min_h} 期历史，按中位数 ± {k_w} 倍稳健标准差（1.4826·MAD）检验是否同步超出常规波动"


@dataclass
class _ParsedSeries:
    snap: Snap
    period_col: str
    measure_col: str
    grain: str
    label: str
    by_ordinal: dict[int, tuple[pd.Period, Fraction]]


@dataclass
class _EvalOutcome:
    target_period: pd.Period
    target_val: Fraction
    evaluated: Fraction       # change rate or level
    median: Fraction
    sigma: Fraction
    z: Fraction | None        # None when sigma == 0
    anomalous: bool
    severe: bool
    history_n: int
    history_pairs: list[tuple[pd.Period, Fraction, Fraction]]  # (period, raw_value, evaluated)


def _extract_series(
    env: Env,
    snap: Snap,
    period_col: str,
    measure_col: str,
    *,
    grain_hint: str | None,
    label: str,
    role_title: str,
) -> tuple[_ParsedSeries | None, tuple[str, str] | None]:
    i_period = snap.index(period_col)
    raw_periods = [row[i_period] if i_period < len(row) else None for row in snap.rows]
    weeks = pd.week_scheme(snap.sql, period_col, snap.dialect)
    parsed = pd.parse(raw_periods, grain_hint, weeks=weeks)
    if parsed is None:
        return None, ("period_unreadable", unreadable_text(period_col, raw_periods, grain_hint, weeks))
    items, detected_grain = parsed
    seen: set[int] = set()
    by_ord: dict[int, tuple[pd.Period, Fraction]] = {}
    for r, item in enumerate(items):
        if item.ordinal in seen:
            raise CallError(
                "duplicate_rows",
                ph.say(f"{snap.ref} 里同一期有不止一行：请先在 SQL 中按 ", column_label(env, snap, period_col), " 汇总到每期一行"),
            )
        seen.add(item.ordinal)
        val = snap.number(r, measure_col, role_title)
        if val is None:
            continue
        if pd.short_days(item, detected_grain) is not None:
            continue
        by_ord[item.ordinal] = (item, val)
    return _ParsedSeries(snap, period_col, measure_col, detected_grain, label, by_ord), None


def _evaluate_at(
    series: _ParsedSeries,
    target_ord: int,
    *,
    basis: str,
    min_history: int,
    max_history: int,
    k_warn: Fraction,
    k_alert: Fraction,
    who: str,
) -> tuple[_EvalOutcome | None, tuple[str, str] | None]:
    if target_ord not in series.by_ordinal:
        return None, ("target_gap", f"{who}缺少目标期次的数据，无法核对伴随波动")

    step = 1
    eval_points: list[tuple[pd.Period, Fraction, Fraction]] = []
    for ord_k in sorted(series.by_ordinal):
        if ord_k > target_ord:
            continue
        p_item, val = series.by_ordinal[ord_k]
        if basis == "level":
            eval_points.append((p_item, val, val))
        else:
            prev = series.by_ordinal.get(ord_k - step)
            if prev is None:
                if ord_k == target_ord:
                    return None, ("target_gap", f"{who}在目标期次「{p_item.raw}」的前一期缺失，无法计算环比变化率")
                continue
            if prev[1] == 0:
                if ord_k == target_ord:
                    return None, ("zero_base", f"{who}在比较期「{prev[0].raw}」为 0，无法计算变化率（可改用 basis='level'）")
                continue
            eval_points.append((p_item, val, (val - prev[1]) / abs(prev[1])))

    if not eval_points or eval_points[-1][0].ordinal != target_ord:
        return None, ("target_gap", f"{who}在目标期次无可用评估值")

    target_tuple = eval_points[-1]
    history = eval_points[:-1][-max_history:]
    if len(history) < min_history:
        return None, (
            "history_short",
            f"{who}可用历史只有 {len(history)} 期（至少需要 {min_history} 期）",
        )

    hist_vals = [x[2] for x in history]
    med = stats.median(hist_vals)
    mad = stats.mad(hist_vals, med)
    sigma = MAD_SCALE * mad
    curr = target_tuple[2]
    diff = curr - med
    if sigma == 0:
        z = None
        anomalous = diff != 0
        severe = diff != 0
    else:
        z = diff / sigma
        anomalous = abs(z) >= k_warn
        severe = abs(z) >= k_alert

    return _EvalOutcome(
        target_period=target_tuple[0],
        target_val=target_tuple[1],
        evaluated=curr,
        median=med,
        sigma=sigma,
        z=z,
        anomalous=anomalous,
        severe=severe,
        history_n=len(history),
        history_pairs=[*history, target_tuple],
    ), None


def cochange(raw: Mapping[str, Any], env: Env) -> Any:
    k_warn = env.threshold("k_warn")
    k_alert = env.threshold("k_alert")
    min_history = int(env.settings["min_history"])
    max_history = int(env.settings["max_history"])
    _, criterion = refusal_basis(env.settings)

    has_single = raw.get("query") not in (None, "")
    has_two = raw.get("base_query") not in (None, "") or raw.get("target_query") not in (None, "")
    if has_single and has_two:
        raise CallError("conflicting_queries", "伴随异动检验不能同时写 query 和 base_query / target_query")
    if not has_single and not has_two:
        raise CallError("missing_query", "伴随异动检验请提供两份序列查询（base_query 与 target_query），或包含两列的单份查询（query）")

    basis = str(raw.get("basis") or "change").strip()
    if basis not in ("change", "level"):
        raise CallError("bad_basis", "比较方式（basis）只能是 change（变化率）或 level（水平值）")
    grain_hint = raw.get("grain")
    if grain_hint is not None and grain_hint not in (*pd.GRAINS, "seq"):
        raise CallError("bad_grain", "期次粒度（grain）只能写 day、week、month、quarter 或留空")

    lag_raw = raw.get("lag")
    if lag_raw in (None, ""):
        lag = 0
    elif isinstance(lag_raw, bool) or not isinstance(lag_raw, int) or not (0 <= lag_raw <= _LAG_MAX):
        raise CallError("bad_lag", f"上游领先期数（lag）须为 0 到 {_LAG_MAX} 之间的整数（0 表示同期）")
    else:
        lag = lag_raw

    params: dict[str, Any] = {"basis": basis}
    if lag > 0:
        params["lag"] = lag

    if has_single:
        snap = env.inputs.snap(need(raw, "query", "查询编号"), "查询编号", role="data")
        p_col = snap.column(need(raw, "period", "期次列"), "期次列")
        reserved_check(p_col, FIXED, "期次列")
        if raw.get("target_period") not in (None, ""):
            raise CallError("unexpected_target_period", "单份查询（query）共用期次列 period，不需要写 target_period")
        m_col = snap.column(need(raw, "measure", "主指标度量列"), "主指标度量列")
        d_col = snap.column(need(raw, "driver_measure", "驱动度量列"), "驱动度量列")
        if m_col == d_col:
            raise CallError("same_column", f"主指标度量列与驱动度量列不能是同一列「{m_col}」")
        params["query"] = snap.ref
        params["period"] = p_col
        params["measure"] = m_col
        params["driver_measure"] = d_col
        p_label = label_for(env, raw.get("label"), snap, m_col)
        d_label = column_label(env, snap, d_col)
        if raw.get("label") is not None:
            params["label"] = ph.clean_label(raw.get("label")) or None
        env.inputs.use(snap, "data", [p_col, m_col, d_col])
        if snap.truncated:
            return refuse(env, params, USED, "truncated", f"{snap.ref} 结果已被截断，无法核对完整时间序列", criterion=criterion)
        p_series, err = _extract_series(env, snap, p_col, m_col, grain_hint=grain_hint, label=p_label, role_title="主指标度量列")
        if err is not None or p_series is None:
            return refuse(env, params, USED, err[0], err[1], criterion=criterion)  # type: ignore[index]
        d_series, err = _extract_series(env, snap, p_col, d_col, grain_hint=p_series.grain, label=d_label, role_title="驱动度量列")
        if err is not None or d_series is None:
            return refuse(env, params, USED, err[0], err[1], criterion=criterion)  # type: ignore[index]
        period_dim_type = snap.kind(p_col)
    else:
        p_snap = env.inputs.snap(need(raw, "base_query", "主指标查询编号"), "主指标查询编号", role="base")
        d_snap = env.inputs.snap(need(raw, "target_query", "驱动指标查询编号"), "驱动指标查询编号", role="target")
        p_col = p_snap.column(need(raw, "period", "主指标期次列"), "主指标期次列")
        reserved_check(p_col, FIXED, "主指标期次列")
        d_p_col = d_snap.column(raw.get("target_period") or p_col, "驱动期次列")
        m_col = p_snap.column(need(raw, "measure", "主指标度量列"), "主指标度量列")
        d_col = d_snap.column(need(raw, "driver_measure", "驱动度量列"), "驱动度量列")

        params["base_query"] = p_snap.ref
        params["target_query"] = d_snap.ref
        params["period"] = p_col
        if raw.get("target_period") not in (None, ""):
            params["target_period"] = d_p_col
        params["measure"] = m_col
        params["driver_measure"] = d_col
        p_label = label_for(env, raw.get("label"), p_snap, m_col)
        d_label = column_label(env, d_snap, d_col)
        if raw.get("label") is not None:
            params["label"] = ph.clean_label(raw.get("label")) or None

        env.inputs.use(p_snap, "base", [p_col, m_col])
        env.inputs.use(d_snap, "target", [d_p_col, d_col])
        if p_snap.truncated:
            return refuse(env, params, USED, "truncated", f"{p_snap.ref} 结果已被截断，无法核对完整时间序列", criterion=criterion)
        if d_snap.truncated:
            return refuse(env, params, USED, "truncated", f"{d_snap.ref} 结果已被截断，无法核对完整时间序列", criterion=criterion)

        p_series, err = _extract_series(env, p_snap, p_col, m_col, grain_hint=grain_hint, label=p_label, role_title="主指标度量列")
        if err is not None or p_series is None:
            return refuse(env, params, USED, err[0], err[1], criterion=criterion)  # type: ignore[index]
        d_series, err = _extract_series(env, d_snap, d_p_col, d_col, grain_hint=p_series.grain, label=d_label, role_title="驱动度量列")
        if err is not None or d_series is None:
            return refuse(env, params, USED, err[0], err[1], criterion=criterion)  # type: ignore[index]
        if d_series.grain != p_series.grain:
            return refuse(
                env,
                params,
                USED,
                "incomparable_sides",
                f"主指标期次粒度（{p_series.grain}）与驱动指标期次粒度（{d_series.grain}）不一致，无法对齐检验",
                criterion=criterion,
            )
        period_dim_type = p_snap.kind(p_col)

    params["grain"] = p_series.grain
    if not p_series.by_ordinal:
        return refuse(env, params, USED, "history_short", f"主指标「{p_label}」没有有效数值行", criterion=criterion)

    wanted_target = raw.get("target")
    if wanted_target in (None, ""):
        target_ord = max(p_series.by_ordinal)
    else:
        matched_ords = [ord_k for ord_k, (p_it, _) in p_series.by_ordinal.items() if same_value(p_it.raw, wanted_target)]
        if not matched_ords:
            raise CallError(
                "period_not_found",
                f"主指标序列里没有期次「{wanted_target}」",
                reason=f"主指标这次的数据里没有期次「{wanted_target}」",
            )
        target_ord = matched_ords[0]
        params["target"] = str(p_series.by_ordinal[target_ord][0].raw)

    p_eval, p_err = _evaluate_at(
        p_series,
        target_ord,
        basis=basis,
        min_history=min_history,
        max_history=max_history,
        k_warn=k_warn,
        k_alert=k_alert,
        who=f"主指标「{p_label}」",
    )
    if p_err is not None or p_eval is None:
        return refuse(env, params, USED, p_err[0], p_err[1], criterion=criterion)  # type: ignore[index]

    driver_ord = target_ord - lag
    d_eval, d_err = _evaluate_at(
        d_series,
        driver_ord,
        basis=basis,
        min_history=min_history,
        max_history=max_history,
        k_warn=k_warn,
        k_alert=k_alert,
        who=f"驱动指标「{d_label}」",
    )
    if d_err is not None or d_eval is None:
        return refuse(env, params, USED, d_err[0], d_err[1], criterion=criterion)  # type: ignore[index]

    p_delta = p_eval.evaluated - p_eval.median
    d_delta = d_eval.evaluated - d_eval.median
    p_shown = ph.pct(p_eval.evaluated, signed=True) if basis == "change" else ph.amount(p_eval.evaluated)
    d_shown = ph.pct(d_eval.evaluated, signed=True) if basis == "change" else ph.amount(d_eval.evaluated)
    lag_word = f"领先 {lag} 期（{d_eval.target_period.raw}）" if lag > 0 else f"同期（{d_eval.target_period.raw}）"

    if not p_eval.anomalous:
        verdict_code = "primary_normal"
        direction = None
        phrase = ph.say(
            f"主指标「{p_label}」在 {p_eval.target_period.raw} 处于自身常规波动范围内（{p_shown}），无需触发跨源伴随异动归因。"
        )
    elif d_eval.anomalous:
        verdict_code = "co_anomalous"
        direction = "up" if p_delta > 0 else ("down" if p_delta < 0 else None)
        same_sign = (p_delta > 0 and d_delta > 0) or (p_delta < 0 and d_delta < 0)
        rel_word = "同向伴随异动" if same_sign else "反向伴随异动"
        pz_txt = f"，稳健 z = {ph.amount(p_eval.z, signed=True)}" if p_eval.z is not None else ""
        dz_txt = f"，稳健 z = {ph.amount(d_eval.z, signed=True)}" if d_eval.z is not None else ""
        phrase = ph.say(
            f"主指标「{p_label}」在 {p_eval.target_period.raw} 超出常规波动范围（{p_shown}{pz_txt}），"
            f"上游关联指标「{d_label}」在{lag_word}亦超出自身常规波动范围（{d_shown}{dz_txt}），"
            f"两者呈{rel_word}（属统计共变线索，不直接构成因果证明）。"
        )
    else:
        verdict_code = "target_normal"
        direction = "up" if p_delta > 0 else ("down" if p_delta < 0 else None)
        phrase = ph.say(
            f"主指标「{p_label}」在 {p_eval.target_period.raw} 超出常规波动范围（{p_shown}），"
            f"但上游关联指标「{d_label}」在{lag_word}处于常规波动范围内（{d_shown}），未见伴随异动。"
        )

    # 组装对齐结果表（最近最多 12 期历史 + 目标期）
    p_hist_map = {pt[0].ordinal: pt for pt in p_eval.history_pairs}
    d_hist_map = {pt[0].ordinal: pt for pt in d_eval.history_pairs}
    recent_ords = [pt[0].ordinal for pt in p_eval.history_pairs[-12:]]
    rows_out: list[list[Any]] = []
    for ord_k in recent_ords:
        p_pt = p_hist_map[ord_k]
        d_pt = d_hist_map.get(ord_k - lag)
        role_txt = "当期" if ord_k == target_ord else "历史基线"
        rows_out.append([
            p_pt[0].raw,
            d_pt[0].raw if d_pt else None,
            canon(p_pt[1]),
            canon(p_pt[2]),
            canon(d_pt[1]) if d_pt else None,
            canon(d_pt[2]) if d_pt else None,
            role_txt,
        ])

    cols = [p_col, *FIXED]
    labels_map = {
        p_col: "主指标期次",
        "driver_period_val": "驱动指标期次",
        "primary_value": p_label,
        "primary_eval": "主指标变化率" if basis == "change" else "主指标水平",
        "driver_value": d_label,
        "driver_eval": "驱动变化率" if basis == "change" else "驱动水平",
        "role": "期次角色",
    }
    table_formats = {"primary_eval": CONV_PCT, "driver_eval": CONV_PCT} if basis == "change" else {}
    named_formats = {"primary_evaluated": CONV_PCT, "driver_evaluated": CONV_PCT} if basis == "change" else {}

    table = {
        "columns": cols,
        "labels": labels_map,
        "rows": rows_out,
        "column_types": column_types(cols, dims={p_col: period_dim_type}, texts=("driver_period_val", "role")),
    }
    values: dict[str, Any] = {
        "primary_value": canon(p_eval.target_val),
        "primary_evaluated": canon(p_eval.evaluated),
        "primary_median": canon(p_eval.median),
        "primary_z": canon(p_eval.z),
        "driver_value": canon(d_eval.target_val),
        "driver_evaluated": canon(d_eval.evaluated),
        "driver_median": canon(d_eval.median),
        "driver_z": canon(d_eval.z),
        "history_periods": str(min(p_eval.history_n, d_eval.history_n)),
        "lag_periods": str(lag),
    }
    verdict = {"code": verdict_code, "subjects": [[d_label]] if verdict_code == "co_anomalous" else [], "direction": direction}
    return build(
        env,
        params,
        USED,
        table=table,
        values=values,
        verdict=verdict,
        criterion=criterion,
        phrase=phrase,
        formats=table_formats,
        named=named_formats,
    )
