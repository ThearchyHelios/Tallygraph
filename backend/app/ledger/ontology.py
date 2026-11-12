"""指标本体模型：跨口径语义关系（links）、DAG 无环门禁、自动反向数据血缘（metric -> source/table）。

设计原则（对应计划书 §2 与铁律 1-3）：
1. **三类显式指标关系**（挂在口径卡指标定义的 `links` 数组，参与 definition_hash）：
   - `factor`：构成因子 / 公式拆解（有向无环 DAG，发布门禁强拒任何自环或跨口径成环）；
   - `reconciles_with`：跨源镜像对账关系（可声明 abs_tol / rel_tol 对账容差，双向可对称）；
   - `upstream_driver`：跨源上游业务/物理驱动关系（带 entity_key 对齐实体与可选 lag_periods 滞后期）。
2. **建议态与已确认态分离**（P2）：
   - `status` 取值 `proposed | verified | confirmed | rejected`；
   - 文档起草（docdraft）或 AI 推荐产出的候选一律强制 `proposed`，**运行时自动穿透只遍历 `verified | confirmed`**。
3. **自动反向血缘**：
   - 不靠人工维护“指标用了哪张表”，直接复用 `ledger/closure.py` 依赖闭包与 `engine/evidence.sql_tables`
     静态提取 `(source_name, table_name)`，绝不把 SQL 正文或遮罩值写进本体索引或发布清单。
"""
from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.artifact_store import canonical_json, content_hash
from app.engine.schema import GraphSpec, NodeType

LINK_KINDS = ("factor", "reconciles_with", "upstream_driver")
FACTOR_ROLES = ("numerator", "denominator", "multiplier", "addend", "subtrahend", "component")
LINK_STATUSES = ("proposed", "verified", "confirmed", "rejected")
ACTIVE_LINK_STATUSES = ("verified", "confirmed")
LINK_SOURCES = ("human", "rule", "docdraft", "ai")

ONTOLOGY_CYCLE_CODE = "ontology.factor_cycle"
ONTOLOGY_LINK_INVALID_CODE = "ontology.link_invalid"

_NODE_REF_RE = re.compile(r"\bnodes\.([A-Za-z_][\w-]*)|\bnodes\[['\"]([A-Za-z_][\w-]*)['\"]\]")
_SLUG_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


class OntologyInvalid(ValueError):
    """指标本体关系声明格式错误。"""

    def __init__(self, message: str, *, field: str = "", code: str = ONTOLOGY_LINK_INVALID_CODE) -> None:
        super().__init__(message)
        self.message = message
        self.field = field
        self.code = code


class OntologyCycleError(OntologyInvalid):
    """指标 factor 构成边存在环。"""

    def __init__(self, cycle: list[str], *, field: str = "links") -> None:
        rendered = " -> ".join(cycle)
        super().__init__(f"指标构成因子（factor）不能形成循环依赖：{rendered}", field=field, code=ONTOLOGY_CYCLE_CODE)
        self.cycle = cycle


def metric_node_key(workflow_id: str, card_id: str, metric_id: str) -> str:
    return f"{workflow_id or 'self'}:{card_id}:{metric_id}"


def normalize_metric_target(
    raw: Any,
    *,
    field: str = "target",
    default_workflow_id: str = "",
    default_card_id: str = "",
) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise OntologyInvalid("关联目标（target）需要是包含 workflow_id、card_id、metric_id 的对象", field=field)
    workflow_id = str(raw.get("workflow_id") or default_workflow_id or "").strip()
    card_id = str(raw.get("card_id") or default_card_id or "").strip()
    metric_id = str(raw.get("metric_id") or "").strip()
    if not metric_id:
        raise OntologyInvalid("关联目标缺少指标标识（metric_id）", field=f"{field}.metric_id")
    if not card_id:
        raise OntologyInvalid("关联目标缺少口径卡节点标识（card_id）", field=f"{field}.card_id")
    out: dict[str, Any] = {
        "workflow_id": workflow_id,
        "card_id": card_id,
        "metric_id": metric_id,
    }
    version = raw.get("version")
    if version not in (None, ""):
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            raise OntologyInvalid("关联目标的固定版本（version）须为正整数", field=f"{field}.version")
        out["version"] = version
    return out


def normalize_link(
    raw: Any,
    *,
    index: int = 0,
    default_workflow_id: str = "",
    default_card_id: str = "",
    force_proposed: bool = False,
    default_source: str = "human",
) -> dict[str, Any]:
    at = f"links[{index}]"
    if not isinstance(raw, Mapping):
        raise OntologyInvalid("指标关联项需要是对象", field=at)
    kind = str(raw.get("kind") or "").strip()
    if kind not in LINK_KINDS:
        raise OntologyInvalid(f"未知的指标关系类型「{kind}」，可选：{', '.join(LINK_KINDS)}", field=f"{at}.kind")
    target = normalize_metric_target(
        raw.get("target"),
        field=f"{at}.target",
        default_workflow_id=default_workflow_id,
        default_card_id=default_card_id,
    )
    source = str(raw.get("source") or default_source).strip()
    if source not in LINK_SOURCES:
        raise OntologyInvalid(f"关系来源「{source}」无效，可选：{', '.join(LINK_SOURCES)}", field=f"{at}.source")
    status = "proposed" if (force_proposed or source in ("docdraft", "ai") and "status" not in raw) else str(
        raw.get("status") or "confirmed"
    ).strip()
    if force_proposed:
        status = "proposed"
    if status not in LINK_STATUSES:
        raise OntologyInvalid(f"关系状态「{status}」无效，可选：{', '.join(LINK_STATUSES)}", field=f"{at}.status")

    out: dict[str, Any] = {
        "kind": kind,
        "target": target,
        "status": status,
        "source": source,
    }
    if raw.get("role") not in (None, ""):
        role = str(raw.get("role") or "").strip()
        if role not in FACTOR_ROLES:
            raise OntologyInvalid(f"因子角色「{role}」无效，可选：{', '.join(FACTOR_ROLES)}", field=f"{at}.role")
        out["role"] = role
    if raw.get("entity_key") not in (None, ""):
        entity_key = str(raw.get("entity_key") or "").strip()
        if not _SLUG_RE.match(entity_key):
            raise OntologyInvalid("对齐实体键（entity_key）格式不合法", field=f"{at}.entity_key")
        out["entity_key"] = entity_key
    if raw.get("lag_periods") not in (None, ""):
        lag = raw.get("lag_periods")
        if isinstance(lag, bool) or not isinstance(lag, int) or lag < 0 or lag > 12:
            raise OntologyInvalid("滞后期数（lag_periods）须为 0 到 12 之间的整数", field=f"{at}.lag_periods")
        if lag > 0:
            out["lag_periods"] = lag
    for tol_key in ("abs_tol", "rel_tol"):
        if raw.get(tol_key) not in (None, ""):
            val = raw.get(tol_key)
            if isinstance(val, bool) or not isinstance(val, (int, float)) or not math.isfinite(float(val)) or float(val) < 0:
                raise OntologyInvalid(f"对账容差「{tol_key}」须为非负有限数字", field=f"{at}.{tol_key}")
            out[tol_key] = float(val)
    rationale = str(raw.get("rationale") or "").strip()
    if rationale:
        out["rationale"] = rationale
    citations = raw.get("citations")
    if isinstance(citations, list) and citations:
        out["citations"] = [dict(c) for c in citations if isinstance(c, Mapping)]
    # 稳定内容 id，便于审阅与去重
    link_id = str(raw.get("id") or "").strip()
    if not link_id:
        link_id = content_hash(canonical_json({"kind": kind, "target": target, "role": out.get("role")}))[:12]
    out["id"] = link_id
    return out


def normalize_links(
    raw_links: Any,
    *,
    default_workflow_id: str = "",
    default_card_id: str = "",
    force_proposed: bool = False,
    default_source: str = "human",
) -> list[dict[str, Any]]:
    if raw_links in (None, ""):
        return []
    if not isinstance(raw_links, list):
        raise OntologyInvalid("指标关联列表（links）需要是数组", field="links")
    seen: set[tuple[str, str, str, str, str]] = set()
    out: list[dict[str, Any]] = []
    for idx, raw in enumerate(raw_links):
        norm = normalize_link(
            raw,
            index=idx,
            default_workflow_id=default_workflow_id,
            default_card_id=default_card_id,
            force_proposed=force_proposed,
            default_source=default_source,
        )
        t = norm["target"]
        dedup_key = (norm["kind"], t["workflow_id"], t["card_id"], t["metric_id"], str(norm.get("role") or ""))
        if dedup_key in seen:
            continue
        seen.add(dedup_key)
        out.append(norm)
    return out


def active_links(metric: Mapping[str, Any], *, kinds: Iterable[str] | None = None) -> list[dict[str, Any]]:
    """取出单个指标定义上已生效（verified / confirmed）的关系边；proposed / rejected 不参与自动穿透。"""
    raw = metric.get("links") if isinstance(metric, Mapping) else None
    if not isinstance(raw, list) or not raw:
        return []
    allowed_kinds = set(kinds) if kinds is not None else None
    out: list[dict[str, Any]] = []
    for idx, item in enumerate(raw):
        try:
            norm = normalize_link(item, index=idx)
        except OntologyInvalid:
            continue
        if norm["status"] not in ACTIVE_LINK_STATUSES:
            continue
        if allowed_kinds is not None and norm["kind"] not in allowed_kinds:
            continue
        out.append(norm)
    return out


# --------------------------------------------------------------------------
# DAG 无环检测（factor 构成边）
# --------------------------------------------------------------------------


def find_factor_cycles(edges: Iterable[tuple[str, str]]) -> list[list[str]]:
    """在 factor 有向边集合 [(u, v)] 中找出代表性环路径（如 ['a', 'b', 'a']）；无环返回 []。"""
    adj: dict[str, list[str]] = {}
    nodes: set[str] = set()
    for src, dst in edges:
        if not src or not dst:
            continue
        nodes.add(src)
        nodes.add(dst)
        adj.setdefault(src, []).append(dst)
    for k in adj:
        adj[k] = sorted(set(adj[k]))

    WHITE, GREY, BLACK = 0, 1, 2
    state: dict[str, int] = dict.fromkeys(sorted(nodes), WHITE)
    stack: list[str] = []
    pos_in_stack: dict[str, int] = {}
    cycles: list[list[str]] = []
    seen_signatures: set[tuple[str, ...]] = set()

    def _record_cycle(target: str) -> None:
        start = pos_in_stack[target]
        loop = [*stack[start:], target]
        ring = loop[:-1]
        min_idx = min(range(len(ring)), key=lambda i: ring[i])
        canonical = tuple(ring[min_idx:] + ring[:min_idx])
        if canonical not in seen_signatures:
            seen_signatures.add(canonical)
            cycles.append(loop)

    for root in sorted(nodes):
        if state[root] != WHITE:
            continue
        state[root] = GREY
        pos_in_stack[root] = 0
        stack = [root]
        frames: list[tuple[str, int]] = [(root, 0)]
        while frames:
            curr, idx = frames[-1]
            neighbors = adj.get(curr, ())
            if idx >= len(neighbors):
                state[curr] = BLACK
                pos_in_stack.pop(curr, None)
                stack.pop()
                frames.pop()
                continue
            frames[-1] = (curr, idx + 1)
            nxt = neighbors[idx]
            if state.get(nxt, WHITE) == GREY:
                _record_cycle(nxt)
            elif state.get(nxt, WHITE) == WHITE:
                state[nxt] = GREY
                pos_in_stack[nxt] = len(stack)
                stack.append(nxt)
                frames.append((nxt, 0))
    return cycles


def check_factor_dag(edges: Iterable[tuple[str, str]]) -> None:
    cycles = find_factor_cycles(edges)
    if cycles:
        raise OntologyCycleError(cycles[0])


# --------------------------------------------------------------------------
# 自动反向血缘：Closure + sql_tables -> metric -> [(source, tables)]
# --------------------------------------------------------------------------


def _referenced_nodes(expression: str) -> list[str]:
    if not isinstance(expression, str) or not expression:
        return []
    out: list[str] = []
    for m in _NODE_REF_RE.finditer(expression):
        nid = m.group(1) or m.group(2)
        if nid and nid not in out:
            out.append(nid)
    return out


def card_metric_lineage(graph: Mapping[str, Any], card_id: str) -> dict[str, Any]:
    """从工作流图的依赖闭包静态推导一张源头口径卡各指标的数据源与物理表血缘。

    不存 SQL 原文、不读任何参数字面量或遮罩值，只记 (node_id, source_name, tables)。
    """
    from app.apps.tree import node_map
    from app.engine.evidence import sql_tables
    from app.ledger import closure as closure_mod
    from app.tools.datasource import source_of_tool

    nmap = node_map(graph or {})
    card = nmap.get(card_id) or {}
    cfg = card.get("config") if isinstance(card.get("config"), Mapping) else {}
    metrics = [m for m in (cfg.get("metrics") or []) if isinstance(m, Mapping) and str(m.get("id") or "").strip()]

    try:
        built = closure_mod.build(dict(graph or {}), "", 1, card_id)
        closure_nodes = {q.node_id for q in built.queries} | set(built.excluded_query_ids)
        if not closure_nodes:
            closure_nodes = set(nmap.keys())
    except Exception:  # noqa: BLE001 - 图不完整时退回整张图里的上游工具节点
        closure_nodes = set(nmap.keys())

    query_lineage: dict[str, dict[str, Any]] = {}
    for nid in sorted(closure_nodes):
        node = nmap.get(nid)
        if not isinstance(node, Mapping) or node.get("type") != "tool":
            continue
        ncfg = node.get("config") if isinstance(node.get("config"), Mapping) else {}
        tool_name = str(ncfg.get("tool") or "").strip()
        src = source_of_tool(tool_name)
        if not src or "{{" in src:
            continue
        args = ncfg.get("args") if isinstance(ncfg.get("args"), Mapping) else {}
        sql = args.get("sql") if isinstance(args.get("sql"), str) else ""
        tables = sorted({t.rsplit(".", 1)[-1] for t in sql_tables(sql) if t}) if sql.strip() else []
        query_lineage[nid] = {"node_id": nid, "source": src, "tables": tables}

    tables_by_source: dict[str, set[str]] = {}
    for q in query_lineage.values():
        tables_by_source.setdefault(q["source"], set()).update(q["tables"])

    metric_map: dict[str, list[dict[str, Any]]] = {}
    all_queries = list(query_lineage.values())
    for metric in metrics:
        mid = str(metric["id"]).strip()
        refs = _referenced_nodes(str(metric.get("expression") or ""))
        direct = [query_lineage[r] for r in refs if r in query_lineage]
        metric_map[mid] = direct if direct else all_queries

    return {
        "card_id": card_id,
        "sources": sorted(tables_by_source.keys()),
        "tables_by_source": {src: sorted(tbls) for src, tbls in sorted(tables_by_source.items())},
        "metrics": metric_map,
    }


def workflow_ontology_index(
    *,
    workflow_id: str,
    version: int,
    graph: Mapping[str, Any],
    workflow_name: str = "",
) -> dict[str, Any]:
    """提取一份工作流版本上全部源头口径卡的指标节点、本体关系边与自动反向表血缘。"""
    from app.apps.publications import source_cards
    from app.apps.tree import node_map

    nmap = node_map(graph or {})
    cards_out: list[dict[str, Any]] = []
    edges_out: list[dict[str, Any]] = []
    for card_meta in source_cards(graph):
        cid = str(card_meta["id"])
        lineage = card_metric_lineage(graph, cid)
        node = nmap.get(cid) or {}
        cfg = node.get("config") if isinstance(node.get("config"), Mapping) else {}
        metrics_out: list[dict[str, Any]] = []
        for raw_m in cfg.get("metrics") or []:
            if not isinstance(raw_m, Mapping) or not str(raw_m.get("id") or "").strip():
                continue
            mid = str(raw_m["id"]).strip()
            mname = str(raw_m.get("name") or mid).strip()
            raw_links = raw_m.get("links")
            links: list[dict[str, Any]] = []
            if isinstance(raw_links, list) and raw_links:
                for idx, item in enumerate(raw_links):
                    try:
                        norm = normalize_link(item, index=idx, default_workflow_id=workflow_id, default_card_id=cid)
                    except OntologyInvalid:
                        continue
                    links.append(norm)
                    target = norm["target"]
                    edges_out.append({
                        "id": norm["id"],
                        "kind": norm["kind"],
                        "status": norm["status"],
                        "source": norm["source"],
                        "role": norm.get("role"),
                        "entity_key": norm.get("entity_key"),
                        "lag_periods": norm.get("lag_periods", 0),
                        "abs_tol": norm.get("abs_tol"),
                        "rel_tol": norm.get("rel_tol"),
                        "rationale": norm.get("rationale", ""),
                        "from": {"workflow_id": workflow_id, "version": version, "card_id": cid, "metric_id": mid},
                        "to": {
                            "workflow_id": target["workflow_id"] or workflow_id,
                            "version": target.get("version"),
                            "card_id": target["card_id"],
                            "metric_id": target["metric_id"],
                        },
                    })
            metrics_out.append({
                "metric_id": mid,
                "name": mname,
                "unit": str(raw_m.get("unit") or ""),
                "contract": dict(raw_m["contract"]) if isinstance(raw_m.get("contract"), Mapping) else None,
                "links": links,
                "lineage": lineage["metrics"].get(mid, []),
            })
        cards_out.append({
            "workflow_id": workflow_id,
            "workflow_name": workflow_name,
            "version": version,
            "card_id": cid,
            "title": card_meta["title"],
            "caliber": card_meta["caliber"],
            "sources": lineage["sources"],
            "tables_by_source": lineage["tables_by_source"],
            "metrics": metrics_out,
        })
    return {"workflow_id": workflow_id, "version": version, "cards": cards_out, "edges": edges_out}


def ontology_manifest_section(*, workflow_id: str, version: int, graph: Mapping[str, Any]) -> dict[str, Any] | None:
    """供 publications.build_content 物化进发布清单：仅当图上声明了指标本体 links 时写入，保证存量清单哈希一字不变。"""
    index = workflow_ontology_index(workflow_id=workflow_id, version=version, graph=graph)
    if not index["edges"]:
        return None
    lineage = {
        card["card_id"]: {
            "sources": card["sources"],
            "tables_by_source": card["tables_by_source"],
            "metrics": {
                m["metric_id"]: [
                    {"source": item["source"], "tables": item["tables"]}
                    for item in m["lineage"]
                ]
                for m in card["metrics"]
            },
        }
        for card in index["cards"]
    }
    return {"edges": index["edges"], "lineage": lineage}


# --------------------------------------------------------------------------
# 发布预检与全站本体图构建
# --------------------------------------------------------------------------


def _graph_factor_edges(graph: Mapping[str, Any], *, workflow_id: str) -> tuple[list[tuple[str, str]], list[dict[str, Any]]]:
    """返回 (factor 有向边列表, 语法校验问题列表)。只对非 rejected 的 factor 边判环（含 proposed，早发现早拦）。"""
    spec_nodes = graph.get("nodes") if isinstance(graph, Mapping) else None
    edges: list[tuple[str, str]] = []
    issues: list[dict[str, Any]] = []
    if not isinstance(spec_nodes, list):
        return edges, issues
    wf_key = workflow_id or "self"
    for node in spec_nodes:
        if not isinstance(node, Mapping) or node.get("type") != NodeType.METRICS:
            continue
        nid = str(node.get("id") or "")
        cfg = node.get("data", {}).get("config") if isinstance(node.get("data"), Mapping) else node.get("config")
        if not isinstance(cfg, Mapping) or cfg.get("caliber_from") not in (None, "", {}):
            continue
        metrics = cfg.get("metrics")
        if not isinstance(metrics, list):
            continue
        for m_idx, metric in enumerate(metrics):
            if not isinstance(metric, Mapping):
                continue
            mid = str(metric.get("id") or "").strip()
            raw_links = metric.get("links")
            if raw_links in (None, "", []):
                continue
            if not isinstance(raw_links, list):
                issues.append({
                    "level": "error",
                    "node_id": nid,
                    "field": f"metrics[{m_idx}].links",
                    "code": ONTOLOGY_LINK_INVALID_CODE,
                    "message": f"指标「{mid or m_idx + 1}」的本体关系（links）需要是数组",
                })
                continue
            for l_idx, raw_link in enumerate(raw_links):
                try:
                    norm = normalize_link(raw_link, index=l_idx, default_workflow_id=wf_key, default_card_id=nid)
                except OntologyInvalid as exc:
                    issues.append({
                        "level": "error",
                        "node_id": nid,
                        "field": f"metrics[{m_idx}].{exc.field}",
                        "code": exc.code,
                        "message": f"指标「{mid or m_idx + 1}」：{exc.message}",
                    })
                    continue
                if norm["kind"] == "factor" and norm["status"] != "rejected":
                    target = norm["target"]
                    dst_wf = target["workflow_id"] or wf_key
                    src_key = metric_node_key(wf_key, nid, mid)
                    dst_key = metric_node_key(dst_wf, target["card_id"], target["metric_id"])
                    edges.append((src_key, dst_key))
    return edges, issues


async def ontology_publish_issues(
    session: AsyncSession,
    spec: GraphSpec,
    *,
    workflow_id: str = "",
) -> list[dict[str, Any]]:
    """发布门禁检查：校验指标 links 结构，并结合已发布口径检查跨工作流 factor 环依赖。"""
    from app.db.models import Workflow, WorkflowVersion

    raw_graph = spec.model_dump()
    local_edges, issues = _graph_factor_edges(raw_graph, workflow_id=workflow_id)
    if issues:
        return issues

    all_edges = list(local_edges)
    if local_edges:
        rows = (
            await session.execute(
                select(Workflow.id, Workflow.published_version, WorkflowVersion.graph)
                .join(
                    WorkflowVersion,
                    (WorkflowVersion.workflow_id == Workflow.id)
                    & (WorkflowVersion.version == Workflow.published_version),
                )
                .where(Workflow.published_version.is_not(None))
            )
        ).all()
        for pub_wf_id, _ver, pub_graph in rows:
            if workflow_id and str(pub_wf_id) == str(workflow_id):
                continue
            peer_edges, _ = _graph_factor_edges(pub_graph or {}, workflow_id=str(pub_wf_id))
            all_edges.extend(peer_edges)

    for cycle in find_factor_cycles(all_edges):
        issues.append({
            "level": "error",
            "node_id": cycle[0].split(":")[1] if ":" in cycle[0] else None,
            "field": "metrics.links",
            "code": ONTOLOGY_CYCLE_CODE,
            "message": f"指标构成因子（factor）不能形成循环依赖：{' -> '.join(cycle)}",
        })
    return issues


def attach_docdraft_links(metric: Mapping[str, Any], raw_links: Any, chunks: Mapping[str, dict]) -> list[dict[str, Any]]:
    """供 ledger/docdraft 规整模型从文档抽取的候选指标关系（P2）。

    只有至少带一条可逐字核对原文的引文（或者明确复用本指标已核对通过的引文）才保留，且状态一律锁为 `proposed`、
    来源记为 `docdraft`，绝不直接变成运行时自动穿透的已确认边。
    """
    from app.ledger.docdraft import _check_citation

    if not isinstance(raw_links, list) or not raw_links:
        return []
    fallback_citations = list(metric.get("citations") or [])
    out: list[dict[str, Any]] = []
    for idx, item in enumerate(raw_links):
        if not isinstance(item, Mapping):
            continue
        raw_cites = item.get("citations")
        verified = [kept for kept in (_check_citation(c, chunks) for c in (raw_cites or [])) if kept] \
            if isinstance(raw_cites, list) and raw_cites else fallback_citations
        if not verified:
            continue
        candidate = {**dict(item), "citations": verified, "source": "docdraft", "status": "proposed"}
        try:
            norm = normalize_link(candidate, index=idx, force_proposed=True, default_source="docdraft")
        except OntologyInvalid:
            continue
        out.append(norm)
    return out


async def build_published_ontology_graph(
    session: AsyncSession,
    *,
    include_proposed: bool = False,
    allowed_calibers: set[tuple[str, int, str]] | None = None,
    restricted: bool = False,
) -> dict[str, Any]:
    """汇总当前已发布口径的指标本体节点、关系边、自动表血缘及标准业务实体跨源桥。"""
    from app.data.entities import confirmed_bindings, entity_to_dict, list_entities
    from app.db.models import Workflow, WorkflowVersion

    rows = (
        await session.execute(
            select(Workflow.id, Workflow.name, Workflow.published_version, WorkflowVersion.graph)
            .join(
                WorkflowVersion,
                (WorkflowVersion.workflow_id == Workflow.id)
                & (WorkflowVersion.version == Workflow.published_version),
            )
            .where(Workflow.published_version.is_not(None))
            .order_by(Workflow.id.asc())
        )
    ).all()

    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    node_keys: set[str] = set()

    for wf_id, wf_name, pub_ver, graph in rows:
        ver = int(pub_ver)
        idx = workflow_ontology_index(
            workflow_id=str(wf_id),
            version=ver,
            graph=graph or {},
            workflow_name=str(wf_name or wf_id),
        )
        for card in idx["cards"]:
            cid = card["card_id"]
            if allowed_calibers is not None and (str(wf_id), ver, cid) not in allowed_calibers:
                continue
            for m in card["metrics"]:
                mid = m["metric_id"]
                key = metric_node_key(str(wf_id), cid, mid)
                node_keys.add(key)
                node_entry: dict[str, Any] = {
                    "key": key,
                    "workflow_id": str(wf_id),
                    "workflow_name": str(wf_name or wf_id),
                    "version": ver,
                    "card_id": cid,
                    "card_title": card["title"],
                    "caliber": card["caliber"],
                    "metric_id": mid,
                    "name": m["name"],
                    "unit": m["unit"],
                    "contract": m["contract"],
                    "sources": card["sources"],
                }
                if not restricted:
                    node_entry["tables_by_source"] = card["tables_by_source"]
                    node_entry["lineage"] = m["lineage"]
                nodes.append(node_entry)

        for edge in idx["edges"]:
            if not include_proposed and edge["status"] not in ACTIVE_LINK_STATUSES:
                continue
            src = edge["from"]
            dst = edge["to"]
            if allowed_calibers is not None:
                if (src["workflow_id"], ver, src["card_id"]) not in allowed_calibers:
                    continue
                # 若受限用户无权访问目标口径，不暴露未授权邻接边（铁律 2）
                dst_allowed = any(
                    w == dst["workflow_id"] and c == dst["card_id"]
                    and (dst.get("version") is None or v == dst.get("version"))
                    for w, v, c in allowed_calibers
                )
                if not dst_allowed:
                    continue
            edges.append(edge)

    entity_rows = await list_entities(session)
    entities_out: list[dict[str, Any]] = []
    for row in entity_rows:
        if not include_proposed and not confirmed_bindings(row) and not row.members:
            continue
        entities_out.append(entity_to_dict(row, restricted=restricted))

    warnings: list[dict[str, Any]] = []
    factor_pairs = [
        (
            metric_node_key(e["from"]["workflow_id"], e["from"]["card_id"], e["from"]["metric_id"]),
            metric_node_key(e["to"]["workflow_id"], e["to"]["card_id"], e["to"]["metric_id"]),
        )
        for e in edges
        if e["kind"] == "factor" and e["status"] != "rejected"
    ]
    for cycle in find_factor_cycles(factor_pairs):
        warnings.append({"code": ONTOLOGY_CYCLE_CODE, "message": f"检测到 factor 循环：{' -> '.join(cycle)}"})
    for e in edges:
        dst_key = metric_node_key(e["to"]["workflow_id"], e["to"]["card_id"], e["to"]["metric_id"])
        if allowed_calibers is None and dst_key not in node_keys:
            warnings.append({
                "code": "ontology.dangling_target",
                "edge_id": e["id"],
                "target": dst_key,
                "message": f"指标关联指向的已发布口径指标「{dst_key}」当前不存在或未发布",
            })

    return {
        "nodes": nodes,
        "edges": edges,
        "entities": entities_out,
        "warnings": warnings,
    }
