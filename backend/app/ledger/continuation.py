"""Conservative graph append for independently reviewed missing-definition candidates."""
from __future__ import annotations

import copy
import re
from typing import Any


def append_candidate(original: dict[str, Any], candidate: dict[str, Any], *, prefix: str,
                     allowed_definitions: set[str]) -> dict[str, Any]:
    from app.engine.schema import GraphSpec

    graph, extra = copy.deepcopy(original), copy.deepcopy(candidate)
    def one(nodes, kind):
        found = [n for n in nodes if n.get('type') == kind]
        if len(found) != 1:
            raise ValueError('图中入口或口径卡不唯一，请在画布中核对合并；候选已保留')
        return found[0]
    nodes, added = graph['nodes'], extra['nodes']
    start, card = one(nodes, 'input'), one(nodes, 'metrics')
    incoming_start, incoming_card = one(added, 'input'), one(added, 'metrics')
    def config(node):
        return node['data']['config']
    old_metrics, new_metrics = config(card)['metrics'], config(incoming_card)['metrics']
    ids = [m.get('source_definition_id') for m in new_metrics]
    new_names = [m.get('name') for m in new_metrics]
    if not ids or None in ids or not set(ids) <= allowed_definitions:
        raise ValueError('候选包含未请求的指标定义，请核对；候选已保留')
    if not all(new_names) or len(new_names) != len(set(new_names)):
        raise ValueError('候选包含同名重复指标，请核对；候选已保留')
    if set(ids) & {m.get('source_definition_id') for m in old_metrics}:
        raise ValueError('原稿已有这些指标定义，不会覆盖现有实现；候选已保留')
    if set(new_names) & {m.get('name') for m in old_metrics}:
        raise ValueError('存在同名指标，请确认口径后在画布中合并；候选已保留')
    fields = config(start).setdefault('fields', [])
    by_name = {f['name']: f for f in fields}
    for field in config(incoming_start).get('fields', []):
        if field['name'] in by_name and field != by_name[field['name']]:
            raise ValueError(f"入口参数「{field['name']}」定义冲突，不会覆盖原参数；候选已保留")
        if field['name'] not in by_name:
            fields.append(field)
    tools = [n for n in added if n.get('type') == 'tool']
    if not tools or any(n.get('type') not in ('input', 'tool', 'metrics', 'report', 'output') for n in added):
        raise ValueError('候选取数结构已变化，请在画布中核对合并；候选已保留')
    rename = {n['id']: f"{prefix}_{n['id']}" for n in tools}
    rename[incoming_start['id']], rename[incoming_card['id']] = start['id'], card['id']
    if set(rename[n['id']] for n in tools) & {n['id'] for n in nodes}:
        raise ValueError('候选节点已存在，请刷新后核对')
    # Generated SQL uses input parameters, not upstream node references. Refuse edited dependencies.
    if any('nodes.' in str(config(n)) for n in tools):
        raise ValueError('候选查询依赖其他节点，请在画布中核对合并；候选已保留')
    edges = [e for e in extra.get('edges', []) if e['source'] in rename and e['target'] in rename]
    expected_edges = {(a['id'], b['id']) for a, b in zip([incoming_start, *tools], [*tools, incoming_card])}
    if {(e['source'], e['target']) for e in edges} != expected_edges:
        raise ValueError('候选取数链路已变化，请在画布中核对合并；候选已保留')
    for metric in new_metrics:
        expression = metric.get('expression', '')
        refs = set(re.findall(r'\bnodes\.([A-Za-z0-9_]+)', expression))
        if not refs or not refs <= {n['id'] for n in tools}:
            raise ValueError('候选指标依赖未合并的节点，请核对；候选已保留')
        metric['expression'] = re.sub(r'\bnodes\.([A-Za-z0-9_]+)', lambda m: 'nodes.' + rename[m[1]], expression)
        metric['id'] = f"{prefix}_{metric['id']}"
    if {m['id'] for m in new_metrics} & {m['id'] for m in old_metrics}:
        raise ValueError('指标编号冲突，请核对；候选已保留')
    old_metrics.extend(new_metrics)
    for node in tools:
        node['id'] = rename[node['id']]
        nodes.append(node)
    graph['edges'].extend({**e, 'source': rename[e['source']], 'target': rename[e['target']],
                           'id': f'{prefix}_edge_{index}'} for index, e in enumerate(edges))
    for node in nodes:
        contract = config(node).get('contract')
        if node.get('type') == 'output' and isinstance(contract, dict) and contract.get('metrics_from') == card['id']:
            contract['required'] = [*contract.get('required', []), *[m['id'] for m in new_metrics]]
    GraphSpec.model_validate(graph)
    return graph
