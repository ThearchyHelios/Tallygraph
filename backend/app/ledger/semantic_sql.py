"""Conservative cardinality proof for newly generated scalar metric queries.

This is an authoring check, not a business-formula verifier. Runtime contracts still check
actual rows, truncation and nulls. Unsupported query shapes require an explicit rewrite.
"""
from __future__ import annotations

import re

import sqlglot
from sqlglot import exp


def scalar_problem(sql: str) -> str | None:
    body = re.sub(r"\{\{\s*input\.[A-Za-z_][\w-]*\s*\}\}", "?", sql)
    try:
        tree = sqlglot.parse_one(body)
    except sqlglot.errors.ParseError:
        return "无法证明取数为单行，请将最终查询改为无分组聚合"
    if tree.find(exp.Limit, exp.Offset) or any(c.name.upper() == "ROWNUM" for c in tree.find_all(exp.Column)):
        return "标量指标不能用 LIMIT / OFFSET / ROWNUM 截取明细来伪装单值"

    def single(query: exp.Expression, inherited: dict, visiting: frozenset[str] = frozenset()) -> bool:
        if isinstance(query, exp.Subquery):
            return single(query.this, inherited, visiting)
        if not isinstance(query, exp.Select):
            return False
        if any(query.args.get(key) for key in ("group", "having", "qualify")):
            return False
        ctes = dict(inherited)
        with_ = query.args.get("with_")
        if with_:
            ctes.update({c.alias.casefold(): c.this for c in with_.expressions})

        def scoped(node):
            # Subqueries have their own cardinality; windows are not collapsing aggregates.
            yield node
            if not isinstance(node, (exp.Subquery, exp.Select, exp.Window, exp.AggFunc)):
                for child in node.iter_expressions():
                    yield from scoped(child)

        projections = [n for projection in query.expressions for n in scoped(projection)]
        if any(isinstance(n, exp.AggFunc) for n in projections):
            return not any(isinstance(n, (exp.Column, exp.Star, exp.Window)) for n in projections)
        # A WHERE clause may remove the only row even when every source is scalar.
        if query.args.get("where"):
            return False
        sources = []
        from_ = query.args.get("from_")
        if from_:
            sources.append(from_.this)
        for join in query.args.get("joins") or []:
            # Conditional/inner joins can remove a row. CROSS of scalar sources stays scalar.
            if join.args.get("on") or join.args.get("using"):
                return False
            sources.append(join.this)
        for source in sources:
            if isinstance(source, exp.Subquery):
                if not single(source, ctes, visiting):
                    return False
            elif isinstance(source, exp.Table):
                name = source.name.casefold()
                if name == "dual" and not source.db:
                    continue
                if name not in ctes or name in visiting or not single(ctes[name], ctes, visiting | {name}):
                    return False
            else:
                return False
        return True

    if not single(tree, {}):
        return "标量指标的最终取数未收敛为单行：请先按业务规则聚合，再计算比率；不能取明细第 0 行"
    return None
