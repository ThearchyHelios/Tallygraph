"""指标本体与数据源健康内置工具（W2-a）：ontology_neighbors、source_health 与跨源实体参数对齐。

三条边界铁律：
1. **状态门禁（P2）**：运行期自动穿透只暴露与使用 `verified / confirmed` 状态的指标边（`links`）和标准实体绑定
   （`bindings` / `caliber_params`），`proposed` 候选一律不参与运行期穿透。
2. **权限对齐（铁律 2 / K14）**：普通使用者（`restricted_actor == True`）调用 `ontology_neighbors` 时，仅返回已授权给其应用
   （`apps/access.usable_calibers`）的目标口径版本，无权邻居连名字、工作流 ID、表名都不暴露（仅计入 `omitted_unauthorized_count`）；
   越权点名未授权口径直接报 `querycore_not_authorized`。`source_health` 对普通使用者同样核查口径/数据源范围并开启 `restricted=True`。
3. **确定性跨源键值翻译**：`align_caliber_inputs` 严格通过 `CanonicalEntity` 的已确认参数映射与成员编码表转换，绝不靠子串猜值。
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.data import source_health as sh
from app.data.entities import (
     ACTIVE_ENTITY_STATUSES,
    confirmed_bindings,
    confirmed_caliber_params,
    entities_bridging_sources,
    entity_to_dict,
    list_entities,
    resolve_caliber_param,
    translate_entity_value,
)
from app.data.names import name_key
from app.ledger.ontology import (
    ACTIVE_LINK_STATUSES,
    build_published_ontology_graph,
    card_metric_lineage,
    metric_node_key,
    workflow_ontology_index,
)
from app.tools.registry import ToolContext, ToolGuide, register

ONTOLOGY_NEIGHBORS = "ontology_neighbors"
SOURCE_HEALTH = "source_health"
ONTOLOGY_TOOLS = frozenset({ONTOLOGY_NEIGHBORS, SOURCE_HEALTH})

ONTOLOGY_GUIDE = ToolGuide(
    id="ontology.method@1",
    text=(
        "【跨源本体穿透与异常排查纪律】\n"
        "1. 先核健康与对账（第 1 阶段）：指标出现断崖下跌、无数据、契约越界（如比率 > 100%）或跨库口径分歧时，先调 "
        "ontology_neighbors 查看已确认的 1 跳本体关系（factor 构成因子 / reconciles_with 对账镜像 / upstream_driver 上游驱动）"
        "及跨源标准实体桥；涉及外部关联源时优先调 source_health 核查是否存在断更（date_stalled）、断档（date_gap）、"
        "行数骤降（volume_drop）或新增未识别编码（codes_new）。若存在 reconciles_with 镜像口径，取数后调 analysis_reconcile "
        "核对差额。一旦确认为源表断档或对账失配，立即停在数据故障结论（引用 [[h:H1]] 或 [[a:A1]]），不在问题数据上强拆业务维度。\n"
        "2. 同源定切片再跨源（第 2 阶段）：源健康正常、业务统计波动超带（outside_band / far_outside_band）时，先在同源用 "
        "analysis_decompose 锁定贡献最大的业务实体切片（如异常产线、仓库或渠道），再将该切片写入 ontology_neighbors 的 "
        "entity_filters 获取对齐后的邻居口径调用参数。\n"
        "3. 跨源穿透与非因果纪律（第 3 阶段）：按 recommended_caliber_query 调用 caliber_query（在 inputs 中传目标参数或标准实体键，"
        "平台确定性翻译跨源编码）拉取邻居指标，最多穿透 2 跳。核对 upstream_driver 同步波动一律调用 analysis_cochange 并引用系统"
        "判定语；统计伴随不等于因果证明，没有知识库明确规则依据时严禁写成「导致 / 引起 / 造成」。"
    ),
)


class OntologyNeighborsArgs(BaseModel):
    """查询指定口径指标的 1 跳已确认本体邻居、反向表血缘与跨源实体对齐参数。"""

    workflow: str = Field(description="当前口径所在的工作流 id 或唯一工作流名称")
    card: str | None = Field(default=None, description="可选：限定口径卡节点 id；不填则查看该工作流全部源头口径卡")
    metric_id: str | None = Field(default=None, description="可选：限定单个指标 id；不填则查看卡上全部指标")
    entity_filters: dict[str, Any] = Field(
        default_factory=dict,
        description="可选：当前已定位的异常实体切片，如 {'production_line': 'L2'}，自动换算成各邻居口径的入口参数",
    )
    from_source: str | None = Field(
        default=None,
        description="可选：entity_filters 取值来自哪个数据源名称，用于精确匹配该源的编码表",
    )


class SourceHealthArgs(BaseModel):
    """快速核查数据源或口径底层表的健康度、新鲜度与待处理漂移告警。"""

    source_name: str | None = Field(default=None, description="要核查的数据源名称（与 workflow 二选一或同时提供）")
    tables: list[str] | None = Field(default=None, description="可选：限定核查的表名列表；不填则按口径血缘或探查前若干张表")
    workflow: str | None = Field(default=None, description="可选：按已发布口径工作流 id 或名称自动反查其底层数据源与表")
    card: str | None = Field(default=None, description="可选：配合 workflow 限定某张口径卡")
    metric_id: str | None = Field(default=None, description="可选：配合 workflow 限定某个指标")
    start: str | None = Field(default=None, description="可选：分析时间窗起点 YYYY-MM-DD，用于核查窗口覆盖缺口")
    end: str | None = Field(default=None, description="可选：分析时间窗终点 YYYY-MM-DD，用于核查断更或断档")


async def _resolve_actor_and_restriction(session: AsyncSession, ctx: ToolContext) -> tuple[Any, bool, dict[str, Any] | None]:
    """与 QueryCore 同口径解析当前运行的真实主体与是否受限（普通 user 角色）。"""
    from app.auth.chat_policy import restricted_actor
    from app.db.models import Run

    run_id = str(getattr(ctx, "run_id", "") or "").strip()
    parent_row = await session.get(Run, run_id) if run_id else None
    if parent_row is not None:
        actor = parent_row.started_by
        graph = parent_row.graph if isinstance(parent_row.graph, dict) else None
    else:
        actor = getattr(ctx, "started_by", None)
        graph = None
    is_restricted = await restricted_actor(session, actor)
    return actor, is_restricted, graph


def _input_field_names(graph: Mapping[str, Any] | None) -> set[str]:
    out: set[str] = set()
    for node in (graph or {}).get("nodes") or []:
        if not isinstance(node, Mapping) or str(node.get("type") or "") != "input":
            continue
        cfg = node.get("data", {}).get("config") if isinstance(node.get("data"), Mapping) else node.get("config")
        if not isinstance(cfg, Mapping):
            continue
        for f in cfg.get("fields") or []:
            if isinstance(f, Mapping) and str(f.get("name") or "").strip():
                out.add(str(f["name"]).strip())
    return out


async def align_caliber_inputs(
    session: AsyncSession,
    *,
    workflow_id: str,
    card_id: str,
    inputs: Mapping[str, Any] | None = None,
    entity_inputs: Mapping[str, Any] | None = None,
    from_source: str | None = None,
    restricted: bool = False,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """把业务实体切片值确定性地转换为目标口径（workflow_id + card_id）的入口参数与目标源编码。

    返回 (合并后的 inputs 字典, 对齐记录列表)。
    - 只认 `verified / confirmed` 状态的实体绑定与口径参数映射；
    - 既支持显式传入 `entity_inputs={"production_line": "L2"}`，也支持调用方已在 `inputs={"equip_line_id": "L2"}`
      传入源侧别名/跨源编码时自动翻译为目标源编码（如 `"EQ-L2"`）。
    """
    from app.db.models import Workflow, WorkflowVersion

    merged: dict[str, Any] = dict(inputs or {})
    alignments: list[dict[str, Any]] = []

    entities = await list_entities(session)
    if not entities:
        return merged, alignments

    wf = await session.get(Workflow, workflow_id)
    target_graph: Mapping[str, Any] = {}
    if wf is not None:
        ver = wf.published_version or wf.version
        snap = (
            await session.execute(
                select(WorkflowVersion).where(
                    WorkflowVersion.workflow_id == workflow_id,
                    WorkflowVersion.version == int(ver),
                )
            )
        ).scalar_one_or_none()
        target_graph = (snap.graph if snap is not None else wf.graph) or {}

    lineage = card_metric_lineage(target_graph, card_id) if target_graph and card_id else {"sources": []}
    target_sources: list[str] = list(lineage.get("sources") or [])
    declared_fields = _input_field_names(target_graph)

    # 构建实体索引：key / label / aliases -> entity
    by_token: dict[str, Any] = {}
    by_param: dict[str, Any] = {}
    for ent in entities:
        has_active = bool(confirmed_bindings(ent) or confirmed_caliber_params(ent))
        if not has_active:
            continue
        for tok in (ent.key, ent.label, *(ent.aliases or [])):
            clean = str(tok or "").strip().lower()
            if clean and clean not in by_token:
                by_token[clean] = ent
        param_name = resolve_caliber_param(ent, workflow_id=workflow_id, card_id=card_id)
        if param_name:
            by_param[param_name] = ent

    def _pick_to_source(ent: Any) -> str | None:
        if len(target_sources) == 1:
            return target_sources[0]
        for src in target_sources:
            if confirmed_bindings(ent, source_name=src):
                return src
        return target_sources[0] if target_sources else None

    # 1) 先处理已经写在 inputs 里的口径参数值（如传了别名或跨源编码 'L2'，翻译为目标源编码 'EQ-L2'）
    for param_key, raw_val in list(merged.items()):
        ent = by_param.get(param_key)
        if ent is None or raw_val in (None, "") or isinstance(raw_val, (dict, list, tuple, bool)):
            continue
        to_src = _pick_to_source(ent)
        translated = translate_entity_value(ent, raw_val, from_source=from_source, to_source=to_src)
        if translated is not None and str(translated["target_value"]) != str(raw_val):
            merged[param_key] = translated["target_value"]
            rec: dict[str, Any] = {
                "entity_key": ent.key,
                "entity_label": ent.label,
                "param_name": param_key,
                "canonical_value": translated["canonical"],
            }
            if not restricted:
                rec["from_value"] = str(raw_val)
                rec["target_value"] = translated["target_value"]
                if to_src:
                    rec["to_source"] = to_src
            alignments.append(rec)

    # 1b) 若调用方直接在 inputs 里写了标准实体标识键（如 {'production_line': 'L2'}），自动映射到目标口径入口参数名并换算编码
    for raw_key, raw_val in list(merged.items()):
        if raw_key in declared_fields or raw_key in by_param:
            continue
        if raw_val in (None, "") or isinstance(raw_val, (dict, list, tuple, bool)):
            continue
        ent = by_token.get(str(raw_key or "").strip().lower())
        if ent is None:
            continue
        param_name = resolve_caliber_param(ent, workflow_id=workflow_id, card_id=card_id)
        if not param_name and ent.key in declared_fields and confirmed_bindings(ent):
            param_name = ent.key
        if not param_name or param_name in merged:
            continue
        to_src = _pick_to_source(ent)
        translated = translate_entity_value(ent, raw_val, from_source=from_source, to_source=to_src)
        if translated is None:
            continue
        merged.pop(raw_key, None)
        merged[param_name] = translated["target_value"]
        rec = {
            "entity_key": ent.key,
            "entity_label": ent.label,
            "param_name": param_name,
            "canonical_value": translated["canonical"],
        }
        if not restricted:
            rec["from_value"] = str(raw_val)
            rec["target_value"] = translated["target_value"]
            if to_src:
                rec["to_source"] = to_src
        alignments.append(rec)

    # 2) 再处理显式传入的 entity_inputs（如 {'production_line': 'L2'} -> {'equip_line_id': 'EQ-L2'}）
    for raw_key, raw_val in (entity_inputs or {}).items():
        if raw_val in (None, "") or isinstance(raw_val, (dict, list, tuple, bool)):
            continue
        ent = by_token.get(str(raw_key or "").strip().lower())
        if ent is None:
            continue
        param_name = resolve_caliber_param(ent, workflow_id=workflow_id, card_id=card_id)
        if not param_name and ent.key in declared_fields and confirmed_bindings(ent):
            param_name = ent.key
        if not param_name or param_name in merged:
            continue
        to_src = _pick_to_source(ent)
        translated = translate_entity_value(ent, raw_val, from_source=from_source, to_source=to_src)
        if translated is None:
            continue
        merged[param_name] = translated["target_value"]
        rec = {
            "entity_key": ent.key,
            "entity_label": ent.label,
            "param_name": param_name,
            "canonical_value": translated["canonical"],
        }
        if not restricted:
            rec["from_value"] = str(raw_val)
            rec["target_value"] = translated["target_value"]
            if to_src:
                rec["to_source"] = to_src
        alignments.append(rec)

    return merged, alignments


@register(
    name=ONTOLOGY_NEIGHBORS,
    category="口径",
    effect="read",
    description=(
        "查询一份已发布口径或指标的 1 跳已确认业务本体邻居（构成因子 factor、跨源镜像对账 reconciles_with、"
        "上游业务驱动 upstream_driver）、底层数据源与标准业务实体跨源桥；传入 entity_filters 时可自动把异常切片"
        "转换为邻居口径可直接调用的入口参数。只读。"
    ),
    args_schema=OntologyNeighborsArgs,
    guide=ONTOLOGY_GUIDE,
)
async def ontology_neighbors(ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
    args = OntologyNeighborsArgs(**kwargs)
    from app.apps.access import usable_calibers
    from app.db.base import SessionLocal
    from app.db.models import Workflow, WorkflowVersion
    from app.ledger.querycore import NOT_AUTHORIZED, QueryCoreRefused, _workflow_id
    from app.tools.analysis import scope_of

    async with SessionLocal() as session:
        actor, is_restricted, _ = await _resolve_actor_and_restriction(session, ctx)
        try:
            wf_id = await _workflow_id(session, args.workflow)
        except QueryCoreRefused as exc:
            raise ValueError(f"{exc.message}（{exc.code}）") from None

        wf = await session.get(Workflow, wf_id)
        if wf is None or wf.published_version is None:
            raise ValueError(f"工作流「{args.workflow}」尚未发布，无法查询已生效本体关系")
        pub_ver = int(wf.published_version)

        usable: dict[tuple[str, int], str] = {}
        if is_restricted:
            usable = await usable_calibers(session, actor)
            if (wf_id, pub_ver) not in usable:
                raise ValueError(f"没有权限查看口径「{args.workflow}」的本体关系（{NOT_AUTHORIZED}）")

        snap = (
            await session.execute(
                select(WorkflowVersion).where(
                    WorkflowVersion.workflow_id == wf_id,
                    WorkflowVersion.version == pub_ver,
                )
            )
        ).scalar_one_or_none()
        local_idx = workflow_ontology_index(
            workflow_id=wf_id,
            version=pub_ver,
            graph=(snap.graph if snap is not None else wf.graph) or {},
            workflow_name=wf.name,
        )

        # 全站已发布本体图（仅取 verified/confirmed 生效边）
        full_graph = await build_published_ontology_graph(session, include_proposed=False, restricted=False)
        node_by_key = {n["key"]: n for n in full_graph["nodes"]}

        focal_keys: set[str] = set()
        focal_sources: set[str] = set()
        focal_tables: dict[str, set[str]] = {}
        focal_metrics: list[dict[str, Any]] = []
        for card in local_idx["cards"]:
            if args.card and card["card_id"] != args.card:
                continue
            for m in card["metrics"]:
                if args.metric_id and m["metric_id"] != args.metric_id:
                    continue
                mkey = metric_node_key(wf_id, card["card_id"], m["metric_id"])
                focal_keys.add(mkey)
                for src_name in card["sources"]:
                    focal_sources.add(src_name)
                for src_name, tbls in (card.get("tables_by_source") or {}).items():
                    focal_tables.setdefault(src_name, set()).update(tbls)
                metric_summary: dict[str, Any] = {
                    "workflow_id": wf_id,
                    "workflow_name": wf.name,
                    "version": pub_ver,
                    "card_id": card["card_id"],
                    "card_title": card["title"],
                    "caliber": card["caliber"],
                    "metric_id": m["metric_id"],
                    "name": m["name"],
                    "unit": m["unit"],
                    "contract": m["contract"],
                }
                if not is_restricted:
                    metric_summary["sources"] = card["sources"]
                    metric_summary["tables_by_source"] = card["tables_by_source"]
                focal_metrics.append(metric_summary)

        if not focal_keys:
            raise ValueError("未找到匹配的已发布口径卡或指标，请检查 card / metric_id 参数")

        omitted_unauthorized = 0
        neighbors: list[dict[str, Any]] = []
        seen_edge_keys: set[tuple[str, str, str]] = set()

        for edge in full_graph["edges"]:
            if edge.get("status") not in ACTIVE_LINK_STATUSES:
                continue
            src_ref = edge["from"]
            dst_ref = edge["to"]
            src_key = metric_node_key(src_ref["workflow_id"], src_ref["card_id"], src_ref["metric_id"])
            dst_key = metric_node_key(dst_ref["workflow_id"], dst_ref["card_id"], dst_ref["metric_id"])

            direction: str | None = None
            peer_key: str | None = None
            if src_key in focal_keys and dst_key not in focal_keys:
                direction, peer_key = "outbound", dst_key
            elif src_key in focal_keys and dst_key in focal_keys and src_key != dst_key:
                direction, peer_key = "outbound", dst_key
            elif dst_key in focal_keys and src_key not in focal_keys and edge["kind"] == "reconciles_with":
                # 对账镜像关系天然对称：反向声明的 reconciles_with 也作为 1 跳对账邻居发现
                direction, peer_key = "inbound_mirror", src_key

            if direction is None or peer_key is None:
                continue
            dedup = (edge["kind"], direction, peer_key)
            if dedup in seen_edge_keys:
                continue
            seen_edge_keys.add(dedup)

            peer_node = node_by_key.get(peer_key)
            if peer_node is None:
                continue
            peer_wf_id = str(peer_node["workflow_id"])
            peer_ver = int(peer_node["version"])

            if is_restricted and (peer_wf_id, peer_ver) not in usable:
                omitted_unauthorized += 1
                continue

            effective_from_src = args.from_source or (sorted(focal_sources)[0] if len(focal_sources) == 1 else None)
            aligned_inputs, alignments = await align_caliber_inputs(
                session,
                workflow_id=peer_wf_id,
                card_id=peer_node["card_id"],
                inputs={},
                entity_inputs=args.entity_filters,
                from_source=effective_from_src,
                restricted=is_restricted,
            )

            recommended_call: dict[str, Any] = {
                "workflow": peer_wf_id,
                "card": peer_node["card_id"],
                "metric_ids": [peer_node["metric_id"]],
                "inputs": aligned_inputs,
            }
            neighbor_item: dict[str, Any] = {
                "kind": edge["kind"],
                "direction": direction,
                "role": edge.get("role"),
                "entity_key": edge.get("entity_key"),
                "lag_periods": edge.get("lag_periods", 0),
                "abs_tol": edge.get("abs_tol"),
                "rel_tol": edge.get("rel_tol"),
                "rationale": edge.get("rationale", ""),
                "target": {
                    "workflow_id": peer_wf_id,
                    "workflow_name": peer_node["workflow_name"],
                    "version": peer_ver,
                    "card_id": peer_node["card_id"],
                    "card_title": peer_node["card_title"],
                    "caliber": peer_node["caliber"],
                    "metric_id": peer_node["metric_id"],
                    "name": peer_node["name"],
                    "unit": peer_node["unit"],
                    **({"sources": peer_node["sources"], "tables_by_source": peer_node.get("tables_by_source", {})}
                       if not is_restricted else {}),
                },
                "recommended_caliber_query": recommended_call,
            }
            if alignments:
                neighbor_item["entity_alignment"] = alignments
            neighbors.append(neighbor_item)

        # 标准业务实体跨源桥（对 builder/admin 展示跨源物理表连接提示，对 user 隐去物理表列细节）
        entity_rows = await list_entities(session)
        bridges: list[dict[str, Any]] = []
        if not is_restricted:
            seen_ent: set[str] = set()
            for src_name in sorted(focal_sources):
                for br in entities_bridging_sources(
                    entity_rows,
                    source_name=src_name,
                    tables=focal_tables.get(src_name),
                ):
                    if br["key"] in seen_ent:
                        continue
                    seen_ent.add(br["key"])
                    bridges.append(br)
        else:
            for row in entity_rows:
                if any(b.get("source_name") in focal_sources for b in confirmed_bindings(row)):
                    bridges.append(entity_to_dict(row, restricted=True))

    out: dict[str, Any] = {
        "kind": ONTOLOGY_NEIGHBORS,
        "focal_metrics": focal_metrics,
        "neighbors": neighbors,
        "entity_bridges": bridges,
    }
    if omitted_unauthorized:
        out["omitted_unauthorized_count"] = omitted_unauthorized

    scope = scope_of(getattr(ctx, "extra", None))
    if scope is not None and hasattr(scope, "record_ontology_neighbors"):
        scope.record_ontology_neighbors(neighbors, focal_metrics=focal_metrics)
    return out


async def _permitted_sources_for_restricted_user(
    session: AsyncSession,
    actor: Any,
    graph: Mapping[str, Any] | None,
) -> set[str]:
    """普通使用者允许用 source_health 探查的数据源名集合：本轮图显式绑定的数据源 + 已授权口径用到的底层数据源。"""
    from app.apps.access import usable_calibers
    from app.db.models import Workflow, WorkflowVersion
    from app.engine.launch import tool_bindings
    from app.engine.schema import GraphSpec
    from app.tools.datasource import source_of_tool

    allowed: set[str] = set()
    if isinstance(graph, dict):
        try:
            spec = GraphSpec.model_validate(graph)
            for _who, tname in tool_bindings(spec):
                src = source_of_tool(tname)
                if src:
                    allowed.add(name_key(src))
        except ValueError:
            pass

    usable = await usable_calibers(session, actor)
    for (wf_id, ver) in usable:
        snap = (
            await session.execute(
                select(WorkflowVersion).where(
                    WorkflowVersion.workflow_id == wf_id,
                    WorkflowVersion.version == int(ver),
                )
            )
        ).scalar_one_or_none()
        wf = await session.get(Workflow, wf_id)
        g = (snap.graph if snap is not None else (wf.graph if wf is not None else None)) or {}
        idx = workflow_ontology_index(workflow_id=wf_id, version=int(ver), graph=g)
        for card in idx["cards"]:
            for s in card["sources"]:
                allowed.add(name_key(s))
    return allowed


@register(
    name=SOURCE_HEALTH,
    category="数据",
    effect="read",
    description=(
        "快速核查指定数据源或已发布口径底层表的健康度、最新业务日期覆盖与待处理结构/数据漂移（断更 date_stalled、"
        "断档 date_gap、行数骤降 volume_drop、新编码 codes_new 等），把结果存为可引用的健康度工件（H 编号）。只读。"
    ),
    args_schema=SourceHealthArgs,
    guide=ONTOLOGY_GUIDE,
)
async def source_health_tool(ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
    args = SourceHealthArgs(**kwargs)
    if not (args.source_name or "").strip() and not (args.workflow or "").strip():
        raise ValueError("source_health 至少需要提供 source_name 或 workflow 之一")

    from app.apps.access import usable_calibers
    from app.db.base import SessionLocal
    from app.db.models import Workflow
    from app.ledger.querycore import NOT_AUTHORIZED, QueryCoreRefused, _workflow_id

    async with SessionLocal() as session:
        actor, is_restricted, graph = await _resolve_actor_and_restriction(session, ctx)
        wf_id: str | None = None
        pub_ver: int | None = None

        if (args.workflow or "").strip():
            try:
                wf_id = await _workflow_id(session, str(args.workflow).strip())
            except QueryCoreRefused as exc:
                raise ValueError(f"{exc.message}（{exc.code}）") from None
            wf = await session.get(Workflow, wf_id)
            pub_ver = int(wf.published_version) if wf is not None and wf.published_version is not None else None
            if is_restricted:
                usable = await usable_calibers(session, actor)
                if pub_ver is None or (wf_id, pub_ver) not in usable:
                    raise ValueError(f"没有权限探查该口径关联的数据源健康度（{NOT_AUTHORIZED}）")

        if is_restricted and (args.source_name or "").strip():
            allowed_sources = await _permitted_sources_for_restricted_user(session, actor, graph)
            if name_key(str(args.source_name).strip()) not in allowed_sources:
                raise ValueError(f"没有权限探查数据源「{args.source_name}」的健康度（{NOT_AUTHORIZED}）")

        result = await sh.check_source_health(
            session,
            source_name=str(args.source_name).strip() if args.source_name else None,
            tables=args.tables,
            workflow_id=wf_id,
            version=pub_ver,
            card_id=args.card,
            metric_id=args.metric_id,
            start=args.start,
            end=args.end,
            restricted=is_restricted,
            live_probe=True,
        )

    run_id = str(getattr(ctx, "run_id", "") or "").strip() or None
    node_id = str(getattr(ctx, "node_id", "") or "").strip() or None
    artifact = await sh.store_source_health(result, run_id=run_id, node_id=node_id)
    return {
        "tool": SOURCE_HEALTH,
        "kind": sh.ARTIFACT_KIND,
        "artifact": artifact,
        "verdict": result.get("verdict"),
        "phrase": result.get("phrase"),
        "result": result,
    }


__all__ = [
    "ONTOLOGY_GUIDE",
    "ONTOLOGY_NEIGHBORS",
    "ONTOLOGY_TOOLS",
    "OntologyNeighborsArgs",
    "SOURCE_HEALTH",
    "SourceHealthArgs",
    "align_caliber_inputs",
    "ontology_neighbors",
    "source_health_tool",
]
