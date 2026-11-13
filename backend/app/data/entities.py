"""标准业务实体（CanonicalEntity）的校验、持久化与跨源键值对齐。

核心铁律：
1. 物理表列绑定（bindings）与口径参数映射（caliber_params）分设 proposed / verified / confirmed / rejected 四态；
   运行时自动穿透、跨源实体值翻译、参数传递**只认 verified / confirmed**，proposed 候选绝不自动参与穿透。
2. 跨源实体值翻译（translate_entity_value）坚持确定性映射：有标准成员表（members）时只按显式 source_codes /
   标准值 / 别名精确匹配，映射不上返回 None，严禁靠子串猜测。
3. 受限视角（restricted=True，如普通 user 角色）隐去物理表名、物理列名与跨源底层编码，只暴露业务实体名与标准值。
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import new_id, utcnow
from app.db.models_ontology import CanonicalEntity

ENTITY_STATUSES = ("proposed", "verified", "confirmed", "rejected")
ACTIVE_ENTITY_STATUSES = ("verified", "confirmed")
ENTITY_SOURCES = ("human", "rule", "ai", "profile")

_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


class EntityInvalid(ValueError):
    """标准实体请求格式或取值不合法。"""

    def __init__(self, message: str, *, field: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.field = field


class EntityConflict(ValueError):
    """标准实体版本冲突或唯一键冲突。"""

    def __init__(self, message: str, *, field: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.field = field


class EntityNotFound(LookupError):
    """标准实体或子项不存在。"""


def _norm_token(value: Any) -> str:
    return str(value or "").strip().lower()


def normalize_entity_key(raw: Any, *, field: str = "key") -> str:
    if not isinstance(raw, str) or not _KEY_RE.match(raw.strip()):
        raise EntityInvalid("实体标识（key）须以小写字母开头，仅含小写字母、数字和下划线（最长 64 字符）", field=field)
    return raw.strip()


def _clean_str_list(raw: Any, *, field: str) -> list[str]:
    if raw in (None, ""):
        return []
    if not isinstance(raw, list):
        raise EntityInvalid(f"字段「{field}」需要是字符串列表", field=field)
    seen: set[str] = set()
    out: list[str] = []
    for idx, item in enumerate(raw):
        if not isinstance(item, str):
            raise EntityInvalid(f"字段「{field}[{idx}]」需要是字符串", field=f"{field}[{idx}]")
        val = item.strip()
        if val and val.lower() not in seen:
            seen.add(val.lower())
            out.append(val)
    return out


def normalize_member(raw: Any, *, index: int = 0) -> dict[str, Any]:
    at = f"members[{index}]"
    if not isinstance(raw, Mapping):
        raise EntityInvalid("实体成员需要是对象", field=at)
    value = str(raw.get("value") or "").strip()
    if not value:
        raise EntityInvalid("实体成员缺少标准值（value）", field=f"{at}.value")
    label = str(raw.get("label") or value).strip() or value
    aliases = _clean_str_list(raw.get("aliases"), field=f"{at}.aliases")
    raw_codes = raw.get("source_codes")
    source_codes: dict[str, str] = {}
    if raw_codes not in (None, {}):
        if not isinstance(raw_codes, Mapping):
            raise EntityInvalid("跨源编码（source_codes）需要是「数据源名 → 编码」对象", field=f"{at}.source_codes")
        for src, code in raw_codes.items():
            src_key = str(src or "").strip()
            code_val = str(code or "").strip()
            if src_key and code_val:
                source_codes[src_key] = code_val
    return {"value": value, "label": label, "aliases": aliases, "source_codes": source_codes}


def normalize_binding(raw: Any, *, index: int = 0, actor: str | None = None,
                      force_proposed: bool = False) -> dict[str, Any]:
    at = f"bindings[{index}]"
    if not isinstance(raw, Mapping):
        raise EntityInvalid("物理表列绑定需要是对象", field=at)
    source_name = str(raw.get("source_name") or "").strip()
    source_id = str(raw.get("source_id") or "").strip() or None
    table_name = str(raw.get("table_name") or "").strip()
    column_name = str(raw.get("column_name") or "").strip()
    if not source_name:
        raise EntityInvalid("物理绑定缺少数据源名称（source_name）", field=f"{at}.source_name")
    if not table_name:
        raise EntityInvalid("物理绑定缺少表名（table_name）", field=f"{at}.table_name")
    if not column_name:
        raise EntityInvalid("物理绑定缺少列名（column_name）", field=f"{at}.column_name")
    source = str(raw.get("source") or "human").strip()
    if source not in ENTITY_SOURCES:
        raise EntityInvalid(f"绑定来源「{source}」无效，可选：{', '.join(ENTITY_SOURCES)}", field=f"{at}.source")
    status = "proposed" if (force_proposed or source in ("ai", "profile") and "status" not in raw) else str(
        raw.get("status") or "confirmed"
    ).strip()
    if force_proposed:
        status = "proposed"
    if status not in ENTITY_STATUSES:
        raise EntityInvalid(f"绑定状态「{status}」无效，可选：{', '.join(ENTITY_STATUSES)}", field=f"{at}.status")
    return {
        "id": str(raw.get("id") or "").strip() or new_id()[:12],
        "source_id": source_id,
        "source_name": source_name,
        "table_name": table_name,
        "column_name": column_name,
        "status": status,
        "source": source,
        "note": str(raw.get("note") or "").strip(),
        "reason": str(raw.get("reason") or "").strip(),
        "updated_at": str(raw.get("updated_at") or utcnow().isoformat()),
        "updated_by": str(raw.get("updated_by") or actor or "").strip() or None,
    }


def normalize_caliber_param(raw: Any, *, index: int = 0, force_proposed: bool = False) -> dict[str, Any]:
    at = f"caliber_params[{index}]"
    if not isinstance(raw, Mapping):
        raise EntityInvalid("口径参数映射需要是对象", field=at)
    workflow_id = str(raw.get("workflow_id") or "").strip()
    card_id = str(raw.get("card_id") or "").strip()
    param_name = str(raw.get("param_name") or "").strip()
    if not workflow_id or not param_name:
        raise EntityInvalid("口径参数映射须写明工作流 ID（workflow_id）与参数名（param_name）", field=at)
    source = str(raw.get("source") or "human").strip()
    if source not in ENTITY_SOURCES:
        raise EntityInvalid(f"口径参数映射来源「{source}」无效", field=f"{at}.source")
    status = "proposed" if force_proposed else str(raw.get("status") or "confirmed").strip()
    if status not in ENTITY_STATUSES:
        raise EntityInvalid(f"口径参数映射状态「{status}」无效", field=f"{at}.status")
    return {
        "id": str(raw.get("id") or "").strip() or new_id()[:12],
        "workflow_id": workflow_id,
        "card_id": card_id,
        "param_name": param_name,
        "status": status,
        "source": source,
        "note": str(raw.get("note") or "").strip(),
    }


def normalize_entity_payload(payload: Mapping[str, Any], *, partial: bool = False,
                             actor: str | None = None) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise EntityInvalid("请求内容需要是对象")
    out: dict[str, Any] = {}
    if not partial or "key" in payload:
        out["key"] = normalize_entity_key(payload.get("key"))
    if not partial or "label" in payload:
        label = str(payload.get("label") or "").strip()
        if not label:
            raise EntityInvalid("实体名称（label）不能为空", field="label")
        if len(label) > 100:
            raise EntityInvalid("实体名称不能超过 100 个字符", field="label")
        out["label"] = label
    if not partial or "description" in payload:
        out["description"] = str(payload.get("description") or "").strip()
    if not partial or "aliases" in payload:
        out["aliases"] = _clean_str_list(payload.get("aliases"), field="aliases")
    if not partial or "members" in payload:
        raw_members = payload.get("members") or []
        if not isinstance(raw_members, list):
            raise EntityInvalid("成员列表（members）需要是数组", field="members")
        seen_vals: set[str] = set()
        members: list[dict[str, Any]] = []
        for idx, m in enumerate(raw_members):
            norm = normalize_member(m, index=idx)
            vkey = norm["value"].lower()
            if vkey in seen_vals:
                raise EntityInvalid(f"实体成员值「{norm['value']}」重复", field=f"members[{idx}].value")
            seen_vals.add(vkey)
            members.append(norm)
        out["members"] = members
    if not partial or "bindings" in payload:
        raw_bindings = payload.get("bindings") or []
        if not isinstance(raw_bindings, list):
            raise EntityInvalid("物理绑定列表（bindings）需要是数组", field="bindings")
        seen_cols: set[tuple[str, str, str]] = set()
        bindings: list[dict[str, Any]] = []
        for idx, b in enumerate(raw_bindings):
            norm = normalize_binding(b, index=idx, actor=actor)
            col_key = (norm["source_name"].lower(), norm["table_name"].lower(), norm["column_name"].lower())
            if col_key in seen_cols:
                raise EntityInvalid(
                    f"数据源「{norm['source_name']}」表「{norm['table_name']}.{norm['column_name']}」重复绑定",
                    field=f"bindings[{idx}]",
                )
            seen_cols.add(col_key)
            bindings.append(norm)
        out["bindings"] = bindings
    if not partial or "caliber_params" in payload:
        raw_params = payload.get("caliber_params") or []
        if not isinstance(raw_params, list):
            raise EntityInvalid("口径参数映射（caliber_params）需要是数组", field="caliber_params")
        out["caliber_params"] = [normalize_caliber_param(p, index=idx) for idx, p in enumerate(raw_params)]
    return out


# --------------------------------------------------------------------------
# 运行时对齐与状态过滤（只认 verified / confirmed）
# --------------------------------------------------------------------------


def _as_dict(entity: CanonicalEntity | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(entity, Mapping):
        return dict(entity)
    return {
        "id": entity.id,
        "key": entity.key,
        "label": entity.label,
        "description": entity.description or "",
        "aliases": list(entity.aliases or []),
        "members": list(entity.members or []),
        "bindings": list(entity.bindings or []),
        "caliber_params": list(entity.caliber_params or []),
        "version": int(entity.version or 1),
        "created_by": entity.created_by,
        "updated_by": entity.updated_by,
        "created_at": entity.created_at.isoformat() if getattr(entity, "created_at", None) else None,
        "updated_at": entity.updated_at.isoformat() if getattr(entity, "updated_at", None) else None,
    }


def entity_to_dict(entity: CanonicalEntity | Mapping[str, Any], *, restricted: bool = False) -> dict[str, Any]:
    """序列化标准实体；受限视角（user 角色）隐去物理表名、列名、数据源内部 ID 与底层编码。"""
    data = _as_dict(entity)
    if not restricted:
        return data
    return {
        "id": data["id"],
        "key": data["key"],
        "label": data["label"],
        "description": data["description"],
        "aliases": list(data.get("aliases") or []),
        "members": [
            {"value": m.get("value"), "label": m.get("label"), "aliases": list(m.get("aliases") or [])}
            for m in (data.get("members") or []) if isinstance(m, Mapping)
        ],
        "version": data["version"],
    }


def confirmed_bindings(
    entity: CanonicalEntity | Mapping[str, Any],
    *,
    source_id: str | None = None,
    source_name: str | None = None,
    table_name: str | None = None,
) -> list[dict[str, Any]]:
    """返回已生效（verified / confirmed）的物理列绑定；proposed 与 rejected 一律排除。"""
    data = _as_dict(entity)
    out: list[dict[str, Any]] = []
    for item in data.get("bindings") or []:
        if not isinstance(item, Mapping) or item.get("status") not in ACTIVE_ENTITY_STATUSES:
            continue
        if source_id and str(item.get("source_id") or "") != source_id:
            continue
        if source_name and _norm_token(item.get("source_name")) != _norm_token(source_name):
            continue
        if table_name and _norm_token(item.get("table_name")) != _norm_token(table_name):
            continue
        out.append(dict(item))
    return out


def confirmed_caliber_params(
    entity: CanonicalEntity | Mapping[str, Any],
    *,
    workflow_id: str | None = None,
    card_id: str | None = None,
) -> list[dict[str, Any]]:
    """返回已生效（verified / confirmed）的口径输入参数映射。"""
    data = _as_dict(entity)
    out: list[dict[str, Any]] = []
    for item in data.get("caliber_params") or []:
        if not isinstance(item, Mapping) or item.get("status") not in ACTIVE_ENTITY_STATUSES:
            continue
        if workflow_id and str(item.get("workflow_id") or "") != workflow_id:
            continue
        if card_id and item.get("card_id") and str(item.get("card_id") or "") != card_id:
            continue
        out.append(dict(item))
    return out


def resolve_caliber_param(
    entity: CanonicalEntity | Mapping[str, Any],
    *,
    workflow_id: str,
    card_id: str | None = None,
) -> str | None:
    """把标准业务实体解析为目标口径的入口参数名（优先匹配指定 card_id，其次匹配同工作流通用参数）。"""
    matches = confirmed_caliber_params(entity, workflow_id=workflow_id)
    if card_id:
        for item in matches:
            if str(item.get("card_id") or "") == card_id:
                return str(item["param_name"])
    for item in matches:
        if not item.get("card_id") or card_id is None:
            return str(item["param_name"])
    return str(matches[0]["param_name"]) if matches else None


def translate_entity_value(
    entity: CanonicalEntity | Mapping[str, Any],
    raw_value: Any,
    *,
    from_source: str | None = None,
    to_source: str | None = None,
) -> dict[str, str] | None:
    """把源侧实体值确定性地翻译成标准值与目标源编码。

    - 有标准成员表（members 非空）时：严格按 from_source 的 source_codes、标准 value、label、aliases 精确匹配，
      再用 to_source 的 source_codes（缺省回退标准 value）给出 target_value；找不到返回 None，绝不模糊猜测。
    - 成员表为空（开放式编码域，如千万级订单号/批次号）时：仅当两侧数据源在该实体上均有已确认绑定时原样透传。
    """
    probe = str(raw_value or "").strip()
    if not probe:
        return None
    data = _as_dict(entity)
    members = [m for m in (data.get("members") or []) if isinstance(m, Mapping)]
    probe_norm = probe.lower()

    if members:
        matched: Mapping[str, Any] | None = None
        if from_source:
            src_norm = _norm_token(from_source)
            for member in members:
                codes = member.get("source_codes")
                if isinstance(codes, Mapping):
                    for k, v in codes.items():
                        if _norm_token(k) == src_norm and _norm_token(v) == probe_norm:
                            matched = member
                            break
                if matched is not None:
                    break
        if matched is None:
            for member in members:
                candidates = [member.get("value"), member.get("label"), *(member.get("aliases") or [])]
                codes = member.get("source_codes")
                if not from_source and isinstance(codes, Mapping):
                    candidates.extend(codes.values())
                if any(_norm_token(c) == probe_norm for c in candidates if c is not None):
                    matched = member
                    break
        if matched is None:
            return None
        canonical = str(matched.get("value") or "").strip()
        label = str(matched.get("label") or canonical).strip()
        target_value = canonical
        if to_source and isinstance(matched.get("source_codes"), Mapping):
            dst_norm = _norm_token(to_source)
            for k, v in matched["source_codes"].items():
                if _norm_token(k) == dst_norm and str(v or "").strip():
                    target_value = str(v).strip()
                    break
        return {"canonical": canonical, "label": label, "target_value": target_value}

    if from_source and not confirmed_bindings(data, source_name=from_source):
        return None
    if to_source and not confirmed_bindings(data, source_name=to_source):
        return None
    return {"canonical": probe, "label": probe, "target_value": probe}


def entities_bridging_sources(
    entities: Iterable[CanonicalEntity | Mapping[str, Any]],
    *,
    source_name: str,
    tables: Iterable[str] | None = None,
) -> list[dict[str, Any]]:
    """找出在当前数据源（及可选表集合）上有已确认绑定、并且同时桥接到其他数据源的标准实体。"""
    table_keys = {_norm_token(t) for t in tables} if tables is not None else None
    out: list[dict[str, Any]] = []
    for entity in entities:
        data = _as_dict(entity)
        active = confirmed_bindings(data)
        here = [
            b for b in active
            if _norm_token(b.get("source_name")) == _norm_token(source_name)
            and (table_keys is None or _norm_token(b.get("table_name")) in table_keys)
        ]
        if not here:
            continue
        peers = [b for b in active if _norm_token(b.get("source_name")) != _norm_token(source_name)]
        out.append({
            "entity_id": data["id"],
            "key": data["key"],
            "label": data["label"],
            "local_bindings": here,
            "peer_bindings": peers,
            "peer_sources": sorted({str(b["source_name"]) for b in peers}),
        })
    return out


# --------------------------------------------------------------------------
# 数据库读写与乐观锁审阅
# --------------------------------------------------------------------------


async def list_entities(
    session: AsyncSession,
    *,
    source_name: str | None = None,
    only_active_bindings: bool = False,
) -> list[CanonicalEntity]:
    rows = list((await session.execute(select(CanonicalEntity).order_by(CanonicalEntity.key.asc()))).scalars())
    if not source_name:
        return rows
    src_norm = _norm_token(source_name)
    matched: list[CanonicalEntity] = []
    for row in rows:
        bindings = confirmed_bindings(row) if only_active_bindings else (row.bindings or [])
        if any(isinstance(b, Mapping) and _norm_token(b.get("source_name")) == src_norm for b in bindings):
            matched.append(row)
    return matched


async def get_entity(session: AsyncSession, entity_id: str) -> CanonicalEntity | None:
    return await session.get(CanonicalEntity, str(entity_id or "").strip())


async def get_entity_by_key(session: AsyncSession, key: str) -> CanonicalEntity | None:
    clean = str(key or "").strip().lower()
    if not clean:
        return None
    return (
        await session.execute(select(CanonicalEntity).where(CanonicalEntity.key == clean).limit(1))
    ).scalar_one_or_none()


async def create_entity(
    session: AsyncSession,
    payload: Mapping[str, Any],
    *,
    actor: str | None = None,
) -> CanonicalEntity:
    fields = normalize_entity_payload(payload, partial=False, actor=actor)
    existing = await get_entity_by_key(session, fields["key"])
    if existing is not None:
        raise EntityConflict(f"标准实体标识「{fields['key']}」已存在", field="key")
    row = CanonicalEntity(
        key=fields["key"],
        label=fields["label"],
        description=fields["description"],
        aliases=fields["aliases"],
        members=fields["members"],
        bindings=fields["bindings"],
        caliber_params=fields["caliber_params"],
        version=1,
        created_by=actor,
        updated_by=actor,
    )
    session.add(row)
    await session.flush()
    return row


async def update_entity(
    session: AsyncSession,
    entity_id: str,
    payload: Mapping[str, Any],
    *,
    actor: str | None = None,
) -> CanonicalEntity:
    row = await get_entity(session, entity_id)
    if row is None:
        raise EntityNotFound(f"标准实体「{entity_id}」不存在")
    expected_version = payload.get("version")
    if expected_version is not None:
        if isinstance(expected_version, bool) or not isinstance(expected_version, int) or expected_version != row.version:
            raise EntityConflict(
                f"标准实体「{row.label}」已被其他人修改（当前版本 v{row.version}），请刷新后再保存",
                field="version",
            )
    fields = normalize_entity_payload(payload, partial=True, actor=actor)
    if "key" in fields and fields["key"] != row.key:
        clash = await get_entity_by_key(session, fields["key"])
        if clash is not None and clash.id != row.id:
            raise EntityConflict(f"标准实体标识「{fields['key']}」已存在", field="key")
        row.key = fields["key"]
    for attr in ("label", "description", "aliases", "members", "bindings", "caliber_params"):
        if attr in fields:
            setattr(row, attr, fields[attr])
    row.version = int(row.version or 1) + 1
    row.updated_by = actor
    await session.flush()
    return row


async def review_entity_item(
    session: AsyncSession,
    entity_id: str,
    *,
    target_kind: str,
    item_id: str,
    status: str,
    reason: str = "",
    actor: str | None = None,
    expected_version: int | None = None,
) -> CanonicalEntity:
    """审阅单条物理表列绑定或口径参数映射（确认 confirmed / 驳回 rejected / 验证通过 verified）。"""
    if target_kind not in ("binding", "caliber_param"):
        raise EntityInvalid("审阅对象类型须为 binding 或 caliber_param", field="target_kind")
    if status not in ENTITY_STATUSES:
        raise EntityInvalid(f"状态「{status}」无效，可选：{', '.join(ENTITY_STATUSES)}", field="status")
    row = await get_entity(session, entity_id)
    if row is None:
        raise EntityNotFound(f"标准实体「{entity_id}」不存在")
    if expected_version is not None and int(expected_version) != int(row.version or 1):
        raise EntityConflict(f"标准实体「{row.label}」版本已变更（当前 v{row.version}），请刷新后重试", field="version")

    attr = "bindings" if target_kind == "binding" else "caliber_params"
    items = [dict(x) for x in (getattr(row, attr) or []) if isinstance(x, Mapping)]
    found = False
    now = utcnow().isoformat()
    for item in items:
        if str(item.get("id") or "") == str(item_id or "").strip():
            item["status"] = status
            if reason.strip():
                item["reason"] = reason.strip()
            item["updated_at"] = now
            item["updated_by"] = actor
            found = True
            break
    if not found:
        raise EntityNotFound(f"标准实体「{row.label}」中不存在子项「{item_id}」")
    setattr(row, attr, items)
    row.version = int(row.version or 1) + 1
    row.updated_by = actor
    await session.flush()
    return row


async def delete_entity(session: AsyncSession, entity_id: str) -> bool:
    row = await get_entity(session, entity_id)
    if row is None:
        return False
    await session.delete(row)
    await session.flush()
    return True
