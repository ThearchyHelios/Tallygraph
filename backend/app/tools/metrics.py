"""独立语义指标库内置只读工具：metric_query。

设计原则：
1. 只执行 status="enabled" 的已启用指标卡片（SemanticMetric），确定性编译为单条只读 SELECT 语句。
2. 底层委托对应数据源的 `db_query__<源名>` 工具执行：自动继承上传快照版本锁定（data_versions）、
   普通使用者结果遮罩（MASK_RESULTS_OPTION）、只读守卫（READ_ONLY_OPTION）、数据目录 SQL 检查（SqlChecker）
   以及内容寻址查询快照落盘（query_snapshot），运行证据链与 `db_query__` 完全一致。
3. 支持按日期区间（date_from / date_to）、时间序列趋势（group_by_date / time_grain）、业务维度下钻（group_by）
   与标准业务实体值过滤（filters，自动把业务别名翻译为目标源底层编码）。
4. 当单日查询返回空或全 NULL 且配置了业务时间列时，在同一次调用结果内附带底表最新可用日期 `latest_available_date`，
   消除 Agent 多轮探查开销。
"""
from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field, model_validator

from app.data.entities import list_entities
from app.data.semantic_metrics import (
    MetricInvalid,
    MetricNotFound,
    compile_metric_sql,
    get_metric,
    is_empty_metric_result,
    probe_latest_metric_date,
)
from app.db.base import SessionLocal
from app.db.models import DataSource
from app.tools.datasource import QUERY_PREFIX
from app.tools.registry import ToolContext, ToolGuide, Versions, build_tools, register

METRIC_QUERY = "metric_query"

METRIC_GUIDE = ToolGuide(
    id="semantic_metric.method@1",
    text=(
        "【语义指标查询纪律】\n"
        "当用户提问命中已启用的独立语义指标（metric_key）时，优先调用 `metric_query` 获取标准口径结果：\n"
        "- 查某一天传 `date_from` 与 `date_to` 为同一日期；查趋势或近 N 日明细传区间并设 `group_by_date=true`；\n"
        "- 按业务维度拆分传 `group_by`（填指标声明的维度 key）；按实体或维度筛选传 `filters`（可直接传业务名称，系统自动翻译底层编码）；\n"
        "- 若返回结果附带 `latest_available_date`（例如当日 T+1 尚未入库），可据此说明当日暂无数据或按需再查最新日期。"
    ),
)


class MetricQueryArgs(BaseModel):
    """按已启用的语义指标卡片（SemanticMetric）执行确定性聚合取数。"""

    metric: str = Field(description="要查询的语义指标 key、指标 id 或唯一指标名称")
    date_from: str | None = Field(default=None, description="可选：起始业务日期 YYYY-MM-DD（闭区间）")
    date_to: str | None = Field(default=None, description="可选：结束业务日期 YYYY-MM-DD（闭区间）")
    group_by_date: bool = Field(default=False, description="是否按时间序列分组输出趋势（如逐日/逐月）")
    time_grain: str | None = Field(
        default=None,
        description="可选：时间分组粒度 day | month | hour（配合 group_by_date=true 使用，默认按指标设定）",
    )
    group_by: list[str] = Field(
        default_factory=list,
        description="可选：按指标声明的维度 key 分组下钻，例如 ['area', 'store']",
    )
    filters: dict[str, Any] = Field(
        default_factory=dict,
        description="可选：按指标声明的维度过滤，例如 {'area': '西区'}（关联了标准业务实体的维度会自动翻译物理编码）",
    )
    limit: int = Field(default=200, ge=1, le=1000, description="分组查询最多返回行数，默认 200")

    @model_validator(mode="before")
    @classmethod
    def _accept_metric_key_aliases(cls, data: Any) -> Any:
        if isinstance(data, dict) and "metric" not in data:
            for alt in ("metric_key", "key", "metric_id"):
                if alt in data and data[alt] is not None:
                    copied = dict(data)
                    copied["metric"] = copied.pop(alt)
                    return copied
        return data


@register(
    name=METRIC_QUERY,
    description="按已启用的独立语义指标卡片查询标准口径数值、时间趋势或维度拆解明细（自动应用有效记录过滤与标准业务实体编码转换）",
    category="data",
    args_schema=MetricQueryArgs,
    dangerous=False,
    needs_context=True,
    guide=METRIC_GUIDE,
)
async def run_metric_query(
    ctx: ToolContext,
    metric: str,
    date_from: str | None = None,
    date_to: str | None = None,
    group_by_date: bool = False,
    time_grain: str | None = None,
    group_by: list[str] | None = None,
    filters: dict[str, Any] | None = None,
    limit: int = 200,
) -> str:
    async with SessionLocal() as session:
        try:
            row = await get_metric(session, metric)
        except MetricNotFound as exc:
            return f"查询失败：{exc}"
        if row.status != "enabled":
            return f"查询失败：指标「{row.name}（{row.key}）」当前状态为 {row.status}，未启用"
        ds = await session.get(DataSource, row.source_id)
        if ds is None or not ds.enabled:
            return f"查询失败：指标「{row.name}」关联的数据源不可用或已停用"

        entities = await list_entities(session)
        try:
            sql, alignments = compile_metric_sql(
                row,
                source=ds,
                entities=entities,
                date_from=date_from,
                date_to=date_to,
                group_by_date=group_by_date,
                time_grain=time_grain,
                group_by=group_by or [],
                filters=filters or {},
                limit=limit,
            )
        except MetricInvalid as exc:
            return f"查询失败：{exc}"

        sub_ctx = ctx
        if sub_ctx.data_versions is Versions.UNSET and not sub_ctx.run_id:
            # 工具试用等非运行上下文默认读当前版本
            from dataclasses import replace

            sub_ctx = replace(ctx, data_versions=Versions.CURRENT)

        query_tool_name = f"{QUERY_PREFIX}{ds.name}"
        built = await build_tools([query_tool_name], sub_ctx, session=session)
        if not built:
            return f"查询失败：无法初始化数据源「{ds.name}」的查询工具"

        raw_res = await built[0].ainvoke({"sql": sql, "limit": limit})
        raw_str = raw_res if isinstance(raw_res, str) else json.dumps(raw_res, ensure_ascii=False, default=str)
        if not raw_str.lstrip().startswith("{"):
            return raw_str

        try:
            payload = json.loads(raw_str)
        except ValueError:
            return raw_str

        payload["metric"] = {
            "id": row.id,
            "key": row.key,
            "name": row.name,
            "unit": row.unit or "",
        }
        if alignments:
            payload["entity_alignments"] = alignments

        has_groups = bool(group_by_date or group_by)
        rows_data = payload.get("rows") if isinstance(payload.get("rows"), list) else []
        if is_empty_metric_result(rows_data, has_groups=has_groups) and row.date_column:
            latest_date = await probe_latest_metric_date(
                ds,
                table_name=row.table_name,
                date_column=row.date_column,
                filter_sql=row.filter_sql or "",
            )
            if latest_date:
                payload["latest_available_date"] = latest_date

        return json.dumps(payload, ensure_ascii=False, default=str)
