"""K04 的合成订单夹具：A/B/C/D 四单、一条受控模板 SQL、一张两个指标共享这条 SQL 的口径卡。

任务书 6.2 的正向样例（数据全是编的，和项目生产数据无关）：

| 订单 | 下单日期 | 支付日期 | 退款状态 | 金额 |
| --- | --- | --- | --- | --- |
| A | 2026-09-30 | 2026-10-02 | 未退款 | 100 |
| B | 2026-10-05 | 2026-11-01 | 未退款 | 200 |
| C | 2026-10-10 | 2026-10-10 | 全额退款 | 300 |
| D | 2026-10-20 | 2026-10-21 | 部分退款 | 400 |

**旧定义**（按下单时间统计、不排除退款）十月 3 单 900 元：B、C、D。
**新定义**（按支付时间统计、排除全额退款、保留部分退款）十月 2 单 500 元：A 纳入、B 跨期排除、C 排除、D 保留。
两个指标（订单数、销售额）**共享同一条取数 SQL**：一条 SQL 上只能有一个时间口径，所以改定义必须两个指标的说法
一致，才谈得上把 SQL 一起改过来。

取数是一个受控模板：`... WHERE ([[time_role]]) AND ([[refund_rule]])`，两个 token 展开成什么写在取数节点自己的
`data.config.binding` 里（配置级、带版本，不是全局映射表）。夹具只负责把这些行、这份模板和这张卡摆出来，
判定和展开都在 app/ledger/bindings.py。
"""
from __future__ import annotations

import copy
import sqlite3
import uuid
from collections.abc import Mapping
from typing import Any

from app.core.artifact_store import graph_hash
from app.db.base import SessionLocal
from app.db.models import CaliberDraft, DataSource, Workflow, WorkflowVersion
from app.ledger import bindings

#: 四单：id、下单日期、支付日期、退款状态、金额
ROWS = [("A", "2026-09-30", "2026-10-02", "none", 100.0),
        ("B", "2026-10-05", "2026-11-01", "none", 200.0),
        ("C", "2026-10-10", "2026-10-10", "full", 300.0),
        ("D", "2026-10-20", "2026-10-21", "partial", 400.0)]

#: 十月（半开区间）和九月各一个窗口：换窗口是请求变化，不建定义版本
OCTOBER = {"start": "2026-10-01", "end": "2026-11-01"}
SEPTEMBER = {"start": "2026-09-01", "end": "2026-10-01"}

#: 受控模板：两个业务 token 占位，别的都是常量；{{ input.* }} 仍归 K01，执行期照旧按入口渲染
TEMPLATE = ("SELECT COUNT(*) AS orders, SUM(o.amount) AS amount FROM orders o "
            "WHERE ([[time_role]]) AND ([[refund_rule]])")
SNIPPETS: dict[str, dict[str, str]] = {
    "time_role": {"order_time": "o.ordered_at >= '{{ input.start }}' AND o.ordered_at < '{{ input.end }}'",
                  "payment_time": "o.paid_at >= '{{ input.start }}' AND o.paid_at < '{{ input.end }}'"},
    "refund_rule": {"include_all": "1 = 1", "exclude_full_refund": "o.refund_state <> 'full'"},
}
OLD = {"time_role": "order_time", "refund_rule": "include_all"}
NEW = {"time_role": "payment_time", "refund_rule": "exclude_full_refund"}

#: 独立预期（不来自任何一次执行）：十月，旧定义 3 单 900 元，新定义 2 单 500 元
EXPECTED = {"old": {"orders": 3, "amount": 900.0}, "new": {"orders": 2, "amount": 500.0}}

#: 复杂 SQL 反例：CTE + 窗口函数算出来的东西，时间条件写死在里头——受控模板认不出落点，
#: 不是「字符串替换一下就行」的那种（改它要人先拿主意）
COMPLEX_SQL = ("WITH paid AS (SELECT id, ordered_at, paid_at, refund_state, amount, "
               "ROW_NUMBER() OVER (PARTITION BY id ORDER BY paid_at DESC) AS rn "
               "FROM orders WHERE paid_at IS NOT NULL) "
               "SELECT COUNT(*) AS orders, SUM(amount) AS amount FROM paid "
               "WHERE rn = 1 AND paid_at >= '2026-10-01' AND paid_at < '2026-11-01' "
               "AND ([[refund_rule]])")

#: 这个夹具里数据源的名字（fresh_source 换）：图里的查询工具是 db_query__<它>
CURRENT = {"source": "orders"}
#: fresh_source 起过的数据源名：用例结束由测试的清理 fixture 删掉，不给别的用例留行
MADE: dict[str, list[str]] = {"sources": []}

__all__ = ["COMPLEX_SQL", "CURRENT", "EXPECTED", "NEW", "OCTOBER", "OLD", "ROWS", "SEPTEMBER", "SNIPPETS",
           "TEMPLATE", "binding", "build_order_db", "card_metrics", "chained_graph", "complex_graph", "draft",
           "fresh_source", "graph", "publish", "render", "tool", "upstream"]


def tool() -> str:
    return f"db_query__{CURRENT['source']}"


def render(values: dict[str, str] = OLD) -> str:
    """这份模板按业务值展开出来的 SQL（夹具里用同一个函数，和服务端判定的是同一份写法）。"""
    return bindings.render(TEMPLATE, SNIPPETS, values)


def build_order_db(path) -> None:
    db = sqlite3.connect(path)
    db.executescript("CREATE TABLE orders (id TEXT PRIMARY KEY, ordered_at TEXT, paid_at TEXT, "
                     "refund_state TEXT, amount REAL);")
    db.executemany("INSERT INTO orders VALUES (?, ?, ?, ?, ?)", ROWS)
    db.commit()
    db.close()


async def fresh_source(path) -> str:
    """建库、起一个这个用例专用的数据源（名字不重复），返回数据源名。"""
    build_order_db(path)
    CURRENT["source"] = f"orders_{uuid.uuid4().hex[:8]}"
    async with SessionLocal() as session:
        session.add(DataSource(name=CURRENT["source"], kind="sqlite", database=str(path), readonly=True,
                               enabled=True))
        await session.commit()
    MADE["sources"].append(CURRENT["source"])
    return CURRENT["source"]


def binding(values: dict[str, str] = OLD, *, domain: str = "order_sales", mapping_version: int = 1,
            template: str = TEMPLATE, snippets: dict[str, dict[str, str]] | None = None,
            sql: str | None = None, version: int = bindings.BINDER_VERSION,
            binder: str = bindings.BINDER) -> dict[str, Any]:
    """取数节点上的受控绑定（形状见 ledger/bindings）。"""
    snippets = SNIPPETS if snippets is None else snippets
    rendered = sql if sql is not None else bindings.render(template, snippets, values)
    return {"binder": binder, "version": version, "domain": domain, "mapping_version": mapping_version,
            "template": template, "snippets": copy.deepcopy(snippets), "values": copy.deepcopy(values),
            "rendered_hash": bindings.sql_hash(rendered)}


def node(nid: str, ntype: str, **config: Any) -> dict[str, Any]:
    return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0}, "data": {"label": nid, "config": config}}


def chain(*nodes: dict[str, Any]) -> dict[str, Any]:
    return {"nodes": list(nodes), "edges": [{"source": a["id"], "target": b["id"]} for a, b in zip(nodes, nodes[1:])]}


def card_metrics(time_role: str | None = OLD["time_role"], refund_rule: str | None = OLD["refund_rule"],
                 **_extra: Any) -> list[dict[str, Any]]:
    """两个共享同一条 SQL 的指标：订单数、销售额。业务字段按参数写进去（None 表示不声明，走旧资产那条路）。"""
    out = []
    for mid, name, column in (("m1", "订单数", "orders"), ("m2", "销售额", "amount")):
        metric: dict[str, Any] = {"id": mid, "name": name, "unit": "单" if mid == "m1" else "元",
                                  "decimals": 0, "expression": f"cell(nodes.fetch, 0, '{column}')",
                                  "definition": {"text": f"{name}按业务口径统计"}}
        if time_role:
            metric["time_role"] = time_role
        if refund_rule:
            metric["refund_rule"] = refund_rule
        out.append(metric)
    return out


def graph(*, values: dict[str, str] = OLD, sql: str | None = None, metrics: list[dict[str, Any]] | None = None,
          with_binding: bool = True, fields: list[dict[str, Any]] | None = None,
          node_config: dict[str, Any] | None = None, source: str | None = None) -> dict[str, Any]:
    """这张卡：入口 → 取数（带受控绑定）→ 口径卡 → 出口。同样的参数永远得到同一张图。

    不给 metrics 时，指标声明的时间口径和退款规则就按 values 写——一份**自洽**的定义：定义说支付时间，SQL 也就是
    支付时间那份展开（v1 和 v2 各自自洽）。要构造「定义和实现对不上」的图，显式传 metrics。
    """
    sql = render(values) if sql is None else sql
    metrics = (card_metrics(time_role=values.get("time_role"), refund_rule=values.get("refund_rule"))
               if metrics is None else copy.deepcopy(metrics))
    fields = fields if fields is not None else [
        {"name": "start", "type": "string", "required": True, "description": "开始日期（含）"},
        {"name": "end", "type": "string", "required": True, "description": "结束日期（不含）"}]
    config: dict[str, Any] = {"tool": f"db_query__{source or CURRENT['source']}", "args": {"sql": sql},
                              "hints": {"keep": "me"}}
    if with_binding:
        config[bindings.KEY] = binding(values, sql=sql)
    config.update(copy.deepcopy(node_config or {}))
    return chain(node("start", "input", fields=fields),
                 node("fetch", "tool", **config),
                 node("card", "metrics", caliber="订单口径", caliber_version="v1", metrics=metrics),
                 node("out", "output", fields=[{"name": "结果", "value": "{{ nodes.card.text }}"}]),
                 )


def complex_graph(*, values: dict[str, str] | None = None, metrics: list[dict[str, Any]] | None = None) -> dict:
    """复杂 SQL 反例：时间条件写死在 CTE 里，绑定里只有 [[refund_rule]] 的落点。

    指标照样声明 time_role=payment_time——这正是「定义改了，但这条 SQL 表示不了它」的那种改动。
    """
    values = {"refund_rule": (values or OLD)["refund_rule"]}
    sql = bindings.render(COMPLEX_SQL, SNIPPETS, values)
    made = graph(values={**values, "time_role": "payment_time"}, sql=sql, metrics=metrics)
    config = made["nodes"][1]["data"]["config"]
    config[bindings.KEY] = binding(values, template=COMPLEX_SQL, snippets={"refund_rule": SNIPPETS["refund_rule"]},
                                   sql=sql)
    return made


async def publish(graph: dict[str, Any], *, name: str = "K04 订单口径", level: str = "published") -> str:
    """建一个已发布的（未退役）工作流 v1：口径描述接口只认已发布的那一版。返回工作流 id。"""
    workflow_id = f"wf-{uuid.uuid4().hex[:12]}"
    async with SessionLocal() as session:
        session.add(Workflow(id=workflow_id, name=name, status="published", graph=copy.deepcopy(graph),
                             version=1, published_version=1))
        session.add(WorkflowVersion(workflow_id=workflow_id, version=1, graph=copy.deepcopy(graph),
                                    graph_hash=graph_hash(graph), level=level))
        await session.commit()
    return workflow_id


async def upstream(*, name: str | None = None) -> dict[str, Any]:
    """一个上游工作流：v1 按下单时间、不排除退款（旧），v2 按支付时间 + 排除全额退款（新，定义和 SQL 已同步）。

    消费侧固定 v2 时，独立预期就是新定义的 2 单 500 元；本地那句取数要是还停在旧写法，跑出来的 3 单 900 元就是在
    拿旧数冒充新定义。交回 {workflow_id, name, v1, v2, card}。
    """
    workflow_id = f"wf-{uuid.uuid4().hex[:12]}"
    name = name or f"K04 上游订单口径 {uuid.uuid4().hex[:4]}"
    old, new = graph(values=OLD), graph(values=NEW)
    async with SessionLocal() as session:
        session.add(Workflow(id=workflow_id, name=name, status="published", graph=copy.deepcopy(new),
                             version=2, published_version=2))
        for number, made in ((1, old), (2, new)):
            session.add(WorkflowVersion(workflow_id=workflow_id, version=number, graph=copy.deepcopy(made),
                                        graph_hash=graph_hash(made), level="published"))
        await session.commit()
    return {"workflow_id": workflow_id, "name": name, "v1": old, "v2": new, "card": "card"}


def chained_graph(pinned: Mapping[str, Any], *, version: int, local_values: dict[str, str] = OLD,
                  local_sql: str | None = None, rename: Mapping[str, str] | None = None) -> dict[str, Any]:
    """消费侧的图：取数节点是本地的（可以还停在上游的旧写法上），口径卡 caliber_from 固定到上游的某一版。

    rename 是 caliber_from.nodes 的源→本地映射（{上游节点 id: 本地节点 id}）：本地取数节点的 id 换成映射里的那个
    名字，式子照上游写（nodes.fetch），判定和运行时都按映射落到本地节点上。
    """
    local_id = next(iter(dict(rename or {}).values()), "fetch")
    sql = local_sql if local_sql is not None else render(local_values)
    source: dict[str, Any] = {"workflow_id": pinned["workflow_id"], "workflow_version": version,
                              "node_id": pinned["card"]}
    if rename:
        source["nodes"] = dict(rename)
    return chain(node("start", "input", fields=[
                     {"name": "start", "type": "string", "required": True, "description": "开始日期（含）"},
                     {"name": "end", "type": "string", "required": True, "description": "结束日期（不含）"}]),
                 node(local_id, "tool", tool=tool(), args={"sql": sql},
                      **{bindings.KEY: binding(local_values, sql=sql)}),
                 node("card", "metrics", caliber_from=source),
                 node("out", "output", fields=[{"name": "结果", "value": "{{ nodes.card.text }}"}]))


async def draft(graph: dict[str, Any], *, name: str = "K04 订单口径", payload: dict[str, Any] | None = None,
                tags: list[str] | None = None) -> dict[str, Any]:
    """一份已接管到工作流上的草稿（docattach 那种产物：tags 有 doc_draft、origin 里没有 kind）。

    交回 {id, workflow_id, revision, card_id}：写入（PATCH operations）和试跑都用它。tags 传别的就是「不是已接管
    的资产」，用来看旧路径。
    """
    workflow_id = f"wf-{uuid.uuid4().hex[:12]}"
    draft_id = uuid.uuid4().hex[:32]
    async with SessionLocal() as session:
        session.add(Workflow(id=workflow_id, name=name, status="draft", graph=copy.deepcopy(graph), version=1,
                             tags=["doc_draft"] if tags is None else list(tags),
                             origin={"run_id": "run-1", "freeze": True, "sources": [{"run_id": "run-1"}]}))
        session.add(WorkflowVersion(workflow_id=workflow_id, version=1, graph=copy.deepcopy(graph),
                                    graph_hash=graph_hash(graph)))
        await session.flush()
        session.add(CaliberDraft(id=draft_id, title=name, status="ready", workflow_id=workflow_id,
                                 payload={"version": 1, "workflow_id": workflow_id,
                                          "metrics": copy.deepcopy(graph["nodes"][2]["data"]["config"]["metrics"]),
                                          **(payload or {})}))
        await session.commit()
    return {"id": draft_id, "workflow_id": workflow_id, "revision": 1, "card_id": "card"}
