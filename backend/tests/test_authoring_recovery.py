"""Fault injection at graph acceptance and safe missing-definition continuation."""
import asyncio

from sqlalchemy import select

from app.api import ledger
from app.db.base import SessionLocal
from app.db.models import CaliberDraft, DataSource, Workflow
from app.ledger import authoring
from tests.test_ledger_docdraft_api import client, datasource, documents, extract, fake_model, fake_trial, plan_reply
from tests.test_definition_batches import imported, reply


async def test_graph_acceptance_is_atomic_and_resume_does_not_duplicate(
        client, documents, datasource, fake_trial, monkeypatch):
    doc = await documents('atomic-acceptance')
    source = await datasource()
    model = fake_model(monkeypatch, plan_reply(f'{doc}-2'))
    capture = authoring._capture_field_baseline

    async def interrupted(**kwargs):
        async with SessionLocal() as session:
            saved = await session.get(CaliberDraft, kwargs['draft'].id)
            assert saved.workflow_id == kwargs['workflow_id']
            assert saved.payload['finalization_pending']
        raise asyncio.CancelledError

    monkeypatch.setattr(authoring, '_capture_field_baseline', interrupted)
    made = await extract(client, doc, source)
    draft_id = made['draft_id']
    failed = (await client.get(f'/api/ledger/drafts/{draft_id}')).json()
    assert failed['status'] == 'failed' and failed['job']['retryable']
    workflow_id = failed['workflow_id']
    monkeypatch.setattr(authoring, '_capture_field_baseline', capture)
    resumed = await client.post(f'/api/ledger/drafts/{draft_id}/retry')
    assert resumed.status_code == 200, resumed.text
    ready = (await client.get(f'/api/ledger/drafts/{draft_id}')).json()
    assert ready['workflow_id'] == workflow_id and ready['status'] == 'ready'
    assert not ready['finalization_pending'] and len(model.calls) == 1
    async with SessionLocal() as session:
        workflows = (await session.execute(select(Workflow))).scalars().all()
        assert len([w for w in workflows if (w.origin or {}).get('draft_id') == draft_id]) == 1


async def test_disabled_source_rejected_in_old_new_retry_and_worker(
        client, documents, datasource, monkeypatch):
    draft_id, source, chunk, items = await imported(client, datasource, monkeypatch, count=1)
    async with SessionLocal() as session:
        row = await session.get(DataSource, source)
        row.enabled = False
        await session.commit()
    assert (await client.post(f'/api/ledger/drafts/{draft_id}/implement',
                              json={'datasource_id': source})).status_code == 422
    doc = await documents('disabled-source')
    assert (await client.post('/api/ledger/drafts/extract',
                              json={'documents': [doc], 'datasource_id': source})).status_code == 422
    async with SessionLocal() as session:
        row = await session.get(CaliberDraft, draft_id)
        row.status, row.datasource_id = 'drafting', source
        await session.commit()
    await ledger._extract_in_background(draft_id)
    result = (await client.get(f'/api/ledger/drafts/{draft_id}')).json()
    assert result['status'] == 'failed' and '停用' in result['error']
    assert (await client.post(f'/api/ledger/drafts/{draft_id}/retry')).status_code == 409


async def test_continue_is_idempotent_missing_only_and_preserves_original(
        client, datasource, fake_trial, monkeypatch):
    draft_id, source, chunk, items = await imported(client, datasource, monkeypatch, count=2)
    await client.post(f'/api/ledger/drafts/{draft_id}/implement', json={'datasource_id': source})
    fake_model(monkeypatch, reply(items[:1], chunk), '{"groups": [], "open_questions": ["缺少字段"]}')
    await ledger._extract_in_background(draft_id)
    before = (await client.get(f'/api/ledger/drafts/{draft_id}')).json()
    assert before['metric_count'] == 1 and before['status'] == 'questions_open'
    async with SessionLocal() as session:
        workflow = await session.get(Workflow, before['workflow_id'])
        original_graph, version = workflow.graph, workflow.version
    body = {'expected_revision': version, 'guidance': '补充：计数使用 orders'}
    result = await client.post(f'/api/ledger/drafts/{draft_id}/continue', json=body)
    assert result.status_code == 202, result.text
    candidate = result.json()['draft_id']
    assert candidate != draft_id and result.json()['merge_required']
    assert (await client.post(f'/api/ledger/drafts/{draft_id}/continue', json=body)).json()['draft_id'] == candidate
    fake_model(monkeypatch, reply(items[1:], chunk))
    await ledger._extract_in_background(candidate)
    ready = (await client.get(f'/api/ledger/drafts/{candidate}')).json()
    assert ready['metric_count'] == 1
    assert ready['metrics'][0]['source_definition_id'] == items[1]['id']
    async with SessionLocal() as session:
        workflow = await session.get(Workflow, before['workflow_id'])
        assert workflow.graph == original_graph and workflow.version == version
    assert (await client.post(f'/api/ledger/drafts/{draft_id}/continue',
                              json={**body, 'expected_revision': version + 1})).status_code == 409

    merged = await client.post(f'/api/ledger/drafts/{candidate}/merge', json={
        'expected_revision': version, 'candidate_revision': ready['version']})
    assert merged.status_code == 200, merged.text
    combined = merged.json()
    assert combined['metric_count'] == 2 and combined['workflow_id'] == before['workflow_id']
    assert combined['version'] == version + 1
    async with SessionLocal() as session:
        workflow = await session.get(Workflow, before['workflow_id'])
        old_card = next(n for n in original_graph['nodes'] if n['type'] == 'metrics')
        new_card = next(n for n in workflow.graph['nodes'] if n['type'] == 'metrics')
        assert new_card['data']['config']['metrics'][0] == old_card['data']['config']['metrics'][0]
        assert len({m['id'] for m in new_card['data']['config']['metrics']}) == 2
    replay = await client.post(f'/api/ledger/drafts/{candidate}/merge', json={
        'expected_revision': version, 'candidate_revision': ready['version']})
    assert replay.status_code == 200 and replay.json()['replayed']
    assert replay.json()['version'] == combined['version']


async def test_all_blocked_can_continue_and_merge_without_sharing_candidate_graph(
        client, datasource, fake_trial, monkeypatch):
    draft_id, source, chunk, items = await imported(client, datasource, monkeypatch, count=1)
    await client.post(f'/api/ledger/drafts/{draft_id}/implement', json={'datasource_id': source})
    empty = '{"groups": [], "open_questions": ["缺少字段"]}'
    fake_model(monkeypatch, empty, empty)
    await ledger._extract_in_background(draft_id)
    before = (await client.get(f'/api/ledger/drafts/{draft_id}')).json()
    assert before['status'] == 'questions_open' and not before['workflow_id']
    result = await client.post(f'/api/ledger/drafts/{draft_id}/continue', json={
        'expected_revision': 0, 'guidance': '已补齐字段'})
    assert result.status_code == 202, result.text
    candidate = result.json()['draft_id']
    fake_model(monkeypatch, reply(items, chunk))
    await ledger._extract_in_background(candidate)
    ready = (await client.get(f'/api/ledger/drafts/{candidate}')).json()
    conflict = await client.post(f'/api/ledger/drafts/{candidate}/merge', json={
        'expected_revision': 0, 'candidate_revision': 99})
    assert conflict.status_code == 409
    merged = await client.post(f'/api/ledger/drafts/{candidate}/merge', json={
        'expected_revision': 0, 'candidate_revision': ready['version']})
    assert merged.status_code == 200, merged.text
    assert merged.json()['metric_count'] == 1
    assert merged.json()['workflow_id'] != ready['workflow_id']


def test_merge_keeps_human_edits_and_rejects_conflicting_parameters():
    import copy
    import pytest
    from app.ledger.continuation import append_candidate
    from app.ledger.draftplan import build_graph

    def graph(key):
        return build_graph([{'sql': 'SELECT 1 AS n', 'entry_fields': [
            {'name': 'date', 'type': 'string', 'default': 'yesterday'}], 'metrics': [
                {'id': 'm1', 'name': key, 'source_definition_id': key, 'expression': "cell(nodes.fetch, 0, 'n')"}]}], name=key)
    original, candidate = graph('old'), graph('new')
    old_card = next(n for n in original['nodes'] if n['type'] == 'metrics')
    old_card['data']['config']['metrics'][0]['expression'] = "cell(nodes.fetch, 0, 'n') * 100"
    original_copy = copy.deepcopy(original)
    merged = append_candidate(original, candidate, prefix='candidate', allowed_definitions={'new'})
    merged_card = next(n for n in merged['nodes'] if n['type'] == 'metrics')
    assert original == original_copy
    assert merged_card['data']['config']['metrics'][0] == old_card['data']['config']['metrics'][0]
    assert 'nodes.candidate_fetch' in merged_card['data']['config']['metrics'][1]['expression']
    next(n for n in candidate['nodes'] if n['type'] == 'input')['data']['config']['fields'][0]['default'] = 'today'
    with pytest.raises(ValueError, match='参数'):
        append_candidate(original, candidate, prefix='candidate', allowed_definitions={'new'})


async def test_continue_supports_subset_datasource_switch_multi_metric_split_and_field_baseline(
        client, datasource, fake_trial, monkeypatch):
    import json
    import uuid

    draft_id, source1, chunk, items = await imported(client, datasource, monkeypatch, count=3)
    source2 = f'ds-{uuid.uuid4().hex[:10]}'
    async with SessionLocal() as session:
        session.add(DataSource(
            id=source2, name=f'mes_{uuid.uuid4().hex[:6]}', kind='sqlite',
            description='制造库', schema_cache={'tables': {'orders': {'columns': [
                {'name': 'amount', 'type': 'NUMERIC'}]}}, 'total': 1},
        ))
        await session.commit()

    await client.post(f'/api/ledger/drafts/{draft_id}/implement', json={'datasource_id': source1})
    empty = '{"groups": [], "open_questions": ["请确认取数表名"]}'
    fake_model(monkeypatch, empty)
    await ledger._extract_in_background(draft_id)
    initial = (await client.get(f'/api/ledger/drafts/{draft_id}')).json()
    assert initial['status'] == 'questions_open' and not initial['workflow_id']

    bad_subset = await client.post(f'/api/ledger/drafts/{draft_id}/continue', json={
        'expected_revision': 0, 'guidance': '先验证第一项', 'selected': ['unknown-def-id'],
    })
    assert bad_subset.status_code == 422

    cont1 = await client.post(f'/api/ledger/drafts/{draft_id}/continue', json={
        'expected_revision': 0, 'guidance': '切换到制造库并先处理第一项',
        'selected': [items[0]['id']], 'datasource_id': source2,
    })
    assert cont1.status_code == 202, cont1.text
    cand1 = cont1.json()['draft_id']

    split_reply = json.dumps({
        'sql': 'SELECT COUNT(*) AS a, SUM(amount) AS b FROM orders',
        'entry_fields': [],
        'metrics': [
            {
                'id': 'm1', 'name': f"{items[0]['name']}（计划口径）", 'source_definition_id': items[0]['id'],
                'expression': "cell(nodes.fetch, 0, 'a')",
                'definition': {'text': '计划口径', 'citations': [{'chunk': chunk, 'quote': '按日汇总计数，不取明细第一行'}]},
            },
            {
                'id': 'm2', 'name': f"{items[0]['name']}（产线口径）", 'source_definition_id': items[0]['id'],
                'expression': "cell(nodes.fetch, 0, 'b')",
                'definition': {'text': '产线口径', 'citations': [{'chunk': chunk, 'quote': '按日汇总计数，不取明细第一行'}]},
            },
        ],
        'uncovered': [], 'open_questions': [],
    }, ensure_ascii=False)
    fake_model(monkeypatch, split_reply)
    await ledger._extract_in_background(cand1)
    cand1_view = (await client.get(f'/api/ledger/drafts/{cand1}')).json()
    assert cand1_view['metric_count'] == 2

    merged1 = await client.post(f'/api/ledger/drafts/{cand1}/merge', json={
        'expected_revision': 0, 'candidate_revision': cand1_view['version'],
    })
    assert merged1.status_code == 200, merged1.text
    after1 = merged1.json()
    assert after1['datasource_id'] == source2
    assert after1['metric_count'] == 2
    assert after1['implementation_progress']['completed'] == 1
    assert after1['implementation_progress']['blocked'] == 2
    assert after1['field_baseline']['status'] == 'known'
    assert after1['field_baseline']['coverage']['status'] == 'complete'

    # Switching datasource is rejected once the draft owns a workflow graph.
    switch_rejected = await client.post(f'/api/ledger/drafts/{draft_id}/continue', json={
        'expected_revision': after1['version'], 'guidance': '尝试再换库', 'datasource_id': source1,
    })
    assert switch_rejected.status_code == 409

    # Second subset continuation advances field_baseline cleanly onto existing workflow.
    cont2 = await client.post(f'/api/ledger/drafts/{draft_id}/continue', json={
        'expected_revision': after1['version'], 'guidance': '继续处理第二项', 'selected': [items[1]['id']],
    })
    assert cont2.status_code == 202, cont2.text
    cand2 = cont2.json()['draft_id']
    fake_model(monkeypatch, reply([items[1]], chunk))
    await ledger._extract_in_background(cand2)
    cand2_view = (await client.get(f'/api/ledger/drafts/{cand2}')).json()
    merged2 = await client.post(f'/api/ledger/drafts/{cand2}/merge', json={
        'expected_revision': after1['version'], 'candidate_revision': cand2_view['version'],
    })
    assert merged2.status_code == 200, merged2.text
    after2 = merged2.json()
    assert after2['metric_count'] == 3
    assert after2['version'] == after1['version'] + 1
    assert after2['implementation_progress']['completed'] == 2
    assert after2['field_baseline']['status'] == 'known'
    assert after2['field_baseline']['coverage']['status'] == 'complete'
    assert len(after2['field_baseline']['metric_ids']) == 3
