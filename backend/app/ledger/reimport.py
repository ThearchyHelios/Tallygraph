"""增量重导入与 B / L / R 三方合并（K11）：把新一段材料重新抽成候选 R，和当前图 L、字段基线 B 逐字段比。

**三个东西各自是什么、谁跟谁走。**

- **B**（baseline）是「上一次被接受的导入值」：K05 首次接受导入那一刻、按 metric_id + field_path 存下的不可变工件
  （app/ledger/fieldbaseline.py）。它只由「接受一次导入」推进，**只按被接受的那些字段往前走**；没接受的冲突一格不动。
- **L**（live）是当前人工维护的图：口径卡上的指标。字段操作（K03）、整份换指标改的都是 L，改 L 一个字都不动 B。
- **R**（重导入候选）是这次抽取出来的新说法。R 只生成候选，**不改 L**：要不要落到图上，由人按字段接受。

**按什么对齐。** 只认两样：稳定的 metric_id（候选显式带着、或者人指定），和**来源证据**（引用指向哪几段）。名字像不像
不参与自动合并——同名不同范围（同一个名字、引的原文不是同一段）必须人来定，机器不替人猜「这说的是同一条指标」。
按来源证据对上的依据优先取 B 的引用（那是「上次被接受时引的段落」）；B 不可用时退回 L 现在引的段落。对上了不止一条
（歧义）也交给人。

**baseline_unknown 不假装有基线。** 老草稿没有 B、或 B 读不回来时，不能拿 L 现在的内容冒充 B，也**不能假设 L = B**：
对齐上的每一项都标成「需要人工对齐」，逐字段由人决定，谁都不自动落。B 只在人明确接受那一刻、按接受的那几格从空建起来
（见 fieldbaseline.advance）。

**逐字段的三方状态**（机器按它判能不能接受，见 FIELD_STATES）：无冲突更新 / 人工修改冲突 / 新增候选 / 缺失候选四
种，另加「不变」和「需要人工对齐」（没有可比基线时的兜底）。

| 状态 | 什么时候 | 能不能接受 |
| --- | --- | --- |
| update（无冲突更新） | R 和 B 不同，L 还停在 B 上 | 能：`auto` 一次收掉 |
| conflict（人工修改冲突） | R 和 B 不同，L 已经被人工改过 | 要人点名接受（接受 = 取 R；拒绝 = 留 L） |
| new（新增候选） | R 有这个字段，B 和 L 都没有 | 能 |
| missing（缺失候选） | R 这次没提取到这个字段 | **不能**：漏提取不当删除，L 的这一格原样留着 |
| manual（需要人工对齐） | 没有基线可比（baseline_unknown） | 要人点名接受 |
| unchanged（不变） | R 和 L 已经一致，或 R 和基线一致（同一原文重提） | 不用接受；人工覆盖不会被悄悄改回去 |

**来源里的段落没给过模型，就不能当引用。** 候选的引用只留 `visible_chunk_ids` 里真的进了这次提示的段落，其余一律丢掉
并记进 align.invisible_citations——引用一段模型没看到的原文，不算出处（和 K05 引用核对同一条规矩）。

**任务进度不是 B。** run.checkpoint 只记「哪些工作项还没做完、哪些字段已经接受」，重启后据此显式恢复（resume）。
和 K05 一样：checkpoint 记的是任务恢复要用的东西，B 在草稿 payload 的 field_baseline 上。

**接受一次字段是原子的。** 「L 新增一版」和「B 往前推一格」必须在同一次提交里落地，否则会出现「L 提交了、B 永远没
推进」的空档（重试幂等回放，B 却读不回来）。所以顺序固定：先在图拷贝上改完（不写库）→ 先写**不可变的新 B 工件**
（写失败就让整次失败，L 不落；万一后面回滚，留下的是没人引用的孤儿工件，可以接受）→ **一个**数据库事务提交 L 新版本、
工作项决定、草稿上的新 B 引用与历史、幂等键 → 提交后只做读侧重算。全程只有一次提交。

**工作项的身份是持久对齐键，不是位置。** 一条工作项的键 = 稳定 metric 身份 + 规范化来源证据（段落 id），存在
`CaliberImportItem.align_key` 上；恢复 / 重跑按它认项，候选顺序变了、条数变了都不会把接受 / 拒绝的决定搬到别的项上。
每项自己记着它那一轮的可见段落和引用证据，不被新一轮覆盖。接口定位工作项按 `item_id`；旧调用只给 `metric_id` 时要求它
唯一对得上一条（重复候选回 `item_ambiguous`，**不取最后一条**）。`metric_id` 只在候选带着**唯一的**编号、或按来源证据
唯一对上时才写；模型跨组重号（每组各自从 m1 编起）不当稳定身份。

**失败的候选一格都不能接受。** 引用一段都没核对上（没给过模型的段落 / 模型没给引用）的候选整条 `failed`：逐格
`acceptable=false`，明确去接受回 `item_failed`，auto 直接跳过——尤其不能用空的 R 把 citations 清掉。

**接受之前再看一眼 L。** 每项记着「算候选时那条指标的指纹」；接受时和现在图上的比，对不上就回 `import_stale_l`
（客户端只把 `expected_revision` 更新到最新一版也不行），要人 `POST …/refresh` 按现在的口径重算三方、重新确认。
"""
from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.core import job_runtime as runtime
from app.db.base import SessionLocal, utcnow
from app.db.models import CaliberDraft, CaliberImportItem, CaliberImportRun, Chunk, DataSource, Document, Workflow
from app.ledger import fieldbaseline

#: 逐字段三方状态（见模块说明）。机器按它判能不能接受，界面按它显示。
FIELD_STATES = ("update", "conflict", "new", "missing", "manual", "unchanged")
#: 工作项（一条候选指标）的状态：对齐+三方算出来的 + 人处置之后的
ITEM_STATUSES = ("pending", "update", "conflict", "new", "manual", "unchanged", "partial",
                 "accepted", "rejected", "failed")
#: 能接受的那几种字段状态：无冲突更新、人工冲突（接受=取R）、新增候选、需要人工对齐（baseline_unknown）
ACCEPT_STATES = ("update", "conflict", "new", "manual")
#: run 的状态（core/job_runtime 的那一套词）
RUN_STATUSES = ("queued", "running", "succeeded", "partial", "failed", "cancelled", "interrupted")

#: 遇到不能按预期继续时的机器码（接口一律带 code 回）
IMPORT_UNAVAILABLE = "import_unavailable"
IMPORT_REVISION_REQUIRED = "revision_required"
IMPORT_STALE_REVISION = "stale_revision"
IMPORT_IDEMPOTENCY_CONFLICT = "idempotency_conflict"
IMPORT_ITEM_UNKNOWN = "item_unknown"
IMPORT_FIELD_UNKNOWN = "field_unknown"
IMPORT_FIELD_NOT_ACCEPTABLE = "field_not_acceptable"
IMPORT_NOTHING_SELECTED = "nothing_selected"
IMPORT_ALREADY_RUNNING = "already_running"
#: 推进后的 B 工件写不进去（工件库故障）：这次接受整体不落库（L 未提交），可以重试
IMPORT_BASELINE_UNAVAILABLE = "baseline_unavailable"
#: 工作项定位：这次导入里对得上不止一条（重复 metric 候选 / 两条候选对到同一条指标）——按 item_id 指明
IMPORT_ITEM_AMBIGUOUS = "item_ambiguous"
#: 这条工作项还没对齐到图上的指标（或整条失败）：先人工对齐 / 重新抽取，再接受
IMPORT_ITEM_UNALIGNED = "item_unaligned"
IMPORT_ITEM_FAILED = "item_failed"
#: 候选算出来之后 L 被人改过：不能只更新 expected_revision 就覆盖，要刷新三方、人重新确认
IMPORT_STALE_L = "import_stale_l"
IMPORT_NOTHING_TO_RESUME = "nothing_to_resume"

_MISSING = object()


class ImportRefused(Exception):
    """这次导入操作不合规（接口照 status_code / code / message 回）。"""

    def __init__(self, status_code: int, message: str, code: str = IMPORT_UNAVAILABLE, *,
                 extra: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.code = code
        self.extra = extra or {}


# ==========================================================================
# 读值：B / L / R 各自的取法
# ==========================================================================


def _same(left: Any, right: Any) -> bool:
    """两个值是不是同一份（含「都没这一格」）。JSON 值直接比，不做隐式舍入。"""
    if left is _MISSING or right is _MISSING:
        return left is _MISSING and right is _MISSING
    return bool(left == right)


def citation_chunks(container: Any) -> list[str]:
    """一段内容（指标或一份基线值）里的引用段落 id，按出现顺序去重；`{citations:[…]}` 或 citations 数组都收。"""
    citations = container.get("citations") if isinstance(container, Mapping) else container
    out: list[str] = []
    for item in citations if isinstance(citations, (list, tuple)) else []:
        if not isinstance(item, Mapping):
            continue
        chunk_id = item.get("chunk") or item.get("chunk_id") or item.get("chunkId")
        if isinstance(chunk_id, str) and chunk_id and chunk_id not in out:
            out.append(chunk_id)
    return out


def visible_citations(metric: Any, visible_chunk_ids: Sequence[str]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """把候选的引用分成两拨：真的进了这次提示的段落（留作引用），和没给过模型的段落（丢掉、记下来）。

    返回 (kept, invisible)。被丢掉的不能当引用：模型没看到那段原文，它对不上出处。
    """
    visible = {str(c) for c in visible_chunk_ids or []}
    citations = (((metric or {}).get("definition") or {}).get("citations")
                 if isinstance((metric or {}).get("definition"), Mapping) else None)
    kept: list[dict[str, Any]] = []
    invisible: list[dict[str, Any]] = []
    for item in citations if isinstance(citations, list) else []:
        if not isinstance(item, Mapping):
            continue
        chunk_id = item.get("chunk") or item.get("chunk_id") or item.get("chunkId")
        if isinstance(chunk_id, str) and chunk_id in visible:
            kept.append(dict(item))
        else:
            invisible.append(dict(item))
    return kept, invisible


def _live_value(metric: Any, path: str) -> Any:
    """L 上某个字段路径的值；没有这一格就是 _MISSING。"""
    if not isinstance(metric, Mapping):
        return _MISSING
    try:
        return fieldbaseline.value_at(metric, path)
    except KeyError:
        return _MISSING


def _baseline_value(values: Any, path: str) -> Any:
    return values[path] if isinstance(values, Mapping) and path in values else _MISSING


def field_state(*, baseline: Any, live: Any, candidate: Any, baseline_known: bool) -> str:
    """一格的三方状态（见模块说明的四种 + 不变 + 需要人工对齐）。

    baseline_known 是「这条指标有没有可比的基线（B 里收着它的接受值）」；**这一格**有没有基线还要看 baseline
    本身在不在。按字段推进出来的 B 常常只覆盖一部分格子：那些没覆盖到的格子照旧判 manual（不能因为这条指标有
    别的格子的基线，就把这一格当成「L 还停在 B 上」）。
    """
    if candidate is _MISSING:
        if live is _MISSING and (not baseline_known or baseline is _MISSING):
            return "unchanged"                # 本来就没有这一格，不算漏提取
        return "missing"                      # L 或 B 上有，R 这次没提到 → 漏提取不当删除
    if _same(candidate, live):
        return "unchanged"                    # R 和 L 已经一致
    covered = baseline_known and baseline is not _MISSING
    if covered and _same(candidate, baseline):
        return "unchanged"                    # R 和基线一致：这次导入没有新说法，人工改过的 L 不动、也不报冲突
    if not covered:
        if live is _MISSING:
            return "new"                      # B、L 都没有，R 新提出来的字段
        return "manual"                       # 这一格没有可比基线 → 需要人工对齐，不自动落
    if live is _MISSING:
        return "conflict"                     # L 这一格被删了（B 上有），R 又提了别的说法 → 要人定
    if _same(live, baseline):
        return "update"                       # L 还停在 B 上 → 无冲突更新
    return "conflict"                         # L 已经被人工改过 → 人工修改冲突


def _status_of_item(*, mode: str, fields: Mapping[str, Any]) -> str:
    """一条候选指标的状态：对齐模式先定调，再看逐字段里最重的那一种。"""
    if mode == "new":
        return "new"
    if mode == "manual":
        return "manual"
    states = {cell.get("state") for cell in fields.values() if isinstance(cell, Mapping)}
    for state, status in (("conflict", "conflict"), ("manual", "manual"), ("update", "update"),
                          ("new", "new"), ("missing", "partial")):
        if state in states:
            return status
    return "unchanged"


def item_status(*, mode: str, fields: Mapping[str, Any], accepted: Sequence[str], rejected: Sequence[str],
                failed: bool = False) -> str:
    """一条工作项现在的状态：还没决定的格子算状态；都决定完了就是 accepted / rejected。

    「已经决定的不再参与状态」是关键：接受一格之后它算 unchanged，不会让整条又退回 conflict。
    """
    decided = {str(p) for p in [*accepted, *rejected]}
    remaining = {path: cell for path, cell in fields.items()
                 if isinstance(cell, Mapping) and path not in decided}
    if failed and not accepted:
        return "failed"
    if not remaining or all(cell.get("state") == "unchanged" for cell in remaining.values()):
        if accepted:
            return "accepted"
        return "rejected" if rejected else "unchanged"
    return _status_of_item(mode=mode, fields=remaining)


# ==========================================================================
# 对齐（只认稳定 metric_id 和来源证据；不按名称）
# ==========================================================================


def align(*, candidate_id: str, candidate_citations: Sequence[Mapping[str, Any]],
          live_metrics: Sequence[Mapping[str, Any]], baseline_values: Mapping[str, Any],
          candidate_id_unique: bool = True) -> dict[str, Any]:
    """把一条候选对到当前图的一条指标上：{metric_id, mode, reason, evidence, …}。

    mode：
    - metric_id：候选显式带着一个在图上存在、而且**这次导入里唯一**的稳定 id，且来源证据和图上那条不打架；
    - evidence：按来源证据对上唯一一条（先比 B 的引用，B 没有这一格时退回比 L 的引用）；
    - new：都没对上——**不按名字合并**，交给人；
    - manual：同一个 id 但来源证据不一致、或来源证据对上了不止一条：要人指定。

    只有 metric_id / evidence 两种模式才给 metric_id：对不上的（new / manual）一律留空，免得「对不上却挂了个 id」
    被后面的写入当成已对齐。**跨组重号的候选 id 不当稳定身份**（candidate_id_unique=False，例如模型在两组里各自
    从 m1 编起）：这时只按来源证据对，不拿模型的新编号冒充稳定身份。
    """
    live_by_id = {str(m.get("id")): m for m in live_metrics or [] if isinstance(m, Mapping) and m.get("id")}
    evidence = sorted(set(citation_chunks({"citations": list(candidate_citations or [])})))
    id_usable = bool(candidate_id) and candidate_id_unique
    if id_usable and candidate_id in live_by_id:
        live_evidence = sorted(set(citation_chunks((live_by_id[candidate_id] or {}).get("definition") or {})))
        if not evidence or not live_evidence or evidence == live_evidence:
            return {"metric_id": candidate_id, "mode": "metric_id", "evidence": evidence,
                    "live_evidence": live_evidence,
                    "reason": "候选带着和图上一致的稳定 metric_id，来源证据也对得上"}
        return {"metric_id": "", "mode": "manual", "evidence": evidence, "live_evidence": live_evidence,
                "candidates": [candidate_id],
                "reason": "候选带着和图上一样的 metric_id，但引的原文不是同一段（同名不同范围）：请人工确认"}

    matches = [str(mid) for mid, values in (baseline_values or {}).items()
               if evidence and sorted(set(citation_chunks((values or {}).get(fieldbaseline.CITATIONS_FIELD)))) == evidence]
    basis = "baseline"
    if not matches:
        matches = [mid for mid, metric in live_by_id.items()
                   if evidence and sorted(set(citation_chunks((metric or {}).get("definition") or {}))) == evidence]
        basis = "live"
    note = ""
    if candidate_id and not candidate_id_unique:
        note = (f"候选 id「{candidate_id}」在这次导入里出现不止一次（跨组重号）：不拿它当稳定身份，"
                f"只按来源证据对")
    if len(matches) == 1:
        out = {"metric_id": matches[0], "mode": "evidence", "evidence": evidence, "basis": basis,
               "reason": f"按来源证据（引了同一段原文，依据取自{'字段基线 B' if basis == 'baseline' else '当前图 L'}）"
                         f"对上了指标 {matches[0]}"}
        if note:
            out["identity_note"] = note
        return out
    if len(matches) > 1:
        out = {"metric_id": "", "mode": "manual", "evidence": evidence, "basis": basis, "candidates": matches,
               "reason": "来源证据对上了不止一条指标：请人工指定对哪一条"}
        if note:
            out["identity_note"] = note
        return out
    out = {"metric_id": "", "mode": "new", "evidence": evidence,
           "reason": "没有对得上的指标（稳定 ID 和来源证据都没对上）：名称相不相似不参与自动合并，请人工确认"}
    if note:
        out["identity_note"] = note
    return out


def alignment_key(*, metric_id: str, evidence: Sequence[str]) -> str:
    """一条工作项的**持久对齐键**：稳定 metric 身份 + 规范化来源证据（段落 id 排序去重）。

    恢复 / 重跑时按它认同一个工作项，不按候选位置：顺序变了、条数变了都不会把接受 / 拒绝的决定搬到别的项上。
    对不上的候选 metric_id 是空串，键就只剩证据。
    """
    from app.core.artifact_store import canonical_json, content_hash

    return content_hash(canonical_json({"metric_id": str(metric_id or ""),
                                        "evidence": sorted({str(e) for e in evidence or []})}))


def live_fingerprint(metric: Any) -> str:
    """一条指标（L）在某个时刻的样子：逐字段值哈希的规范化指纹。

    接受之前拿它和现在图上的那条比：**L 在候选之后被人改过就不能落**（哪怕客户端把 expected_revision 更新成
    最新的一版）——要人刷新三方、重新确认。给不出指标（对不上、或指标没了）时是空指标的指纹。
    """
    from app.core.artifact_store import canonical_json, content_hash

    cells = fieldbaseline.fields_of(metric) if isinstance(metric, Mapping) else {}
    return content_hash(canonical_json({path: (cell or {}).get("hash") for path, cell in sorted(cells.items())}))


# ==========================================================================
# 组装工作项：一条候选 = 一条工作项，逐字段算三方
# ==========================================================================


def build_item(*, seq: int, candidate: Mapping[str, Any], live_metric: Mapping[str, Any] | None,
               align_info: Mapping[str, Any], baseline_values: Mapping[str, Any], baseline_known: bool,
               visible_chunk_ids: Sequence[str]) -> dict[str, Any]:
    """一条候选指标 → 一条工作项：过滤引用、逐字段算三方（不改 L、不改 B）。

    align_info 是调用方（build_items / 人工对齐）已经算好的对齐依据；这里不再自己按名字或证据重对。
    **失败的工作项没有任何可接受的格子**：引用全都不可见时一条引用都留不下，这条整体标 failed，
    逐格 acceptable=false（尤其不能把 citations 接受成空——那等于把出处的证据删掉）。
    """
    metric = copy.deepcopy(dict(candidate or {}))
    kept, invisible = visible_citations(metric, visible_chunk_ids)
    definition = metric.get("definition")
    definition = dict(definition) if isinstance(definition, Mapping) else {}
    definition["citations"] = kept
    metric["definition"] = definition
    align_info = dict(align_info or {})
    align_info["visible_chunk_ids"] = sorted({str(c) for c in visible_chunk_ids or []})
    align_info["live_fingerprint"] = live_fingerprint(live_metric)
    aligned_id = str(align_info.get("metric_id") or "")
    cells = fieldbaseline.fields_of(metric)
    fields: dict[str, Any] = {}
    for path in fieldbaseline.field_paths():
        cell = cells.get(path)
        candidate_value = cell.get("value") if isinstance(cell, Mapping) else _MISSING
        live_value = _live_value(live_metric, path)
        base_value = _baseline_value(baseline_values, path)
        state = field_state(baseline=base_value, live=live_value, candidate=candidate_value, baseline_known=baseline_known)
        fields[path] = {
            "state": state,
            "b": None if base_value is _MISSING else copy.deepcopy(base_value),
            "l": None if live_value is _MISSING else copy.deepcopy(live_value),
            "r": None if candidate_value is _MISSING else copy.deepcopy(candidate_value),
            "b_present": base_value is not _MISSING,
            "l_present": live_value is not _MISSING,
            "r_present": candidate_value is not _MISSING,
            "r_hash": cell.get("hash") if isinstance(cell, Mapping) else "",
            "decided": None,
            "acceptable": False,
        }
    if invisible:
        align_info = {**align_info, "invisible_citations": invisible}
    error = ""
    status = _status_of_item(mode=align_info.get("mode") or "new", fields=fields)
    if not kept:
        # 一条引用都留不下：模型没看到那段原文，就不算出处（没有出处的定义不落，整条也不可接受）
        status, error = "failed", "候选的引用指向的段落没有提供给模型：不能当出处，请重新抽取或人工补依据"
    for path, cell in fields.items():
        cell["acceptable"] = _acceptable(status, cell.get("state"), bool(cell.get("r_present")), path,
                                         cell.get("r"))
    align_info["align_key"] = alignment_key(metric_id=aligned_id, evidence=kept_chunks(kept))
    return {
        "seq": int(seq),
        "metric_id": aligned_id,
        "candidate_id": str(metric.get("id") or ""),
        "name": str(metric.get("name") or ""),
        "status": status,
        "align": align_info,
        "align_key": align_info["align_key"],
        "fields": fields,
        "accepted": [],
        "rejected": [],
        "applied_revision": None,
        "error": error,
    }


def kept_chunks(citations: Sequence[Mapping[str, Any]]) -> list[str]:
    """留下来的引用指向的段落 id（工作项持久对齐键里的那一半「来源证据」）。"""
    return sorted(set(citation_chunks({"citations": list(citations or [])})))


def _acceptable(item_status: str, state: Any, r_present: bool, path: str, r_value: Any = None) -> bool:
    """这一格能不能被接受：整条失败的不行；没提到、和现在一致的不行；引用不能被接受成空的。"""
    if item_status == "failed" or not r_present:
        return False
    if path == fieldbaseline.CITATIONS_FIELD and not r_value:
        return False          # 出处不能被接受成空：那等于把证据删掉
    return state in ACCEPT_STATES


def build_items(*, candidates: Sequence[Mapping[str, Any]], live_metrics: Sequence[Mapping[str, Any]],
                baseline_values: Mapping[str, Any], visible_chunk_ids: Sequence[str]) -> list[dict[str, Any]]:
    """整批候选 → 一列工作项。先把候选按稳定 ID / 来源证据对到 L 上，再逐条算三方。

    `baseline_values` 是 {metric_id: {field_path: 值}}（B 读回来的样子）；某条候选 align 到的指标在它里面，
    这一条才有基线可比（baseline_known），否则逐字段判「需要人工对齐」。名称不参与对齐。

    **跨组重号不当稳定身份**：模型可能每组各自从 m1 编起（draftplan 的 normalize_plan 就是这样），同一个编号出现
    不止一次时只按来源证据对（见 align 的 candidate_id_unique）。
    """
    from collections import Counter

    live_by_id = {str(m.get("id")): m for m in live_metrics or [] if isinstance(m, Mapping) and m.get("id")}
    ids = Counter(str((candidate or {}).get("id") or "") for candidate in candidates or [])
    items: list[dict[str, Any]] = []
    keys: Counter[str] = Counter()
    for position, candidate in enumerate(candidates or [], start=1):
        kept, _invisible = visible_citations(candidate, visible_chunk_ids)
        candidate_id = str((candidate or {}).get("id") or "")
        probe = align(candidate_id=candidate_id, candidate_citations=kept,
                      live_metrics=list(live_by_id.values()), baseline_values=baseline_values or {},
                      candidate_id_unique=bool(candidate_id) and ids[candidate_id] == 1)
        metric_id = str(probe.get("metric_id") or "")
        live_metric = live_by_id.get(metric_id)
        baseline_known = bool(metric_id) and metric_id in (baseline_values or {})
        item = build_item(seq=position, candidate=candidate, live_metric=live_metric, align_info=probe,
                          baseline_values=(baseline_values or {}).get(metric_id) or {},
                          baseline_known=baseline_known, visible_chunk_ids=visible_chunk_ids)
        # 同键（同一身份 + 同一证据）的候选不止一条时按出现次序区分：它们本来就互为替身，谁先谁后不影响
        keys[item["align_key"]] += 1
        if keys[item["align_key"]] > 1:
            item["align_key"] = f"{item['align_key']}#{keys[item['align_key']]}"
            item["align"]["align_key"] = item["align_key"]
        items.append(item)
    return items


def refresh_item(item: Mapping[str, Any], *, live_metric: Mapping[str, Any] | None,
                 baseline_values: Mapping[str, Any], baseline_known: bool) -> dict[str, Any]:
    """L 或 B 动过之后重算一条工作项的三方：已决定（接受/拒绝）的字段保持决定，只更新数值和状态。

    「不重复接受、也不因为别人的改动把人的决定抹掉」：已接受的格子状态只在被接受那次推进 B 时变过，之后照旧。
    **对齐键、来源证据、可见段落这一段原样保留**：它们是这一项自己的依据，不被新一轮重算覆盖；只刷新
    live_fingerprint（那是「上次按哪一版 L 算的三方」的比较锚点）。
    """
    fields = copy.deepcopy(dict(item.get("fields") or {}))
    accepted = [str(p) for p in item.get("accepted") or []]
    rejected = [str(p) for p in item.get("rejected") or []]
    for path, cell in fields.items():
        if not isinstance(cell, Mapping):
            continue
        live_value = _live_value(live_metric, path)
        base_value = _baseline_value(baseline_values, path)
        candidate_value = cell.get("r") if cell.get("r_present") else _MISSING
        cell["l"] = None if live_value is _MISSING else copy.deepcopy(live_value)
        cell["l_present"] = live_value is not _MISSING
        cell["b"] = None if base_value is _MISSING else copy.deepcopy(base_value)
        cell["b_present"] = base_value is not _MISSING
        if path in accepted:
            cell["state"], cell["decided"] = "unchanged", "accepted"
        elif path in rejected:
            cell["decided"] = "rejected"
        else:
            cell["state"] = field_state(baseline=base_value, live=live_value, candidate=candidate_value,
                                        baseline_known=baseline_known)
            cell["decided"] = None
    mode = str((item.get("align") or {}).get("mode") or "new")
    failed = item.get("status") == "failed"
    status = item_status(mode=mode, fields=fields, accepted=accepted, rejected=rejected, failed=failed)
    for path, cell in fields.items():
        if isinstance(cell, dict):
            cell["acceptable"] = _acceptable(status, cell.get("state"), bool(cell.get("r_present")), path,
                                             cell.get("r"))
    align = dict(item.get("align") or {})
    if "align_key" not in align and item.get("align_key"):
        align["align_key"] = item["align_key"]
    align["live_fingerprint"] = live_fingerprint(live_metric)
    return {**dict(item), "fields": fields, "status": status, "align": align}


# ==========================================================================
# 抽取候选（真实现调模型；测试注入假的，不连真实业务库）
# ==========================================================================


async def extract_candidates(*, entries: list[dict[str, Any]], chunks: Mapping[str, Any], guidance: str,
                             datasource: Mapping[str, Any] | None, tables_text: str, fixed: str,
                             allowed_tables: set[str]) -> dict[str, Any]:
    """把这次材料抽成候选 R：一次模型调用，交回 {metrics, visible_chunk_ids, prompt_version, parse_version,
    model_id, problems, invisible…}。和起草主路径共用同一套提示词与校验（draftplan），只取定义，不用 SQL 建图。

    引用核对只认**实际进提示**的段落（fit_material 的结果）：被截断丢掉的段落，模型引用它也核不上。
    """
    from app.ledger import draftplan

    fitted = draftplan.fit_material(entries, chunks, guidance, dict(datasource) if datasource else None,
                                    tables_text, (), fixed)
    async with SessionLocal() as session:
        result = await draftplan.plan_groups(session, docs=entries, chunks=dict(chunks), guidance=guidance,
                                             datasource=dict(datasource) if datasource else None,
                                             tables_text=tables_text, covered=(), fixed=fixed, fitted=fitted)
    visible = fitted[0]
    metrics: list[dict[str, Any]] = []
    problems: list[dict[str, Any]] = []
    for raw in result.get("groups") or []:
        plan, issues = draftplan.normalize_plan(raw, visible, allowed_tables=allowed_tables)
        metrics.extend(plan.get("metrics") or [])
        problems.extend(issues)
    return {"metrics": metrics, "problems": problems, "visible_chunk_ids": list(result.get("visible_chunk_ids") or []),
            "prompt_version": int(result.get("prompt_version") or 0),
            "parse_version": int(result.get("parse_version") or 0), "model_id": str(result.get("model_id") or ""),
            "sql_groups": list(result.get("groups") or [])}


async def _schema_material(session: Any, source: DataSource, focus_text: str) -> tuple[str, set[str]]:
    from app.ledger.authoring import _schema_material as impl

    return await impl(session, source, focus_text)


# ==========================================================================
# 抽取主体：读材料 → 存来源快照 → 抽候选 → 算工作项 → 落库
# ==========================================================================


async def _load_material(session: Any, draft: CaliberDraft) -> dict[str, Any]:
    """读这次要导入的材料：草稿来源里的文档段落 + 数据源表结构。"""
    origin = draft.origin if isinstance(draft.origin, dict) else {}
    wanted = [str(d) for d in origin.get("documents") or []]
    docs: list[Document] = []
    for doc_id in wanted:
        doc = await session.get(Document, doc_id)
        if doc is not None:
            docs.append(doc)
    chunks: dict[str, dict[str, Any]] = {}
    entries: list[dict[str, Any]] = []
    for doc in docs:
        rows = (await session.execute(
            select(Chunk).where(Chunk.document_id == doc.id).order_by(Chunk.ordinal, Chunk.id))).scalars()
        chunk_ids: list[str] = []
        for chunk in rows:
            chunks[chunk.id] = {"ordinal": chunk.ordinal, "content": chunk.content or "", "document_id": doc.id}
            chunk_ids.append(chunk.id)
        entries.append({"document_id": doc.id, "title": doc.title or doc.source or doc.id, "chunk_ids": chunk_ids})
    source = await session.get(DataSource, draft.datasource_id) if draft.datasource_id else None
    focus_text = "\n".join(str(c.get("content") or "") for c in chunks.values())
    tables_text, allowed = await _schema_material(session, source, focus_text) if source is not None else ("", set())
    documents = [{"document_id": doc.id, "title": doc.title or doc.source or doc.id, "source": doc.source or "",
                  "collection": doc.collection or "", "content": doc.content or ""} for doc in docs]
    datasource = {"name": source.name, "type": source.kind, "description": source.description} if source else None
    guidance = str(origin.get("guidance") or "")
    fixed = str(origin.get("fixed_filters") or "")
    return {"docs": documents, "chunks": chunks, "entries": entries, "datasource": datasource,
            "tables_text": tables_text, "allowed_tables": allowed, "guidance": guidance, "fixed": fixed,
            "source_row": source}


async def run_import(run_id: str) -> None:
    """后台跑一次导入：读材料 → 存 R 的来源快照 → 抽候选 → 和 L / B 算工作项 → 落库。**不碰 L，也不碰 B。**

    可重入：中途被打断之后再来一次，已经决定（接受/拒绝）的工作项保持决定，只重算还没定的。抽取只是候选，
    真正落到图上的是 accept（另一条路，见 accept_fields），所以重跑这里不会多建版本、也不会重复接受。
    """
    from app.core.artifact_store import put_json
    from app.ledger import docdraft

    async with SessionLocal() as session:
        run = await session.get(CaliberImportRun, run_id)
        if run is None or run.status == "cancelled":
            return
        draft = await session.get(CaliberDraft, run.draft_id)
        if draft is None:
            run.status, run.error, run.finished_at = "failed", "草稿已删除", utcnow()
            await session.commit()
            return
        workflow = await session.get(Workflow, run.workflow_id) if run.workflow_id else None
        try:
            material = await _load_material(session, draft)
        except Exception as e:  # noqa: BLE001 - 材料读不出来照实记失败，不留在「进行中」
            run.status, run.error, run.finished_at = "failed", f"读来源材料出错：{e}", utcnow()
            await session.commit()
            return
        if not material["chunks"]:
            run.status, run.error, run.finished_at = "failed", "这份草稿没有可导入的文档段落", utcnow()
            await session.commit()
            return
        if not material["source_row"]:
            run.status, run.error, run.finished_at = "failed", "没有数据源：抽取候选必须对照数据目录", utcnow()
            await session.commit()
            return
        await session.commit()

        snapshot = docdraft.source_snapshot(documents=material["docs"], chunks=material["chunks"],
                                            datasource=material["datasource"], catalog=material["tables_text"],
                                            guidance=material["guidance"], fixed_filters=material["fixed"])
        try:
            artifact_id = await put_json(snapshot, kind=docdraft.SNAPSHOT_KIND, node_id=run_id,
                                         meta={"draft_id": draft.id, "import_run_id": run_id,
                                               "source_snapshot_version": docdraft.SNAPSHOT_VERSION})
            ref = docdraft.snapshot_ref(artifact_id, snapshot)
        except Exception:  # noqa: BLE001 - 快照存不下：照实说没有可还原的 R 来源，不当成功
            ref = None

        async with SessionLocal() as session:
            run = await session.get(CaliberImportRun, run_id)
            if run is None or run.status == "cancelled":
                return
            run.source_snapshot = ref or {}
            run.checkpoint = {**(run.checkpoint or {}), "stage": "extracting"}
            await session.commit()

        try:
            result = await extract_candidates(entries=material["entries"], chunks=material["chunks"],
                                              guidance=material["guidance"], datasource=material["datasource"],
                                              tables_text=material["tables_text"], fixed=material["fixed"],
                                              allowed_tables=material["allowed_tables"])
        except Exception as e:  # noqa: BLE001 - 抽取出错照实记，不假装完成
            async with SessionLocal() as session:
                run = await session.get(CaliberImportRun, run_id)
                if run is not None and run.status != "cancelled":
                    run.status, run.error, run.finished_at = "failed", f"抽取候选出错：{e}", utcnow()
                    await session.commit()
            return

        candidates = list(result.get("metrics") or [])
        visible = list(result.get("visible_chunk_ids") or [])

        async with SessionLocal() as session:
            run = await session.get(CaliberImportRun, run_id)
            if run is None or run.status == "cancelled":
                return
            draft = await session.get(CaliberDraft, run.draft_id)
            workflow = await session.get(Workflow, run.workflow_id) if run.workflow_id else None
            run.visible_chunk_ids = visible
            run.prompt_version = int(result.get("prompt_version") or 0)
            run.parse_version = int(result.get("parse_version") or 0)
            run.model_id = result.get("model_id") or None
            live_metrics = list(fieldbaseline.accepted_card(workflow.graph if workflow is not None else {})[1])
            ref = fieldbaseline.ref_of(draft.payload) if draft is not None else None
            try:
                baseline_values = fieldbaseline.values(ref) if ref else {}
            except Exception:  # noqa: BLE001 - B 读不回来当没有基线（baseline_unknown），不猜
                baseline_values = {}
            built = build_items(candidates=candidates, live_metrics=live_metrics,
                                baseline_values=baseline_values, visible_chunk_ids=visible)
            # 按**持久对齐键**认同一个工作项（不是候选位置 seq）：顺序变了、条数变了都不会把接受 / 拒绝搬错项。
            # 对不上的候选是新的一项；老项（没有任何候选对上）原样留着，它自己的决定和依据一个字不动。
            by_key = {str(item.align_key): item for item in await _load_items(session, run_id) if item.align_key}
            used_seq = {int(item.seq) for item in await _load_items(session, run_id)}
            for made in built:
                _upsert_item(session, run_id, made, by_key.get(made["align_key"]), live_metrics=live_metrics,
                             baseline_values=baseline_values, used_seq=used_seq)
            await session.flush()
            await _refresh_counts(session, run)
            run.error = _problems_text(result.get("problems") or [])
            run.status = "succeeded" if candidates else "failed"
            if candidates and (result.get("problems") or run.error):
                run.status = "partial"
            if not candidates:
                run.error = run.error or "模型没有交出可用的候选指标"
            run.checkpoint = {**(run.checkpoint or {}), "stage": "aligned"}
            run.finished_at = utcnow()
            await session.commit()


def _problems_text(problems: Sequence[Mapping[str, Any]]) -> str:
    return "；".join(f"{p.get('name') or '问题'}：{p.get('reason') or ''}".strip("：")
                     for p in problems if isinstance(p, Mapping) and p.get("reason"))[:2000]


async def _refresh_counts(session: Any, run: CaliberImportRun) -> None:
    """重算读列表用的计数投影（工作项的处置状态变了就跟着变）。"""
    rows = await _load_items(session, run.id)
    run.items_total = len(rows)
    run.items_decided = sum(1 for item in rows if item.status in ("accepted", "rejected", "unchanged"))


async def _load_items(session: Any, run_id: str) -> list[CaliberImportItem]:
    return list((await session.execute(select(CaliberImportItem).where(CaliberImportItem.run_id == run_id)
                                       .order_by(CaliberImportItem.seq))).scalars())


def _alloc_seq(desired: int, used: set[int]) -> int:
    """给一条新工作项挑一个还没被占的 seq：seq 只是显示顺序，(run_id, seq) 仍然唯一。

    对不上的旧行（迁移过来的、align_key 为空的）不参与匹配，所以新行不能直接沿用同一个 seq。
    """
    seq = int(desired)
    if seq in used:
        seq = (max(used) + 1) if used else 1
    while seq in used:
        seq += 1
    used.add(seq)
    return seq


def _upsert_item(session: Any, run_id: str, made: Mapping[str, Any], existing: CaliberImportItem | None, *,
                 live_metrics: Sequence[Mapping[str, Any]] = (), baseline_values: Mapping[str, Any] = {},
                 used_seq: set[int] | None = None) -> None:
    """把算好的工作项落库。

    已经有的行（按 align_key 对上的同一项）保留人的决定：接受的字段、拒绝的字段、applied_revision，以及
    **这一项自己的对齐依据**（对齐键、来源证据、可见段落）——新一轮重算不会把这些覆盖掉，只刷新三方和
    live_fingerprint 那个比较锚点。对不上的候选是新的一项（旧行原样留着）。
    """
    accepted = [str(p) for p in (existing.accepted or [])] if existing is not None else []
    rejected = [str(p) for p in (existing.rejected or [])] if existing is not None else []
    align = copy.deepcopy(dict(made["align"] or {}))
    if existing is not None:
        kept = dict(existing.align or {})
        for key in ("align_key", "evidence", "visible_chunk_ids", "invisible_citations", "identity_note", "manual"):
            if key not in kept and key in align:
                kept[key] = copy.deepcopy(align[key])
        kept["live_fingerprint"] = align.get("live_fingerprint")
        align = kept
    seed = {"fields": copy.deepcopy(dict(made["fields"])), "accepted": accepted, "rejected": rejected,
            "align": align, "status": made.get("status"), "align_key": made.get("align_key")}
    live = {str(m.get("id")): m for m in live_metrics if isinstance(m, Mapping)}
    metric_id = str(made.get("metric_id") or "")
    refreshed = refresh_item(seed, live_metric=live.get(metric_id),
                             baseline_values=(baseline_values or {}).get(metric_id) or {},
                             baseline_known=bool(metric_id) and metric_id in (baseline_values or {}))
    fields, status = refreshed["fields"], refreshed["status"]
    row = existing
    if row is None:
        used = used_seq if used_seq is not None else set()
        row = CaliberImportItem(run_id=run_id, seq=_alloc_seq(int(made["seq"]), used))
        session.add(row)
    else:
        if used_seq is not None:
            used_seq.add(int(existing.seq))
        row.seq = int(existing.seq)
    row.align_key = str(existing.align_key) if existing is not None and existing.align_key else \
        str(made.get("align_key") or align.get("align_key") or "")
    row.metric_id, row.candidate_id, row.name = made["metric_id"], made["candidate_id"], made["name"]
    row.align, row.fields = align, fields
    row.accepted, row.rejected = accepted, rejected
    row.status = status
    if not (accepted or rejected):
        row.error = made.get("error") or None


# ==========================================================================
# 工作项定位：按 item.id（首选）；旧调用只给 metric_id 时要求唯一
# ==========================================================================


async def resolve_item(session: Any, run: CaliberImportRun, spec: Mapping[str, Any]) -> CaliberImportItem:
    """一次选择指向哪条工作项：**首选 item_id**（工作项的稳定主键）。

    旧调用只给 metric_id（或未对齐项的 seq）时：
    - metric_id 只在这条导入里**唯一**对得上一条工作项时才认；对上不止一条（重复 metric 候选、或两条候选对到
      同一条指标）回 409，把候选的 item_id 列出来让人指明——绝**不取最后一条**；
    - seq 只当旧客户端的兜底（位置本身不稳定，新调用别用）。
    """
    spec = spec or {}
    item_id = str(spec.get("item_id") or "").strip()
    if item_id:
        item = await session.get(CaliberImportItem, item_id)
        if item is None or item.run_id != run.id:
            raise ImportRefused(404, "这条工作项不存在（可能这次导入已经重建过）：请刷新后再试",
                                IMPORT_ITEM_UNKNOWN)
        return item
    metric_id = str(spec.get("metric_id") or "").strip()
    items = await _load_items(session, run.id)
    if metric_id:
        matched = [item for item in items if str(item.metric_id) == metric_id]
        if len(matched) == 1:
            return matched[0]
        if len(matched) > 1:
            ids = "、".join(str(item.id) for item in matched)
            raise ImportRefused(409, f"指标「{metric_id}」在这次导入里对上了不止一条工作项（{ids}）："
                                     f"请按 item_id 指明是哪一条，别按指标名猜", IMPORT_ITEM_AMBIGUOUS,
                                extra={"item_ids": [str(item.id) for item in matched]})
        raise ImportRefused(409, f"这次导入里没有对齐到指标「{metric_id}」的工作项，请刷新后再试",
                            IMPORT_ITEM_UNKNOWN)
    if spec.get("seq") is not None:
        for item in items:
            if int(item.seq) == int(spec.get("seq")):
                return item
    raise ImportRefused(409, "这次导入里没有对得上的工作项（给 item_id，或唯一对得上一条的 metric_id）",
                        IMPORT_ITEM_UNKNOWN)


# ==========================================================================
# 显式恢复未完成的工作项
# ==========================================================================


async def unfinished(session: Any, run_id: str) -> list[CaliberImportItem]:
    """这次导入里还没做完的工作项：抽取失败、或有还没决定的格子。

    「还没做完」不含已经接受 / 拒绝完了的：恢复不会去动人的决定，也不会重复接受、重复建版。
    """
    return [item for item in await _load_items(session, run_id)
            if item.status not in ("accepted", "rejected", "unchanged")]


async def reject_fields(session: Any, *, run: CaliberImportRun, selections: Sequence[Mapping[str, Any]],
                        note: str | None = None) -> int:
    """拒绝选定的字段（或整条工作项）：保持 L 不动、也不推进 B。

    定位按 item_id（旧调用给 metric_id 时要唯一对得上一条，见 resolve_item）。`fields` 为空表示整条都拒
    （还没决定的格子全拒）。已经接受过的格子不动——拒绝不改已经落到图上的事。
    """
    touched = 0
    for spec in selections:
        item = await resolve_item(session, run, spec or {})
        fields = dict(item.fields or {})
        paths = [str(p) for p in ((spec or {}).get("fields") or [])] or [
            path for path, cell in fields.items()
            if isinstance(cell, Mapping) and cell.get("decided") != "accepted"]
        for path in paths:
            cell = fields.get(path)
            if not isinstance(cell, Mapping):
                raise ImportRefused(422, f"工作项「{item.name or item.metric_id}」上没有字段 {path}",
                                    IMPORT_FIELD_UNKNOWN, extra={"path": path})
            if cell.get("decided") == "accepted":
                continue
            cell = dict(cell)
            cell["decided"] = "rejected"
            fields[path] = cell
            if path not in (item.rejected or []):
                item.rejected = sorted({*(item.rejected or []), path})
        item.fields = fields
        item.status = item_status(mode=str((item.align or {}).get("mode") or "new"), fields=fields,
                                  accepted=item.accepted or [], rejected=item.rejected or [],
                                  failed=item.status == "failed")
        if note:
            item.error = str(note)[:500]
        touched += 1
    await _refresh_counts(session, run)
    await session.commit()
    return touched


async def realign_item(session: Any, *, run: CaliberImportRun, draft: CaliberDraft, workflow: Workflow,
                       item: CaliberImportItem, metric_id: str) -> None:
    """人工把一条工作项对到当前图的某条指标上（或 `metric_id=""` 取消对齐）。

    「baseline_unknown 必须人工对齐」和「同名不同范围要人来定」都走这里：R 的值原样留着，换一个 L 一格、重算三方。
    人一旦重新对齐，旧的接受 / 拒绝决定不再适用（换了对哪一条，决定就换了对象），清空重来。
    """
    live_metrics = list(fieldbaseline.accepted_card(workflow.graph if workflow is not None else {})[1])
    live_by_id = {str(m.get("id")): m for m in live_metrics if isinstance(m, Mapping) and m.get("id")}
    metric_id = str(metric_id or "").strip()
    if metric_id and metric_id not in live_by_id:
        raise ImportRefused(409, f"图上没有指标「{metric_id}」：先刷新页面看现在的口径卡", IMPORT_ITEM_UNKNOWN)
    ref = fieldbaseline.ref_of(draft.payload)
    try:
        baseline_values = fieldbaseline.values(ref) if ref else {}
    except Exception:  # noqa: BLE001 - B 读不回来当没有基线
        baseline_values = {}
    baseline_known = bool(metric_id) and metric_id in baseline_values
    fields: dict[str, Any] = {}
    for path, cell in (item.fields or {}).items():
        if not isinstance(cell, Mapping):
            continue
        candidate = cell.get("r") if cell.get("r_present") else _MISSING
        live = _live_value(live_by_id.get(metric_id), path)
        base = _baseline_value(baseline_values.get(metric_id) or {}, path)
        fields[path] = {**dict(cell),
                        "state": field_state(baseline=base, live=live, candidate=candidate,
                                             baseline_known=baseline_known),
                        "b": None if base is _MISSING else copy.deepcopy(base),
                        "b_present": base is not _MISSING,
                        "l": None if live is _MISSING else copy.deepcopy(live),
                        "l_present": live is not _MISSING,
                        "decided": None}
    align_info = dict(item.align or {})
    # 人工对齐之后：对齐键跟着新的稳定身份走（下次恢复按它对得上）；模式按「对上了哪一条」记，
    # 不再按 baseline 有没有来定——没有基线时逐格的 state 本来就是 manual，不必让整条卡在 manual 上。
    align_info.update({"mode": "evidence" if metric_id else "new", "manual": True, "metric_id": metric_id,
                       "reason": "人工对齐" if metric_id else "人工取消对齐（当作新指标）"})
    align_info["align_key"] = alignment_key(metric_id=metric_id,
                                            evidence=align_info.get("evidence") or [])
    align_info["live_fingerprint"] = live_fingerprint(live_by_id.get(metric_id))
    for path, cell in fields.items():
        if isinstance(cell, dict):
            cell["acceptable"] = _acceptable(
                "failed" if item.status == "failed" else "manual", cell.get("state"),
                bool(cell.get("r_present")), path, cell.get("r"))
    item.metric_id, item.align, item.align_key = metric_id, align_info, align_info["align_key"]
    item.fields, item.accepted, item.rejected = fields, [], []
    item.status = item_status(mode=align_info["mode"], fields=fields, accepted=[], rejected=[],
                              failed=item.status == "failed")
    for path, cell in item.fields.items():
        if isinstance(cell, dict):
            cell["acceptable"] = _acceptable(item.status, cell.get("state"), bool(cell.get("r_present")), path,
                                             cell.get("r"))
    await _refresh_counts(session, run)
    await session.commit()


# ==========================================================================
# 接受选定字段：原子落库（先写不可变的新 B 工件，再一个事务提交 L + 工作项决定 + 新 B 引用）
# ==========================================================================


async def accept_fields(session: Any, *, run: CaliberImportRun, draft: CaliberDraft, workflow: Workflow,
                        card_id: str, selections: Sequence[Mapping[str, Any]], actor: str | None,
                        request_id: str | None, expected_revision: int | None) -> dict[str, Any]:
    """接受选定的字段：**一次性**落到 L（新增一版图），并按被接受的那几格推进 B。

    顺序是刻意的（原子性，也是这次修复的要点）：

    1. 校验 + 幂等：同 request_id 的重放直接交回上一次已提交的结果，不重写；
    2. 在**图的深拷贝**上把整批改完（K03 的白名单字段走 changes.apply_operations，引用原文按同一份图一起写），
       拿到新图和它对应的版本号——**这时还不写库**；
    3. 先把推进后的**不可变 B 工件写出来**。工件写失败就让整次接受失败，L 一个字节都不落（不会出现「L 提交了、
       B 永远没推进」：重试幂等回放却发现 B 缺失）。万一后面的事务回滚，留下的是一个**孤儿工件**——内容寻址的
       不可变工件，没有任何引用指向它，可以接受；
    4. **同一个数据库事务**里提交 L 的新版本（save_graph 的 CAS / 版本语义）、工作项的决定、草稿 payload 上的
       新 B 引用与历史，以及写入幂等键。任一步失败一起回滚，新 L 不可见；
    5. 提交之后只做读侧的重算（刷新工作项的三方），不再写库。

    于是「L 新增一版」和「B 往前推一格」是同一次提交里的事：重放交回已提交的结果、B 读得回来；同键不同内容仍旧冲突。
    """
    from app.apps.versions import (WRITE_CONFLICT, WRITE_REPLAY, WorkflowChanged, claim_write, remember_write,
                                   save_graph, write_fingerprint)
    from app.ledger import changes
    from app.ledger.repository import CaliberGone

    requested: list[tuple[CaliberImportItem, str]] = []
    for spec in selections:
        item = await resolve_item(session, run, spec or {})
        fields = item.fields if isinstance(item.fields, dict) else {}
        explicit = [str(p) for p in ((spec or {}).get("fields") or [])]
        auto = bool((spec or {}).get("auto"))
        if item.status == "failed":
            # 整条失败（引用一段都没核对上）：任何一格都不能接受——尤其不能用空的 R 把 citations 清掉
            if explicit:
                raise ImportRefused(422, f"这条候选没有一条引用能核对上（{item.error or '抽取失败'}）："
                                         f"整条不可接受，请重新抽取或人工补依据", IMPORT_ITEM_FAILED,
                                    extra={"item_id": str(item.id)})
            continue                                       # auto：整条跳过去，不选任何一格
        if not str(item.metric_id or ""):
            raise ImportRefused(409, f"工作项「{item.name or item.candidate_id or item.id}」还没对齐到图上的指标："
                                     f"先在这次导入里人工对齐，再接受", IMPORT_ITEM_UNALIGNED,
                                extra={"item_id": str(item.id)})
        if explicit:
            paths = explicit
        elif auto:
            # auto **只**收还没决定的「无冲突更新 + 新增候选」：已接受 / 已拒绝的一律跳过
            # （重放请把明确字段一起带上，前端就是这么发的）
            paths = [str(path) for path, cell in fields.items()
                     if isinstance(cell, Mapping) and cell.get("decided") is None
                     and cell.get("state") in ("update", "new")]
        else:
            paths = []
        for path in paths:
            if path not in fields:
                raise ImportRefused(422, f"工作项「{item.name or item.metric_id}」上没有字段 {path}",
                                    IMPORT_FIELD_UNKNOWN,
                                    extra={"item_id": str(item.id), "metric_id": item.metric_id, "path": path})
            requested.append((item, path))
    if not requested:
        raise ImportRefused(422, "没有选中任何可以接受的字段", IMPORT_NOTHING_SELECTED)

    workflow_id = str(workflow.id)
    fingerprint = write_fingerprint(sorted([{"item_id": str(item.id), "path": path}
                                            for item, path in requested], key=lambda x: (x["item_id"], x["path"])))
    claim = await claim_write(session, actor=actor, workflow_id=workflow_id, target=card_id,
                              request_id=request_id, payload_hash=fingerprint)
    if claim.state == WRITE_CONFLICT:
        raise ImportRefused(409, "同一个 request_id 已经用不同的内容提交过：请换一个 request_id 再提交",
                            IMPORT_IDEMPOTENCY_CONFLICT, extra={"current_version": workflow.version})
    if claim.state == WRITE_REPLAY:
        # 上一次已经整批提交过（L 那一版和 B 那一件是同一次提交）：交回那一次的结果，B 读得回来
        return {"applied": False, "replayed": True,
                "revision": claim.revision if isinstance(claim.revision, int) else workflow.version}

    picked: list[tuple[CaliberImportItem, str, Any]] = []
    for item, path in requested:
        cell = (item.fields or {}).get(path)
        if not isinstance(cell, Mapping):
            raise ImportRefused(422, f"工作项「{item.name or item.metric_id}」上没有字段 {path}",
                                IMPORT_FIELD_UNKNOWN, extra={"item_id": str(item.id), "path": path})
        if item.status == "failed":
            raise ImportRefused(422, f"这条候选没有一条引用能核对上（{item.error or '抽取失败'}）："
                                     f"整条不可接受，请重新抽取或人工补依据", IMPORT_ITEM_FAILED,
                                extra={"item_id": str(item.id)})
        if cell.get("decided") == "accepted":
            continue                                           # 已经接受过这一格，幂等
        state = cell.get("state")
        value = cell.get("r")
        if not cell.get("r_present") or state not in ACCEPT_STATES:
            raise ImportRefused(422, _not_acceptable_message(str(item.metric_id), path, state),
                                IMPORT_FIELD_NOT_ACCEPTABLE,
                                extra={"item_id": str(item.id), "metric_id": item.metric_id, "path": path,
                                       "state": state})
        if path == fieldbaseline.CITATIONS_FIELD and not value:
            # 出处不能被接受成空：那等于把这条定义的证据删掉
            raise ImportRefused(422, f"{path} 不能接受成空的：出处是这条定义的依据，不接受就把这一格留着",
                                IMPORT_FIELD_NOT_ACCEPTABLE,
                                extra={"item_id": str(item.id), "metric_id": item.metric_id, "path": path})
        picked.append((item, path, value))
    if not picked:
        raise ImportRefused(422, "没有选中任何可以接受的字段", IMPORT_NOTHING_SELECTED)

    if expected_revision is None:
        raise ImportRefused(409, f"这次导入要落到工作流上（现在是 v{workflow.version}）："
                                 f"接受要带上读到的版本（expected_revision），请刷新页面后再改",
                            IMPORT_REVISION_REQUIRED, extra={"current_version": workflow.version})
    # 候选算出来之后 L 被人改过就不落：客户端只把 expected_revision 更新到最新一版也不行——
    # 三方要按现在的 L 重新算、人重新确认（刷新这次导入的三方，或重新抽一次）。
    stale = _stale_l_items(picked, workflow.graph if isinstance(workflow.graph, dict) else {},
                           fieldbaseline.accepted_card(workflow.graph if isinstance(workflow.graph, dict) else {})[1])
    if stale:
        names = "、".join(f"{item.name or item.metric_id}（{path}）" for item, path in stale)
        raise ImportRefused(409, f"这几格的当前内容和这次导入算候选时的那一版不一样了：{names}。"
                                 f"请刷新这次导入的三方、按现在的口径重新确认后再接受",
                            IMPORT_STALE_L, extra={"fields": [{"item_id": str(item.id),
                                                               "metric_id": item.metric_id, "path": path}
                                                              for item, path in stale]})
    if expected_revision != workflow.version:
        raise ImportRefused(409, f"口径已经改到 v{workflow.version}，你的版本是 v{expected_revision}："
                                 f"请刷新后重新核对再接受", IMPORT_STALE_REVISION,
                            extra={"current_version": workflow.version})

    # 同一条指标的同一位被两项候选各带一份说法：不能「取最后一条」——请人分开接受
    seen: dict[tuple[str, str], Any] = {}
    for item, path, value in picked:
        key = (str(item.metric_id), path)
        if key in seen and seen[key] != value:
            raise ImportRefused(409, f"这次接受给指标「{key[0]}」的 {path} 带来了两份不一样的说法："
                                     f"请按 item_id 分开接受", IMPORT_ITEM_AMBIGUOUS,
                                extra={"metric_id": key[0], "path": path})
        seen[key] = value

    whitelist = [(item.metric_id, path, value) for item, path, value in picked if path in changes.FIELDS]
    citations = {item.metric_id: value for item, path, value in picked if path == fieldbaseline.CITATIONS_FIELD}
    for metric_id, path, _value in picked:
        if path in changes.FIELDS or path == fieldbaseline.CITATIONS_FIELD:
            continue
        raise ImportRefused(422, f"字段 {path} 不在接受的范围内", IMPORT_FIELD_NOT_ACCEPTABLE,
                            extra={"metric_id": metric_id, "path": path})
    # 2) 先在拷贝上把整批改完（不落库）
    try:
        change = changes.apply_operations(workflow.graph or {}, card_id=card_id, operations=[
            {"op": "set", "metric_id": metric_id, "path": path, "value": copy.deepcopy(value)}
            for metric_id, path, value in whitelist]) if whitelist else None
    except changes.ChangeRefused as e:
        raise ImportRefused(422, e.message, e.code, extra={"operation": e.operation, "path": e.path}) from None
    except CaliberGone:
        raise _card_gone(workflow.version) from None
    graph = change.graph if change is not None else copy.deepcopy(workflow.graph or {})
    if citations:
        graph = _set_citations(graph, card_id, citations)

    # 3) 先写不可变的新 B 工件（孤儿可接受）：写不出来就让整次接受失败，L 不提交
    new_ref = await _write_advanced_baseline(run=run, draft=draft, workflow=workflow, card_id=card_id,
                                             graph=graph, picked=picked, actor=actor)

    # 4) 一个事务：L 的新版本 + 工作项的决定 + 草稿上的新 B 引用/历史（+ 幂等键）
    #    回滚之后 ORM 对象会被 expire，异常分支里不再读它们的属性（current_version 提前取好）。
    current_version = int(workflow.version)
    try:
        await save_graph(session, workflow, graph, note="接受增量导入", expected_revision=expected_revision)
        for item, path, _value in picked:
            item.accepted = sorted({*(item.accepted or []), path})
            item.rejected = [p for p in (item.rejected or []) if p != path]
            item.applied_revision = workflow.version
        run.checkpoint = {**(run.checkpoint or {}),
                          "accepted": sorted({f"{item.metric_id}{path}" for item, path, _ in picked})}
        history = fieldbaseline.history(draft.payload)
        draft.payload = {**(draft.payload if isinstance(draft.payload, dict) else {}),
                         fieldbaseline.REF_KEY: new_ref,
                         fieldbaseline.HISTORY_KEY: [*history, new_ref]}
        await remember_write(session, actor=actor, workflow_id=workflow_id, target=card_id,
                             request_id=request_id, payload_hash=fingerprint, revision=workflow.version)
        await _refresh_run_items(session, run=run, draft=draft, workflow=workflow)
        await session.commit()
    except WorkflowChanged as e:
        await session.rollback()
        raise ImportRefused(409, str(e), IMPORT_STALE_REVISION,
                            extra={"current_version": getattr(e, "current", current_version)}) from None
    except IntegrityError:
        # 两路真并发带同一个 request_id：后到的那一路撞唯一键，回滚掉自己写的图，按赢的那一路当重放交回。
        # 只有重查确认是同一件事（同键同内容）才当重放；记录还不存在照旧抛出去。
        await session.rollback()
        if not request_id:
            raise
        claim = await claim_write(session, actor=actor, workflow_id=workflow_id, target=card_id,
                                  request_id=request_id, payload_hash=fingerprint)
        if claim.state == WRITE_CONFLICT:
            raise ImportRefused(409, "同一个 request_id 已经用不同的内容提交过：请换一个 request_id 再提交",
                                IMPORT_IDEMPOTENCY_CONFLICT) from None
        if claim.state != WRITE_REPLAY:
            raise
        current = await session.scalar(select(Workflow.version).where(Workflow.id == workflow_id))
        return {"applied": False, "replayed": True,
                "revision": claim.revision if isinstance(claim.revision, int) else (int(current) if current else None)}
    # 5) 读侧的重算已经在同一个事务里做完了（刷新工作项的三方），这里不再写库
    return {"applied": True, "replayed": False, "revision": workflow.version}


def _card_metrics(graph: Mapping[str, Any] | None, card_id: str) -> list[dict[str, Any]]:
    """卡上的指标（接受之后的那一份）：coverage 的 expected 和 uncovered 都按它算。"""
    for node in (graph or {}).get("nodes") or []:
        if not isinstance(node, Mapping) or str(node.get("id")) != card_id:
            continue
        data = node.get("data")
        config = data.get("config") if isinstance(data, Mapping) else None
        config = config if isinstance(config, Mapping) else {}
        return [dict(m) for m in config.get("metrics") or [] if isinstance(m, Mapping) and m.get("id")]
    return []


def _next_revision(current: int, graph: Any, old_graph: Any) -> int:
    """这次接受会落在哪一版：内容真变了才新增一版（和 save_graph 的判定同一套）。"""
    return int(current) + (1 if graph != (old_graph or {}) else 0)


async def _write_advanced_baseline(*, run: CaliberImportRun, draft: CaliberDraft, workflow: Workflow,
                                   card_id: str, graph: dict[str, Any],
                                   picked: Sequence[tuple[Any, str, Any]], actor: str | None) -> dict[str, Any]:
    """先写推进后的不可变 B 工件（不改库、不动 L）；写不出来抛 503，让这次接受整体失败。

    只并进这次被接受的格子；expected_fields 取**接受之后**卡上那条指标声明的字段路径，card_metric_ids 取卡上
    全部指标——coverage 据此判 partial（没覆盖到的格、整条没有基线的指标都记下来）。
    """
    updates: dict[str, dict[str, Any]] = {}
    for item, path, value in picked:
        entry = updates.setdefault(str(item.metric_id), {"name": item.name, "fields": {}, "expected_fields": []})
        entry["fields"][path] = copy.deepcopy(value)
    card_metrics = _card_metrics(graph, card_id)
    by_id = {str(m.get("id")): m for m in card_metrics}
    for metric_id, entry in updates.items():
        entry["expected_fields"] = sorted(fieldbaseline.fields_of(by_id.get(metric_id) or {}))
    made = await fieldbaseline.advance(
        ref=fieldbaseline.ref_of(draft.payload), updates=updates, card_id=card_id, workflow_id=workflow.id,
        accepted_revision=_next_revision(workflow.version, graph, workflow.graph), draft_id=draft.id,
        source_snapshot=run.source_snapshot or None, accepted_by=actor,
        card_metric_ids=[str(m.get("id")) for m in card_metrics])
    if made is None:
        raise ImportRefused(503, "字段基线工件写不进去：这次接受没有落库（L 未提交），请稍后重试",
                            IMPORT_BASELINE_UNAVAILABLE)
    return made


def _card_gone(revision: int | None) -> "ImportRefused":
    return ImportRefused(409, "这张口径卡不见了（可能刚被删掉或改成转抄卡）：请刷新页面再看一次",
                         IMPORT_ITEM_UNKNOWN, extra={"current_version": revision})


def _stale_l_items(picked: Sequence[tuple[Any, str, Any]], graph: Mapping[str, Any],
                   live_metrics: Sequence[Mapping[str, Any]]) -> list[tuple[Any, str]]:
    """这次要接受的格子：候选算出来时记下的 L 指纹和现在图上的对不对得上。

    对不上的返回（工作项, 路径）：→ 接口回 409 import_stale_l，要人刷新三方重新确认，不能只更新 expected_revision 覆盖。
    """
    live = {str(m.get("id")): m for m in live_metrics if isinstance(m, Mapping) and m.get("id")}
    stale: list[tuple[Any, str]] = []
    for item, path, _value in picked:
        anchor = str((item.align or {}).get("live_fingerprint") or "")
        if not anchor:
            continue                                   # 老工作项没有这个锚点：不拿它判（宁可不拦，也不误拦）
        if live_fingerprint(live.get(str(item.metric_id or ""))) != anchor:
            stale.append((item, path))
    return stale


def _not_acceptable_message(metric_id: str, path: str, state: Any) -> str:
    if state == "missing":
        return f"字段 {path} 这次没有提取到（漏提取不当删除）：不能接受，L 上原来的值原样留着"
    if state == "unchanged":
        return f"字段 {path} 和现在的内容一致：不用接受"
    return f"字段 {path} 现在的状态是「{state}」，不能接受"


def _set_citations(graph: dict[str, Any], card_id: str, citations: Mapping[str, Any]) -> dict[str, Any]:
    """把接受的引用原文写进口径卡上的指标（同一次 save_graph：只新增一版，不覆盖旧版本）。

    卡上没有这条指标时不静默丢掉：对齐之后目标指标必须还在图上，不在了就是这次接受落不下去。
    """
    seen: set[str] = set()
    for node in graph.get("nodes") or []:
        if not isinstance(node, dict) or str(node.get("id")) != card_id:
            continue
        config = (node.get("data") or {}).get("config") if isinstance(node.get("data"), Mapping) else None
        config = config if isinstance(config, dict) else {}
        for metric in config.get("metrics") or []:
            if not isinstance(metric, dict) or str(metric.get("id")) not in citations:
                continue
            metric_id = str(metric["id"])
            definition = metric.get("definition")
            definition = dict(definition) if isinstance(definition, dict) else {}
            definition["citations"] = copy.deepcopy(citations[metric_id])
            metric["definition"] = definition
            seen.add(metric_id)
    missing = sorted(set(str(mid) for mid in citations) - seen)
    if missing:
        raise ImportRefused(409, f"这张口径卡上找不到要写引用的指标：{'、'.join(missing)}，请刷新后再试",
                            IMPORT_ITEM_UNKNOWN, extra={"metric_ids": missing})
    return graph


async def _refresh_run_items(session: Any, *, run: CaliberImportRun, draft: CaliberDraft,
                             workflow: Workflow) -> None:
    """L / B 动过之后，把这次导入还没决定的格子重算一遍（已接受的保持接受、没对齐的不动）。

    没对齐上的工作项（mode 是 new / manual、metric_id 为空）没有可比的 L 一格，保持抽出来时的样子：
    要对齐得先走人工对齐（reimport.align 的入口在接口层）。
    """
    live_metrics = list(fieldbaseline.accepted_card(workflow.graph if workflow is not None else {})[1])
    live_by_id = {str(m.get("id")): m for m in live_metrics if isinstance(m, Mapping)}
    ref = fieldbaseline.ref_of(draft.payload)
    try:
        baseline_values = fieldbaseline.values(ref) if ref else {}
    except Exception:  # noqa: BLE001 - B 读不回来当没有基线
        baseline_values = {}
    for item in await _load_items(session, run.id):
        if not item.metric_id:
            continue
        merged = refresh_item({"fields": item.fields, "accepted": item.accepted, "rejected": item.rejected,
                               "align": item.align, "status": item.status},
                              live_metric=live_by_id.get(str(item.metric_id)),
                              baseline_values=(baseline_values.get(str(item.metric_id)) or {}),
                              baseline_known=str(item.metric_id) in baseline_values)
        item.fields, item.status, item.align = merged["fields"], merged["status"], merged["align"]
    await _refresh_counts(session, run)
    if workflow is not None:
        live = await fieldbaseline.draft_graph_projection(session, workflow, workflow.version)
        if live is not None:
            draft.payload = {**(draft.payload or {}), **live}


# ==========================================================================
# 后台任务载体（core/job_runtime：重启标中断、不自动续跑、能显式恢复）
# ==========================================================================


class ImportJob(runtime.JobRun):
    model = CaliberImportRun
    kind = "caliber_import"

    def lane_of(self, job: Any) -> None:
        """抽候选只读文档和表结构、调模型，不查业务库：不占数据源的队，直接跑。"""
        return None

    async def _run(self) -> None:
        if not await self._write(runtime.claim, CaliberImportRun, self.job_id):
            return
        await run_import(self.job_id)

    async def _stopped(self) -> None:
        interrupted = self.stop_reason != "user"
        await self._write(_finish, self.job_id, "interrupted" if interrupted else "cancelled",
                          "服务中断，已保留导入进度，可恢复" if interrupted else "导入已停止，已保留进度")

    async def _failed(self, reason: str) -> None:
        await self._write(_finish, self.job_id, "failed", reason)

    async def _crashed(self, exc: Exception) -> None:
        await self._write(_finish, self.job_id, "failed", f"增量导入失败：{exc}")


async def _finish(session: Any, run_id: str, status: str, error: str | None) -> None:
    run = await session.get(CaliberImportRun, run_id)
    if run is None or run.status in ("cancelled",):
        return
    run.status, run.error, run.finished_at = status, error, utcnow()
    await session.commit()


manager = runtime.JobManager(ImportJob, label="口径增量导入", name="caliber_import")


async def start(run_id: str) -> None:
    manager.start(run_id)


async def startup(*, live: Any = None) -> int:
    """服务启动时接手属主不在了的导入：记 interrupted、保留工作项，等人点「恢复」再续（不自动续跑）。"""
    async def close(session: Any, job_id: str) -> None:
        await _finish(session, job_id, "interrupted", "服务中断，已保留导入进度，可恢复")

    return await runtime.interrupt_on_startup(CaliberImportRun, close, running=manager.running, live=live)


async def resume(session: Any, run: CaliberImportRun) -> dict[str, Any]:
    """显式恢复：把这次导入重新排进后台，续做还没完成的工作项（已接受 / 拒绝的不动，不重复建版）。

    工作项按**持久对齐键**认（稳定 metric 身份 + 规范化来源证据），不按候选位置：顺序变了、条数变了都不会串项。
    """
    if run.status in ("queued", "running"):
        where = "正在运行" if run.status == "running" else "排队中"
        raise ImportRefused(409, f"这次导入{where}（状态：{run.status}）：请等它结束再恢复，"
                                 f"期间不用重复点", IMPORT_ALREADY_RUNNING, extra={"status": run.status})
    pending = await unfinished(session, run.id)
    if run.items_total and not pending:
        raise ImportRefused(409, "这次导入没有未完成的工作项", IMPORT_NOTHING_TO_RESUME)
    run.status, run.error, run.finished_at = "queued", None, None
    run.checkpoint = {**(run.checkpoint or {}), "resumed": True}
    await session.commit()
    await start(run.id)
    return {"run_id": run.id, "status": "queued"}


async def refresh(session: Any, *, run: CaliberImportRun, draft: CaliberDraft, workflow: Workflow) -> None:
    """按**现在的** L / B 重算这次导入还没决定的三方，并刷新每项的 live_fingerprint 比较锚点。

    这是 import_stale_l 之后的恢复动作：工件项、接受 / 拒绝的决定、每项自己的来源依据都不动，只重算三方；
    人看过新三方、重新确认，再接受。
    """
    await _refresh_run_items(session, run=run, draft=draft, workflow=workflow)
    run.checkpoint = {**(run.checkpoint or {}), "refreshed": True}
    await session.commit()


__all__ = ["ACCEPT_STATES", "FIELD_STATES", "ITEM_STATUSES", "RUN_STATUSES", "ImportJob", "ImportRefused",
           "accept_fields", "align", "alignment_key", "build_item", "build_items", "citation_chunks",
           "extract_candidates", "field_state", "item_status", "live_fingerprint", "manager", "realign_item",
           "refresh", "refresh_item", "reject_fields", "resolve_item", "resume", "run_import", "start",
           "startup", "unfinished", "visible_citations"]
