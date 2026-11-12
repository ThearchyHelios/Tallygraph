"""跨源指标本体穿透验收与回归评测用的三源虚构制造库（与任何真实业务无关）。

    python -m tests.fixtures.acceptance.multi_source_ontology <输出目录> [--today YYYY-MM-DD]

生成「今天」之前 24 天（截止昨天）的三份独立 SQLite 业务库，随机数种子固定：同一个「今天」生成的三份库逐字节一致。
三个数据源使用异构物理列名与编码格式，只能经标准实体 `production_line`（产线）对齐：

1. `ops_shift`（班次报工库）：
   - `shift_output(id, shift_date, line_code, actual_qty, good_qty, scrap_qty, run_hours, operator_phone)`
     其中 `line_code` 取值 `L1..L4`，`operator_phone` 为遮罩敏感列。
   - `energy_readings(id, shift_date, line_code, meter_kwh, settled_mwh)`
     用于核对跨量纲换算（千瓦时 vs 兆瓦时，`unit_factor = 1000`）的对账场景。
2. `equip_master`（设备与停机主数据库）：
   - `line_specs(equip_line_id, line_name, rated_hourly_qty, updated_day)`
     其中 `equip_line_id` 取值 `EQ-L1..EQ-L4`。
   - `downtime_events(id, event_date, equip_line_id, net_run_ratio, unplanned_min, fault_category)`
     记录各设备产线每日有效运转率与非计划停机分钟数。
3. `quality_scrap`（质量报废台账库）：
   - `scrap_ledger(id, inspect_date, workcenter_code, scrap_units, defect_family, inspector_id)`
     其中 `workcenter_code` 取值 `WC-01..WC-04`，`inspector_id` 为遮罩敏感列。

埋入的确定性效应（互不干扰）：
- **S1（契约越界 → 跨库主数据过期失配）**：`ops_shift` 中 `L3` 昨日实际每小时产量 `108` 件（8 小时产 864 件），但 `equip_master.line_specs`
  中 `EQ-L3` 的 `rated_hourly_qty` 仍停留在旧版 `80` 件/小时，导致负荷率算得 `135%`（违反 `[0, 100]` 契约上界）。
- **S2a（跨源对账断更 → 对侧源停更早停）**：`quality_scrap.scrap_ledger` 截止于「今天 - 3 天」（最近两天断更，0 行），而 `ops_shift`
  照常落库；健康检查应识别 `date_stalled` 并在阶段一停止，不误判为业务报废清零。
- **S2b（跨源量纲与超差对账）**：`ops_shift.energy_readings` 昨日 `L1..L3` 按 `1000 kWh = 1 MWh` 完全对齐，`L4` 抄表 `14800 kWh`
  但结算导入记为 `12.5 MWh`（换算后相差 `+2300 kWh`，`+18.4%`），`reconcile` 应判 `diverged`。
- **S3（统计异动 → 切片定位 `L2` → 跨源设备停机同步异动 `EQ-L2`）**：昨日 `L2` 合格产量从常态约 `900` 跌至 `612`（`-32%`），
  穿透标准实体 `L2 -> EQ-L2` 后，`equip_master.downtime_events` 中 `EQ-L2` 非计划停机从常态 `11..14` 分钟飙升至 `96` 分钟，
  `cochange` 判 `co_anomalous`（仅陈述同期同步异动，不下因果断言）。
- **对照 1（常规波动不误报）**：`L1` 过去 24 天合格产量在 `892..916` 窄幅波动，昨日 `898` 相对前日 `908` 属常规波动（`within_band`）。
- **对照 2（主指标异动但上游正常 → `target_normal`）**：`今天 - 3 天` 时 `EQ-L4` 有效运转率从 `0.90` 跌至 `0.73`，但同期非计划停机
  仍为常态 `12` 分钟，`cochange` 判 `target_normal`，不强行归咎于设备非计划停机。
"""
from __future__ import annotations

import argparse
import random
import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

DAYS = 24
SEED = 20261022

SOURCE_OPS = "ops_shift"
SOURCE_EQUIP = "equip_master"
SOURCE_QUALITY = "quality_scrap"
ALL_SOURCES = (SOURCE_OPS, SOURCE_EQUIP, SOURCE_QUALITY)

LINES: tuple[tuple[str, str, str, str], ...] = (
    ("L1", "EQ-L1", "WC-01", "星梭一线"),
    ("L2", "EQ-L2", "WC-02", "星梭二线"),
    ("L3", "EQ-L3", "WC-03", "星梭三线"),
    ("L4", "EQ-L4", "WC-04", "星梭四线"),
)

OPS_SCHEMA = """
CREATE TABLE shift_output (
  id INTEGER PRIMARY KEY,
  shift_date DATE NOT NULL,
  line_code TEXT NOT NULL,
  actual_qty INTEGER NOT NULL,
  good_qty INTEGER NOT NULL,
  scrap_qty INTEGER NOT NULL,
  run_hours REAL NOT NULL,
  operator_phone TEXT NOT NULL
);
CREATE TABLE energy_readings (
  id INTEGER PRIMARY KEY,
  shift_date DATE NOT NULL,
  line_code TEXT NOT NULL,
  meter_kwh REAL NOT NULL,
  settled_mwh REAL NOT NULL
);
"""

EQUIP_SCHEMA = """
CREATE TABLE line_specs (
  equip_line_id TEXT PRIMARY KEY,
  line_name TEXT NOT NULL,
  rated_hourly_qty REAL NOT NULL,
  updated_day DATE NOT NULL
);
CREATE TABLE downtime_events (
  id INTEGER PRIMARY KEY,
  event_date DATE NOT NULL,
  equip_line_id TEXT NOT NULL,
  net_run_ratio REAL NOT NULL,
  unplanned_min REAL NOT NULL,
  fault_category TEXT NOT NULL
);
"""

QUALITY_SCHEMA = """
CREATE TABLE scrap_ledger (
  id INTEGER PRIMARY KEY,
  inspect_date DATE NOT NULL,
  workcenter_code TEXT NOT NULL,
  scrap_units INTEGER NOT NULL,
  defect_family TEXT NOT NULL,
  inspector_id TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class BuiltDatabases:
    today: date
    ops_path: Path
    equip_path: Path
    quality_path: Path


def build(out_dir: Path | str, today: date | None = None) -> BuiltDatabases:
    """在 out_dir 下生成 ops_shift.db、equip_master.db、quality_scrap.db（已存在则覆盖）。"""
    root = Path(out_dir)
    root.mkdir(parents=True, exist_ok=True)
    today = today or date(2026, 10, 22)
    rng = random.Random(SEED)

    ops_path = root / f"{SOURCE_OPS}.db"
    equip_path = root / f"{SOURCE_EQUIP}.db"
    quality_path = root / f"{SOURCE_QUALITY}.db"
    for p in (ops_path, equip_path, quality_path):
        if p.exists():
            p.unlink()

    yesterday = today - timedelta(days=1)
    control2_day = yesterday - timedelta(days=2)
    stalled_after = yesterday - timedelta(days=2)  # quality_scrap 停在 yesterday - 2 天，最近两天缺失
    days = [today - timedelta(days=offset) for offset in range(DAYS, 0, -1)]

    shift_rows: list[tuple[Any, ...]] = []
    energy_rows: list[tuple[Any, ...]] = []
    downtime_rows: list[tuple[Any, ...]] = []
    scrap_rows: list[tuple[Any, ...]] = []

    base_good = {"L1": 904, "L2": 900, "L3": 844, "L4": 888}
    base_scrap = {"L1": 18, "L2": 20, "L3": 19, "L4": 17}
    phones = {
        "L1": "138-0010-0101",
        "L2": "138-0010-0102",
        "L3": "138-0010-0103",
        "L4": "138-0010-0104",
    }
    inspectors = {
        "WC-01": "INSP-9001",
        "WC-02": "INSP-9002",
        "WC-03": "INSP-9003",
        "WC-04": "INSP-9004",
    }

    shift_id = 1
    energy_id = 1
    downtime_id = 1
    scrap_id = 1

    for idx, d in enumerate(days):
        ds = d.isoformat()
        wave = (-1 if idx % 2 else 1) * (4 + (idx % 3) * 2)  # 确定性小幅波动 ±4..±8
        for line_code, equip_id, wc_code, _ in LINES:
            noise = rng.choice((-3, -1, 1, 3))
            good = base_good[line_code] + wave + noise
            scrap = base_scrap[line_code] + (idx % 3) - 1
            run_hours = 8.0
            net_ratio = round(0.905 + ((idx % 4) - 1.5) * 0.006 + noise * 0.001, 4)
            unplanned = float(12 + (-1 if idx % 2 else 1) * (1 + (idx % 3)) + (0.5 if noise > 0 else -0.5))
            fault_cat = "routine_check"

            # 对照 1：L1 末尾两日固定为平缓常规波动（前日 908 → 昨日 898，-1.1%）
            if line_code == "L1" and d == yesterday - timedelta(days=1):
                good = 908
            elif line_code == "L1" and d == yesterday:
                good = 898

            # S1：L3 昨日实际产率 108 件/小时（864 件 / 8 小时），对比 equip_master 过期额定 80 件/小时 = 135%
            if line_code == "L3" and d == yesterday:
                good = 844
                scrap = 20

            # S3：L2 昨日产率显著下跌，同时 EQ-L2 非计划停机飙升至 96 分钟（统计伴随异动）
            if line_code == "L2" and d == yesterday:
                good = 612
                net_ratio = 0.6320
                unplanned = 96.0
                fault_cat = "feeder_jam"

            # 对照 2：EQ-L4 在 control2_day 有效运转率明显下跌，但非计划停机保持常态 12 分钟（target_normal）
            if line_code == "L4" and d == control2_day:
                good = 720
                net_ratio = 0.7280
                unplanned = 12.0
                fault_cat = "routine_check"

            actual = good + scrap
            shift_rows.append((shift_id, ds, line_code, actual, good, scrap, run_hours, phones[line_code]))
            shift_id += 1

            # 能耗表：昨日 L4 出现结算导入偏差（14800 kWh vs 12.5 MWh），其余对齐
            kwh = round(actual * 14.0, 1)
            mwh = round(kwh / 1000.0, 4)
            if line_code == "L4" and d == yesterday:
                kwh = 14800.0
                mwh = 12.5
            elif d == yesterday:
                kwh = 12000.0 + (LINES.index((line_code, equip_id, wc_code, _)) * 500.0)
                mwh = round(kwh / 1000.0, 4)
            energy_rows.append((energy_id, ds, line_code, kwh, mwh))
            energy_id += 1

            downtime_rows.append((downtime_id, ds, equip_id, net_ratio, unplanned, fault_cat))
            downtime_id += 1

            # S2a：quality_scrap 在 stalled_after 之后断更（最近两天 0 行）
            if d <= stalled_after:
                scrap_rows.append((scrap_id, ds, wc_code, scrap, "surface_burr", inspectors[wc_code]))
                scrap_id += 1

    specs_rows = [
        ("EQ-L1", "星梭一线", 120.0, (today - timedelta(days=30)).isoformat()),
        ("EQ-L2", "星梭二线", 120.0, (today - timedelta(days=30)).isoformat()),
        # S1 根因：EQ-L3 技改提速后主数据未更新，仍为 80 件/小时
        ("EQ-L3", "星梭三线", 80.0, "2026-06-01"),
        ("EQ-L4", "星梭四线", 120.0, (today - timedelta(days=30)).isoformat()),
    ]

    ops_db = sqlite3.connect(ops_path)
    ops_db.executescript(OPS_SCHEMA)
    ops_db.executemany("INSERT INTO shift_output VALUES (?, ?, ?, ?, ?, ?, ?, ?)", shift_rows)
    ops_db.executemany("INSERT INTO energy_readings VALUES (?, ?, ?, ?, ?)", energy_rows)
    ops_db.commit()
    ops_db.close()

    equip_db = sqlite3.connect(equip_path)
    equip_db.executescript(EQUIP_SCHEMA)
    equip_db.executemany("INSERT INTO line_specs VALUES (?, ?, ?, ?)", specs_rows)
    equip_db.executemany("INSERT INTO downtime_events VALUES (?, ?, ?, ?, ?, ?)", downtime_rows)
    equip_db.commit()
    equip_db.close()

    quality_db = sqlite3.connect(quality_path)
    quality_db.executescript(QUALITY_SCHEMA)
    quality_db.executemany("INSERT INTO scrap_ledger VALUES (?, ?, ?, ?, ?, ?)", scrap_rows)
    quality_db.commit()
    quality_db.close()

    return BuiltDatabases(today=today, ops_path=ops_path, equip_path=equip_path, quality_path=quality_path)


async def seed_ontology_fixture(
    tmp_path: Path,
    scratch_source: Any,
    *,
    today: date | None = None,
) -> dict[str, Any]:
    """登记三源 SQLite、标准实体 `production_line`、断更变更记录及三条跨源已发布口径工作流。"""
    from app.api.workflows import commit_publish
    from app.data import source_changes as sc
    from app.data.entities import create_entity, entity_to_dict
    from app.db.base import SessionLocal
    from app.db.models import DataSource, Workflow, WorkflowVersion

    built = build(tmp_path, today=today)
    ops_id = await scratch_source(
        SOURCE_OPS, built.ops_path, options={"mask_columns": ["operator_phone"]}, schema=True
    )
    equip_id = await scratch_source(SOURCE_EQUIP, built.equip_path, schema=True)
    quality_id = await scratch_source(
        SOURCE_QUALITY, built.quality_path, options={"mask_columns": ["inspector_id"]}, schema=True
    )

    stalled_day = (built.today - timedelta(days=3)).isoformat()
    async with SessionLocal() as session:
        quality_src = await session.get(DataSource, quality_id)
        await sc.record(
            session,
            quality_src,
            sc.NewChange(
                kind=sc.DATE_STALLED,
                table="scrap_ledger",
                column="inspect_date",
                severity="action",
                before={"max_date": stalled_day},
                after={"max_date": stalled_day, "missing_days": 2},
                key=stalled_day,
            ),
            sc.Detected(job_kind="profile", trigger="manual", actor="fixture_builder"),
        )
        await session.commit()

    async with SessionLocal() as session:
        # 建三条分属不同数据源的口径工作流：先落基础工作流以拿到固定 workflow_id，再互相挂本体边
        wf_equip = Workflow(name="设备主数据与停机口径", description="设备额定产能与非计划停机口径", graph={"nodes": [], "edges": []})
        wf_quality = Workflow(name="质量报废台账口径", description="质量检验报废台账口径", graph={"nodes": [], "edges": []})
        wf_ops = Workflow(name="班次产率与负荷率口径", description="班次合格产率、负荷率与报工报废口径", graph={"nodes": [], "edges": []})
        session.add_all([wf_equip, wf_quality, wf_ops])
        await session.flush()

        equip_graph = {
            "nodes": [
                {
                    "id": "in_eq",
                    "type": "input",
                    "position": {"x": 0, "y": 0},
                    "data": {
                        "label": "入口",
                        "config": {
                            "fields": [
                                {"name": "equip_line", "label": "设备产线", "type": "text", "default": "EQ-L3"},
                                {"name": "stat_day", "label": "统计日期", "type": "text", "default": (built.today - timedelta(days=1)).isoformat()},
                            ]
                        },
                    },
                },
                {
                    "id": "q_spec",
                    "type": "tool",
                    "position": {"x": 220, "y": 0},
                    "data": {
                        "label": "查额定产能",
                        "config": {
                            "tool": f"db_query__{SOURCE_EQUIP}",
                            "args": {
                                "sql": "SELECT equip_line_id, rated_hourly_qty FROM line_specs WHERE equip_line_id = '{{ input.equip_line }}'",
                            },
                        },
                    },
                },
                {
                    "id": "q_down",
                    "type": "tool",
                    "position": {"x": 220, "y": 140},
                    "data": {
                        "label": "查停机时长",
                        "config": {
                            "tool": f"db_query__{SOURCE_EQUIP}",
                            "args": {
                                "sql": "SELECT event_date, unplanned_min FROM downtime_events WHERE equip_line_id = '{{ input.equip_line }}' AND event_date = '{{ input.stat_day }}'",
                            },
                        },
                    },
                },
                {
                    "id": "m_equip",
                    "type": "metrics",
                    "position": {"x": 480, "y": 60},
                    "data": {
                        "label": "设备指标卡",
                        "config": {
                            "caliber": "设备主数据与非计划停机日口径",
                            "metrics": [
                                {
                                    "id": "rated_hourly_qty",
                                    "name": "额定每小时产量",
                                    "expression": "cell(nodes.q_spec, 0, 'rated_hourly_qty')",
                                    "unit": "件/小时",
                                    "semantic": {
                                        "entities": [{"name": "产线", "key": "equip_line_id", "entity_key": "production_line"}]
                                    },
                                },
                                {
                                    "id": "unplanned_downtime_min",
                                    "name": "非计划停机时长",
                                    "expression": "cell(nodes.q_down, 0, 'unplanned_min')",
                                    "unit": "分钟",
                                    "semantic": {
                                        "grain": "day",
                                        "time": {"column": "event_date", "timezone": "Asia/Shanghai"},
                                        "entities": [{"name": "产线", "key": "equip_line_id", "entity_key": "production_line"}],
                                    },
                                },
                            ],
                        },
                    },
                },
                {"id": "out_eq", "type": "output", "position": {"x": 720, "y": 60}, "data": {"label": "出口", "config": {}}},
            ],
            "edges": [
                {"source": "in_eq", "target": "q_spec"},
                {"source": "in_eq", "target": "q_down"},
                {"source": "q_spec", "target": "m_equip"},
                {"source": "q_down", "target": "m_equip"},
                {"source": "m_equip", "target": "out_eq"},
            ],
        }

        quality_graph = {
            "nodes": [
                {
                    "id": "in_q",
                    "type": "input",
                    "position": {"x": 0, "y": 0},
                    "data": {
                        "label": "入口",
                        "config": {
                            "fields": [
                                {"name": "workcenter", "label": "工作中心", "type": "text", "default": "WC-02"},
                                {"name": "inspect_day", "label": "检验日期", "type": "text", "default": (built.today - timedelta(days=1)).isoformat()},
                            ]
                        },
                    },
                },
                {
                    "id": "q_scrap",
                    "type": "tool",
                    "position": {"x": 240, "y": 0},
                    "data": {
                        "label": "查质量报废",
                        "config": {
                            "tool": f"db_query__{SOURCE_QUALITY}",
                            "args": {
                                "sql": "SELECT COALESCE(SUM(scrap_units), 0) AS scrap_units FROM scrap_ledger WHERE workcenter_code = '{{ input.workcenter }}' AND inspect_date = '{{ input.inspect_day }}'",
                            },
                        },
                    },
                },
                {
                    "id": "m_quality",
                    "type": "metrics",
                    "position": {"x": 480, "y": 0},
                    "data": {
                        "label": "质量报废卡",
                        "config": {
                            "caliber": "质量检验台账日报废口径",
                            "metrics": [
                                {
                                    "id": "ledger_scrap_units",
                                    "name": "台账报废件数",
                                    "expression": "cell(nodes.q_scrap, 0, 'scrap_units')",
                                    "unit": "件",
                                    "semantic": {
                                        "grain": "day",
                                        "time": {"column": "inspect_date", "timezone": "Asia/Shanghai"},
                                        "entities": [{"name": "产线", "key": "workcenter_code", "entity_key": "production_line"}],
                                    },
                                }
                            ],
                        },
                    },
                },
                {"id": "out_q", "type": "output", "position": {"x": 720, "y": 0}, "data": {"label": "出口", "config": {}}},
            ],
            "edges": [
                {"source": "in_q", "target": "q_scrap"},
                {"source": "q_scrap", "target": "m_quality"},
                {"source": "m_quality", "target": "out_q"},
            ],
        }

        ops_graph = {
            "nodes": [
                {
                    "id": "in_ops",
                    "type": "input",
                    "position": {"x": 0, "y": 0},
                    "data": {
                        "label": "入口",
                        "config": {
                            "fields": [
                                {"name": "line_code", "label": "产线编码", "type": "text", "default": "L3"},
                                {"name": "shift_day", "label": "班次日期", "type": "text", "default": (built.today - timedelta(days=1)).isoformat()},
                            ]
                        },
                    },
                },
                {
                    "id": "q_ops",
                    "type": "tool",
                    "position": {"x": 240, "y": 0},
                    "data": {
                        "label": "查班次产出",
                        "config": {
                            "tool": f"db_query__{SOURCE_OPS}",
                            "args": {
                                "sql": "SELECT actual_qty, good_qty, scrap_qty, run_hours FROM shift_output WHERE line_code = '{{ input.line_code }}' AND shift_date = '{{ input.shift_day }}'",
                            },
                        },
                    },
                },
                {
                    "id": "m_ops",
                    "type": "metrics",
                    "position": {"x": 500, "y": 0},
                    "data": {
                        "label": "班次指标卡",
                        "config": {
                            "caliber": "班次负荷率、合格产量与报工报废口径",
                            "metrics": [
                                {
                                    "id": "load_rate_pct",
                                    "name": "产线负荷率",
                                    "expression": "round((cell(nodes.q_ops, 0, 'actual_qty') / cell(nodes.q_ops, 0, 'run_hours')) / 80.0 * 100, 2)",
                                    "unit": "%",
                                    "contract": {"minimum": 0, "maximum": 100, "unit": "%"},
                                    "semantic": {
                                        "grain": "day",
                                        "time": {"column": "shift_date", "timezone": "Asia/Shanghai"},
                                        "entities": [{"name": "产线", "key": "line_code", "entity_key": "production_line"}],
                                    },
                                    "links": [
                                        {
                                            "kind": "factor",
                                            "target": {
                                                "workflow_id": wf_equip.id,
                                                "card_id": "m_equip",
                                                "metric_id": "rated_hourly_qty",
                                            },
                                            "role": "denominator",
                                            "entity_key": "production_line",
                                            "status": "confirmed",
                                            "rationale": "负荷率分母取设备主数据的额定每小时产量",
                                        },
                                        {
                                            "kind": "upstream_driver",
                                            "target": {
                                                "workflow_id": wf_equip.id,
                                                "card_id": "m_equip",
                                                "metric_id": "unplanned_downtime_min",
                                            },
                                            "entity_key": "production_line",
                                            "status": "proposed",
                                            "rationale": "候选未确认边：验证运行期不会自动沿 proposed 边穿透",
                                        },
                                    ],
                                },
                                {
                                    "id": "good_output_qty",
                                    "name": "班次合格产量",
                                    "expression": "cell(nodes.q_ops, 0, 'good_qty')",
                                    "unit": "件",
                                    "semantic": {
                                        "grain": "day",
                                        "time": {"column": "shift_date", "timezone": "Asia/Shanghai"},
                                        "entities": [{"name": "产线", "key": "line_code", "entity_key": "production_line"}],
                                    },
                                    "links": [
                                        {
                                            "kind": "upstream_driver",
                                            "target": {
                                                "workflow_id": wf_equip.id,
                                                "card_id": "m_equip",
                                                "metric_id": "unplanned_downtime_min",
                                            },
                                            "entity_key": "production_line",
                                            "lag_periods": 0,
                                            "status": "confirmed",
                                            "rationale": "设备非计划停机时长升高会同步压缩班次有效产出时间",
                                        }
                                    ],
                                },
                                {
                                    "id": "shift_scrap_qty",
                                    "name": "报工报废件数",
                                    "expression": "cell(nodes.q_ops, 0, 'scrap_qty')",
                                    "unit": "件",
                                    "semantic": {
                                        "grain": "day",
                                        "time": {"column": "shift_date", "timezone": "Asia/Shanghai"},
                                        "entities": [{"name": "产线", "key": "line_code", "entity_key": "production_line"}],
                                    },
                                    "links": [
                                        {
                                            "kind": "reconciles_with",
                                            "target": {
                                                "workflow_id": wf_quality.id,
                                                "card_id": "m_quality",
                                                "metric_id": "ledger_scrap_units",
                                            },
                                            "entity_key": "production_line",
                                            "rel_tol": 0.02,
                                            "status": "confirmed",
                                            "rationale": "班次报工报废数应与质量检验台账日报废件数在 2% 容差内对齐",
                                        }
                                    ],
                                },
                            ],
                        },
                    },
                },
                {"id": "out_ops", "type": "output", "position": {"x": 740, "y": 0}, "data": {"label": "出口", "config": {}}},
            ],
            "edges": [
                {"source": "in_ops", "target": "q_ops"},
                {"source": "q_ops", "target": "m_ops"},
                {"source": "m_ops", "target": "out_ops"},
            ],
        }

        snapshots: list[tuple[Workflow, WorkflowVersion]] = []
        for wf, graph in ((wf_equip, equip_graph), (wf_quality, quality_graph), (wf_ops, ops_graph)):
            wf.graph = graph
            wf.version = 1
            snap = WorkflowVersion(workflow_id=wf.id, version=1, graph=graph, note="初始版本")
            session.add(snap)
            snapshots.append((wf, snap))
        # 先提交未写锁的草稿行，再逐一走 commit_publish（避免 SQLite 单写锁下嵌套会话写工件抢锁）
        await session.commit()

        for wf, snap in snapshots:
            await session.refresh(wf)
            await session.refresh(snap)
            await commit_publish(session, wf, snap, "published", "fixture_builder")

        entity_row = await create_entity(
            session,
            {
                "key": "production_line",
                "label": "产线",
                "description": "星梭示范装配厂标准产线实体，桥接班次报工、设备主数据与质量报废台账三套异构编码",
                "aliases": ["装配产线", "车间产线", "工作中心"],
                "bindings": [
                    {
                        "source_name": SOURCE_OPS,
                        "table_name": "shift_output",
                        "column_name": "line_code",
                        "status": "confirmed",
                    },
                    {
                        "source_name": SOURCE_OPS,
                        "table_name": "energy_readings",
                        "column_name": "line_code",
                        "status": "confirmed",
                    },
                    {
                        "source_name": SOURCE_EQUIP,
                        "table_name": "line_specs",
                        "column_name": "equip_line_id",
                        "status": "confirmed",
                    },
                    {
                        "source_name": SOURCE_EQUIP,
                        "table_name": "downtime_events",
                        "column_name": "equip_line_id",
                        "status": "confirmed",
                    },
                    {
                        "source_name": SOURCE_QUALITY,
                        "table_name": "scrap_ledger",
                        "column_name": "workcenter_code",
                        "status": "confirmed",
                    },
                    {
                        "source_name": SOURCE_QUALITY,
                        "table_name": "unverified_archive",
                        "column_name": "legacy_line",
                        "status": "proposed",
                    },
                ],
                "caliber_params": [
                    {"workflow_id": wf_ops.id, "param_name": "line_code", "status": "confirmed"},
                    {"workflow_id": wf_equip.id, "param_name": "equip_line", "status": "confirmed"},
                    {"workflow_id": wf_quality.id, "param_name": "workcenter", "status": "confirmed"},
                ],
                "members": [
                    {
                        "value": code,
                        "label": name,
                        "aliases": [code, equip_id, wc_code],
                        "source_codes": {SOURCE_OPS: code, SOURCE_EQUIP: equip_id, SOURCE_QUALITY: wc_code},
                    }
                    for code, equip_id, wc_code, name in LINES
                ],
            },
            actor="fixture_builder",
        )
        entity_dict = entity_to_dict(entity_row)
        await session.commit()

    return {
        "today": built.today,
        "paths": {"ops": built.ops_path, "equip": built.equip_path, "quality": built.quality_path},
        "source_ids": {SOURCE_OPS: ops_id, SOURCE_EQUIP: equip_id, SOURCE_QUALITY: quality_id},
        "workflow_ids": {"ops": wf_ops.id, "equip": wf_equip.id, "quality": wf_quality.id},
        "entity_id": entity_dict["id"],
        "entity": entity_dict,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="生成跨源指标本体穿透用的三源虚构制造库")
    parser.add_argument("out_dir")
    parser.add_argument("--today", type=date.fromisoformat, default=None)
    args = parser.parse_args()
    built = build(args.out_dir, args.today)
    print(f"{built.ops_path}\n{built.equip_path}\n{built.quality_path}")


if __name__ == "__main__":
    main()
