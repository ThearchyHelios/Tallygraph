"""Default-all implementation, bounded internal batches and durable resume without user batching."""
import json

import pytest

from app.api import ledger
from app.db.base import SessionLocal
from app.db.models import Chunk, Document
from tests.test_ledger_docdraft_api import client, datasource, fake_model, fake_trial  # noqa: F401


async def imported(client, datasource, monkeypatch, count=31, conflicts=False):
    source = await datasource()
    text = '# 合成计数定义\n| 指标 | 公式 | 口径/说明 |\n|---|---|---|\n'
    text += ''.join(f'| 计数指标{i} | COUNT(*) | 按日汇总计数，不取明细第一行 |\n' for i in range(count))
    if conflicts:
        text += '| 无来源指标 | 无数据源 | 不推算 |\n| 同名指标 | COUNT(*) | 范围甲 |\n| 同名指标 | SUM(x) | 范围乙 |\n'
    async with SessionLocal() as session:
        doc = Document(title='自动处理定义', content=text, status='ready', chunk_count=1)
        session.add(doc)
        await session.flush()
        chunk = Chunk(document_id=doc.id, ordinal=1, content=text)
        session.add(chunk)
        await session.commit()
        doc_id, chunk_id = doc.id, chunk.id
    response = await client.post('/api/ledger/drafts/definitions', json={'documents': [doc_id]})
    assert response.status_code == 201, response.text
    draft_id = response.json()['draft_id']
    view = (await client.get(f'/api/ledger/drafts/{draft_id}')).json()

    async def enqueue(_):
        pass
    monkeypatch.setattr(ledger.draft_jobs, 'start', enqueue)
    return draft_id, source, chunk_id, view['semantic_inventory']['items']


def reply(items, chunk):
    return json.dumps({'sql': 'SELECT COUNT(*) AS count FROM orders', 'entry_fields': [],
        'metrics': [{'id': f'm{index}', 'name': item['name'], 'source_definition_id': item['id'],
                     'expression': "cell(nodes.fetch, 0, 'count')",
                     'definition': {'text': item['name'], 'citations': [{'chunk': chunk, 'quote': '按日汇总计数，不取明细第一行'}]}}
                    for index, item in enumerate(items)], 'uncovered': [], 'open_questions': []}, ensure_ascii=False)


@pytest.mark.parametrize('count', [31, 67])
async def test_all_definitions_run_past_four_rounds_without_manual_batches(client, datasource, monkeypatch, fake_trial, count):
    draft_id, source, chunk, items = await imported(client, datasource, monkeypatch, count=count, conflicts=True)
    response = await client.post(f'/api/ledger/drafts/{draft_id}/implement', json={'datasource_id': source})
    assert response.status_code == 202, response.text
    eligible = items[:count]
    model = fake_model(monkeypatch, *[reply(eligible[i:i+6], chunk) for i in range(0, count, 6)])
    await ledger._extract_in_background(draft_id)
    made = (await client.get(f'/api/ledger/drafts/{draft_id}')).json()
    assert made['metric_count'] == count, made
    assert len(model.calls) == (count + 5) // 6 and made['progress']['round'] == len(model.calls)
    assert made['implementation_mode'] == 'all' and len(made['definition_issues']) == 3
    assert made['implementation_progress'] == {'total': count, 'completed': count, 'pending': 0, 'blocked': 0, 'active_names': []}
    assert made['status'] == 'questions_open' and not made['uncovered']
    assert [p['definition_ids'] for p in made['prompts'] if p['purpose'] == 'plan'] == [
        [i['id'] for i in eligible[n:n+6]] for n in range(0, count, 6)]


async def test_partial_selection_is_optional_and_can_exceed_six(client, datasource, monkeypatch, fake_trial):
    draft_id, source, chunk, items = await imported(client, datasource, monkeypatch, count=15)
    chosen = items[2:15]
    response = await client.post(f'/api/ledger/drafts/{draft_id}/implement', json={
        'datasource_id': source, 'selected': [i['id'] for i in chosen]})
    assert response.status_code == 202, response.text
    model = fake_model(monkeypatch, *[reply(chosen[n:n+6], chunk) for n in range(0, 13, 6)])
    await ledger._extract_in_background(draft_id)
    made = (await client.get(f'/api/ledger/drafts/{draft_id}')).json()
    assert made['status'] == 'ready' and made['metric_count'] == 13 and len(model.calls) == 3
    assert {m['source_definition_id'] for m in made['metrics']} == {i['id'] for i in chosen}


async def test_unresolved_batch_does_not_stop_other_definitions(client, datasource, monkeypatch, fake_trial):
    draft_id, source, chunk, items = await imported(client, datasource, monkeypatch, count=13)
    await client.post(f'/api/ledger/drafts/{draft_id}/implement', json={'datasource_id': source})
    empty = json.dumps({'groups': [], 'open_questions': ['首组缺少字段映射'], 'uncovered': []})
    model = fake_model(monkeypatch, empty, reply(items[6:12], chunk),
                       reply(items[12:] + items[:5], chunk), empty)
    await ledger._extract_in_background(draft_id)
    made = (await client.get(f'/api/ledger/drafts/{draft_id}')).json()
    assert made['status'] == 'questions_open' and len(model.calls) == 4
    assert made['metric_count'] == 12 and made['uncovered'] == [items[5]['name']]
    assert made['implementation_progress']['blocked'] == 1 and made['implementation_progress']['pending'] == 0
    assert any(items[5]['name'] in q for q in made['open_questions'])


async def test_failed_job_resumes_only_unfinished_definitions(client, datasource, monkeypatch, fake_trial):
    from app.ledger import draftplan, docdraft

    draft_id, source, chunk, items = await imported(client, datasource, monkeypatch, count=13)
    await client.post(f'/api/ledger/drafts/{draft_id}/implement', json={'datasource_id': source})
    fake_model(monkeypatch, reply(items[:6], chunk))
    original = draftplan.plan_groups
    calls = 0

    async def fail_second(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise docdraft.DraftUnavailable('模型连接中断')
        return await original(*args, **kwargs)

    monkeypatch.setattr(draftplan, 'plan_groups', fail_second)
    await ledger._extract_in_background(draft_id)
    failed = (await client.get(f'/api/ledger/drafts/{draft_id}')).json()
    assert failed['status'] == 'failed' and failed['metric_count'] == 6
    async with SessionLocal() as session:
        await ledger.draft_jobs.finish(session, draft_id, 'failed', '模型连接中断')
    response = await client.post(f'/api/ledger/drafts/{draft_id}/retry')
    assert response.status_code == 200, response.text
    monkeypatch.setattr(draftplan, 'plan_groups', original)
    model = fake_model(monkeypatch, reply(items[6:12], chunk), reply(items[12:], chunk))
    await ledger._extract_in_background(draft_id)
    made = (await client.get(f'/api/ledger/drafts/{draft_id}')).json()
    assert made['status'] == 'ready' and made['metric_count'] == 13 and len(model.calls) == 2
    assert made['implementation_progress']['completed'] == 13
    assert len({m['source_definition_id'] for m in made['metrics']}) == 13


async def test_oee_overlapping_chunks_and_wide_prefixed_catalog_keep_visible_citations_and_diagnostics(
        client, monkeypatch, fake_trial):
    from pathlib import Path
    import uuid
    from app.db.models import DataSource
    from app.ledger import authoring

    fixture_path = Path(__file__).parent / 'fixtures' / 'oee_metric_definitions.md'
    text = fixture_path.read_text(encoding='utf-8')
    lines = text.splitlines(keepends=True)
    # Build 16 overlapping chunks similar to production KB chunking (~16k chars total).
    overlapping_chunks: list[str] = []
    stride = max(1, len(lines) // 16)
    for idx in range(16):
        start = max(0, idx * stride - 6)
        end = len(lines) if idx == 15 else min(len(lines), (idx + 2) * stride + 6)
        overlapping_chunks.append(''.join(lines[start:end]))

    tables: dict[str, dict] = {}
    for idx in range(42):
        tables[f'dw_v_erp_wide_{idx:02d}'] = {
            'columns': [{'name': f'sap_col_{c:02d}', 'type': 'VARCHAR2'} for c in range(60)]
        }
    tables['dw_v_scrap_record'] = {'columns': [{'name': 'scrap_qty', 'type': 'NUMBER'}]}
    tables['dw_v_carrier_factor'] = {'columns': [{'name': 'conv_factor', 'type': 'NUMBER'}]}
    tables['dw_v_line_master'] = {'columns': [{'name': 'std_slots', 'type': 'NUMBER'}, {'name': 'line_code', 'type': 'VARCHAR2'}]}
    tables['dw_v_shift_kpi'] = {
        'columns': [{'name': 'line_code', 'type': 'VARCHAR2'}, {'name': 'shift_date', 'type': 'DATE'}]
        + [{'name': f'ext_val_{c:02d}', 'type': 'NUMBER'} for c in range(1, 56)]
    }

    source_id = f'ds-{uuid.uuid4().hex[:10]}'
    async with SessionLocal() as session:
        src = DataSource(
            id=source_id, name=f'bi_{uuid.uuid4().hex[:6]}', kind='oracle',
            description='制造数据仓库', schema_cache={'schema': 'ANALYTICS', 'tables': tables, 'total': len(tables)},
        )
        session.add(src)
        doc = Document(title='指标公式总表.md', content=text, status='ready', chunk_count=len(overlapping_chunks))
        session.add(doc)
        await session.flush()
        for ordinal, content in enumerate(overlapping_chunks, start=1):
            session.add(Chunk(document_id=doc.id, ordinal=ordinal, content=content))
        await session.commit()
        doc_id = doc.id
        focused = authoring._focus_tables(tables, text, source=src)
        assert 'dw_v_scrap_record' in focused
        assert 'dw_v_line_master' in focused
        assert 'dw_v_shift_kpi' in focused

    async def enqueue(_):
        pass
    monkeypatch.setattr(ledger.draft_jobs, 'start', enqueue)

    created = await client.post('/api/ledger/drafts/definitions', json={'documents': [doc_id]})
    assert created.status_code == 201, created.text
    draft_id = created.json()['draft_id']
    view = (await client.get(f'/api/ledger/drafts/{draft_id}')).json()
    metric_items = [i for i in view['semantic_inventory']['items'] if i['kind'] == 'metric' and i['status'] != 'unavailable']
    assert len(metric_items) >= 40
    target = next(i for i in metric_items if i['name'] == '装配线 OEE')
    chosen = [target, *[i for i in metric_items if i['id'] != target['id']][:11]]

    # First batch implements `target` even if model omits citations (verified provenance fallback attaches it),
    # while remaining 11 items stay unresolved with a model question (testing >10 unresolved non-spam behavior).
    first_reply = json.dumps({
        'groups': [{
            'sql': 'SELECT AVG(ext_val_36) AS oee_assembly FROM ANALYTICS.dw_v_shift_kpi',
            'entry_fields': [],
            'metrics': [{
                'id': 'm1', 'name': target['name'], 'source_definition_id': target['id'],
                'expression': "cell(nodes.fetch, 0, 'oee_assembly')",
                'definition': {'text': '装配线直接取源系统 OEE', 'citations': []},
            }],
        }],
        'open_questions': ['其余指标需确认产线过滤编码与早晚班归属字段'],
        'uncovered': [],
    }, ensure_ascii=False)
    later_reply = json.dumps({
        'groups': [],
        'open_questions': ['其余指标需确认产线过滤编码与早晚班归属字段'],
        'uncovered': [],
    }, ensure_ascii=False)
    fake_model(monkeypatch, first_reply, later_reply)

    started = await client.post(f'/api/ledger/drafts/{draft_id}/implement', json={
        'datasource_id': source_id, 'selected': [i['id'] for i in chosen],
    })
    assert started.status_code == 202, started.text
    await ledger._extract_in_background(draft_id)

    made = (await client.get(f'/api/ledger/drafts/{draft_id}')).json()
    assert made['metric_count'] == 1
    assert made['metrics'][0]['source_definition_id'] == target['id']
    assert made['metrics'][0]['definition']['citations']
    for trace in made['prompts']:
        assert trace['visible_chars'] and all(chars > 0 for chars in trace['visible_chars'].values())
    assert target['id'] not in made['definition_diagnostics']
    unresolved_ids = {i['id'] for i in chosen if i['id'] != target['id']}
    assert set(made['definition_diagnostics']) == unresolved_ids
    assert not any('尚未生成可用实现，请补充字段映射或确认口径' in q for q in made['open_questions'])


async def test_pure_validation_failure_without_model_questions_marks_draft_failed(
        client, datasource, monkeypatch, fake_trial):
    draft_id, source, _chunk, items = await imported(client, datasource, monkeypatch, count=2)
    bad = json.dumps({
        'groups': [{
            'sql': 'SELECT COUNT(*) AS n FROM nonexistent_table',
            'entry_fields': [],
            'metrics': [{
                'id': 'm1', 'name': items[0]['name'], 'source_definition_id': items[0]['id'],
                'expression': "cell(nodes.fetch, 0, 'n')",
                'definition': {'text': items[0]['name'], 'citations': []},
            }],
        }],
        'open_questions': [],
        'uncovered': [],
    }, ensure_ascii=False)
    fake_model(monkeypatch, bad)
    await client.post(f'/api/ledger/drafts/{draft_id}/implement', json={'datasource_id': source})
    await ledger._extract_in_background(draft_id)
    made = (await client.get(f'/api/ledger/drafts/{draft_id}')).json()
    assert made['status'] == 'failed'
    assert 'nonexistent_table' in (made['error'] or '')
    assert not made['open_questions']
    assert made['definition_diagnostics'][items[0]['id']]['kind'] == 'validation_failed'
