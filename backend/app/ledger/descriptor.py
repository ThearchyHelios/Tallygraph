"""口径可调用描述（descriptor）：给目录页、应用表单、外部集成一份机器可读的稳定描述（next-round 02 的后续）。

口径已经能按固定版本实例化（look/calibers 的查看结果、ledger/compose 的 use_caliber），但入口字段、依赖、能不能出具这些
契约只写在给模型看的文字里。这里把它们写成结构化的数据：

- 只列每个工作流**推荐发布版**（Workflow.published_version，K07：默认调用读推荐版，不机械取最高已发布版）里的
  **源头口径卡**（metrics 节点、caliber_from 为空），和 look/calibers、ledger/derive 的认法一致：转抄卡不列，
  源头才能复用；
- 入口字段用 ledger/derive.entry_fields，按 apps/forms.input_schema 转成 JSON Schema（和表单、use_caliber 校验同一个）；
- 依赖只从图里的取数节点认数据源和表名，**不返回 SQL、不返回参数值**（SQL 里可能写着遮罩值，组合时由服务端照抄）；
- 能不能组合按**每个指标**判（K08 S4，look/calibers.metric_options）：B 独有那条查询坏了、绑定对不上，只让 B 不可以选，
  A 照旧可组合；一个可选指标也没有才不能组合。另给这一版的两份闭包摘要——``closure``（不点名、整卡那份）与
  ``selectable_metrics`` / ``unavailable_metrics``（每个指标的摘要与原因），以及 ``computes_unselected``（共享 SQL 还在
  算哪些没选中的指标）。摘要与描述里只有 query_id、字段、工具、指纹这些，**没有 SQL 正文、没有参数值**；
- 出具只描述这一版的策略：级别、图里有没有人工节点或写外部系统的节点。全局的自动出具开关、每日上限是运行时条件，这里
  不查——descriptor 说的是「这一版允许什么」，不代表这次运行一定能出具。
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.apps import versions as app_versions
from app.apps.backfill import graph_side_effect
from app.apps.forms import input_schema
from app.apps.tree import node_map
from app.db.models import Workflow
from app.ledger.derive import entry_fields
from app.look.calibers import metric_options, option_reason

#: 指标只写口径卡配置里真有的这几个键
METRIC_KEYS = ("id", "name", "unit", "decimals", "format", "kind", "definition", "params", "external", "rule",
               "assessment", "synonyms", "grain", "time_field", "numerator", "denominator", "dimensions", "filters", "contract")


async def list_calibers(session: AsyncSession, *, workflow_id: str | None = None) -> list[dict[str, Any]]:
    """每个工作流**推荐发布版**（Workflow.published_version）里的源头口径卡，一张卡一条描述。给了 workflow_id 只列那个工作流。

    K07：默认调用读的是**推荐版**（重新推上一版旧版之后，默认就跟着它走），不再机械地取「最高已发布版」——最高版
    只在升级发现里用（app/apps/publications.upgrade_of）。

    同一版图里的 graph_side_effect 按 (workflow_id, version) 只算一次：它要读子图、跑发布检查，比其余字段慢得多。"""
    from app.apps import publications

    query = select(Workflow).order_by(Workflow.name, Workflow.id)
    if workflow_id:
        query = query.where(Workflow.id == workflow_id)
    side_cache: dict[tuple[str, int], str | None] = {}
    out: list[dict[str, Any]] = []
    for workflow in (await session.execute(query)).scalars().all():
        row = await publications.recommended(session, workflow)
        if row is None:
            continue
        for node_id, node in node_map(row.graph or {}).items():
            if node["type"] != "metrics" or node["config"].get("caliber_from") not in (None, "", {}):
                continue
            made = await _describe(session, workflow, row, node_id, side_cache)
            if made is not None:
                out.append(made)
    return out


async def caliber_descriptor(session: AsyncSession, workflow_id: str, version: int,
                             node_id: str) -> dict[str, Any] | None:
    """一个工作流版本里一张源头口径卡的描述；工作流不存在、版本没发布过或已退役、节点不是源头口径卡时返回 None。"""
    workflow = await session.get(Workflow, str(workflow_id or ""))
    if workflow is None:
        return None
    row = await app_versions.version_row(session, workflow.id, int(version))
    if row is None or not app_versions.is_published(row, current=workflow.published_version):
        return None
    return await _describe(session, workflow, row, str(node_id), {})


async def _describe(session: AsyncSession, workflow: Workflow, row: Any, node_id: str,
                    side_cache: dict[tuple[str, int], str | None]) -> dict[str, Any] | None:
    """一张卡的描述；节点不是源头口径卡时返回 None。"""
    graph = row.graph or {}
    nodes = node_map(graph)
    card = nodes.get(node_id)
    if card is None or card["type"] != "metrics" or card["config"].get("caliber_from") not in (None, "", {}):
        return None
    cfg = card["config"]
    level = str(row.level or "")
    made = metric_options(graph, node_id, workflow_id=workflow.id, version=int(row.version))
    from app.apps import publications
    from app.data.caliber_identity import semantic_hash

    key = (workflow.id, int(row.version))
    if key not in side_cache:
        side_cache[key] = await graph_side_effect(session, graph, level=level or None)
    side_effect = side_cache[key]
    sources, tables = publications.fetch_dependencies(graph)
    publication = await publications.latest(session, workflow.id, int(row.version))
    reasons: list[str] = []
    if level != "governed":
        reasons.append("这一版是已发布级别，只支持一键出具")
    if side_effect is not None:
        reasons.append(f"这一版{side_effect}，不能替人再次执行")
    if not sources:
        reasons.append("这一版没有取数节点，账本不能按口径证明等价")
    return {
        "workflow_id": workflow.id,
        "workflow_name": workflow.name,
        "version": int(row.version),
        "level": level,
        "graph_hash": row.graph_hash,
        "semantic_hash": semantic_hash(graph, node_id),
        # K07：这一版的发布依据是哪一份清单（没有清单的是加这一机制之前发布的老资产，值为 null）
        "publication": ({"id": publication.id, "manifest": publication.manifest,
                         "proven": bool(publication.proven)}
                        if publication is not None else None),
        "card": {"node_id": node_id, "title": card["title"], "caliber": cfg.get("caliber"),
                 "caliber_version": cfg.get("caliber_version")},
        "metrics": [{k: m[k] for k in METRIC_KEYS if k in m}
                    for m in cfg.get("metrics") or []
                    if isinstance(m, dict) and m.get("id")],
        "entry_schema": input_schema(list(entry_fields(graph))),
        # 能不能组合按**每个指标**判（K08 S4）：B 独有那条查询坏了、绑定对不上，A 照旧可选；一个指标也选不了才不能组合。
        # 摘要里只有 query_id、字段、工具、指纹与共享声明，没有 SQL 正文、没有参数值
        "composable": made.composable,
        "compose_reason": option_reason(made),
        "closure": made.default,
        "selectable_metrics": [dict(m) for m in made.selectable],
        "unavailable_metrics": [dict(m) for m in made.unavailable],
        "computes_unselected": shared_queries(made),
        "dependencies": {"sources": sources, "tables": tables,
                         "catalog_versions": row.catalog_versions, "schema_versions": row.schema_versions},
        "issuance": {"level": level, "auto_issue": level == "governed" and side_effect is None and bool(sources),
                     "requires_independent_proof": True, "side_effects": side_effect, "reasons": reasons},
    }


def shared_queries(made: Any) -> list[dict[str, Any]]:
    """这几份摘要里「还在算没选中的指标」的查询：{query_id, node_id, metric_ids, computes_unselected}。

    按 (query_id, 这次选中的指标) 去重、按出现先后排：一张卡上几个指标共用一条 SQL 时，选谁、还在算谁一目了然（K08 S3
    不做投影，共享 SQL 原样保留）。整卡那一份加上每个可选指标那一份，只从闭包的摘要里收，不另解析一遍依赖。"""
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, tuple[str, ...]]] = set()
    for summary in [made.default, *made.selectable]:
        for query in (summary or {}).get("queries") or []:
            others = [str(m) for m in query.get("computes_unselected") or []]
            read_by = [str(m) for m in query.get("metric_ids") or []]
            if not others or (str(query["query_id"]), tuple(read_by)) in seen:
                continue
            seen.add((str(query["query_id"]), tuple(read_by)))
            out.append({"query_id": query["query_id"], "node_id": query["node_id"],
                        "metric_ids": read_by, "computes_unselected": others})
    return out


__all__ = ["caliber_descriptor", "list_calibers", "shared_queries"]
