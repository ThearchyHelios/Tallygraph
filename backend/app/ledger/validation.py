"""验证记录（K05）：试跑完成时**追加**一条，说清这次在什么定义和绑定上、拿什么输入、和什么独立期望比、结果是什么。

**试跑成功、绑定对齐、业务确认、正式可用是四件不同的事。** 这张表（caliber_validation_records）只记前两件里能被
证据支持的那部分：

- 有独立期望（人工确认 / 固定 fixture / 独立计算）并且逐条对得上 → `matched`；
- 有期望但对不上 → `mismatched`；
- **没有期望 → `observed`**：跑了、取了数，但没有可比的标准答案，一个字也不能说「通过」；证据不足（遮罩、缺指标、
  读不到查询快照）→ `unverified`；运行没成功 → `failed`。
- 样例匹配**不等于**正式批准：`matched` 只是「这一次和这份期望对得上」。批准是 ReviewDecision（app/ledger/review.py），
  要认证主体显式做，见 summarize 的 verdict。

**覆盖是读的时候现算的。** 定义指纹、绑定指纹、取数语义指纹（semantic_hash，含 SQL 和数据源）三者任一变了，旧记录
就**不再算覆盖当前版本**（covers_current=False）——不是把旧记录删掉或改写：历史照旧可追，只是不能拿它给新定义背书。
删掉语义字段（没验证过的业务描述）会改定义指纹，因此旧记录立刻失去覆盖，不会「因为缺了这段描述就升级成通过」（E15/R07）。
布局变更不改这些指纹，所以移动画布不会白白作废验证（4.1）。

**方法版本 1 的比较口径（写死在这里，随版本说清）：**

- **原值比较，不做隐式舍入**（R06）：直接比取数得到的那个值，不四舍五入、不截位、不归一百分号。数字按数值相等
  （2 与 2.0 同），布尔和数字不混，字符串逐字比。
- **全指标比较**：给了期望就要逐条给（卡片上的每条指标都得有），漏一条就报出来，不拿「只比了几条」当通过。
- **全约定形状比较**（R04/R05）：声明成标量的指标必须**恰好一行**。空结果（0 行）和多行都不是「取第一行就对」——
  0 行没有值，多行是标量取数有歧义，两种情况都算形状不满足。形状读不到（没有查询快照、被截断）时不判对，记 unverified。
- 运行期已判出来的约束问题（app/engine/metric_spec.contract_findings 的结论、no_data 等）一并记进这一行，不重复解释。

**追加与去重。** 一行 = 一次试跑的一次终态；同一 (草稿, trial_id, status) 只落一行（重复保存同一次终态不新增）。
晚结束的旧试跑即使不能替换当前 trial 指针，也照旧追加自己这一行：新旧记录都留着，读的时候按各自的定义/绑定依据
算覆盖。
"""
from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from sqlalchemy.exc import IntegrityError

logger = logging.getLogger(__name__)

#: 比较方法本身的版本。改了 compare 的语义（比较口径、形状规则）就 +1：记录里记着这一版，历史结果才知道是按哪一版判的。
METHOD_VERSION = 1
#: 这一版比较方法的一句话说明（接口原样交给界面和外部集成）
METHOD_NOTE = ("原值比较、不做隐式舍入；逐条比较卡上全部指标；声明成标量的指标必须恰好一行，"
               "空结果与多行都不算通过；形状判不了时记 unverified")

#: 记录的状态。**没有 pass / verified 这种状态**：验证通过与否是 summarize 按「覆盖 + 期望 + 审阅」现算的结论。
MATCHED = "matched"          # 有独立期望，逐条对得上，形状也满足
MISMATCHED = "mismatched"    # 有独立期望，至少一条对不上（含期望没覆盖全部指标、形状不满足）
OBSERVED = "observed"        # 跑了、取了数，但没有独立期望：不能算验证通过
UNVERIFIED = "unverified"    # 证据不足：遮罩、缺指标、形状判不了，或这次试跑没对着当前定义
FAILED = "failed"            # 运行没成功
STATUSES = (MATCHED, MISMATCHED, OBSERVED, UNVERIFIED, FAILED)

#: 期望来自哪里。**不能把当前 SQL 的结果抄一份当标准答案**：来源必须是独立于这次执行的一方。
EXPECTATION_SOURCES = ("human_confirmed", "fixture", "independent_computation")
DEFAULT_EXPECTATION_SOURCE = "human_confirmed"
#: 一次运行里已经被判出来的形状/取值问题（engine 的质量检查码）：一并记进记录，不另解释一遍
FINDING_CODES = ("metric_contract", "no_data", "value", "incomplete", "recompute")


# ==========================================================================
# 依据：这次验证绑的是哪一份定义和绑定
# ==========================================================================


def _card_config(graph: Any, card_id: Any) -> dict[str, Any]:
    for node in (graph or {}).get("nodes") or []:
        if isinstance(node, Mapping) and str(node.get("id")) == str(card_id):
            config = (node.get("data") or {}).get("config") if isinstance(node.get("data"), Mapping) else None
            return dict(config) if isinstance(config, Mapping) else {}
    return {}


def card_metrics(graph: Any, card_id: Any) -> list[dict[str, Any]]:
    """这张源头口径卡上的指标（有 id 的那些），顺序照图里。"""
    metrics = _card_config(graph, card_id).get("metrics")
    return [dict(m) for m in metrics if isinstance(m, Mapping) and m.get("id")] if isinstance(metrics, list) else []


def definition_hash(graph: Any, card_id: Any) -> str:
    """这张卡的定义指纹（data/caliber_identity：只按语义字段算）。和仓储、变更服务用的是同一份。"""
    from app.data.caliber_identity import definition_hash as hash_of

    config = _card_config(graph, card_id)
    return hash_of(str(config.get("caliber") or ""), str(config.get("caliber_version") or ""),
                   card_metrics(graph, card_id))


def semantic_fingerprint(graph: Any, card_id: Any) -> str:
    """这张卡及其上游执行依赖的指纹（含取数 SQL、数据源、入口绑定；不含布局）。读不出来时给空串，照实说不判。"""
    from app.data.caliber_identity import semantic_hash

    try:
        return semantic_hash(dict(graph or {}), str(card_id))
    except Exception:  # noqa: BLE001 - 指纹算不出来不该让一次试跑没了记录，照实留空
        logger.warning("口径「%s」的取数语义指纹算不出来", card_id, exc_info=True)
        return ""


def binding_fingerprints(graph: Any, card_id: Any) -> dict[str, str]:
    """这张卡的取数节点绑定指纹（K04：node_id → 绑定指纹）。没声明绑定、认不出的节点不进这里。"""
    from app.ledger import bindings

    try:
        return {str(node): str(value) for node, value in bindings.analyze(graph or {}, str(card_id)).fingerprints.items()}
    except Exception:  # noqa: BLE001 - 绑定判定出错时留空，不让它把记录弄没
        logger.warning("口径「%s」的绑定指纹算不出来", card_id, exc_info=True)
        return {}


def materials(graph: Any, card_id: Any, *, workflow_id: str = "") -> dict[str, Any]:
    """一次验证要绑定的全部依据：卡、定义指纹、绑定指纹、取数语义指纹（另带这份资产的工作流 id）。"""
    return {"workflow_id": str(workflow_id or ""), "card_id": str(card_id or ""),
            "definition_hash": definition_hash(graph, card_id),
            "binding_fingerprints": binding_fingerprints(graph, card_id),
            "semantic_hash": semantic_fingerprint(graph, card_id)}


def covers_current(row: Any, *, current: Mapping[str, Any]) -> bool:
    """这条记录还算不算覆盖**当前**定义和绑定：三样指纹都要对得上，而且三样都得有值（定义、绑定、取数语义）。

    布局变更不在这些指纹里，所以移动画布不作废验证；改了定义、改了 SQL、改了数据源或绑定就对不上了。
    **任一侧缺失都算不覆盖**：定义指纹、取数语义指纹少一个（算不出来、旧行没有、工件对不上）时一律 False——
    「缺证据」不得自动升级成覆盖。
    """
    if not isinstance(current, Mapping):
        return False
    recorded_definition = str(getattr(row, "definition_hash", "") or "")
    live_definition = str(current.get("definition_hash") or "")
    if not recorded_definition or not live_definition or recorded_definition != live_definition:
        return False
    if str(getattr(row, "workflow_id", "") or "") != str(current.get("workflow_id") or ""):
        return False
    if str(getattr(row, "card_id", "") or "") != str(current.get("card_id") or ""):
        return False
    if dict(getattr(row, "binding_fingerprints", None) or {}) != dict(current.get("binding_fingerprints") or {}):
        return False
    recorded_semantics = str(getattr(row, "semantic_hash", "") or "")
    live_semantics = str(current.get("semantic_hash") or "")
    if not recorded_semantics or not live_semantics or recorded_semantics != live_semantics:
        return False
    return True


# ==========================================================================
# 实际值：这次跑出来的每个指标的值和形状
# ==========================================================================


def declared_shapes(metrics: Iterable[Mapping[str, Any]]) -> dict[str, str | None]:
    """卡片上每条指标声明了什么结果形状（contract.shape）；没声明的是 None（不替它判）。"""
    out: dict[str, str | None] = {}
    for metric in metrics:
        if not isinstance(metric, Mapping) or not metric.get("id"):
            continue
        contract = metric.get("contract") if isinstance(metric.get("contract"), Mapping) else {}
        shape = contract.get("shape")
        out[str(metric["id"])] = shape if isinstance(shape, str) and shape.strip() else None
    return out


def _shape_of(*, shape: str | None, rows: int | None, truncated: bool) -> tuple[bool | None, list[str]]:
    """形状判不判得过：返回 (ok, 说明)。ok 是 None 表示判不了（没有查询快照）。"""
    if rows is None:
        return None, ["这次运行没有可核对的查询快照：形状判不了（不算通过）"]
    if rows == 0:
        return False, ["取数结果 0 行（空结果）：标量没有可取的数，不能当作取到了值"]
    if shape == "scalar":
        if truncated:
            return False, ["查询被截断，标量要求恰好一行这一条没法核对"]
        if rows != 1:
            return False, [f"标量指标要求取数结果恰好一行，实际为 {rows} 行：不拿第一行当结果"]
    return True, []


def actual_of(*, metrics: Sequence[Mapping[str, Any]], queries: Sequence[Mapping[str, Any]],
              card_metrics_: Sequence[Mapping[str, Any]], metric_ids: Sequence[str],
              links: Mapping[str, str] | None = None) -> dict[str, dict[str, Any]]:
    """这次运行交回的实际值：metric_id → {value, status, rows, masked, shape, shape_ok, findings}。

    metrics 是接口层读运行证据得到的那一份（app/api/ledger.py 的 _trial_metrics）。**指标记录本身不带取数节点**：
    真实运行的关联由调用方从封存的口径卡里认出来，通过 links（metric_id → 查询快照的键：工件 id 或取数节点 id）传进来；
    没有 links 时退回指标自带的 node_id（桩和测试用）。queries 每项要有 key（或 node_id）+ total_rows + truncated。

    按**行数、截断和引擎已判出来的约束结论**判形状——**不看第一行**。对不上任何快照的指标记 rows=None（形状判不了），
    照实记 unverified，不猜。
    """
    shape_of_metric = declared_shapes(card_metrics_)
    by_key: dict[str, Mapping[str, Any]] = {}
    for query in queries if isinstance(queries, Sequence) else []:
        if not isinstance(query, Mapping):
            continue
        for key in (query.get("key"), query.get("node_id")):
            # 一个节点跑过多次时后来的那份快照算数（指标取的是它最后一次交回的结果）
            if isinstance(key, str) and key:
                by_key[key] = query
    known = {str(m.get("id")): m for m in metrics if isinstance(m, Mapping) and m.get("id")}
    wanted_by_metric = dict(links or {})
    out: dict[str, dict[str, Any]] = {}
    for metric_id in metric_ids:
        item = known.get(str(metric_id))
        shape = shape_of_metric.get(str(metric_id))
        if item is None:
            out[str(metric_id)] = {"value": None, "status": "missing", "rendered": "", "rows": None,
                                   "masked": False, "truncated": False, "shape": shape, "shape_ok": None,
                                   "findings": ["这次运行没有交回这条指标的结果"]}
            continue
        key = wanted_by_metric.get(str(metric_id)) or item.get("node_id")
        query = by_key.get(str(key)) if isinstance(key, str) and key else None
        rows = query.get("total_rows") if isinstance(query, Mapping) else None
        rows = int(rows) if isinstance(rows, int) and not isinstance(rows, bool) else None
        truncated = bool(query.get("truncated")) if isinstance(query, Mapping) else False
        ok, shape_notes = _shape_of(shape=shape, rows=rows, truncated=truncated)
        findings = [str(check.get("detail")) for check in ((item.get("quality") or {}).get("checks") or [])
                    if isinstance(check, Mapping) and check.get("code") in FINDING_CODES and check.get("detail")]
        # 引擎判过「业务描述和取数条件的关系还没被证明」（semantic_unverified）时照实记下来：它是**待人工确认**的
        # 缺口，不是这次比较的结论——所以不并进 problems（那会把 matched 降成 unverified），但也不能被藏起来。
        semantic = any(isinstance(check, Mapping) and check.get("code") == "semantic_definition"
                       for check in ((item.get("quality") or {}).get("checks") or []))
        out[str(metric_id)] = {"value": item.get("value"), "status": str(item.get("status") or ""),
                               "rendered": item.get("rendered"), "rows": rows, "masked": bool(item.get("masked")),
                               "truncated": truncated, "shape": shape, "shape_ok": ok,
                               "semantic_unverified": semantic,
                               "findings": list(dict.fromkeys([*shape_notes, *findings]))}
    return out


# ==========================================================================
# 比较：期望 vs 实际
# ==========================================================================


def same_value(expected: Any, actual: Any) -> bool:
    """原值比较，不做隐式舍入（R06）：数字按数值相等（2 与 2.0 同），布尔与数字不混，其余按规范化 JSON 逐字比。"""
    from app.core.artifact_store import canonical_json

    if isinstance(expected, bool) != isinstance(actual, bool):
        return False
    if isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
        return bool(expected == actual)
    return canonical_json(expected) == canonical_json(actual)


def _label(metric_id: str, card_metrics_: Sequence[Mapping[str, Any]]) -> str:
    for metric in card_metrics_:
        if isinstance(metric, Mapping) and str(metric.get("id")) == str(metric_id):
            name = metric.get("name")
            return str(name) if name else str(metric_id)
    return str(metric_id)


def compare(*, expected: Mapping[str, Any] | None, actual: Mapping[str, Mapping[str, Any]],
            metric_ids: Sequence[str], card_metrics_: Sequence[Mapping[str, Any]],
            run_status: str = "succeeded", problems: Iterable[str] = ()) -> dict[str, Any]:
    """把独立期望和这次的实际值比一遍，返回 {status, problems, actual}。

    - 没有期望：只记 observed（有形状/取值问题也照实写进 problems），**不能**是 matched；
    - 有期望：逐条对，缺一条期望算 mismatched（全指标比较）；形状判不了算 unverified；对不上算 mismatched。
    """
    listed = list(dict.fromkeys(str(p) for p in problems))
    detail: dict[str, dict[str, Any]] = {str(mid): dict(actual.get(str(mid)) or {}) for mid in metric_ids}
    if run_status != "succeeded":
        status = FAILED if run_status == "failed" else UNVERIFIED
        return {"status": status, "problems": [*listed, "这次试运行没有成功，没有可比较的实际结果"],
                "actual": detail}
    if not metric_ids:
        # 一条可比对的指标都没有（找不到定义依据，或卡上本来就没有指标）：缺证据不得升级成通过
        return {"status": UNVERIFIED,
                "problems": list(dict.fromkeys([*listed, "这次没有可比对的指标：找不到这次试运行的定义依据，"
                                                        "不能算验证通过"])),
                "actual": detail}
    shape_bad = False
    unknown = False
    for metric_id in metric_ids:
        entry = detail[str(metric_id)]
        name = _label(str(metric_id), card_metrics_)
        for finding in entry.get("findings") or []:
            listed.append(f"指标「{name}」：{finding}")
        if entry.get("shape_ok") is False:
            shape_bad = True
        elif entry.get("shape_ok") is None:
            unknown = True
    if expected is None:
        return {"status": OBSERVED,
                "problems": list(dict.fromkeys([*listed, "这次试运行没有独立期望：只记 observed，不能算验证通过"])),
                "actual": detail}
    mismatched = False
    for metric_id in metric_ids:
        entry = detail[str(metric_id)]
        name = _label(str(metric_id), card_metrics_)
        if str(metric_id) not in expected:
            mismatched = True
            entry["expected"], entry["ok"] = None, False
            listed.append(f"期望没有覆盖指标「{name}」：希望逐条给全卡指标，不能只比其中几条")
            continue
        want = expected[str(metric_id)]
        entry["expected"] = want
        if entry.get("masked"):
            unknown = True
            entry["ok"] = None
            listed.append(f"指标「{name}」的值已遮罩：不能在这里判对得上")
            continue
        if entry.get("status") == "missing":
            unknown = True
            entry["ok"] = None
            listed.append(f"指标「{name}」这次没有结果：判不了")
            continue
        got = entry.get("value")
        if got is None and want is not None:
            mismatched = True
            entry["ok"] = False
            listed.append(f"指标「{name}」期望 {want!r}，实际没有取到值")
            continue
        ok = same_value(want, got)
        entry["ok"] = ok
        if not ok:
            mismatched = True
            listed.append(f"指标「{name}」对不上：期望 {want!r}，实际 {got!r}")
    if mismatched or shape_bad:
        return {"status": MISMATCHED, "problems": list(dict.fromkeys(listed)), "actual": detail}
    if unknown or listed:
        return {"status": UNVERIFIED, "problems": list(dict.fromkeys(listed)), "actual": detail}
    return {"status": MATCHED, "problems": [], "actual": detail}


# ==========================================================================
# 追加一行
# ==========================================================================


def expectation_of(expect: Any, *, card_metrics_: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
    """把接口交来的期望规范成要落库的那一份；给不出可用期望时返回 None。

    形状是 {"source": …, "metrics": {metric_id: 值}, "note": …, "sample_id": …, "sample_version": …}：值原样留着
    （比较按原值），来源记下来供人判断「这份期望是谁给的、凭什么」。
    """
    if not isinstance(expect, Mapping):
        return None
    metrics = expect.get("metrics")
    if not isinstance(metrics, Mapping) or not metrics:
        return None
    known = {str(m.get("id")) for m in card_metrics_ if isinstance(m, Mapping) and m.get("id")}
    kept = {str(mid): value for mid, value in metrics.items() if str(mid) in known}
    if not kept:
        return None
    source = expect.get("source")
    source = str(source) if isinstance(source, str) and source in EXPECTATION_SOURCES else DEFAULT_EXPECTATION_SOURCE
    out: dict[str, Any] = {"source": source, "metrics": kept}
    for key in ("note", "sample_id", "sample_version"):
        value = expect.get(key)
        if isinstance(value, str) and value.strip():
            out[key] = value.strip()
    return out


def expectation_hash(expectation: Mapping[str, Any] | None) -> str:
    """期望的内容指纹：这一行到底对的哪一份期望（改了期望再来一次，指纹不同，两条记录看得出来不是一回事）。"""
    from app.core.artifact_store import canonical_json, content_hash

    return content_hash(canonical_json(expectation)) if expectation else ""


async def append(*, draft_id: str, workflow_id: str | None, version: int, card_id: str, metric_ids: Sequence[str],
                 basis: Mapping[str, Any], inputs: Mapping[str, Any], inputs_hash: str, trial_id: str, run_id: str,
                 actor: str | None, status: str, problems: Sequence[str], expectation: Mapping[str, Any] | None,
                 actual: Mapping[str, Any], execution_hash: str = "", sample: Mapping[str, Any] | None = None,
                 origin: str = "trial") -> str | None:
    """追加一条验证记录；同一 (草稿, trial_id, status) 已经有一条时不再新增。

    新会话、独立事务：草稿 payload 的写入可能因为并发被拒，但**记录照旧要落**——晚结束的旧试跑也能被追踪。
    任何落库错误都只记警告：记录写不下不该让试跑本身失败。
    """
    from app.db.base import SessionLocal
    from app.db.models import CaliberValidationRecord

    if status not in STATUSES:
        status = UNVERIFIED
    sample = sample if isinstance(sample, Mapping) else {}
    try:
        async with SessionLocal() as session:
            existing = (await session.execute(
                select_record(draft_id, trial_id, status))).scalar_one_or_none()
            if existing is not None:
                return existing.id
            row = CaliberValidationRecord(
                draft_id=draft_id, workflow_id=workflow_id or None, version=int(version or 1),
                card_id=str(card_id or ""), metric_ids=[str(mid) for mid in metric_ids],
                definition_hash=str(basis.get("definition_hash") or ""),
                binding_fingerprints={str(k): str(v) for k, v in (basis.get("binding_fingerprints") or {}).items()},
                semantic_hash=str(basis.get("semantic_hash") or ""), execution_hash=str(execution_hash or ""),
                expectation=dict(expectation) if expectation else None, expectation_hash=expectation_hash(expectation),
                expectation_source=str((expectation or {}).get("source") or ""),
                sample_id=str(sample.get("id") or (expectation or {}).get("sample_id") or ""),
                sample_version=str(sample.get("version") or (expectation or {}).get("sample_version") or ""),
                inputs=dict(inputs or {}), inputs_hash=str(inputs_hash or ""), method_version=METHOD_VERSION,
                actual={str(mid): dict(entry) for mid, entry in (actual or {}).items()},
                status=status, problems=[str(p) for p in problems], trial_id=str(trial_id or ""),
                run_id=str(run_id or ""), actor=actor, origin=origin)
            session.add(row)
            try:
                await session.commit()
            except IntegrityError:  # noqa: F821 - 并发下同一 (草稿, trial_id, status) 先被别人写进去了
                await session.rollback()
                existing = (await session.execute(
                    select_record(draft_id, trial_id, status))).scalar_one_or_none()
                return existing.id if existing is not None else None
            return row.id
    except Exception:  # noqa: BLE001 - 记录写不下不该让试跑本身失败
        logger.exception("验证记录写不下（草稿 %s，试跑 %s）", draft_id, trial_id)
        return None


def select_record(draft_id: str, trial_id: str, status: str) -> Any:
    """同一 (草稿, trial_id, status) 的那一行（唯一键的认法，和表上的约束一致）。"""
    from sqlalchemy import select

    from app.db.models import CaliberValidationRecord

    return select(CaliberValidationRecord).where(
        CaliberValidationRecord.draft_id == draft_id,
        CaliberValidationRecord.trial_id == str(trial_id or ""),
        CaliberValidationRecord.status == status)


async def list_for_draft(session: Any, draft_id: str) -> list[Any]:
    """一份草稿的验证记录，新的在前。"""
    from sqlalchemy import select

    from app.db.models import CaliberValidationRecord

    return list((await session.execute(
        select(CaliberValidationRecord).where(CaliberValidationRecord.draft_id == draft_id)
        .order_by(CaliberValidationRecord.created_at.desc(), CaliberValidationRecord.id.desc()))).scalars())


async def get_record(session: Any, draft_id: str, record_id: str) -> Any:
    """这份草稿上的某一条记录；不是它的一律当不存在（不跨草稿读）。"""
    from sqlalchemy import select

    from app.db.models import CaliberValidationRecord

    return (await session.execute(select(CaliberValidationRecord).where(
        CaliberValidationRecord.id == record_id,
        CaliberValidationRecord.draft_id == draft_id))).scalar_one_or_none()


async def find_record(session: Any, draft_id: str, trial_id: str) -> Any:
    """这次试运行留下的那条记录；没有就是 None。

    同一个身份（trial_id / run_id）可能有多条行（同一次试运行在不同状态下各落一行，比如先 observed、后有期望
    再跑成 matched）：给**最新的一条**。**不给别的试运行的记录**，也不在找不到时退回「整份草稿最新的一条」。
    """
    from sqlalchemy import select

    from app.db.models import CaliberValidationRecord

    if not trial_id:
        return None
    rows = (await session.execute(
        select(CaliberValidationRecord).where(
            CaliberValidationRecord.draft_id == draft_id,
            CaliberValidationRecord.trial_id == str(trial_id))
        .order_by(CaliberValidationRecord.created_at.desc(), CaliberValidationRecord.id.desc())
        .limit(1))).scalars()
    return rows.first()


# ==========================================================================
# 读：K06 要的那份契约
# ==========================================================================


def as_dict(row: Any, *, current: Mapping[str, Any] | None = None, review: Mapping[str, Any] | None = None
            ) -> dict[str, Any]:
    """一条记录的对外形状：期望、实际、状态、覆盖判断、审阅，一次给全。

    `covers_current` 是拿记录里的依据和**当前**定义/绑定比出来的（current 不给时是 None：调用方没有当前依据，
    不替它猜）。`review` 是这条记录上最新的一次审阅决定（没有就是 None），**样例匹配不会自动变成审阅通过**。
    """
    covered = covers_current(row, current=current) if isinstance(current, Mapping) else None
    return {
        "id": row.id, "draft_id": row.draft_id, "workflow_id": row.workflow_id, "version": row.version,
        "card_id": row.card_id, "metric_ids": [str(mid) for mid in row.metric_ids or []],
        "definition_hash": row.definition_hash, "binding_fingerprints": dict(row.binding_fingerprints or {}),
        "semantic_hash": row.semantic_hash, "execution_hash": row.execution_hash,
        "method": {"version": row.method_version, "note": METHOD_NOTE},
        "sample": {"id": row.sample_id, "version": row.sample_version},
        "inputs": dict(row.inputs or {}), "inputs_hash": row.inputs_hash,
        "expected": ({"source": row.expectation_source or DEFAULT_EXPECTATION_SOURCE,
                      "metrics": dict((row.expectation or {}).get("metrics") or {}),
                      "note": (row.expectation or {}).get("note"),
                      "hash": row.expectation_hash}
                     if row.expectation else None),
        "actual": {str(mid): dict(entry) for mid, entry in (row.actual or {}).items()},
        "status": row.status, "problems": [str(p) for p in row.problems or []],
        "run_id": row.run_id, "trial_id": row.trial_id, "actor": row.actor, "origin": row.origin,
        "created_at": row.created_at.isoformat() if getattr(row, "created_at", None) else None,
        "covers_current": covered, "review": dict(review) if isinstance(review, Mapping) else None,
    }


def summarize(rows: Sequence[Any], *, current: Mapping[str, Any] | None = None,
              reviews: Mapping[str, Mapping[str, Any]] | None = None) -> dict[str, Any]:
    """一份草稿的验证总情况（草稿详情和验证列表共用这一份）。

    结论（verdict）只在「有覆盖当前定义/绑定的记录」的基础上说：

    | 情况 | verdict |
    | --- | --- |
    | 没有记录 | none |
    | 有记录但没有一条覆盖当前定义/绑定 | stale |
    | 覆盖的那条最新的状态是 mismatched / failed | mismatched / failed |
    | 覆盖的那条没有独立期望 | observed（执行了，没标准答案） |
    | 覆盖的那条是 unverified 或 matched 但**没有认证审阅** | unverified（样例匹配不是批准） |
    | 覆盖的那条 matched 且有覆盖它、**且范围盖住全部指标**的 confirmed 审阅 | verified |
    | 覆盖的那条被驳回，或只确认了其中一部分 | rejected / unverified（`unconfirmed` 列出还没确认的指标） |

    另外两段：`confirmed` 是这次被确认到的指标（确认范围），`unconfirmed` 是这条记录覆盖但还没被确认的指标——
    **空范围或部分范围都不算整张卡通过**（卡级 verified 要求确认范围覆盖记录的全部指标）。
    """
    reviews = reviews or {}
    counts = {status: 0 for status in STATUSES}
    covering: list[Any] = []
    for row in rows:
        counts[row.status] = counts.get(row.status, 0) + 1
        if isinstance(current, Mapping) and covers_current(row, current=current):
            covering.append(row)
    latest = (covering or list(rows))[0] if (covering or rows) else None
    gaps = _semantic_gaps(latest)
    note = (f"；其中 {len(gaps)} 条指标的语义说明仍待人工确认（受控绑定对齐不等于业务描述已被证明）"
            if gaps else "")
    review = reviews.get(latest.id) if latest is not None else None
    decision = str((review or {}).get("decision") or "") if isinstance(review, Mapping) else ""
    review_covers = bool((review or {}).get("covers_current")) if isinstance(review, Mapping) else False
    # 「确认了哪几条」：审阅的范围要和这份记录覆盖的指标对上；空范围或只确认了一部分，都不算整张卡被确认
    covered_ids = [str(mid) for mid in getattr(latest, "metric_ids", None) or []] if latest is not None else []
    confirmed_scope = [str(mid) for mid in (review or {}).get("scope") or []] \
        if isinstance(review, Mapping) and decision == "confirmed" and review_covers else []
    unconfirmed = [mid for mid in covered_ids if mid not in confirmed_scope]
    fully_confirmed = bool(covered_ids) and not unconfirmed
    if latest is None:
        verdict, reason = "none", "这份草稿还没有试运行留下的验证记录"
    elif not covering:
        verdict = "stale"
        reason = ("现有的验证记录都不是对着当前定义和绑定做出来的（定义、取数或绑定改过，或者现在读不到这份取数图）："
                  "旧记录留着可追，但不能算覆盖这一版，请重新试运行")
    elif review_covers and decision == "rejected":
        verdict, reason = "rejected", "有覆盖当前版本的记录，但它被审阅驳回了"
    elif review_covers and decision == "confirmed":
        if latest.status != MATCHED:
            verdict = "unverified"
            reason = f"审阅确认的是「{latest.status}」状态下的这一次：没有一条 matched 的记录覆盖当前版本"
        elif not fully_confirmed:
            verdict = "unverified"
            reason = (f"审阅只确认了 {len(confirmed_scope)} 条，还有 {len(unconfirmed)} 条没被确认"
                      "（确认范围要覆盖这份记录的全部指标才算整张卡通过）")
        else:
            verdict, reason = "verified", "有独立期望的记录覆盖当前版本，并且有认证主体确认过全部指标" + note
    elif latest.status == MATCHED:
        verdict = "unverified"
        reason = "有独立期望且对得上，但还没有认证审阅：样例匹配不等于正式批准"
    elif latest.status == MISMATCHED:
        verdict, reason = "mismatched", "有独立期望，但这一次没对上"
    elif latest.status == FAILED:
        verdict, reason = "failed", "这一次试运行没有成功"
    elif latest.status == OBSERVED:
        verdict, reason = "observed", "这次试运行执行了、也取到了数，但没有独立期望：不能算验证通过"
    else:
        verdict, reason = "unverified", "证据不足（遮罩、缺指标或形状判不了）：不能算验证通过"
    return {"verdict": verdict, "status": latest.status if latest is not None else "none",
            "covers_current": bool(covering), "record_id": latest.id if latest is not None else None,
            "review": dict(review) if isinstance(review, Mapping) else None,
            "confirmed": confirmed_scope, "unconfirmed": unconfirmed,
            "counts": {"total": len(rows), "covering": len(covering), **counts},
            "reason": reason, "semantic_gaps": gaps,
            "method": {"version": METHOD_VERSION, "note": METHOD_NOTE}}


def _semantic_gaps(latest: Any) -> list[str]:
    """这次（覆盖当前版本的那条）记录里，哪些指标的语义说明仍待人工确认（R07：受控绑定对齐不等于业务描述被证明）。

    它们不改变这一次的比较结论（该记 matched 还是 matched），但要能被人看见——「已确认」也不等于「这段描述被证明」。"""
    if latest is None:
        return []
    actual = getattr(latest, "actual", None)
    return sorted(str(mid) for mid, entry in (actual or {}).items()
                  if isinstance(entry, Mapping) and entry.get("semantic_unverified"))


__all__ = ["DEFAULT_EXPECTATION_SOURCE", "EXPECTATION_SOURCES", "FAILED", "FINDING_CODES", "MATCHED",
           "METHOD_NOTE", "METHOD_VERSION", "MISMATCHED", "OBSERVED", "STATUSES", "UNVERIFIED", "actual_of",
           "append", "as_dict", "binding_fingerprints", "card_metrics", "compare", "covers_current",
           "declared_shapes", "definition_hash", "expectation_hash", "expectation_of", "get_record",
           "list_for_draft", "materials", "same_value", "semantic_fingerprint", "summarize"]
