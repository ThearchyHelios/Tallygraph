"""标准业务实体的受控增量补丁规划器与落库器（E1）。

供交互式「实体助手」（Entity Copilot）与后台「扫描发现候选实体」共用同一套核验与合并内核：
1. 物理真实性硬校验：表列绑定必须在已启用数据源的 schema_cache 中真实存在；口径参数映射必须在对应工作流入口节点中真实存在。
2. 敏感遮罩隔离：遮罩列（masked_columns）严禁写入或更新 members.source_codes 枚举字典。
3. 剥离自封审批态：模型传入的 status / approved / verified / actor / version 一律忽略重算。
4. 双模式落库：
   - mode="confirmed"（人工在实体助手点「保存修改」）：严格校验 expected_versions CAS 乐观锁，绑定与参数落为 confirmed / human。
   - mode="proposed"（后台主动发现）：新增绑定与参数一律落为 proposed / ai，绝不覆盖人工已确认、已验证或已驳回的项。
"""
from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.artifact_store import canonical_json, content_hash
from app.data import entities, introspect
from app.data.engine import masked_columns
from app.db.base import new_id, utcnow
from app.db.models import DataSource, Workflow, WorkflowVersion
from app.db.models_ontology import CanonicalEntity

OP_TYPES = (
    "upsert_entity",
    "upsert_binding",
    "remove_binding",
    "upsert_member",
    "upsert_caliber_param",
)

_FORBIDDEN_KEYS = frozenset({
    "status", "approve", "approved", "approval", "approver", "verified", "verify",
    "actor", "role", "review", "reviewed", "reviewed_by", "updated_by", "created_by", "version",
})


@dataclass(slots=True)
class EntityPatchChange:
    id: str
    op: str
    entity_key: str
    entity_label: str
    summary: str
    before: Any
    after: Any
    reason: str
    op_payload: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "op": self.op,
            "entity_key": self.entity_key,
            "entity_label": self.entity_label,
            "summary": self.summary,
            "before": self.before,
            "after": self.after,
            "reason": self.reason,
            "op_payload": self.op_payload,
        }


@dataclass(slots=True)
class EntityPatchPlan:
    changes: list[EntityPatchChange] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    expected_versions: dict[str, int] = field(default_factory=dict)


@dataclass(slots=True)
class _ValidationEnv:
    sources_by_norm: dict[str, DataSource]
    masked_by_source: dict[str, set[str]]
    workflows_by_id: dict[str, Workflow]
    workflows_by_name: dict[str, Workflow]
    workflow_inputs: dict[str, set[str]]
    workflow_cards: dict[str, set[str]]
    entities_by_key: dict[str, CanonicalEntity]


def _strip_forbidden(raw: Mapping[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in raw.items() if k not in _FORBIDDEN_KEYS}


def _extract_graph_inputs_and_cards(graph: Mapping[str, Any] | None) -> tuple[set[str], set[str]]:
    inputs: set[str] = set()
    cards: set[str] = set()
    if not isinstance(graph, Mapping):
        return inputs, cards
    for node in graph.get("nodes") or []:
        if not isinstance(node, Mapping):
            continue
        ntype = str(node.get("type") or "").strip()
        nid = str(node.get("id") or "").strip()
        if ntype == "metrics" and nid:
            cards.add(nid)
        if ntype == "input":
            data = node.get("data") if isinstance(node.get("data"), Mapping) else {}
            cfg = data.get("config") if isinstance(data.get("config"), Mapping) else (
                node.get("config") if isinstance(node.get("config"), Mapping) else {}
            )
            for f in cfg.get("fields") or []:
                if isinstance(f, Mapping):
                    fname = str(f.get("name") or "").strip()
                    if fname:
                        inputs.add(fname)
    return inputs, cards


async def _load_env(session: AsyncSession) -> _ValidationEnv:
    from app.data import table_versions

    src_rows = list(
        (await session.execute(select(DataSource).where(DataSource.enabled.is_(True)).order_by(DataSource.name))).scalars()
    )
    sources_by_norm: dict[str, Any] = {}
    masked_by_source: dict[str, set[str]] = {}
    for row in src_rows:
        try:
            resolved = await table_versions.resolve_source(session, row)
        except table_versions.SnapshotMissing:
            resolved = row
        key = row.name.strip().lower()
        sources_by_norm[key] = resolved
        sources_by_norm[row.id.strip().lower()] = resolved
        masked_by_source[row.name.strip().lower()] = {
            c.strip().lower() for c in masked_columns(row.options if isinstance(row.options, dict) else None)
        }

    wf_rows = list((await session.execute(select(Workflow))).scalars())
    ver_rows = list(
        (
            await session.execute(
                select(WorkflowVersion).where(WorkflowVersion.published_at.is_not(None))
            )
        ).scalars()
    )
    workflows_by_id: dict[str, Workflow] = {}
    name_counts: dict[str, int] = {}
    for wf in wf_rows:
        workflows_by_id[wf.id] = wf
        if wf.name:
            nkey = wf.name.strip().lower()
            name_counts[nkey] = name_counts.get(nkey, 0) + 1
    workflows_by_name = {
        wf.name.strip().lower(): wf
        for wf in wf_rows
        if wf.name and name_counts.get(wf.name.strip().lower()) == 1
    }

    workflow_inputs: dict[str, set[str]] = {}
    workflow_cards: dict[str, set[str]] = {}
    for wf in wf_rows:
        ins, cds = _extract_graph_inputs_and_cards(wf.graph if isinstance(wf.graph, dict) else None)
        workflow_inputs.setdefault(wf.id, set()).update(ins)
        workflow_cards.setdefault(wf.id, set()).update(cds)
    for ver in ver_rows:
        ins, cds = _extract_graph_inputs_and_cards(ver.graph if isinstance(ver.graph, dict) else None)
        workflow_inputs.setdefault(ver.workflow_id, set()).update(ins)
        workflow_cards.setdefault(ver.workflow_id, set()).update(cds)

    ent_rows = await entities.list_entities(session)
    entities_by_key = {row.key.strip().lower(): row for row in ent_rows}

    return _ValidationEnv(
        sources_by_norm=sources_by_norm,
        masked_by_source=masked_by_source,
        workflows_by_id=workflows_by_id,
        workflows_by_name=workflows_by_name,
        workflow_inputs=workflow_inputs,
        workflow_cards=workflow_cards,
        entities_by_key=entities_by_key,
    )


def _is_column_masked(masked_set: set[str], table_name: str, column_name: str) -> bool:
    col_low = column_name.strip().lower()
    tbl_low = table_name.strip().lower()
    short_tbl = tbl_low.rsplit(".", 1)[-1]
    return (
        col_low in masked_set
        or f"{tbl_low}.{col_low}" in masked_set
        or f"{short_tbl}.{col_low}" in masked_set
    )


def _resolve_table_and_column(
    source: DataSource,
    raw_table: str,
    raw_column: str,
) -> tuple[str | None, str | None, str | None]:
    """返回 (规范表名, 规范列名, 错误说明)。"""
    cache = source.schema_cache if isinstance(source.schema_cache, Mapping) else {}
    tables = cache.get("tables") if isinstance(cache.get("tables"), Mapping) else {}
    if not tables:
        return None, None, f"数据源「{source.name}」尚未探查表结构，无法核实表「{raw_table}」与列「{raw_column}」"

    t_probe = raw_table.strip()
    matched_table: str | None = None
    meta: Mapping[str, Any] | None = None
    if t_probe in tables:
        matched_table, meta = t_probe, tables[t_probe]
    else:
        t_low = t_probe.lower()
        t_short = t_low.rsplit(".", 1)[-1]
        for tname, tmeta in tables.items():
            if not isinstance(tmeta, Mapping):
                continue
            qual = str(tmeta.get("qualified") or tname).strip()
            if tname.lower() == t_low or qual.lower() == t_low or tname.lower() == t_short:
                matched_table, meta = tname, tmeta
                break
    if meta is None or matched_table is None:
        sample = "、".join(list(tables.keys())[:15])
        return None, None, f"数据源「{source.name}」中不存在表「{raw_table}」（已探查表如：{sample}）"

    cols = introspect.table_columns(dict(meta))
    c_low = raw_column.strip().lower()
    for col in cols:
        cname = str(col.get("name") or "").strip()
        if cname.lower() == c_low:
            return matched_table, cname, None
    col_sample = "、".join(str(c.get("name")) for c in cols[:20])
    return None, None, f"数据源「{source.name}」表「{matched_table}」中不存在列「{raw_column}」（可用列：{col_sample}）"


def _working_from_row(row: CanonicalEntity) -> dict[str, Any]:
    return {
        "id": row.id,
        "key": row.key,
        "label": row.label,
        "description": str(row.description or ""),
        "aliases": list(row.aliases or []),
        "members": [copy.deepcopy(dict(m)) for m in (row.members or []) if isinstance(m, Mapping)],
        "bindings": [copy.deepcopy(dict(b)) for b in (row.bindings or []) if isinstance(b, Mapping)],
        "caliber_params": [copy.deepcopy(dict(p)) for p in (row.caliber_params or []) if isinstance(p, Mapping)],
        "version": int(row.version or 1),
        "is_new": False,
    }


def _change_id(op: str, entity_key: str, identity: str) -> str:
    return content_hash(canonical_json({"op": op, "entity_key": entity_key, "id": identity}))[:16]


def _normalize_single_op(
    raw_op: Any,
    *,
    index: int,
    env: _ValidationEnv,
    working: dict[str, dict[str, Any]],
    mode: Literal["confirmed", "proposed"],
) -> tuple[EntityPatchChange | None, str | None]:
    if not isinstance(raw_op, Mapping):
        return None, f"第 {index + 1} 项操作需要是对象"
    cleaned = _strip_forbidden(raw_op)
    op = str(cleaned.get("op") or "").strip()
    if op not in OP_TYPES:
        return None, f"第 {index + 1} 项操作类型「{op}」无效，支持：{', '.join(OP_TYPES)}"
    if mode == "proposed" and op == "remove_binding":
        return None, "后台发现模式不允许删除既有表列绑定（remove_binding）"

    raw_key = cleaned.get("entity_key") or cleaned.get("key")
    try:
        entity_key = entities.normalize_entity_key(raw_key, field="entity_key")
    except entities.EntityInvalid as exc:
        return None, f"第 {index + 1} 项：{exc.message}"

    reason = str(cleaned.get("reason") or "").strip()[:500]
    note = str(cleaned.get("note") or "").strip()[:500]

    # 初始化当前实体工作副本
    state = working.get(entity_key)
    if state is None and entity_key in env.entities_by_key:
        state = _working_from_row(env.entities_by_key[entity_key])
        working[entity_key] = state

    if op == "upsert_entity":
        raw_label = str(cleaned.get("label") or cleaned.get("entity_label") or "").strip()
        if state is None and not raw_label:
            return None, f"新建标准实体「{entity_key}」缺少中文名称（label）"
        label = raw_label or (state["label"] if state else entity_key)
        if len(label) > 100:
            return None, f"标准实体「{entity_key}」名称不能超过 100 个字符"
        desc = str(cleaned.get("description") if "description" in cleaned else (state["description"] if state else "") or "").strip()
        raw_aliases = cleaned.get("aliases")
        if raw_aliases is None:
            new_aliases = list(state["aliases"]) if state else []
        elif isinstance(raw_aliases, list):
            merged = list(state["aliases"]) if (state and mode == "proposed") else []
            seen_a = {a.lower() for a in merged}
            for item in raw_aliases:
                if isinstance(item, str) and item.strip() and item.strip().lower() not in seen_a:
                    seen_a.add(item.strip().lower())
                    merged.append(item.strip())
            new_aliases = merged
        else:
            return None, f"标准实体「{entity_key}」的 aliases 需要是字符串数组"

        if state is None:
            working[entity_key] = {
                "id": None,
                "key": entity_key,
                "label": label,
                "description": desc,
                "aliases": new_aliases,
                "members": [],
                "bindings": [],
                "caliber_params": [],
                "version": 0,
                "is_new": True,
            }
            payload = {
                "op": "upsert_entity",
                "entity_key": entity_key,
                "label": label,
                "description": desc,
                "aliases": new_aliases,
                "reason": reason,
            }
            return EntityPatchChange(
                id=_change_id(op, entity_key, "meta"),
                op=op,
                entity_key=entity_key,
                entity_label=label,
                summary=f"新建标准业务实体「{label}（{entity_key}）」",
                before=None,
                after={"label": label, "description": desc, "aliases": new_aliases},
                reason=reason,
                op_payload=payload,
            ), None

        # 更新已有实体元信息（后台发现模式下不覆盖已有中文名与非空说明）
        target_label = state["label"] if mode == "proposed" else label
        target_desc = (state["description"] or desc) if mode == "proposed" else desc
        before_meta = {
            "label": state["label"],
            "description": state["description"],
            "aliases": list(state["aliases"]),
        }
        after_meta = {
            "label": target_label,
            "description": target_desc,
            "aliases": new_aliases,
        }
        if before_meta == after_meta:
            return None, None
        state["label"] = target_label
        state["description"] = target_desc
        state["aliases"] = new_aliases
        payload = {
            "op": "upsert_entity",
            "entity_key": entity_key,
            "label": target_label,
            "description": target_desc,
            "aliases": new_aliases,
            "reason": reason,
        }
        return EntityPatchChange(
            id=_change_id(op, entity_key, "meta"),
            op=op,
            entity_key=entity_key,
            entity_label=target_label,
            summary=f"更新标准实体「{target_label}（{entity_key}）」基础信息",
            before=before_meta,
            after=after_meta,
            reason=reason,
            op_payload=payload,
        ), None

    # 其余子项操作：若实体尚不存在，允许随操作附带 entity_label 自动初始化，否则明确报错
    if state is None:
        fallback_label = str(cleaned.get("entity_label") or "").strip()
        if not fallback_label:
            return (
                None,
                f"标准实体「{entity_key}」尚不存在，请先提供一条 upsert_entity（含 label 中文名称）或在操作中提供 entity_label",
            )
        state = {
            "id": None,
            "key": entity_key,
            "label": fallback_label[:100],
            "description": "",
            "aliases": [],
            "members": [],
            "bindings": [],
            "caliber_params": [],
            "version": 0,
            "is_new": True,
        }
        working[entity_key] = state

    if op == "upsert_binding":
        raw_src = str(cleaned.get("source_name") or "").strip()
        raw_tbl = str(cleaned.get("table_name") or "").strip()
        raw_col = str(cleaned.get("column_name") or "").strip()
        if not raw_src or not raw_tbl or not raw_col:
            return None, f"实体「{entity_key}」的表列绑定须完整填写 source_name、table_name、column_name"
        src_row = env.sources_by_norm.get(raw_src.lower())
        if src_row is None:
            avail_src = "、".join(sorted({r.name for r in env.sources_by_norm.values()})) or "无"
            return None, f"未找到已启用的数据源「{raw_src}」（当前可用数据源：{avail_src}）"
        canon_tbl, canon_col, problem = _resolve_table_and_column(src_row, raw_tbl, raw_col)
        if problem or canon_tbl is None or canon_col is None:
            return None, problem or "表列核验未通过"

        target_status = "confirmed" if mode == "confirmed" else "proposed"
        target_source = "human" if mode == "confirmed" else "ai"
        bkey = (src_row.name.lower(), canon_tbl.lower(), canon_col.lower())
        existing_idx = next(
            (
                i
                for i, b in enumerate(state["bindings"])
                if (
                    str(b.get("source_name") or "").lower(),
                    str(b.get("table_name") or "").lower(),
                    str(b.get("column_name") or "").lower(),
                )
                == bkey
            ),
            None,
        )
        if existing_idx is not None:
            cur = state["bindings"][existing_idx]
            cur_status = str(cur.get("status") or "confirmed")
            if mode == "proposed" and cur_status in ("confirmed", "verified", "rejected"):
                return None, None
            if (
                cur_status == target_status
                and str(cur.get("note") or "") == (note or str(cur.get("note") or ""))
                and str(cur.get("reason") or "") == (reason or str(cur.get("reason") or ""))
            ):
                return None, None
            before_val = copy.deepcopy(cur)
            updated = {
                **cur,
                "source_id": src_row.id,
                "source_name": src_row.name,
                "table_name": canon_tbl,
                "column_name": canon_col,
                "status": target_status,
                "source": target_source,
                "note": note or str(cur.get("note") or ""),
                "reason": reason or str(cur.get("reason") or ""),
                "updated_at": utcnow().isoformat(),
            }
            state["bindings"][existing_idx] = updated
        else:
            before_val = None
            updated = {
                "id": new_id()[:12],
                "source_id": src_row.id,
                "source_name": src_row.name,
                "table_name": canon_tbl,
                "column_name": canon_col,
                "status": target_status,
                "source": target_source,
                "note": note,
                "reason": reason,
                "updated_at": utcnow().isoformat(),
            }
            state["bindings"].append(updated)

        payload = {
            "op": "upsert_binding",
            "entity_key": entity_key,
            "entity_label": state["label"],
            "source_name": src_row.name,
            "table_name": canon_tbl,
            "column_name": canon_col,
            "note": note,
            "reason": reason,
        }
        action_word = "新增" if before_val is None else "更新"
        return EntityPatchChange(
            id=_change_id(op, entity_key, f"{src_row.name}.{canon_tbl}.{canon_col}"),
            op=op,
            entity_key=entity_key,
            entity_label=state["label"],
            summary=f"{action_word}实体「{state['label']}」表列绑定：{src_row.name}.{canon_tbl}.{canon_col}",
            before=before_val,
            after=updated,
            reason=reason,
            op_payload=payload,
        ), None

    if op == "remove_binding":
        raw_src = str(cleaned.get("source_name") or "").strip()
        raw_tbl = str(cleaned.get("table_name") or "").strip()
        raw_col = str(cleaned.get("column_name") or "").strip()
        bkey = (raw_src.lower(), raw_tbl.lower(), raw_col.lower())
        existing_idx = next(
            (
                i
                for i, b in enumerate(state["bindings"])
                if (
                    str(b.get("source_name") or "").lower(),
                    str(b.get("table_name") or "").lower(),
                    str(b.get("column_name") or "").lower(),
                )
                == bkey
            ),
            None,
        )
        if existing_idx is None:
            return None, f"实体「{entity_key}」中不存在待移除的绑定「{raw_src}.{raw_tbl}.{raw_col}」"
        removed = state["bindings"].pop(existing_idx)
        payload = {
            "op": "remove_binding",
            "entity_key": entity_key,
            "source_name": removed.get("source_name"),
            "table_name": removed.get("table_name"),
            "column_name": removed.get("column_name"),
            "reason": reason,
        }
        return EntityPatchChange(
            id=_change_id(op, entity_key, f"{raw_src}.{raw_tbl}.{raw_col}"),
            op=op,
            entity_key=entity_key,
            entity_label=state["label"],
            summary=f"移除实体「{state['label']}」表列绑定：{removed.get('source_name')}.{removed.get('table_name')}.{removed.get('column_name')}",
            before=removed,
            after=None,
            reason=reason,
            op_payload=payload,
        ), None

    if op == "upsert_member":
        val = str(cleaned.get("value") or "").strip()
        if not val:
            return None, f"实体「{entity_key}」成员映射缺少标准值（value）"
        label = str(cleaned.get("label") or val).strip() or val
        raw_aliases = cleaned.get("aliases")
        if raw_aliases is not None and not isinstance(raw_aliases, list):
            return None, f"实体「{entity_key}」成员「{val}」的 aliases 需要是字符串数组"
        raw_codes = cleaned.get("source_codes")
        if raw_codes is not None and not isinstance(raw_codes, Mapping):
            return None, f"实体「{entity_key}」成员「{val}」的 source_codes 需要是「数据源名 → 编码」对象"

        canon_codes: dict[str, str] = {}
        for src_k, code_v in (raw_codes or {}).items():
            s_str = str(src_k or "").strip()
            c_str = str(code_v or "").strip()
            if not s_str or not c_str:
                continue
            src_row = env.sources_by_norm.get(s_str.lower())
            canon_src = src_row.name if src_row is not None else s_str
            # 检查该实体在此数据源上的绑定是否涉及敏感遮罩列
            masked_set = env.masked_by_source.get(canon_src.lower(), set())
            if masked_set:
                for b in state["bindings"]:
                    if str(b.get("source_name") or "").lower() == canon_src.lower():
                        if _is_column_masked(masked_set, str(b.get("table_name") or ""), str(b.get("column_name") or "")):
                            return (
                                None,
                                f"数据源「{canon_src}」表列「{b.get('table_name')}.{b.get('column_name')}」为敏感遮罩列，禁止为其配置成员编码映射",
                            )
            canon_codes[canon_src] = c_str

        m_idx = next(
            (i for i, m in enumerate(state["members"]) if str(m.get("value") or "").strip().lower() == val.lower()),
            None,
        )
        if m_idx is not None:
            cur_m = state["members"][m_idx]
            before_m = copy.deepcopy(cur_m)
            merged_aliases = list(cur_m.get("aliases") or [])
            seen_ma = {a.lower() for a in merged_aliases if isinstance(a, str)}
            for item in raw_aliases or []:
                if isinstance(item, str) and item.strip() and item.strip().lower() not in seen_ma:
                    seen_ma.add(item.strip().lower())
                    merged_aliases.append(item.strip())
            merged_codes = dict(cur_m.get("source_codes") or {})
            for cs, cv in canon_codes.items():
                if mode == "proposed" and cs in merged_codes and str(merged_codes[cs]).strip():
                    continue
                merged_codes[cs] = cv
            target_mlabel = str(cur_m.get("label") or val) if mode == "proposed" else label
            after_m = {
                "value": str(cur_m.get("value") or val),
                "label": target_mlabel,
                "aliases": merged_aliases,
                "source_codes": merged_codes,
            }
            if before_m == after_m:
                return None, None
            state["members"][m_idx] = after_m
        else:
            before_m = None
            clean_aliases = []
            seen_ma = set()
            for item in raw_aliases or []:
                if isinstance(item, str) and item.strip() and item.strip().lower() not in seen_ma:
                    seen_ma.add(item.strip().lower())
                    clean_aliases.append(item.strip())
            after_m = {
                "value": val,
                "label": label,
                "aliases": clean_aliases,
                "source_codes": canon_codes,
            }
            state["members"].append(after_m)

        payload = {
            "op": "upsert_member",
            "entity_key": entity_key,
            "entity_label": state["label"],
            "value": after_m["value"],
            "label": after_m["label"],
            "aliases": after_m["aliases"],
            "source_codes": after_m["source_codes"],
            "reason": reason,
        }
        code_desc = "，".join(f"{k}={v}" for k, v in after_m["source_codes"].items())
        suffix = f"（{code_desc}）" if code_desc else ""
        return EntityPatchChange(
            id=_change_id(op, entity_key, after_m["value"].lower()),
            op=op,
            entity_key=entity_key,
            entity_label=state["label"],
            summary=f"{'新增' if before_m is None else '更新'}实体「{state['label']}」成员「{after_m['label']}（{after_m['value']}）」{suffix}",
            before=before_m,
            after=after_m,
            reason=reason,
            op_payload=payload,
        ), None

    # upsert_caliber_param
    raw_wf = str(cleaned.get("workflow_id") or "").strip()
    raw_card = str(cleaned.get("card_id") or "").strip()
    raw_param = str(cleaned.get("param_name") or "").strip()
    if not raw_wf or not raw_param:
        return None, f"实体「{entity_key}」口径参数映射须提供 workflow_id 与 param_name"
    wf_row = env.workflows_by_id.get(raw_wf) or env.workflows_by_name.get(raw_wf.lower())
    if wf_row is None:
        return None, f"不存在工作流「{raw_wf}」，请核对已发布口径的工作流 ID"
    valid_inputs = env.workflow_inputs.get(wf_row.id, set())
    canon_param = next((p for p in valid_inputs if p.lower() == raw_param.lower()), None)
    if canon_param is None:
        sample_in = "、".join(sorted(valid_inputs)) or "无入口参数"
        return None, f"工作流「{wf_row.name}（{wf_row.id}）」入口节点不存在参数「{raw_param}」（可用参数：{sample_in}）"
    valid_cards = env.workflow_cards.get(wf_row.id, set())
    canon_card = ""
    if raw_card:
        matched_card = next((c for c in valid_cards if c.lower() == raw_card.lower()), None)
        if matched_card is None:
            sample_cd = "、".join(sorted(valid_cards)) or "无口径卡节点"
            return None, f"工作流「{wf_row.name}（{wf_row.id}）」中不存在口径卡「{raw_card}」（可用口径卡：{sample_cd}）"
        canon_card = matched_card

    target_status = "confirmed" if mode == "confirmed" else "proposed"
    target_source = "human" if mode == "confirmed" else "ai"
    pkey = (wf_row.id, canon_card.lower(), canon_param.lower())
    p_idx = next(
        (
            i
            for i, p in enumerate(state["caliber_params"])
            if (
                str(p.get("workflow_id") or ""),
                str(p.get("card_id") or "").lower(),
                str(p.get("param_name") or "").lower(),
            )
            == pkey
        ),
        None,
    )
    if p_idx is not None:
        cur_p = state["caliber_params"][p_idx]
        cur_status = str(cur_p.get("status") or "confirmed")
        if mode == "proposed" and cur_status in ("confirmed", "verified", "rejected"):
            return None, None
        if cur_status == target_status and str(cur_p.get("note") or "") == (note or str(cur_p.get("note") or "")):
            return None, None
        before_p = copy.deepcopy(cur_p)
        after_p = {
            **cur_p,
            "workflow_id": wf_row.id,
            "card_id": canon_card,
            "param_name": canon_param,
            "status": target_status,
            "source": target_source,
            "note": note or reason or str(cur_p.get("note") or ""),
        }
        state["caliber_params"][p_idx] = after_p
    else:
        before_p = None
        after_p = {
            "id": new_id()[:12],
            "workflow_id": wf_row.id,
            "card_id": canon_card,
            "param_name": canon_param,
            "status": target_status,
            "source": target_source,
            "note": note or reason,
        }
        state["caliber_params"].append(after_p)

    payload = {
        "op": "upsert_caliber_param",
        "entity_key": entity_key,
        "entity_label": state["label"],
        "workflow_id": wf_row.id,
        "card_id": canon_card,
        "param_name": canon_param,
        "note": note or reason,
        "reason": reason,
    }
    card_part = f" · {canon_card}" if canon_card else ""
    return EntityPatchChange(
        id=_change_id(op, entity_key, f"{wf_row.id}:{canon_card}:{canon_param}"),
        op=op,
        entity_key=entity_key,
        entity_label=state["label"],
        summary=f"{'新增' if before_p is None else '更新'}实体「{state['label']}」口径入参映射：{wf_row.name}{card_part} → {canon_param}",
        before=before_p,
        after=after_p,
        reason=reason,
        op_payload=payload,
    ), None


async def plan_entity_patch(
    session: AsyncSession,
    raw_ops: Sequence[Any],
    *,
    pending_ops: Sequence[Any] = (),
    mode: Literal["confirmed", "proposed"] = "confirmed",
) -> EntityPatchPlan:
    """校验一批增量修改操作并生成结构化变更预览，不写入数据库。"""
    env = await _load_env(session)
    working: dict[str, dict[str, Any]] = {}

    # 先回放界面待保存的操作，保证后续新增实体/成员在此基础上叠加
    for p_idx, pop in enumerate(pending_ops or ()):
        _normalize_single_op(pop, index=p_idx, env=env, working=working, mode=mode)

    plan = EntityPatchPlan()
    for idx, item in enumerate(raw_ops or ()):
        change, problem = _normalize_single_op(item, index=idx, env=env, working=working, mode=mode)
        if problem:
            plan.problems.append(problem)
            continue
        if change is not None:
            plan.changes.append(change)

    touched_keys = {c.entity_key for c in plan.changes}
    for ekey in sorted(touched_keys):
        existing = env.entities_by_key.get(ekey)
        plan.expected_versions[ekey] = int(existing.version or 1) if existing is not None else 0
    return plan


async def apply_entity_patch(
    session: AsyncSession,
    raw_ops: Sequence[Any],
    *,
    mode: Literal["confirmed", "proposed"],
    actor: str | None = None,
    expected_versions: Mapping[str, int] | None = None,
) -> dict[str, Any]:
    """将一批增量操作合并并经 entities.create_entity / update_entity 统一落库。"""
    if not raw_ops:
        raise entities.EntityInvalid("没有可保存的实体修改项", field="operations")

    env = await _load_env(session)
    working: dict[str, dict[str, Any]] = {}
    changes: list[EntityPatchChange] = []
    problems: list[str] = []

    for idx, item in enumerate(raw_ops):
        change, problem = _normalize_single_op(item, index=idx, env=env, working=working, mode=mode)
        if problem:
            problems.append(problem)
            continue
        if change is not None:
            changes.append(change)

    if mode == "confirmed" and problems:
        raise entities.EntityInvalid("；".join(problems), field="operations")

    touched_keys = list(dict.fromkeys(c.entity_key for c in changes))
    if mode == "confirmed" and expected_versions is not None:
        for ekey in touched_keys:
            if ekey not in expected_versions:
                continue
            exp_ver = int(expected_versions[ekey])
            cur_row = env.entities_by_key.get(ekey)
            cur_ver = int(cur_row.version or 1) if cur_row is not None else 0
            if exp_ver != cur_ver:
                label = cur_row.label if cur_row is not None else ekey
                raise entities.EntityConflict(
                    f"标准实体「{label}」在对话期间已被修改（基准 v{exp_ver}，当前 v{cur_ver}），请刷新后再应用修改",
                    field="expected_versions",
                )

    created_count = 0
    updated_count = 0
    saved_entities: list[dict[str, Any]] = []

    for ekey in touched_keys:
        state = working.get(ekey)
        if state is None:
            continue
        payload: dict[str, Any] = {
            "key": state["key"],
            "label": state["label"],
            "description": state["description"],
            "aliases": state["aliases"],
            "members": state["members"],
            "bindings": state["bindings"],
            "caliber_params": state["caliber_params"],
        }
        cur_row = env.entities_by_key.get(ekey)
        if cur_row is None:
            row = await entities.create_entity(session, payload, actor=actor)
            created_count += 1
        else:
            payload["version"] = int(cur_row.version or 1)
            row = await entities.update_entity(session, cur_row.id, payload, actor=actor)
            updated_count += 1
        saved_entities.append(entities.entity_to_dict(row))

    proposed_bindings = sum(1 for c in changes if c.op == "upsert_binding")
    proposed_params = sum(1 for c in changes if c.op == "upsert_caliber_param")
    added_members = sum(1 for c in changes if c.op == "upsert_member")

    return {
        "applied_count": len(changes),
        "created_entities": created_count,
        "updated_entities": updated_count,
        "bindings_changed": proposed_bindings,
        "caliber_params_changed": proposed_params,
        "members_changed": added_members,
        "entities": saved_entities,
        "problems": problems,
    }
