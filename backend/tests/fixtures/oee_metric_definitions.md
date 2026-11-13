# TEEP / OEE 及相关指标公式总表（合成测试夹具）

> 依据：示例车间计算规范（合成回归夹具）。
> 范围：日报、周报、月报、看板共用的 OEE、TEEP、设备计划利用率及相关效率指标。
> 说明：日报/周月报的比率类指标可由「配置 → 计算口径」里的 `metric_definitions` 覆盖；
> 下表是**代码内置默认口径**，配置公式生效时以配置为准（`calc/config_seed.py`、`calc/formula.py`）。

## 0. 符号约定

| 符号 | 含义 | 源字段/来源 |
|---|---|---|
| `T_open` | 计划开班时长（秒） | `EXT_VAL_01` |
| `T_stop` | 计划内停机时长（秒） | `EXT_VAL_05` |
| `T_run_plan` | 计划运行时长 = `T_open − T_stop` | 计算值 |
| `T_open_act` | 实际开班时长（秒） | `EXT_VAL_02` |
| `T_run_act` | 实际运行时长（秒） | `EXT_VAL_03` |
| `T_stop_act` | 实际停机时长（秒） | `EXT_VAL_04` |
| `T_stop_unp` | 计划外停机时长（秒） | `EXT_VAL_06` |
| `V_plan` | 计划线速（秒/圈） | `EXT_VAL_12` |
| `M_plan` | 开班量产载具数 | `EXT_VAL_14` |
| `M_trial` | 开班试制载具数 | `EXT_VAL_15` |
| `M_actual` | 实际生产载具数 | `EXT_VAL_31` |
| `M_scrap` | 报废折算件数 | `SCRAP_RECORD` × `CARRIER_FACTOR.CONV_FACTOR` |
| `M_good` | 良品/有效载具数 = `M_actual − M_scrap` | 计算值 |
| `M_line` | 产线额定工位数 | 产线主数据 `LINE_MASTER.STD_SLOTS` |
| `N_cycle` | 班次总圈数 | `EXT_VAL_54` |
| `N_no_stop` | 无停机圈数 | `EXT_VAL_55` |
| `OEE_src` | 源系统 OEE | `EXT_VAL_36` |
| `OEE_eff` | 有效 OEE | `calc/teep_engine.py:190` |
| `T_nat` | 自然时长（秒） | 班次 43,200；天 86,400；周期 = 天数 × 86,400；多条线合并时再 × 产线条数 |

## 1. 基础量

| 量 | 公式 | 缺失/边界处理 | 实现 |
|---|---|---|---|
| 计划运行时长 | `T_run_plan = max(0, T_open − T_stop)` | `T_open`/`T_stop` 任一缺失 → `None`，不写 0 | `calc/daily_metrics.py:1560`、`calc/teep_engine.py:72` |
| 理论圈数 | `planned_cycle_count = T_run_plan ÷ V_plan` | `V_plan ≤ 0` → OEE 不算 | `calc/daily_metrics.py:1496` |
| 计划每圈载具数 | `M_cycle = M_plan + M_trial` | 任一缺失 → OEE 不算；`≤ 0` → OEE 不算 | 同上 `:1479` |
| 理论载具数 | `M_theory = planned_cycle_count × M_cycle` | `≤ 0` → OEE 不算 | 同上 `:1497` |
| 报废折算件数 | `M_scrap = Σ(报废零件数 × CONV_FACTOR)` | 无报废记录 → 0；缺 `CONV_FACTOR` → 按默认系数 **0.5** 折算并点名 | 同上 `:1417-1459` |
| 良品载具数 | `M_good = M_actual − M_scrap` | 负数 → OEE 不算 | 同上 `:1498`、`:1504` |
| 载具工位次 | `M_plan_slots = (M_plan + M_trial) × N_cycle`；`M_line_slots = M_line × N_cycle` | 成型线专用；装配线无此项 | `calc/teep_engine.py:114` |
| 自然时长 | `T_nat = 43,200`（班次）/ `86,400`（天）/ `周期天数 × 86,400`（周月） | 多线合并时 `T_nat = 单线秒数 × 产线条数` | `calc/teep_engine.py:49`、`calc/period_metrics.py:1176` |

## 2. OEE 系列

| 指标 | 公式 | 口径/说明 | 实现 |
|---|---|---|---|
| 成型线公式 OEE（单班） | `OEE_formula = M_good ÷ M_theory × 100`<br>展开：`(M_actual − M_scrap) ÷ [((T_open − T_stop) ÷ V_plan) × (M_plan + M_trial)] × 100` | 本口径**不是**经典 `时间开动率 × 性能开动率 × 合格率` 三率相乘；OEE > 100% 属正常超产，> 200% 判采集异常 | `calc/daily_metrics.py:1398-1512` |
| 装配线 OEE | `OEE_assembly = OEE_src`（`EXT_VAL_36`） | 装配线无载具项、无计划线速，公式无从算起 | `calc/teep_engine.py:206-209` |
| 有效 OEE（报表统一口径） | `OEE_eff = OEE_formula`（成型线）；`OEE_eff = OEE_src`（装配线） | 成型线算不出时返回 `None`，**不回退** `EXT_VAL_36` | `calc/teep_engine.py:190-209` |
| 班次日/单班 OEE | 已开班取 `OEE_eff`；未开班仪表盘显示 0% | 未开班不是「OEE=0」，只是不展示源表默认值 | `calc/daily_metrics.py:2306-2326` |
| 日 OEE（全天聚合） | `Σ(OEE_eff_i × T_open_act_i) ÷ Σ T_open_act_i`；仅 1 个有效班次时直接取该值；权重全缺时退回算术平均 | 按实际开班时长加权，不按计划产量 | `calc/daily_metrics.py:2209-2227` |
| 周/月报 03 表班次 OEE | 同「有效 OEE」，并按 `T_open_act` 加权到早班/晚班 | 与日报逐班值同源 | `calc/period_metrics.py:755-776`、`:1479-1513` |
| 周/月报周期总表 OEE | `Σ(OEE_src_i × T_open_act_i) ÷ Σ T_open_act_i` | **代码现状**：周期总表取 `EXT_VAL_36` 加权，不是 `OEE_eff`；与 TEEP 的 OEE 因子可能分叉，见文末「口径提醒」 | `calc/period_metrics.py:1153-1173` |
| 周期公式 OEE | `Σ M_good ÷ Σ M_theory × 100`，只统计 OEE 输入齐全的班次 | 用于和主 OEE 对账 | `calc/daily_metrics.py:2229-2244` |
| OEE 差值 | `OEE_gap = OEE_day − OEE_formula` | 主 OEE 与公式 OEE 的偏差 | `calc/daily_metrics.py:2245-2249` |
| OEE 是否算全 | `有效班次数 ÷ 应有班次数`；`有效 == 应有` 时 `oee_calculation_complete = 1` | 有效班次 = OEE 输入齐全的已开班班次 | `calc/daily_metrics.py:2278-2280` |

## 3. TEEP、设备计划利用率与两个「U」

| 指标 | 公式 | 口径/说明 | 实现 |
|---|---|---|---|
| 载具装载率（成型线） | `U_load = Σ((M_plan + M_trial) × N_cycle) ÷ Σ(M_line × N_cycle) × 100`<br>单班圈数约掉：`(M_plan + M_trial) ÷ M_line × 100` | 一圈都没转时退回各班等权：`Σ(M_plan + M_trial) ÷ ΣM_line × 100`；缺 `M_line` 或缺开班载具数 → `None`，**不**退回不乘装载率 | `calc/teep_engine.py:127-151` |
| 载具装载率（装配线） | 恒 `100` | 装配线没有载具项；乘进利用率不改变结果 | `calc/teep_engine.py:141-143` |
| 时间利用率 | `U_time = Σ T_run_plan ÷ T_nat × 100` | `T_nat` 按粒度取；未开班也能算 | `calc/teep_engine.py:262-264` |
| 设备计划利用率（成型线） | `U_plan = U_load × (Σ T_run_plan ÷ T_nat)`<br>即 `U_load × U_time ÷ 100` | 百分数乘比例，结果仍是百分数；只看计划值，停产日会被日历时长如实拉低 | `calc/teep_engine.py:169-187` |
| 设备计划利用率（装配线） | `U_plan = Σ T_run_plan ÷ T_nat × 100` | `U_load = 100`，公式退化为时间利用率 | 同上 |
| 单班 TEEP | `TEEP = OEE_eff × U_plan ÷ 100` | 两个因子都是百分数，乘完再 ÷100；任一因子缺失 → `None`，不写 0 | `calc/teep_engine.py:230-251` |
| 周期 TEEP（周/月/组级） | `TEEP = Σ(OEE_eff_i × T_open_act_i) ÷ Σ T_open_act_i × U_plan ÷ 100` | 周期 OEE 因子按实际开班时长加权；利用率分子分母跨线各自相加，`T_nat` 按产线条数放大 | `calc/teep_engine.py:212-241`、`calc/period_metrics.py:1184-1187` |
| 单班 TEEP 等价式（核对用） | `TEEP = M_good ÷ (T_nat ÷ V_plan × M_line) × 100` | 即「合格载具 ÷ 满载产能（日历时间口径）」；可用于反查 TEEP 是否被装载率放大 | `calc/teep_engine.py:19-26`、`docs/metric_handbook.md:245` |
| TEEP 构成 | `U_plan = U_load × U_time`；`TEEP = OEE_eff × U_plan` | 报表「班次信息概览」按这个构成展示，便于评审复核 | `calc/teep_engine.py:254-295` |

## 4. 其他相关指标

| 指标 | 公式 | 口径/说明 | 实现 |
|---|---|---|---|
| 计划完成率（班次） | `C = EXT_VAL_07` 直读 | **禁止**用 `实际产量 ÷ 计划产量` 重算；装配线类型B 实际为 0 时按类型A 报工 ÷ 类型B 计划重算 | `calc/daily_metrics.py:1586-1592` |
| 计划完成率（日/周期） | `Σ(C_i × plan_output_i) ÷ Σ plan_output_i`；权重全缺时算术平均 | 按计划产量加权，不是简单平均 | `calc/daily_metrics.py:2196-2201` |
| 时间开动率 / 设备利用率 | `T_run_act ÷ T_open_act × 100` | 两者在本口径里同公式、不同展示名 | `calc/daily_metrics.py:2282-2284` |
| RTY（日/周期） | `Σ rty_good ÷ Σ rty_input × 100` | 来自 `QUALITY_RTY` 零件行相加；班次展示值直读 `EXT_VAL_39` | `calc/daily_metrics.py:2283-2286`、`:1683` |
| 质量合格率 | `Σ rty_good ÷ Σ rty_input × 100` | 与 RTY 同式，用于 OEE 质量维度展示 | 同上 `:2283` |
| 停机率 | `T_stop_act ÷ T_open_act × 100` | 分母是实际开班时长，含计划内停机 | `calc/daily_metrics.py:1663`、`:2287` |
| 计划外停机率 | `T_stop_unp ÷ T_open_act × 100` | 只算计划外停机 | 同上 `:1664`、`:2288` |
| 报废率 | `scrap ÷ actual_output × 100` | 日报/看板口径；简报另直读 `EXT_VAL_38` | 同上 `:1685`、`:2290` |
| 件均不良 | `(rty_rework + rty_scrap) ÷ rty_input` | 不含报废以外的其他不良来源 | 同上 `:2291` |
| 空载率 | `EXT_VAL_34` 直读 | 源表有值直接用 | `calc/daily_metrics.py:1618` |
| 每圈空载工位 | `unplanned_idle_slot ÷ N_cycle` | 用于判断「每圈固定工位空载 / 偶尔空载 / 范围性空载」 | `calc/idle_slot.py:188-190`、`:147-157` |
| 有停机圈占比 | `(N_cycle − N_no_stop) ÷ N_cycle × 100` | 判断空载与停机相关性 | `calc/idle_slot.py:138-144` |
| 空载损失机时 | `unplanned_idle_slot ÷ (M_plan + M_trial) × V_plan` | 单位秒；漏掉 `÷每圈载具数` 会把损失放大到不可能的量级 | `calc/idle_slot.py:115-122` |
| 空载损失产量 | `unplanned_idle_slot × (actual_output ÷ M_actual)` | 用本班自身「每工位产出」折算，不引入全局常数 | `calc/idle_slot.py:125-135` |
| 计划外空载重算（仅兜底） | `round(T_run_act ÷ actual_speed, 1) × (M_plan + M_trial) − M_actual` | **只在 `EXT_VAL_33` 缺失时使用**，且负数截为 `None`/0；源表有值以源表为准 | `calc/daily_metrics.py:1635-1661` |
| 人员出勤率 | `attendance_actual ÷ attendance_std × 100` | 标准人数优先 `EXT_VAL_49`，为空时按 `(M_plan + M_trial)` 查配置分档；实际出勤 0 视为「没采到」 | `calc/workforce.py:240-261` |
| 缺勤率 / 人员缺口 | `100 − 出勤率`；`attendance_actual − attendance_std` | 超员时为负，不截断 | `calc/workforce.py:262-269` |
| 计划缺口（KPI 口径） | `plan_output − actual_output` | 与排产单口径 `plan_qty − completed_qty` **不是同一个数**，不能互相折算 | `docs/daily_glossary.md:11-12`、`:27-34` |
| 缺口桥 | `gap = 计划外停机损失 + 计划内停机超时损失 + 开班时长不足损失 + 速度与空转损失 (+ 未解释)` | 各桶按计划节拍 `rate = plan_output ÷ T_run_plan` 折成产量当量；代数闭合 | `calc/gap_bridge.py:1-39`、`:360-408` |
| 载具产出缺口 | `max(M_theory − M_actual, 0)` | 理论载具与实际生产载具的差 | `calc/daily_metrics.py:1499-1502` |
| 人均效率 / TEEP 波动 | 无数据源 | 业务方已确认上游不提供，报表按「缺失即不成立」处理，不推算 | `docs/panel_field_mapping.md:165` |

## 口径提醒（核对时最容易出错的地方）

1. **成型线与装配线是两套 OEE 口径**：成型线用载具/理论载具公式，算不出就是 `NA`，绝不回退 `EXT_VAL_36`；装配线才用 `EXT_VAL_36`。两类线的 OEE 不应横向比较。
2. **TEEP 的 OEE 因子是「有效 OEE」**，按实际开班时长加权；周/月报周期总表的 OEE 在代码里取的是 `EXT_VAL_36` 加权。两者在成型线公式值与源值不一致时会分叉，核对 TEEP 时请以 `calc/teep_engine.py` 的 `source_oee_wavg` 为准。
3. **载具装载率 > 100% 在物理上不可能**：样例工厂 P100/P200/P300 曾出现 170~190%，原因是产线主数据 `STD_SLOTS` 与实际每圈载具数不符。装载率每超一倍，设备计划利用率与 TEEP 会被等比例放大。
4. **缺失不写 0**：TEEP、设备计划利用率、OEE 的任一因子缺失都返回 `None`（报表显示 `NA`/灰圈），不是 0。未开班班次只影响 OEE 展示，设备计划利用率仍然照算——「排了班却没开」正是它要暴露的。
5. **告警阈值**：设备计划利用率 > 95% 提示核查；时间利用率 > 95% 提示计划内停机是否漏扣；TEEP > 100% 判物理不成立；OEE > 200% 判采集异常（`calc/teep_engine.py:269-286`、`docs/daily_glossary.md:45-48`）。
6. **配置可覆盖**：日报 `scope=daily`、周月报 `scope=period` 的比率类指标可由「计算口径」页的公式覆盖；表内为代码内置默认值。改口径后历史报表重渲会跟着变，但已入库的旧数据不会自动回溯。
