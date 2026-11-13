from __future__ import annotations

from datetime import date
from fractions import Fraction
import math
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import create_async_engine

from app.analysis import DATA_ERRORS, stats as analysis_stats
from app.analysis.contract import CallError
from app.analysis.decompose import lmdi
from app.analysis.differ import MAX_ROWS_MODE
from app.analysis.periods import plain_day
from app.analysis.stats import theil_sen
from app.chat.direct import build_direct_graph
from app.chat.direct_evidence import normalize_and_bind_fast_citations
from app.chat.grounding import GroundingBundle
from app.cluster.service import _Service
from app.core.bus import EventBus, _MAX_SEQ_ENTRIES
from app.data import coverage, csv_grid
from app.data.tabular import _detect_csv_encoding
from app.db.base import _INDEX_MIGRATIONS, _set_sqlite_pragma, create_schema
from app.engine.schema import GraphSpec, validate_graph
from app.ledger.match import Caliber, Fetch, Query, compare_queries
from tests.analysis_samples import analyze, snap as make_snap


@pytest.mark.asyncio
async def test_sqlite_pragmas_and_index_migrations(tmp_path: Path) -> None:
    db_path = tmp_path / "idx_check.db"
    eng = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    event.listen(eng.sync_engine, "connect", _set_sqlite_pragma)
    try:
        await create_schema(eng)
        async with eng.connect() as conn:
            sync_mode = (await conn.execute(text("PRAGMA synchronous"))).scalar_one()
            # SQLite NORMAL = 1
            assert sync_mode == 1
            rows = (await conn.execute(text("SELECT name FROM sqlite_master WHERE type='index'"))).scalars().all()
            existing = set(rows)
            for idx_name, _, _ in _INDEX_MIGRATIONS:
                assert idx_name in existing
    finally:
        await eng.dispose()


@pytest.mark.asyncio
async def test_event_bus_seq_lru_and_close_eviction() -> None:
    bus = EventBus()
    seq1 = bus.next_seq("run-1")
    assert seq1 == 1
    assert "run-1" in bus._seq
    await bus.close("run-1")
    assert "run-1" not in bus._seq

    for i in range(_MAX_SEQ_ENTRIES + 25):
        bus.next_seq(f"r-{i}")
    assert len(bus._seq) <= _MAX_SEQ_ENTRIES
    assert "r-0" not in bus._seq


def test_cluster_slot_signal_wakes_local_waiters(monkeypatch: pytest.MonkeyPatch) -> None:
    woken: list[bool] = []
    monkeypatch.setattr("app.cluster.service.slots.wake_local", lambda: woken.append(True))
    svc = _Service()
    svc._on_slot(["w1"])
    assert woken == [True]


def test_coverage_parses_two_digit_tz_offset_and_lru_eviction() -> None:
    d1 = coverage.as_day("2026-08-31 23:30:00+08")
    d2 = coverage.as_day("2026-08-31T00:00:00+00")
    d3 = coverage.as_day("2026-08-31 12:00:00-0530")
    assert d1 is not None and d1.day == date(2026, 8, 31) and d1.timed is True
    assert d2 is not None and d2.day == date(2026, 8, 31) and d2.timed is False
    assert d3 is not None and d3.day == date(2026, 8, 31) and d3.timed is True

    coverage._LIVE.clear()
    try:
        for i in range(coverage._LIVE_MAX + 10):
            coverage._LIVE[("src", f"t_{i}")] = (1e12, i)  # type: ignore[assignment]
            if len(coverage._LIVE) > coverage._LIVE_MAX:
                coverage._LIVE.popitem(last=False)
        assert len(coverage._LIVE) == coverage._LIVE_MAX
        assert ("src", "t_0") not in coverage._LIVE
        assert ("src", f"t_{coverage._LIVE_MAX + 9}") in coverage._LIVE
    finally:
        coverage._LIVE.clear()


def test_compare_queries_prioritizes_table_overlap_before_max_candidates() -> None:
    calibers: list[Caliber] = []
    for i in range(95):
        calibers.append(
            Caliber(
                workflow_id=f"wf_{i}",
                version=1,
                level="certified",
                graph_hash=f"h_{i}",
                fetches=(
                    Fetch(
                        node_id="q1",
                        source="sales",
                        sql=f"SELECT COUNT(*) AS cnt_{i} FROM orders",
                        tables=frozenset({"orders"}),
                    ),
                ),
            )
        )
    calibers.append(
        Caliber(
            workflow_id="wf_exact",
            version=1,
            level="certified",
            graph_hash="h_exact",
            fetches=(
                Fetch(
                    node_id="q1",
                    source="sales",
                    sql="SELECT SUM(o.amount - COALESCE(r.refund_amt, 0)) AS net_sales "
                    "FROM orders o LEFT JOIN refunds r ON r.order_id = o.id",
                    tables=frozenset({"orders", "refunds"}),
                ),
            ),
        )
    )
    q = Query(
        artifact="sha_q1",
        node_id="fetch_1",
        source="sales",
        sql="SELECT SUM(o.amount - COALESCE(r.refund_amt, 0)) AS net_sales "
        "FROM orders o LEFT JOIN refunds r ON r.order_id = o.id",
        columns=["net_sales"],
        tables=frozenset({"orders", "refunds"}),
        dialect="sqlite",
    )
    res = compare_queries([q], calibers)
    assert len(res) == 1
    assert res[0]["status"] == "proven"
    assert res[0]["candidates"][0]["workflow_id"] == "wf_exact"


def test_direct_evidence_indexed_auto_bind_preserves_semantics() -> None:
    snap = {
        "kind": "tool_output",
        "tool": "db_query__mes",
        "columns": ["line_code", "good_qty", "yield_rate", "output_amt"],
        "column_types": ["text", "integer", "real", "real"],
        "rows": [["L01", 12340, 0.985, 125000.0]],
        "truncated": False,
    }
    catalog = {
        "Q1": {
            "kind": "query",
            "artifact": "art_q1",
            "tool": "db_query__mes",
            "source": "mes",
            "columns": ["line_code", "good_qty", "yield_rate", "output_amt"],
            "row_count": 1,
        }
    }
    loader = lambda art: snap if art == "art_q1" else None
    raw = "产线 L01 良品数为 12,340 件，良率达到 98.5%，产值 12.5万 元。"
    bound = normalize_and_bind_fast_citations(raw, catalog, loader=loader)
    assert "[[v:Q1.r0.good_qty]]" in bound
    assert "[[v:Q1.r0.yield_rate|pct]]" in bound
    assert "[[v:Q1.r0.output_amt|万]]" in bound


def test_plain_day_recognizes_cast_and_pg_double_colon() -> None:
    assert plain_day("SELECT CAST(stat_date AS DATE) AS dt, SUM(x) FROM t GROUP BY 1", "dt", "sqlite") is True
    assert plain_day("SELECT stat_date::date AS dt, SUM(x) FROM t GROUP BY 1", "dt", "postgres") is True
    assert plain_day("SELECT DATE_TRUNC('month', stat_date) AS dt, SUM(x) FROM t GROUP BY 1", "dt", "postgres") is False


@pytest.mark.asyncio
async def test_differ_rows_mode_enforces_max_rows_ceiling() -> None:
    assert "too_many_rows" in DATA_ERRORS
    rows = [["A" if i % 2 == 0 else "B", float(i)] for i in range(MAX_ROWS_MODE + 5)]
    s = make_snap("Q1", "SELECT grp, val FROM t", ["grp", "val"], rows)
    with pytest.raises(CallError) as exc_info:
        await analyze("differ", "rows", snaps=[s], query="Q1", by="grp", value="val")
    assert exc_info.value.code == "too_many_rows"

    refused = await analyze(
        "differ", "rows", snaps=[s], refuse_data_errors=True, query="Q1", by="grp", value="val"
    )
    assert refused["status"] == "refused"
    assert refused["reason_code"] == "input_unavailable"
    assert str(MAX_ROWS_MODE) in refused["reason"]


def test_theil_sen_handles_large_denominator_lcm_and_extreme_floats() -> None:
    primes = [
        2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37, 41, 43, 47, 53, 59, 61, 67, 71,
        73, 79, 83, 89, 97, 101, 103, 107, 109, 113, 127, 131, 137, 139, 149, 151,
        157, 163, 167, 173, 179, 181, 191, 193, 197, 199, 211, 223, 227, 229, 233,
        239, 241, 251, 257, 263, 269, 271, 277, 281,
    ]
    xs = [Fraction(i + 1, p) for i, p in enumerate(primes)]
    ys = [x * 3 + Fraction(1, 7) for x in xs]
    slope = theil_sen(xs, ys)
    assert math.isclose(slope, 3.0, rel_tol=1e-9)

    huge_xs = [Fraction(0), Fraction(1, 10**200), Fraction(2, 10**200)]
    huge_ys = [Fraction(0), Fraction(10**200, 1), Fraction(2 * 10**200, 1)]
    huge_slope = theil_sen(huge_xs, huge_ys)
    assert huge_slope == Fraction(10**400, 1)
    assert math.isinf(analysis_stats._safe_float(huge_slope))


def test_lmdi_handles_many_factors_without_float_overflow() -> None:
    base = [10.0] * 300
    target = [20.0] * 300
    contribs = lmdi(base, target, 100.0, 250.0)
    assert len(contribs) == 300
    assert math.isclose(sum(contribs), 150.0, rel_tol=1e-9)


def test_streaming_csv_encoding_detection_and_grid_scan() -> None:
    gbk_bytes = "产线,良品数,备注\nL01,1200,正常生产\nL02,980,换型调试\n".encode("gb18030")
    assert _detect_csv_encoding(gbk_bytes) == ("gb18030", "strict")

    utf8_bom = b"\xef\xbb\xbfcol_a,col_b\n1,2\n"
    assert _detect_csv_encoding(utf8_bom) == ("utf-8-sig", "strict")

    scanned = csv_grid.scan(gbk_bytes)
    assert len(scanned.sheets) == 1
    assert scanned.sheets[0].bounds is not None
    assert scanned.sheets[0].bounds.max_row == 3
    assert scanned.sheets[0].bounds.max_col == 3

    parsed = csv_grid.parse(gbk_bytes)
    assert len(parsed.records) == 3
    assert parsed.records[0] == ["产线", "良品数", "备注"]
    assert parsed.records[1] == ["L01", "1200", "正常生产"]

    streamed = list(csv_grid.rows(parsed, min_row=1, max_row=3, min_col=1, max_col=3))
    assert len(streamed) == 3


def test_build_direct_graph_with_and_without_metrics_passes_validation() -> None:
    b = GroundingBundle(narrowed_sources=[SimpleNamespace(name="demo_db")])  # type: ignore[list-item]
    assert b.has_enabled_metrics is False

    g_no_metrics = build_direct_graph(bundle=b, has_metrics=False)
    res_no = validate_graph(GraphSpec.model_validate(g_no_metrics))
    assert res_no.ok is True, res_no.issues

    g_with_metrics = build_direct_graph(bundle=b, has_metrics=True)
    res_yes = validate_graph(GraphSpec.model_validate(g_with_metrics))
    assert res_yes.ok is True, res_yes.issues
