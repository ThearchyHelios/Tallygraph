"""字段基线 B（K05）：**首次接受导入**那一刻被接受的字段值，按 metric_id + field_path 存成不可变工件。

**B / L / R 是三个东西。**

- **B**（baseline）是「上一次被接受的导入值」：导入被接受、写进初始草稿图的那一份。它只由「接受一次导入」推进，
  不随人工编辑变。这份取值只从**当时落进图里的值**取，不拿草稿现在长什么样倒填历史。
- **L**（live）是当前人工维护的内容：画布和草稿页上的字段操作（K03 的 operations）、整份换指标，改的都是 L。
  改 L 一个字都不动 B。
- **R**（re-import candidate）是新导入的候选，K11 才做；这里只把 B 存下来，让那时候有真基线可比。

**为什么是工件不是一张表。** B 是一份「当时是什么」的只读材料：按内容寻址存进 app/core/artifact_store，草稿 payload
只记一份引用（id + 版本 + 收了哪些指标、几个字段）。工件不可变、同一份材料只存一次，读的时候复验哈希——够用，
不用另建一套文档版本平台。

**为什么不能只存哈希。** 哈希只说明「还是那一份」，说不出内容是什么；重导入要比对的是值本身（哪个字段、被接受成
什么、对应哪段原文）。所以工件里**逐字段存值**（数组、引用照原样），每一格另带自己的值哈希。

**旧资产一律 baseline_unknown。** 保存基线之前建的草稿没有这份引用：当时的导入值是什么已经无法还原，不能拿它
现在的内容冒充 B（见 status_of）。工件在、但读不回来（被删掉，或内容与哈希不符）同样算 baseline_unknown，
readable=false：**有引用不等于真的有基线**，也不能在读草稿时把它报成 known。字段级 B 也是「按字段推进」的前提：
只有 accepted_revision 记着它是在哪一版被接受的。

**known 不等于完整（coverage）。** 首次接受导入收的是整张卡，逐格都有值，覆盖是 complete。之后按字段推进
（K11 的 advance）常常只覆盖卡上的一部分：`coverage.status` 因此是 partial，另附每条指标覆盖了几格、还缺哪些格、
哪几条指标整条没有基线。**不能因为有一份读得回来的 B 就把整张卡说成完整 known**——有没有基线看 status，
是不是整张都有看 coverage。
"""
from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from typing import Any

logger = logging.getLogger(__name__)

#: 字段基线工件在工件库里的种类与格式版本
KIND = "caliber_field_baseline"
VERSION = 1
#: B 里除了「能按字段改的那些」之外另收的一项：引用原文是导入接受值的一部分（证据归属，改不了但要有基线）
CITATIONS_FIELD = "/definition/citations"

#: payload 里放引用的键（当前 B）和它的历史（每次接受导入追加一条）
REF_KEY = "field_baseline"
HISTORY_KEY = "field_baseline_history"


def accepted_card(graph: Any) -> tuple[str, list[dict[str, Any]]]:
    """这份图里「这次被接受的导入」落在哪张卡上：(card_id, 卡上的指标)。

    认法和读接口的权威卡一致（见 app/api/ledger.py 的 editable_cards / draft_card）：只认源头口径卡
    （metrics 节点、没有 caliber_from）；只有一张时就是它，多张时优先下游契约声明的那一张（contract.metrics_from
    只写了一张），都认不出时取第一张可写的。一张都没有时 ("", [])——调用方据此照实说没有基线，不猜。
    """
    cards: list[tuple[str, list[dict[str, Any]]]] = []
    referenced = ""
    for node in (graph or {}).get("nodes") or []:
        if not isinstance(node, Mapping):
            continue
        config = (node.get("data") or {}).get("config") if isinstance(node.get("data"), Mapping) else None
        config = config if isinstance(config, Mapping) else {}
        if node.get("type") == "metrics" and config.get("caliber_from") in (None, "", {}):
            metrics = [dict(m) for m in config.get("metrics") or [] if isinstance(m, Mapping) and m.get("id")]
            cards.append((str(node.get("id") or ""), metrics))
        contract = config.get("contract")
        sources = contract.get("metrics_from") if isinstance(contract, Mapping) else None
        ids = [sources] if isinstance(sources, str) else [s for s in sources or [] if isinstance(s, str)]
        if len(ids) == 1:
            referenced = ids[0]
    if not cards:
        return "", []
    for card_id, metrics in cards:
        if referenced and card_id == referenced:
            return card_id, metrics
    return cards[0]


def field_paths() -> tuple[str, ...]:
    """按字段记 B 时收哪些路径：能按字段改的那一套（K03 的白名单，和界面同一份）+ 引用原文。

    直接取 `app/ledger/changes.FIELDS` 的键，不在这儿另抄一份——两处一旦漂移，人工改得动的字段和记了基线的字段
    就对不上了。排序只为了让同一份材料每次都长成同一个规范 JSON。
    """
    from app.ledger import changes

    return (*sorted(changes.FIELDS), CITATIONS_FIELD)


def value_at(metric: Any, path: str) -> Any:
    """一条指标上某个字段路径的值；路径上没有这个键时抛 KeyError（调用方按「没这个字段」处理）。"""
    if not isinstance(metric, Mapping):
        raise KeyError(path)
    segments = [segment for segment in str(path).split("/") if segment]
    node: Any = metric
    for segment in segments:
        if not isinstance(node, Mapping) or segment not in node:
            raise KeyError(path)
        node = node[segment]
    return node


def fields_of(metric: Any, *, paths: Iterable[str] | None = None) -> dict[str, dict[str, Any]]:
    """一条指标被接受的字段值：{field_path: {"value": …, "hash": …}}。

    没有这个字段就不写进去（「没声明」和「值是空」是两件事，见 changes 的四种写法）。值原样深拷贝一份：
    数组、引用（citations 里的 chunk/document_id/quote/span）都按当时的样子留着，重导入要有东西可比。
    """
    import copy

    from app.core.artifact_store import canonical_json, content_hash

    wanted = tuple(paths) if paths is not None else field_paths()
    out: dict[str, dict[str, Any]] = {}
    for path in wanted:
        try:
            value = value_at(metric, path)
        except KeyError:
            continue
        kept = copy.deepcopy(value)
        out[path] = {"value": kept, "hash": content_hash(canonical_json(kept))}
    return out


def document(*, metrics: Iterable[Any], card_id: str, workflow_id: str, accepted_revision: int,
             source_snapshot: Mapping[str, Any] | None = None, accepted_at: str = "",
             accepted_by: str | None = None) -> dict[str, Any]:
    """一份字段基线工件的内容：逐指标的逐字段值 + 它是在哪一版被接受的 + 来源快照引用。

    只收被接受进图里的那份指标（调用方传图上的指标），不收草稿现在的内容；`source_snapshot` 是抽取那一刻存的
    来源快照引用（docdraft.snapshot_ref）——B 和它连着，将来重导入要拿它还原当时的原文。
    """
    items: dict[str, Any] = {}
    metric_ids: list[str] = []
    total = 0
    for metric in metrics if isinstance(metrics, (list, tuple)) else []:
        if not isinstance(metric, Mapping):
            continue
        metric_id = str(metric.get("id") or "").strip()
        if not metric_id:
            continue
        fields = fields_of(metric)
        items[metric_id] = {"name": str(metric.get("name") or ""), "fields": fields,
                            "field_count": len(fields),
                            "expected_fields": sorted(fields), "missing_fields": []}
        metric_ids.append(metric_id)
        total += len(fields)
    # 首次接受导入收的是**整张卡**的指标，逐格都有值：覆盖是完整的。之后按字段推进（advance）会把它降成 partial。
    return {"kind": KIND, "version": VERSION, "card_id": str(card_id or ""),
            "workflow_id": str(workflow_id or ""), "accepted_revision": int(accepted_revision),
            "accepted_at": str(accepted_at or ""), "accepted_by": accepted_by,
            "source_snapshot": dict(source_snapshot) if isinstance(source_snapshot, Mapping) else None,
            "metric_ids": metric_ids, "metrics": items, "field_count": total,
            "coverage": {"status": "complete",
                         "metrics": {mid: {"covered": int(item["field_count"]),
                                           "expected": int(item["field_count"]), "missing": []}
                                     for mid, item in items.items()},
                         "uncovered_metric_ids": []}}


async def put_metrics(*, metrics: Iterable[Any], card_id: str, workflow_id: str, accepted_revision: int,
                      source_snapshot: Mapping[str, Any] | None = None, draft_id: str = "",
                      accepted_at: str = "", accepted_by: str | None = None) -> dict[str, Any] | None:
    """把这一份被接受的字段值存成不可变工件，返回 payload 里要记的引用；落库失败返回 None。

    落库失败不让草稿失败：草稿照出，但没有引用就等于 baseline_unknown（照实说「当时的导入值还原不出来」），
    不假装有基线。引用里的 content_hash 就是工件 id（内容寻址，同一份材料只存一次）。
    """
    from app.db.base import utcnow

    made = document(metrics=metrics, card_id=card_id, workflow_id=workflow_id,
                    accepted_revision=accepted_revision, source_snapshot=source_snapshot,
                    accepted_at=accepted_at or utcnow().isoformat(), accepted_by=accepted_by)
    if not made["metric_ids"]:
        return None
    try:
        from app.core.artifact_store import put_json

        artifact_id = await put_json(made, kind=KIND, node_id=draft_id or None,
                                     meta={"draft_id": draft_id, "card_id": str(card_id or ""),
                                           "accepted_revision": int(accepted_revision),
                                           "field_baseline_version": VERSION})
    except Exception:  # noqa: BLE001 - 基线落库失败照实记「没有基线」，不把整份草稿判死
        logger.exception("字段基线落库失败（草稿 %s）", draft_id)
        return None
    return {"artifact_id": artifact_id, "kind": KIND, "version": VERSION,
            "content_hash": artifact_id, "card_id": made["card_id"],
            "workflow_id": made["workflow_id"], "accepted_revision": int(accepted_revision),
            "accepted_at": made["accepted_at"], "metric_ids": list(made["metric_ids"]),
            "field_count": made["field_count"],
            "source_snapshot_artifact_id": str((source_snapshot or {}).get("artifact_id") or "")
            if isinstance(source_snapshot, Mapping) else ""}


async def advance(*, ref: Any, updates: Mapping[str, Mapping[str, Any]], card_id: str = "",
                  workflow_id: str = "", accepted_revision: int, draft_id: str = "",
                  source_snapshot: Mapping[str, Any] | None = None, accepted_at: str = "",
                  accepted_by: str | None = None, card_metric_ids: Iterable[str] | None = None,
                  ) -> dict[str, Any] | None:
    """**按字段**推进 B（K11）：把这次被接受的字段值并进当前基线，存成一件新的不可变工件，交回新引用。

    B 是「上一次被接受的导入值」：只有**接受**的字段才往前走，没提到的（含未接受的冲突）原样留着。传入的 updates 是
    {metric_id: {"name": …, "fields": {field_path: 值}, "expected_fields": [这条指标在卡上声明的字段路径]}}——
    值原样深拷贝、逐格另算值哈希（和 fields_of 同一套），所以重导入仍有东西可比。

    **读不回来的时候从空开始。** 老草稿没有引用、工件被删或被改，`read` 都还原不出 B；这时不能拿当前内容倒填历史，
    就建一份**只含这次明确接受的那几格**的新基线：其余格仍旧没有基线，下次重导入会按「新增候选 / 需要人工对齐」处理。
    这正是「baseline_unknown 必须人工对齐」的落点：先有人的接受，才有这一格的历史。

    **覆盖照实记（coverage）。** 一次按字段的接受多半只覆盖卡上的一部分：`expected_fields` 说这条指标在卡上总共
    声明了哪些格、`card_metric_ids` 说卡上总共有哪几条指标。没覆盖到的格和整条没有基线的指标都记下来，
    `coverage.status` 因此是 partial——**不能把整张卡伪装成完整 known**。首次接受导入（document/put_metrics）
    收的是整张卡，那才是 complete。

    **同一份材料只存一份。** 工件按内容寻址：同样的接受重放两次得到同一个工件 id，B 不会因为重放而多出一份。
    落库失败返回 None：调用方照实说基线没推进，不假装有（调用方据此让这次接受整体不落库）。
    """
    import copy

    from app.core.artifact_store import canonical_json, content_hash, put_json
    from app.db.base import utcnow

    try:
        made = read(ref) if ref else None
    except Exception:  # noqa: BLE001 - 工件被改过（哈希不符）也算读不出来：从这次接受的字段重建
        made = None
    base = copy.deepcopy(dict(made)) if isinstance(made, Mapping) and made.get("kind") == KIND else None
    metrics: dict[str, Any] = dict(base.get("metrics")) if base and isinstance(base.get("metrics"), dict) else {}
    order: list[str] = [str(mid) for mid in (base or {}).get("metric_ids") or []] if base else []
    for raw_id, payload in (updates or {}).items():
        metric_id = str(raw_id or "").strip()
        if not metric_id:
            continue
        entry = metrics.get(metric_id)
        entry = dict(entry) if isinstance(entry, Mapping) else {"name": "", "fields": {}}
        fields = dict(entry.get("fields")) if isinstance(entry.get("fields"), dict) else {}
        name = str((payload or {}).get("name") or entry.get("name") or "")
        if name:
            entry["name"] = name
        for path, value in ((payload or {}).get("fields") or {}).items():
            kept = copy.deepcopy(value)
            fields[str(path)] = {"value": kept, "hash": content_hash(canonical_json(kept))}
        # 这一格有没有基线、这条指标在卡上还差哪些格：按字段推进之后 coverage 会降成 partial
        expected = payload.get("expected_fields") if isinstance(payload, Mapping) else None
        expected_set = {str(p) for p in expected} if isinstance(expected, (list, tuple)) else set(fields)
        expected_set |= set(fields)
        entry["fields"], entry["field_count"] = fields, len(fields)
        entry["expected_fields"] = sorted(expected_set)
        entry["missing_fields"] = sorted(expected_set - set(fields))
        metrics[metric_id] = entry
        if metric_id not in order:
            order.append(metric_id)
    total = sum(int((item or {}).get("field_count") or 0) for item in metrics.values())
    uncovered = [str(mid) for mid in (card_metric_ids or []) if str(mid) not in metrics]
    coverage_metrics = {
        mid: {"covered": len((item or {}).get("fields") or {}),
              "expected": len((item or {}).get("expected_fields") or (item or {}).get("fields") or {}),
              "missing": list((item or {}).get("missing_fields") or [])}
        for mid, item in metrics.items()
    }
    partial = bool(uncovered) or any(entry["missing"] for entry in coverage_metrics.values())
    document = {
        "kind": KIND, "version": VERSION,
        "card_id": str(card_id or (base or {}).get("card_id") or ""),
        "workflow_id": str(workflow_id or (base or {}).get("workflow_id") or ""),
        "accepted_revision": int(accepted_revision), "accepted_at": str(accepted_at or utcnow().isoformat()),
        "accepted_by": accepted_by,
        "source_snapshot": dict(source_snapshot) if isinstance(source_snapshot, Mapping)
        else (base or {}).get("source_snapshot"),
        "metric_ids": order, "metrics": metrics, "field_count": total,
        "coverage": {"status": "partial" if partial else "complete", "metrics": coverage_metrics,
                     "uncovered_metric_ids": uncovered},
    }
    try:
        artifact_id = await put_json(document, kind=KIND, node_id=draft_id or None,
                                     meta={"draft_id": draft_id, "card_id": document["card_id"],
                                           "accepted_revision": int(accepted_revision),
                                           "field_baseline_version": VERSION, "advanced": True,
                                           "coverage": document["coverage"]["status"]})
    except Exception:  # noqa: BLE001 - 推进失败照实说没有推进，调用方让整次接受不落库
        logger.exception("字段基线推进失败（草稿 %s）", draft_id)
        return None
    return {"artifact_id": artifact_id, "kind": KIND, "version": VERSION,
            "content_hash": artifact_id, "card_id": document["card_id"],
            "workflow_id": document["workflow_id"], "accepted_revision": int(accepted_revision),
            "accepted_at": document["accepted_at"], "metric_ids": list(order), "field_count": total,
            "coverage": document["coverage"],
            "source_snapshot_artifact_id": str((source_snapshot or {}).get("artifact_id") or "")
            if isinstance(source_snapshot, Mapping) else ""}


def coverage_of(doc: Any) -> dict[str, Any]:
    """这份 B 覆盖到哪一步：complete（卡上每条指标的每一格都有基线）/ partial（还有没基线的格或整条指标）。

    老工件没有 coverage 这一段（加它之前 put_metrics 收的是整张卡）时按 complete 处理，不 backfill、不猜。
    返回 {status, metrics: {mid: {covered, expected, missing}}, uncovered_metric_ids}。
    """
    if not isinstance(doc, Mapping):
        return {"status": "unknown", "metrics": {}, "uncovered_metric_ids": []}
    kept = doc.get("coverage")
    if isinstance(kept, Mapping) and kept.get("status"):
        return {"status": str(kept["status"]), "metrics": dict(kept.get("metrics") or {}),
                "uncovered_metric_ids": [str(mid) for mid in kept.get("uncovered_metric_ids") or []]}
    metrics = doc.get("metrics") if isinstance(doc.get("metrics"), dict) else {}
    out: dict[str, Any] = {}
    for mid, entry in metrics.items():
        count = len((entry or {}).get("fields") or {})
        out[str(mid)] = {"covered": count, "expected": count, "missing": []}
    return {"status": "complete", "metrics": out, "uncovered_metric_ids": []}


def read(ref: Any) -> dict[str, Any] | None:
    """按引用读回那份基线工件（load 会复验哈希）。引用不对、工件不在或被改过时返回 None / 抛 ValueError 由调用方定。"""
    from app.core.artifact_store import load

    artifact_id = ref.get("artifact_id") if isinstance(ref, Mapping) else None
    if not isinstance(artifact_id, str) or not artifact_id:
        return None
    made = load(artifact_id)
    if not isinstance(made, dict) or made.get("kind") != KIND:
        return None
    return made


def values(ref: Any, metric_id: str | None = None) -> dict[str, Any]:
    """按引用读出字段值：不给 metric_id 时是 {metric_id: {field_path: 值}}，给了就是那一条的 {field_path: 值}。

    读不回来（没有引用、工件不在了）返回空 dict——「读不到 B」由 status_of 说，不在这里编一份出来。
    """
    made = read(ref)
    if made is None:
        return {}
    items = made.get("metrics") if isinstance(made.get("metrics"), dict) else {}
    if metric_id is not None:
        entry = items.get(str(metric_id)) or {}
        fields = entry.get("fields") if isinstance(entry, dict) else {}
        return {str(path): (cell or {}).get("value")
                for path, cell in (fields or {}).items() if isinstance(cell, Mapping)}
    return {str(mid): {str(path): (cell or {}).get("value")
                       for path, cell in ((entry or {}).get("fields") or {}).items()
                       if isinstance(cell, Mapping)}
            for mid, entry in items.items()}


def history(payload: Any) -> list[dict[str, Any]]:
    """这份草稿接受过的导入基线，按先后（每次接受追加一条；当前 B 是最后一条）。"""
    if not isinstance(payload, Mapping):
        return []
    kept = payload.get(HISTORY_KEY)
    return [dict(item) for item in kept if isinstance(item, Mapping)] if isinstance(kept, list) else []


def ref_of(payload: Any) -> dict[str, Any] | None:
    """这份草稿当前的字段基线引用；没有就是 None。"""
    ref = payload.get(REF_KEY) if isinstance(payload, Mapping) else None
    return dict(ref) if isinstance(ref, Mapping) and ref.get("artifact_id") else None


def status_of(payload: Any) -> dict[str, Any]:
    """这份草稿的字段基线是什么状态：known（有**读得回来**的接受导入值）/ baseline_unknown（没有或读不回来）。

    **有引用不等于有基线，也不等于验证通过。** 这里会把工件真的读一遍（load 会复验哈希）：工件不在、内容被改过、
    或工件的种类不对，都照实说 baseline_unknown 并把 readable 标成 false——「工件坏了」不能报成 known，
    也不能在读草稿时抛出去把页面弄崩。老草稿（保存基线之前起草的）同样 baseline_unknown：不能拿它现在的内容
    冒充 B，也不能因为「读不到」就当通过。

    **known 不等于完整。** 按字段推进（advance）出来的基线常常只覆盖卡上的一部分：`coverage` 逐条指标说清了
    覆盖了几格、还缺哪些格，`coverage.status` 是 complete / partial（老工件没有这一段按 complete 处理）。
    界面上「这条草稿有没有基线」看 status，「这张卡是不是整张都有基线」看 coverage——两件事，不能混。
    """
    ref = ref_of(payload)
    stored = payload.get(REF_KEY) if isinstance(payload, Mapping) else None
    if ref is None and isinstance(stored, Mapping) and not stored.get("artifact_id") and stored.get("status"):
        # 已经是一份状态（落库失败时写下的那种）：原样交回，不重算、不补字段
        return dict(stored)
    if ref is None:
        return {"status": "baseline_unknown", "readable": False,
                "coverage": {"status": "unknown", "metrics": {}, "uncovered_metric_ids": []},
                "reason": "这份草稿没有字段基线（保存基线之前起草的，或这次落库失败）：当时被接受的导入值无法还原，"
                          "不能拿当前内容当作 B"}
    try:
        made = read(ref)
    except Exception as e:  # noqa: BLE001 - 工件被改过（哈希不符）也算读不出来，照实说，不让读草稿失败
        logger.warning("字段基线工件读不出来（%s）：%s", ref.get("artifact_id"), e)
        made = None
    if not isinstance(made, Mapping):
        return {"status": "baseline_unknown", "readable": False, "artifact_id": ref["artifact_id"],
                "version": ref.get("version") or VERSION, "accepted_revision": ref.get("accepted_revision"),
                "coverage": {"status": "unknown", "metrics": {}, "uncovered_metric_ids": []},
                "reason": "字段基线工件读不回来（不在、内容与哈希不符，或不是字段基线工件）："
                          "当时被接受的导入值无法还原，不能拿当前内容当作 B"}
    return {"status": "known", "readable": True, "artifact_id": ref["artifact_id"],
            "version": ref.get("version") or VERSION,
            "content_hash": str(ref.get("content_hash") or ref["artifact_id"]),
            "card_id": str(ref.get("card_id") or ""), "workflow_id": str(ref.get("workflow_id") or ""),
            "accepted_revision": ref.get("accepted_revision"), "accepted_at": ref.get("accepted_at") or "",
            "metric_ids": [str(mid) for mid in ref.get("metric_ids") or []],
            "field_count": ref.get("field_count") or 0, "coverage": coverage_of(made),
            "source_snapshot_artifact_id": str(ref.get("source_snapshot_artifact_id") or "")}


__all__ = ["CITATIONS_FIELD", "HISTORY_KEY", "KIND", "REF_KEY", "VERSION", "accepted_card", "advance",
           "coverage_of", "document", "field_paths", "fields_of", "history", "put_metrics", "read", "ref_of",
           "status_of", "value_at", "values"]
