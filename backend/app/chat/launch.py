"""问数据的运行怎么发起：开登录后 user 角色的那几条规则，HTTP 发起（api/runs.start_run）和服务端跑的一轮（chat/job.py）共用。

以前只有 start_run 一处（浏览器发 POST /api/runs）。一轮问数据搬到服务端之后，发起运行的是这一轮的后台任务，规则不能另抄一份——
使用者的问数据运行只许白名单里的节点和工具、记忆固定为自己的范围、查询结果在工具边界遮、数据源一律只读，这几条一处说清。
请求体能不能带记忆范围、知识库，能不能发正式运行，这些是 HTTP 请求本身的事，仍在 start_run 里判。

**口径查询工具是服务端补上的（K14）。** 使用者的图里不用自己画一个「按口径取数」：准备发起时在这里替它绑——有授权的
口径至少有一份时，把 `caliber_query`（只读、按资产授权收窄）加进这张图里各个 Agent 的 `tools`；一份也没有就原样不动，
模型不会拿到一个一调就被拒的工具。模型因此在工具清单里看得到它（chat_policy 的白名单里有它、copilot 的工具清单按白名单
列），能自己从 look 给出的**有授权的**口径里挑一张调用（look/calibers 只列这些）。绑上不等于放开数据：真正执行时
QueryCore 统一按资产授权核（querycore 的 `_require_caliber`，没有授权当场 403 `querycore_not_authorized`）。
"""
from __future__ import annotations

import copy
from typing import Any


class ChatRunRefused(Exception):
    """这张图使用者在问数据里不能运行：str(e) 就是给人看的原因（HTTP 发起时是 403 的 detail）。"""


async def user_chat_run(session: Any, graph: Any, actor: str | None) -> tuple[str, dict[str, Any], dict[str, Any]]:
    """开登录后 user 角色发起问数据运行：交回 (记忆范围, 运行选项, 这次要运行的图)。图里有白名单之外的节点、工具抛 ChatRunRefused。

    记忆固定为这位使用者自己的范围、知识库取设置里的默认值（记忆、知识库的内容不经遮罩，见 auth/chat_policy 开头）；查询结果
    在工具边界就遮（事件、回答里只有遮过的值——它读得到自己这次运行的事件，api/deps.py 的 _run_scope）；数据源查询一律只读，
    写语句不交给审批。交回的图是 `bind_caliber_tool` 补过口径查询工具的那一份（调用方拿它发起，见模块说明）。`session` 只用来
    读这个人有授权的口径（apps/access.usable_calibers），发起本身仍由调用方负责。"""
    from app.auth.chat_policy import actor_name, chat_graph_problem, chat_memory_scope
    from app.tools.datasource import MASK_RESULTS_OPTION, READ_ONLY_OPTION

    if problem := chat_graph_problem(graph):
        raise ChatRunRefused(problem)
    # 主体归一成名字：记进记忆范围、判「是不是普通使用者」都按同一个名字算（调用方给的可能是 Principal）
    name = actor_name(actor)
    bound = await bind_caliber_tool(session, graph, name)
    return chat_memory_scope(name), {MASK_RESULTS_OPTION: True, READ_ONLY_OPTION: True}, bound


async def bind_caliber_tool(session: Any, graph: Any, actor: str | None) -> dict[str, Any]:
    """普通使用者的图里补上口径查询工具，交回这次要运行的图（搭建者及以上、没有可用口径、图不成形都原样交回）。

    - 只补 **Agent 节点的 tools**：调用工具节点一次只调它写死的那个工具，补不进去（要按口径取数就把 tool 写成 caliber_query）；
    - 只在**这份授权确实有口径可用**时补（apps/access.usable_calibers）：一份也没有就原样交回，免得模型拿到一个一调就被拒
      的工具；builder / admin / 关登录不补（auth/chat_policy.restricted_actor）——它们本来就能自己在画布上绑；
    - 改的是**副本**（deepcopy）：调用方的请求体、这一轮的图都不动。"""
    from app.auth.chat_policy import restricted_actor
    from app.engine.launch import node_setting
    from app.engine.schema import GraphSpec
    from app.ledger.querycore import CALIBER_QUERY

    if not isinstance(graph, dict) or not graph.get("nodes"):
        return graph
    try:
        spec = GraphSpec.model_validate(graph)
    except ValueError:
        return graph                       # 图不成形：交给 launch_run 照常报校验问题
    if not any(str(node.type) == "agent" for node in spec.nodes):
        return graph
    if not await restricted_actor(session, actor):
        return graph
    from app.apps.access import usable_calibers
    from sqlalchemy import select
    from app.db.models import Workflow, WorkflowVersion
    from app.ledger.ontology import ACTIVE_LINK_STATUSES, workflow_ontology_index
    from app.tools.ontology import ONTOLOGY_NEIGHBORS, SOURCE_HEALTH

    usable = await usable_calibers(session, actor)
    if not usable:
        return graph

    has_ontology_edges = False
    for wf_id, ver in usable:
        snap = (
            await session.execute(
                select(WorkflowVersion.graph).where(
                    WorkflowVersion.workflow_id == wf_id,
                    WorkflowVersion.version == int(ver),
                )
            )
        ).scalar_one_or_none()
        if not isinstance(snap, dict):
            wf = await session.get(Workflow, wf_id)
            snap = wf.graph if wf is not None and isinstance(wf.graph, dict) else {}
        idx = workflow_ontology_index(workflow_id=wf_id, version=int(ver), graph=snap)
        if any(e.get("status") in ACTIVE_LINK_STATUSES for e in idx["edges"]):
            has_ontology_edges = True
            break

    nodes = copy.deepcopy(graph["nodes"])
    for node in nodes:
        if not isinstance(node, dict) or str(node.get("type")) != "agent":
            continue
        here = node.get("data")
        if not isinstance(here, dict):
            continue
        config = here.setdefault("config", {})
        if not isinstance(config, dict):
            continue
        spec_node = spec.node_map()[str(node.get("id"))]
        tools = [str(t) for t in (node_setting(spec, spec_node, "tools") or [])]
        additions = [CALIBER_QUERY]
        if has_ontology_edges or bool(node_setting(spec, spec_node, "investigation")):
            additions.extend([ONTOLOGY_NEIGHBORS, SOURCE_HEALTH])
        changed = False
        for extra_tool in additions:
            if extra_tool not in tools:
                tools.append(extra_tool)
                changed = True
        if changed:
            config["tools"] = tools
    return {**graph, "nodes": nodes}


__all__ = ["ChatRunRefused", "bind_caliber_tool", "user_chat_run"]
