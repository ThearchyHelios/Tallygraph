"""独立语义指标库（SemanticMetric）与口径文档拆解后台任务（SemanticDecomposeJob）持久化模型。

由 app/db/models.py 末尾整模块导入，create_all 自动建表，存量表新列由 app/db/base.py:_COLUMN_MIGRATIONS 补齐（兼容 SQLite 与 PostgreSQL）。

设计目的：
- 解耦原先强绑定在「多节点工作流 + 单行单元格抽取 + 多重发布门禁」上的繁重口径模式；
- 同时支持「单表聚合表达式（expression）」与「多表 JOIN / CTE 参数化 SQL 模板（sql_template）」两种确定性计算模式；
- status="enabled" 即存即生效，直接供问数入口语义预命中（chat/grounding.py）与内置工具 metric_query 按任意日期区间或业务维度动态聚合取数；
- SemanticDecomposeJob 支持后台异步执行「从目录与文档智能提取指标」与「文档一键拆解工作流（字段目录补丁 + 指标口径 + 标准实体 + 知识库归档）」，实时持久化阶段进度与待审阅结果。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UTCDateTime, new_id


class SemanticMetric(Base, TimestampMixin):
    """独立语义指标卡片定义（即存即用、支持单表聚合与多表参数化 SQL 模板）。"""

    __tablename__ = "semantic_metrics"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    #: 机器稳定标识（小写字母开头，小写字母/数字/下划线，最长 64 字符，如 park_daily_visitors）
    key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    #: 指标中文名称（如「园区日客流（去重口径）」）
    name: Mapped[str] = mapped_column(String(200))
    #: 业务别名 / 同义词列表（如 ["客流", "总客流", "日客流量"]）
    aliases: Mapped[list[Any]] = mapped_column(default=list)
    #: 口径业务说明与注意事项（如统计口径、去重规则、T+1 延迟说明）
    description: Mapped[str] = mapped_column(Text, default="")
    #: 展示单位（如「人次」「元」「%」）
    unit: Mapped[str] = mapped_column(String(40), default="")
    #: 归属数据源 id（data_sources.id）
    source_id: Mapped[str] = mapped_column(String(32), index=True)
    #: 计算模式：expression（单表聚合表达式） | sql_template（多表 JOIN / CTE 参数化 SQL 模板）
    calc_mode: Mapped[str] = mapped_column(String(24), default="expression")
    #: 主物理底表名（expression 模式下为查询底表；sql_template 模式下为主锚点表）
    table_name: Mapped[str] = mapped_column(String(255))
    #: 只读聚合 SQL 表达式（expression 模式必填；sql_template 模式可作外层聚合表达式或展示摘要）
    sql_expression: Mapped[str] = mapped_column(Text)
    #: 多表 / 参数化只读 SQL 模板（sql_template 模式使用，支持 {{select_dims}}、{{and_where}}、{{group_by}}、{{period_days}} 等槽位）
    sql_template: Mapped[str] = mapped_column(Text, default="")
    #: 业务时间列名（可选，如 stat_date 或多表别名 s.stat_date；为空表示无时间序列维度）
    date_column: Mapped[str] = mapped_column(String(128), default="")
    #: 默认有效记录过滤条件（可选 WHERE 子表达式，如 status = 1 AND is_deleted = 0）
    filter_sql: Mapped[str] = mapped_column(Text, default="")
    #: 允许按维度下钻与过滤的列定义：[{key, label, column, entity_key?}]
    dimensions: Mapped[list[Any]] = mapped_column(default=list)
    #: 默认时间聚合粒度：day | month | hour | none
    default_time_grain: Mapped[str] = mapped_column(String(24), default="day")
    #: 状态：enabled（启用，直接参与问数预命中与查询） | draft（草稿） | disabled（停用）
    status: Mapped[str] = mapped_column(String(24), default="enabled", index=True)
    #: 来源：human（人工创建） | ai（目录/文档智能提取）
    source: Mapped[str] = mapped_column(String(24), default="human")
    #: 乐观锁版本号（从 1 起，每次修改加 1）
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_by: Mapped[str | None] = mapped_column(String(100), default=None)
    updated_by: Mapped[str | None] = mapped_column(String(100), default=None)


class SemanticDecomposeJob(Base, TimestampMixin):
    """后台口径文档一键拆解与智能提取指标任务（支持实时阶段进度、后台运行与审阅一键应用）。"""

    __tablename__ = "semantic_decompose_jobs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    #: queued | running | succeeded | failed | cancelled
    status: Mapped[str] = mapped_column(String(24), default="queued", index=True)
    #: 目标数据源 id
    source_id: Mapped[str] = mapped_column(String(32), index=True)
    source_name: Mapped[str] = mapped_column(String(120), default="")
    #: 拆解范围列表：["catalog", "metrics", "entities"]
    scopes: Mapped[list[Any]] = mapped_column(default=list)
    #: 关联的知识库文档 id 列表
    document_ids: Mapped[list[Any]] = mapped_column(default=list)
    #: 上传附件 id 列表（entity_docs 暂存解析文本）
    attachment_ids: Mapped[list[Any]] = mapped_column(default=list)
    #: 直接粘贴的业务文档标题与正文
    doc_title: Mapped[str] = mapped_column(String(240), default="")
    doc_text: Mapped[str] = mapped_column(Text, default="")
    #: 是否同步将粘贴/上传的文档存入知识库（保留判异阈值与边界契约供问数检索）
    save_to_kb: Mapped[bool] = mapped_column(Boolean, default=True)
    #: 用户补充要求
    note: Mapped[str] = mapped_column(Text, default="")
    #: 当前阶段标识与文案：preparing | syncing_kb | extracting_catalog | extracting_metrics | extracting_entities | done
    phase: Mapped[str] = mapped_column(String(48), default="preparing")
    phase_label: Mapped[str] = mapped_column(String(200), default="正在准备数据源与文档上下文")
    #: 0 ~ 100 百分比进度
    progress: Mapped[int] = mapped_column(Integer, default=0)
    #: 分步轨迹：[{key, label, status, detail, elapsed_ms}]
    steps: Mapped[list[Any]] = mapped_column(default=list)
    #: 结构化拆解产物：{kb_document, catalog_patches, metric_candidates, metric_skipped, entity_operations, guardrail_notes, applied}
    result: Mapped[dict[str, Any]] = mapped_column(default=dict)
    #: 简要汇总计数：{catalog_count, metric_count, entity_count, kb_synced}
    summary: Mapped[dict[str, Any]] = mapped_column(default=dict)
    error: Mapped[str | None] = mapped_column(Text, default=None)
    started_by: Mapped[str | None] = mapped_column(String(100), default=None)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)
