"""Clarifications must respect database scope before stealing an exploratory turn."""
import pytest

from app.api import copilot
from app.db.base import SessionLocal
from app.ledger import nlchange


async def test_legacy_clarification_ignores_other_or_disabled_sources(monkeypatch):
    async def cards(session):
        return [{'workflow_id': 'legacy', 'version': 1, 'card_id': 'card', 'workflow_name': '历史口径',
                 'caliber': '旧定义', 'card_title': '指标卡', 'metric_source_tools': {'m1': {'db_query__old_database'}},
                 'metrics': [{'id': 'm1', 'name': '遗留销售额', 'expression': "cell(nodes.fetch, 0, 'n')"}]}]
    monkeypatch.setattr(nlchange, '_auto_cards', cards)
    async with SessionLocal() as session:
        assert await nlchange.auto_route(session, question='昨天P100的遗留销售额',
                                         source_tools={'db_query__selected_database'}) is None
        assert await nlchange.auto_route(session, question='昨天P100的遗留销售额', source_tools=set()) is None
        matched = await nlchange.auto_route(session, question='昨天P100的遗留销售额',
                                            source_tools={'db_query__old_database'})
        assert matched.mode == nlchange.ROUTE_CLARIFY


async def test_missing_explicit_source_is_rejected_before_any_clarification(monkeypatch):
    from app.api.coded import CodedHTTPException

    async def should_not_route(*args, **kwargs):
        raise AssertionError('must validate source scope before routing')
    monkeypatch.setattr(nlchange, 'auto_route', should_not_route)
    with pytest.raises(CodedHTTPException) as refused:
        await copilot._auto_route_ops(copilot.GenerateIn(
            instruction='昨天P100的遗留销售额', datasource_ids=['missing-route-regression-source']))
    assert refused.value.status_code == 400
    assert refused.value.code == 'datasource_scope_empty'


async def test_multisource_graph_routes_only_selected_metric_dependencies():
    from sqlalchemy import delete
    from app.db.models import Workflow, WorkflowVersion
    from app.ledger.draftplan import build_graph
    from tests import binding_samples as B

    graph = build_graph([
        {'sql': 'SELECT 1 AS n', 'entry_fields': [], 'metrics': [
            {'id': 'a', 'name': '单源路由指标甲', 'expression': "cell(nodes.fetch, 0, 'n')"}]},
        {'sql': 'SELECT 2 AS n', 'entry_fields': [], 'metrics': [
            {'id': 'b', 'name': '单源路由指标乙', 'expression': "cell(nodes.fetch, 0, 'n')"}]},
    ], name='多源图', datasource='source_a')
    second = next(n for n in graph['nodes'] if n['id'] == 'fetch_2')
    second['data']['config']['tool'] = 'db_query__source_b'
    # Another card in the same workflow cannot pollute the target card's source scope either.
    graph['nodes'].append({'id': 'other_card', 'type': 'metrics', 'data': {'config': {'metrics': [
        {'id': 'c', 'name': '另卡路由指标丙', 'expression': "cell(nodes.fetch_2, 0, 'n')"}]}}})
    next(n for n in graph['nodes'] if n['id'] == 'card')['data']['config']['metrics'].append(
        {'id': 'constant', 'name': '常量路由指标丁', 'expression': '1'})
    workflow_id = await B.publish(graph, name='多源选择回归')
    try:
        async with SessionLocal() as session:
            first = await nlchange.auto_route(session, question='单源路由指标甲是多少',
                                              source_tools={'db_query__source_a'})
            assert first.mode == nlchange.ROUTE_EXECUTE
            assert first.request['metric_ids'] == ['a']
            assert await nlchange.auto_route(session, question='单源路由指标乙是多少',
                                              source_tools={'db_query__source_a'}) is None
            assert await nlchange.auto_route(session, question='另卡路由指标丙是多少',
                                              source_tools={'db_query__source_a'}) is None
            second = await nlchange.auto_route(session, question='单源路由指标乙是多少',
                                               source_tools={'db_query__source_b'})
            assert second.mode == nlchange.ROUTE_EXECUTE and second.request['metric_ids'] == ['b']
            clarified = await nlchange.auto_route(session, question='昨天P100的单源路由指标甲',
                                                   source_tools={'db_query__source_a'})
            assert clarified.mode == nlchange.ROUTE_CLARIFY
            assert await nlchange.auto_route(session, question='昨天P100的常量路由指标丁',
                                              source_tools={'db_query__source_a'}) is None
            global_constant = await nlchange.auto_route(session, question='昨天P100的常量路由指标丁')
            assert global_constant.mode == nlchange.ROUTE_CLARIFY
    finally:
        async with SessionLocal() as session:
            await session.execute(delete(WorkflowVersion).where(WorkflowVersion.workflow_id == workflow_id))
            await session.execute(delete(Workflow).where(Workflow.id == workflow_id))
            await session.commit()
