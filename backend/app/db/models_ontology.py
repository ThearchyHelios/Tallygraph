"""标准业务实体与跨源本体映射表。

由 app/db/models.py 末尾整模块导入，create_all 自动建表（兼容 SQLite 与 PostgreSQL）。

- CanonicalEntity：标准业务实体（如产线、仓库、门店、设备、区域），承接跨数据源的物理表列绑定（bindings）、
  跨源编码映射（members.source_codes）与口径输入参数映射（caliber_params）。
- 每一项物理列绑定、口径参数映射都带显式状态（proposed / verified / confirmed / rejected）：
  自动穿透与跨源对齐运行时只读取 verified / confirmed 的映射，proposed 候选绝不自动参与穿透（铁律 1）。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UTCDateTime, new_id


class CanonicalEntity(Base, TimestampMixin):
    """标准业务实体及跨源物理列、编码、口径参数映射。"""

    __tablename__ = "canonical_entities"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    #: 机器稳定的标准键（小写字母、数字、下划线，如 production_line、warehouse）
    key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    #: 业务展示名称（如「产线」「仓库」）
    label: Mapped[str] = mapped_column(String(100))
    description: Mapped[str] = mapped_column(Text, default="")
    #: 业务同义词（如 ["生产线", "线体", "车间产线"]）
    aliases: Mapped[list[Any]] = mapped_column(default=list)
    #: 标准成员与跨源编码映射：[{value, label, aliases: [...], source_codes: {source_name: code}}]
    members: Mapped[list[Any]] = mapped_column(default=list)
    #: 跨源物理表列绑定：[{id, source_id, source_name, table_name, column_name, status, source, note, reason, updated_at, updated_by}]
    bindings: Mapped[list[Any]] = mapped_column(default=list)
    #: 口径输入参数映射：[{id, workflow_id, card_id, param_name, status, source, note}]
    caliber_params: Mapped[list[Any]] = mapped_column(default=list)
    #: 乐观锁版本号（从 1 起，每次修改加 1）
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_by: Mapped[str | None] = mapped_column(String(100), default=None)
    updated_by: Mapped[str | None] = mapped_column(String(100), default=None)


class EntityDiscoveryJob(Base, TimestampMixin):
    """后台扫描发现候选标准业务实体的异步任务（E2）。"""

    __tablename__ = "entity_discovery_jobs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    #: queued | running | succeeded | failed | cancelled | interrupted
    status: Mapped[str] = mapped_column(String(24), default="queued", index=True)
    source_ids: Mapped[list[Any]] = mapped_column(default=list)
    document_ids: Mapped[list[Any]] = mapped_column(default=list)
    attachment_ids: Mapped[list[Any]] = mapped_column(default=list)
    note: Mapped[str] = mapped_column(Text, default="")
    phase: Mapped[str] = mapped_column(String(120), default="正在准备扫描范围…")
    summary: Mapped[dict[str, Any]] = mapped_column(default=dict)
    error: Mapped[str | None] = mapped_column(Text, default=None)
    started_by: Mapped[str | None] = mapped_column(String(100), default=None)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)
