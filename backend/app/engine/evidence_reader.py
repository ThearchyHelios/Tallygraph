"""读取并验证封存证据的公共服务，供试运行、证据接口和评测使用。"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any
from sqlalchemy import select
from app.core.artifact_store import load
from app.core.events import EventType
from app.db.base import SessionLocal
from app.db.models import Run, RunEvent
from app.engine.evidence import ledger_enabled
from app.engine.lineage import scoped_id as _scoped
from app.analysis.contract import LEDGER_KIND

MASKED = "已遮罩"
RUN_NOT_FOUND = "run_not_found"
REPORT_NOT_FOUND = "evidence_report_not_found"


def _fetch(artifact: str) -> tuple[Any, bool]:
    try:
        value = load(artifact)
        return value, value is not None
    except (OSError, ValueError):
        return None, False


@dataclass
class Sealed:
    run_id: str
    graph: dict[str, Any]
    seal: dict[str, Any]
    events: list[tuple[int, str, str | None, dict[str, Any]]]
    #: 封存范围内 node.finished.evidence 记过的 (节点, 工件)
    ledger: set[tuple[str, str]] = field(default_factory=set)
    #: 节点 id → 它最后一次 node.finished 的产出工件 id
    outputs: dict[str, str] = field(default_factory=dict)
    #: 封存范围内交回过的查询快照：工件 id → {node_id, tool, source, via, call_id, exec, query_id, …}
    #: 同一份快照被两次执行时这里只留一条（工件 id 相同、内容一样），要逐次区分用 query_records
    queries: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: 封存范围内交回过的查询快照，**按事件顺序逐条**记：同一份快照被两次执行（循环里、两个节点写同一条 SQL）
    #: 就是两条，各自带执行身份（K10）。每条是 {artifact, node_id, exec, query_id, execution_id, attempt, …}
    query_records: list[dict[str, Any]] = field(default_factory=list)
    #: 口径卡节点 id → 封存范围内它的 caliber.upgrade 事件（升版处置）
    upgrades: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: 封存范围内 node.finished.evidence 的台账条目，按事件顺序（给可疑实体找「最接近的名字」重建目录用）
    entries: list[dict[str, Any]] = field(default_factory=list)
    #: 封存范围内交回过的表结构快照：工件 id → {node_id, source}
    schemas: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: 封存范围内交回过的检索快照：工件 id → {node_id, source}
    retrievals: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: 封存范围内台账里记过的分析结论：工件 id → {node_id, exec, call_id / item, inputs}
    analyses: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: 封存范围内台账里记过的数据源健康度快照：工件 id → {node_id, exec, call_id, source_name, status}
    source_healths: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: 封存时记下的清单哈希（没封存的是 None）：旧答案的猜测按 (run_id, 它) 缓存
    manifest_hash: str | None = None
    #: 运行记录上的 manifest_seq 原值（老运行是 None）：按需裁判追加判定前核对封存没换过
    run_manifest_seq: int | None = None
    #: verify_manifest 的完整结论（审计表照实附上）
    verdict: dict[str, Any] = field(default_factory=dict)
    #: 运行记录上的几样：按需裁判只给跑完、封存了的探索运行，升级前发起的不给
    run_class: str | None = None
    status: str | None = None
    ledger_on: bool = False
    #: 按需裁判的判定（evidence.judged 事件），按先后：(seq, 节点, 载荷)。不管落在封存前后都收——
    #: 失败后接着跑的运行会把早先追加的判定封进新的清单里，它仍然是按需裁判的批注
    judged: list[tuple[int, str | None, dict[str, Any]]] = field(default_factory=list)
    #: 节点 id → node.started 上记的名字、类型。子工作流里的节点（「子工作流节点 id/…」）不在这次运行的图上，名字从这里取
    labels: dict[str, str] = field(default_factory=dict)
    types: dict[str, str] = field(default_factory=dict)
    #: 这一次请求里复算过的分析结论：结论工件 id → 复算的结论（_analysis_recheck）。导出页里同一份结论被判定语、具名的数、
    #: 整表的每一格、分析范围各引一处，各接一条链，复算只做一次。只活在这一次请求里：下一次请求重新取、重新验
    rechecks: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def sealed(self) -> bool:
        return bool(self.seal["sealed"])

    @property
    def trusted(self) -> bool:
        """封存了、而且封存核对通过。封存范围内有事件被改过的话，范围里的哪一条都不能再
        当「已封存」展示——只看 sealed 的消费方会把一条断掉的封存链画成绿的。"""
        return self.sealed and self.seal["ok"] is True

    def nodes_of(self, node_type: str, *, nested: bool = False) -> list[str]:
        """这次运行的图上这一类的节点；nested 为真时连子工作流里跑过的一起给（「子工作流节点 id/…」，类型照 node.started 记的）。"""
        out = [str(n.get("id")) for n in self.graph.get("nodes") or []
               if isinstance(n, dict) and n.get("type") == node_type]
        if nested:
            out += [nid for nid, kind in self.types.items() if "/" in nid and kind == node_type]
        return out

    def query_records_of(self, node_id: Any) -> list[dict[str, Any]]:
        """这个节点交回过的查询快照，按执行先后（同一次执行只有一条）。同一份快照被执行两次就两条，
        老条目、新条目都在（老条目也记 exec，只是没有 execution_id）。"""
        wanted = str(node_id)
        return [r for r in self.query_records if str(r.get("node_id") or "") == wanted]

    def query_at(self, artifact: Any, *, node_id: Any = None, exec_no: Any = None,
                 execution_id: Any = None) -> dict[str, Any] | None:
        """按执行身份取那一次查询：新条目按 (artifact, node_id, exec/attempt, execution_id) 认。

        exec=2 取第二次而不是第一次：先按 exec/attempt 对，对不上再按这个节点的第 N 条。老条目（一个执行
        身份字段都没有）也照这条走——它们同样记了 node_id / exec，逐次记的 query_records 里分得开；只有
        连台账条目都没有（工具只回了 tool.end）时才退回 queries 里按 artifact 的那一份，和以前一样。
        """
        key = str(artifact or "")
        records = [r for r in self.query_records if str(r.get("artifact") or "") == key]
        if execution_id not in (None, ""):
            hit = next((r for r in records if r.get("execution_id") == execution_id
                        and (node_id in (None, "") or str(r.get("node_id") or "") == str(node_id))), None)
            if hit is not None:
                return hit
        if node_id not in (None, ""):
            mine = [r for r in records if str(r.get("node_id") or "") == str(node_id)]
            if exec_no is not None:
                hit = next((r for r in mine if r.get("exec") == exec_no or r.get("attempt") == exec_no), None)
                if hit is not None:
                    return hit
                if isinstance(exec_no, int) and not isinstance(exec_no, bool) and 1 <= exec_no <= len(mine):
                    return mine[exec_no - 1]
            if mine:
                return mine[-1]
        return self.queries.get(key)

    def payload_of(self, node_ids: list[str]) -> dict[str, Any] | None:
        """这几个节点里最后跑完的那个的产出，从封存的事件指向的工件里取。"""
        for node_id in reversed([nid for _, t, nid, _ in self.events if t == "node.finished" and nid in node_ids]):
            artifact = self.outputs.get(node_id)
            try:
                payload = load(artifact) if artifact else None
            except ValueError:
                payload = None
            if isinstance(payload, dict):
                return payload
        return None



async def read_sealed(run_id: str) -> Sealed:
    from app.engine.runner import verify_manifest

    async with SessionLocal() as session:
        run = await session.get(Run, run_id)
        if run is None:
            raise LookupError("运行记录不存在")
        rows = [tuple(r) for r in await session.execute(
            select(RunEvent.seq, RunEvent.type, RunEvent.node_id, RunEvent.data)
            .where(RunEvent.run_id == run_id).order_by(RunEvent.seq))]
        graph = run.graph if isinstance(run.graph, dict) else {}
        manifest, manifest_seq = run.manifest_hash, run.manifest_seq
        run_class, status, ledger_on = run.run_class, run.status, ledger_enabled(run)
    verdict = await verify_manifest(run_id)
    bound = verdict.get("sealed_at") if verdict.get("sealed") else None
    # 没封存（还在跑、停在审批）的运行没有「封存之后」：现有的事件都算，但每件证据都标未封存
    events = [r for r in rows if bound is None or r[0] <= bound]
    out = Sealed(run_id=run_id, graph=graph, events=events, seal={
        "sealed": bool(verdict.get("sealed")), "ok": verdict.get("ok"), "manifest_seq": bound,
        "legacy": bool(verdict.get("legacy", False))}, manifest_hash=manifest, run_manifest_seq=manifest_seq,
        verdict=verdict, run_class=run_class, status=status, ledger_on=ledger_on,
        judged=[(seq, nid, data) for seq, etype, nid, data in rows
                if etype == EventType.EVIDENCE_JUDGED and isinstance(data, dict)])
    for _, etype, node_id, data in events:
        if not node_id or not isinstance(data, dict):
            continue
        if etype == "tool.end":
            if data.get("query_artifact"):
                out.queries.setdefault(str(data["query_artifact"]), {
                    "node_id": node_id, "tool": data.get("tool"), "via": data.get("artifact")})
            if data.get("schema_artifact"):
                out.schemas.setdefault(str(data["schema_artifact"]), {"node_id": node_id})
        elif etype == "retrieve.end" and data.get("artifact"):
            out.retrievals.setdefault(str(data["artifact"]), {"node_id": node_id, "source": data.get("collection")})
        elif etype == "caliber.upgrade":
            out.upgrades[node_id] = data
        elif etype == "node.started":
            if isinstance(data.get("label"), str) and data["label"]:
                out.labels.setdefault(node_id, data["label"])
            if isinstance(data.get("node_type"), str) and data["node_type"]:
                out.types.setdefault(node_id, data["node_type"])
        if etype != "node.finished":
            continue
        if data.get("artifact"):
            out.outputs[node_id] = data["artifact"]
        for entry in data.get("evidence") or []:
            if isinstance(entry, dict) and entry.get("artifact"):
                out.ledger.add((node_id, entry["artifact"]))
                # 条目里另记的节点 id（双盲复算复算的口径卡）是那个节点所在那一层图里的 id：换成这次运行里的
                out.entries.append({**entry, "node_id": node_id,
                                    **({"card": _scoped(node_id, entry["card"])} if entry.get("card") else {})})
                artifact = str(entry["artifact"])
                if entry.get("kind") == "query":
                    out.queries[artifact] = {
                        "node_id": node_id, **{k: entry[k] for k in ("tool", "source", "via", "call_id", "exec",
                                                                    "query_id", "execution_id", "attempt")
                                               if entry.get(k) is not None}}
                    # 逐次记一条（同一份快照两次执行就是两条）：eid、出处链、app 按 exec 取快照都靠它认得出是哪一次
                    out.query_records.append({
                        "artifact": artifact, "node_id": node_id,
                        **{k: entry[k] for k in ("tool", "source", "via", "call_id", "exec",
                                                 "query_id", "execution_id", "attempt")
                           if entry.get(k) is not None}})
                elif entry.get("kind") == "schema":
                    out.schemas[artifact] = {"node_id": node_id, "source": entry.get("source")}
                elif entry.get("kind") == "retrieval":
                    out.retrievals[artifact] = {"node_id": node_id, "source": entry.get("source")}
                elif entry.get("kind") == LEDGER_KIND:
                    out.analyses.setdefault(artifact, {
                        "node_id": node_id, **{k: entry[k] for k in ("exec", "call_id", "item", "inputs")
                                               if entry.get(k) is not None}})
                elif entry.get("kind") == "source_health":
                    out.source_healths.setdefault(artifact, {
                        "node_id": node_id, **{k: entry[k] for k in ("exec", "call_id", "verdict", "sources", "issue_count")
                                               if entry.get(k) is not None}})
    return out



def metric_cards(sealed: Sealed) -> list[tuple[str, str, dict[str, Any]]]:
    """封存范围内的口径卡：(节点, 工件, 指标集)。取回时复验哈希，对不上的、取不回来的不要。

    一期以后口径卡把指标集落成 metric_set 工件、记在台账里；一期以前没有，节点的产出（node.finished
    的 node_output 工件）本身就是指标集——只对画布上确实是口径卡的节点这样认。
    """
    out: list[tuple[str, str, dict[str, Any]]] = []
    seen: set[tuple[str, str]] = set()
    calibers = set(sealed.nodes_of("metrics"))
    for _, etype, node_id, data in sealed.events:
        if etype != "node.finished" or not node_id or not isinstance(data, dict):
            continue
        arts = [str(e["artifact"]) for e in data.get("evidence") or []
                if isinstance(e, dict) and e.get("kind") == "metric_set" and e.get("artifact")]
        if not arts and node_id in calibers and data.get("artifact"):
            arts = [str(data["artifact"])]
        for art in arts:
            if (node_id, art) in seen:
                continue
            seen.add((node_id, art))
            content, ok = _fetch(art)
            if ok is True and isinstance(content, dict) and isinstance(content.get("metrics"), list):
                out.append((node_id, art, content))
    return out


async def load_sealed(run_id: str) -> Sealed:
    """读取封存范围；运行记录不存在时抛 404 CodedHTTPException（供引擎与领域层共用，无需引入 api/evidence）。"""
    from app.api.coded import CodedHTTPException

    try:
        return await read_sealed(run_id)
    except LookupError:
        raise CodedHTTPException(404, "运行记录不存在", RUN_NOT_FOUND) from None


@dataclass
class Report:
    node_id: str
    doc_artifact: str
    ok: bool | None
    repairs: int | None
    stats: dict[str, Any] | None
    doc: dict[str, Any] | None
    hash_ok: bool | None
    fields: list[str]
    claims: str | None = None


def output_evidence(sealed: Sealed) -> list[dict[str, Any]]:
    output = sealed.payload_of(sealed.nodes_of("output")) or {}
    primary = output.get("_evidence")
    if not isinstance(primary, dict):
        return []
    return [primary, *[r for r in primary.get("others") or [] if isinstance(r, dict)]]


def reports_of(sealed: Sealed, *, nested: bool = False) -> list[Report]:
    """封存范围内每个报告节点最后一次的自查结果，按第一次出现的顺序。"""
    latest: dict[str, dict[str, Any]] = {}
    for _, etype, node_id, data in sealed.events:
        if (
            etype == "report.checked"
            and node_id
            and isinstance(data, dict)
            and data.get("doc_artifact")
            and (nested or "/" not in node_id)
        ):
            latest[node_id] = data
    evidence = output_evidence(sealed)
    out: list[Report] = []
    for node_id, data in latest.items():
        artifact = str(data["doc_artifact"])
        try:
            doc = load(artifact)
            hash_ok: bool | None = True if doc is not None else None
        except ValueError:
            doc, hash_ok = None, False
        if isinstance(doc, dict) and (doc.get("node_id") != node_id or doc.get("run_id") != sealed.run_id):
            doc, hash_ok = None, False
        fields = [
            f
            for r in evidence
            if r.get("report_node") == node_id and r.get("doc_artifact") == artifact
            for f in r.get("fields") or []
        ]
        out.append(
            Report(
                node_id=node_id,
                doc_artifact=artifact,
                ok=data.get("ok"),
                repairs=data.get("repairs"),
                stats=(doc or {}).get("stats") or data.get("stats"),
                doc=doc if isinstance(doc, dict) else None,
                hash_ok=hash_ok,
                fields=fields,
                claims=data.get("claims") if isinstance(data.get("claims"), str) else None,
            )
        )
    return out


def choose_report(reports: list[Report], wanted: str | None, sealed: Sealed) -> Report:
    """从封存报告列表里选出目标报告（指定 wanted 优先，否则取出口 _evidence 主报告，再退回第一份）。"""
    from app.api.coded import CodedHTTPException

    if wanted:
        chosen = next((r for r in reports if r.node_id == wanted), None)
        if chosen is None:
            raise CodedHTTPException(404, "本次运行中没有所选报告撰写节点的文档", REPORT_NOT_FOUND)
        return chosen
    primary = next(iter(output_evidence(sealed)), {})
    return next((r for r in reports if r.node_id == primary.get("report_node")), reports[0])


class ShownText:
    """把展示用 SQL、检查片段、合并警告里命中的运行遮罩值替换为「已遮罩」。"""

    def __init__(self, secrets: Any) -> None:
        self._secrets = secrets
        self.changed = False

    def text(self, value: Any) -> Any:
        if self._secrets is None or not isinstance(value, str) or not value:
            return value
        out = self._secrets.scrub(value, loose=True)
        self.changed = self.changed or out != value
        return out

    def checks(self, checks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {**c, **{k: self.text(c[k]) for k in ("message", "sql_excerpt") if isinstance(c.get(k), str)}}
            for c in checks
        ]

    def merge(self, view: dict[str, Any]) -> dict[str, Any]:
        view["sql"] = self.text(view.get("sql"))
        view["warnings"] = [{**w, "message": self.text(w.get("message"))} for w in view.get("warnings") or []]
        view["inputs"] = [
            {**i, **({"checks": self.checks(i["checks"])} if i.get("checks") else {})}
            for i in view.get("inputs") or []
        ]
        return view


def open_masks(sealed: Sealed | None = None) -> Any:
    """获取请求级数据源遮罩上下文（通过统一边界暴露给领域层，兼容测试对证据遮罩构造器的桩替换）。"""
    from app.api import evidence as _evidence_mod

    return _evidence_mod._Masks(sealed)


async def resolve_metric_masks(
    metric: dict[str, Any],
    card_node: str,
    artifact: str,
    sealed: Sealed,
    masks: Any,
    *,
    card: dict[str, Any] | None = None,
    output: Any = None,
    by_value: bool = True,
    sure: bool = False,
) -> tuple[dict[str, Any], str | None]:
    """判断单个指标及其输入是否命中的数据源遮罩。"""
    from app.api import evidence as _evidence_mod

    return await _evidence_mod._metric_masks(
        metric,
        card_node,
        artifact,
        sealed,
        masks,
        card=card,
        output=output,
        by_value=by_value,
        sure=sure,
    )
