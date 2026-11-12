"""发布预检的应用服务；编辑页、发布接口和发布单使用同一组检查。"""
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.engine.schema import GraphSpec


async def gate_issues(
    session: AsyncSession, graph: Any, level: str, *, workflow_id: str | None = None,
) -> list[dict[str, Any]] | None:
    from app.data.catalog_impact import open_structure_issues
    from app.data.sqlcheck import graph_sources, load_checkers_by_name
    from app.engine.governance import publish_issues
    from app.engine.lint_process import load_subgraphs, with_subgraphs
    from app.engine.provider_refs import load_providers
    from app.ledger.ontology import ontology_publish_issues

    try:
        spec = GraphSpec.model_validate(graph)
    except Exception:
        return None
    checkers = await load_checkers_by_name(session, graph_sources(spec.nodes))
    providers = await load_providers(session, spec)
    with with_subgraphs(await load_subgraphs(session, spec)):
        issues = [i.model_dump() for i in publish_issues(spec, level=level, checkers=checkers, providers=providers)]
    ontology_issues = await ontology_publish_issues(session, spec, workflow_id=str(workflow_id or ""))
    return issues + ontology_issues + await open_structure_issues(session, spec)
