"""固定版本依赖的反向索引：一次读取已发布快照，沿引用关系计算传递影响。"""
from __future__ import annotations

from collections import defaultdict, deque
from typing import Any

from sqlalchemy import select

from app.db.models import Workflow, WorkflowVersion
from app.engine.schema import GraphSpec


async def dependents(session: Any, workflow_id: str, version: int) -> dict[str, Any]:
    from app.apps.versions import is_published, pinned_upstream, pinning_apps

    rows = (await session.execute(select(WorkflowVersion, Workflow.name, Workflow.published_version)
                                 .join(Workflow, Workflow.id == WorkflowVersion.workflow_id))).all()
    current = {row.workflow_id: published for row, _, published in rows}
    reverse: dict[tuple[str, int], list[dict[str, Any]]] = defaultdict(list)
    unreadable = []
    for row, name, published in rows:
        if not is_published(row, current=published):
            continue
        try:
            spec = GraphSpec.model_validate(row.graph)
        except ValueError:
            unreadable.append({"workflow_id": row.workflow_id, "version": row.version, "name": name})
            continue
        for node in spec.nodes:
            ref = pinned_upstream(node)
            if ref is None and str(node.type) == "subgraph" and node.config.get("workflow_id"):
                wid = str(node.config["workflow_id"])
                if current.get(wid):
                    ref = ("subgraph", wid, current[wid])
            if ref:
                kind, wid, pinned = ref
                reverse[(wid, pinned)].append({"workflow_id": row.workflow_id, "version": row.version,
                                               "name": name, "node_id": node.id, "kind": kind})
    root = (workflow_id, version)
    queue, visited = deque([(root, 0)]), {root}
    affected, indirect_apps = [], []
    while queue:
        key, depth = queue.popleft()
        for ref in reverse.get(key, []):
            upstream = (ref["workflow_id"], ref["version"])
            affected.append({**ref, "depth": depth + 1, "via": {"workflow_id": key[0], "version": key[1]}})
            if upstream in visited:
                continue
            visited.add(upstream)
            queue.append((upstream, depth + 1))
            indirect_apps.extend({**app, "via": {"workflow_id": upstream[0], "version": upstream[1]}}
                                 for app in await pinning_apps(session, *upstream))
    return {"workflows": affected, "indirect_apps": indirect_apps, "unreadable_workflows": unreadable}
